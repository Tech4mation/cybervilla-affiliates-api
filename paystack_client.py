"""Talking to Paystack.

Everything that reaches Paystack goes through this module, for three reasons.

One, the live-key guard has to be somewhere that cannot be bypassed: a caller
who forgets it would send real money with no warning, so the check lives at
the bottom of the stack rather than in whichever route happens to remember.

Two, Paystack sits behind Cloudflare, which rejects Python's default
User-Agent with a 1010 error that looks exactly like a bad key and sends you
hunting for the wrong problem. One place to set the header is one place to get
it right.

Three, money is counted in kobo over the wire and in naira everywhere else in
this codebase. Converting in one direction in one place is the difference
between paying somebody 1,000 naira and paying them 100,000.
"""

from __future__ import annotations

import logging

import requests

from config import Config

log = logging.getLogger(__name__)

API_ROOT = "https://api.paystack.co"

# Cloudflare blocks the default python-requests agent. See the module docstring.
USER_AGENT = "cybervilla-affiliates/1.0"


class PaystackError(RuntimeError):
    """A call to Paystack did not succeed.

    Carries what Paystack said, because the message is usually the only
    explanation an admin is going to get ("account name does not match",
    "insufficient balance").
    """

    def __init__(self, message: str, *, status_code: int | None = None, payload: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload or {}


class PaystackNotConfigured(PaystackError):
    """No key, or a key we refuse to use."""


def naira_to_kobo(amount) -> int:
    """Paystack counts in kobo. Rounded, never truncated.

    float() first because these arrive as Decimal from the database, and
    Decimal * 100 then int() would quietly drop a kobo on some values.
    """
    return int(round(float(amount) * 100))


def kobo_to_naira(amount: int) -> float:
    return round(int(amount) / 100.0, 2)


def _guard() -> str:
    """The key to use, or an exception explaining why there isn't one."""
    if not Config.paystack_is_configured():
        raise PaystackNotConfigured(
            "No PAYSTACK_SECRET_KEY is set, so payouts cannot be sent automatically."
        )
    refusal = Config.paystack_refusal()
    if refusal:
        raise PaystackNotConfigured(refusal)
    return Config.PAYSTACK_SECRET_KEY


def _request(method: str, path: str, payload: dict | None = None, params: dict | None = None) -> dict:
    key = _guard()
    url = f"{API_ROOT}{path}"
    try:
        response = requests.request(
            method,
            url,
            json=payload,
            params=params,
            timeout=Config.PAYSTACK_TIMEOUT_SECONDS,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
    except requests.RequestException as exc:
        # A timeout on a transfer is the dangerous case: the request may well
        # have been accepted. The caller must treat this as "unknown", never
        # as "failed", and reconcile by reference rather than retrying blind.
        raise PaystackError(f"Could not reach Paystack: {exc}") from exc

    try:
        body = response.json()
    except ValueError:
        raise PaystackError(
            f"Paystack returned something that was not JSON (HTTP {response.status_code}).",
            status_code=response.status_code,
        ) from None

    if not body.get("status"):
        raise PaystackError(
            body.get("message") or f"Paystack rejected the request (HTTP {response.status_code}).",
            status_code=response.status_code,
            payload=body,
        )
    return body.get("data") or {}


# --------------------------------------------------------------------------- #
# Banks and account verification
# --------------------------------------------------------------------------- #

_BANKS_CACHE: dict = {"fetched_at": 0.0, "rows": []}
_BANKS_TTL_SECONDS = 24 * 60 * 60


def list_banks(country: str = "nigeria", refresh: bool = False) -> list[dict]:
    """Every bank that can receive a transfer, as {code, name}.

    Banks that cannot receive transfers are filtered out here rather than in
    the dashboard: offering someone a bank we then cannot pay into is a
    failure discovered far too late.

    Held for a day in memory. Each gunicorn worker keeps its own copy, which
    is fine for a list that changes a few times a year and is identical for
    everyone; it is not a shared cache and must not be used for anything
    where workers disagreeing would matter.
    """
    import time

    fresh_enough = (time.time() - _BANKS_CACHE["fetched_at"]) < _BANKS_TTL_SECONDS
    if not refresh and _BANKS_CACHE["rows"] and fresh_enough:
        return _BANKS_CACHE["rows"]

    data = _request("GET", "/bank", params={"country": country, "perPage": 200})
    # Paystack lists some banks more than once under one code (separate rows,
    # same institution). Shown as-is that is a dropdown with "Zenith Bank"
    # twice and no way to tell which to pick, so the code is made unique here.
    by_code: dict[str, str] = {}
    for row in data or []:
        code, name = row.get("code"), row.get("name")
        if not (code and name and row.get("active") and row.get("supports_transfer")):
            continue
        # Keep the shortest name for a code: the duplicates are usually the
        # same bank with a longer, more qualified label.
        if code not in by_code or len(name) < len(by_code[code]):
            by_code[code] = name

    banks = [{"code": code, "name": name} for code, name in by_code.items()]
    banks.sort(key=lambda row: row["name"].lower())

    _BANKS_CACHE["rows"] = banks
    _BANKS_CACHE["fetched_at"] = time.time()
    return banks


def resolve_account(account_number: str, bank_code: str) -> str:
    """The real name on an account, as the bank reports it.

    Note this is stubbed in test mode — Paystack returns a placeholder name
    for any well-formed number, so a passing check here proves the plumbing
    and not the account.
    """
    data = _request(
        "GET",
        "/bank/resolve",
        params={"account_number": account_number, "bank_code": bank_code},
    )
    name = (data.get("account_name") or "").strip()
    if not name:
        raise PaystackError("Paystack could not confirm the name on that account.")
    return name


# --------------------------------------------------------------------------- #
# Recipients and transfers
# --------------------------------------------------------------------------- #

def create_recipient(name: str, account_number: str, bank_code: str) -> dict:
    """Register where money may be sent. Returns {code, account_name}.

    This is the real test of whether an account can be paid — stricter than
    `resolve_account`, which answers for some accounts that cannot actually
    receive a transfer (the test-mode bank code 001 being the obvious one).
    So it doubles as validation: if this refuses, the account is not a
    payout destination, whatever a lookup said.

    Paystack treats repeat registrations of the same account as the same
    recipient, so calling this twice is safe; we still store the code to
    save the round trip.
    """
    data = _request("POST", "/transferrecipient", {
        "type": "nuban",
        "name": name,
        "account_number": account_number,
        "bank_code": bank_code,
        "currency": "NGN",
    })
    code = (data.get("recipient_code") or "").strip()
    if not code:
        raise PaystackError("Paystack did not return a recipient code.")
    # The bank's own name for the account, which is what should be stored —
    # it comes back here, so no second lookup is needed.
    resolved = ((data.get("details") or {}).get("account_name") or "").strip()
    return {"code": code, "account_name": resolved or name}


def initiate_transfer(amount_naira, recipient_code: str, reason: str, reference: str) -> dict:
    """Ask Paystack to send money.

    `reference` is ours and must be stable for a given payout: Paystack
    rejects a duplicate reference, and that rejection is precisely what stops
    a double-click or a retry from paying somebody twice. Never generate a
    fresh reference on retry.

    The returned status is not final. `otp` means somebody must still approve
    it, `pending` means Paystack has taken it and will report the outcome by
    webhook. Only a webhook — or an explicit re-fetch — settles a transfer.
    """
    data = _request("POST", "/transfer", {
        "source": "balance",
        "amount": naira_to_kobo(amount_naira),
        "recipient": recipient_code,
        "reason": reason[:100],
        "reference": reference,
    })
    return {
        "status": data.get("status"),
        "transfer_code": data.get("transfer_code"),
        "reference": data.get("reference") or reference,
        "raw": data,
    }


def fetch_transfer(id_or_code: str) -> dict:
    """What Paystack currently thinks of a transfer.

    Used to reconcile after a timeout, where our records and Paystack's may
    disagree and Paystack is right.
    """
    return _request("GET", f"/transfer/{id_or_code}")


def verify_transfer(reference: str) -> dict:
    """What Paystack knows about the transfer carrying our reference.

    Looked up by *our* reference rather than Paystack's code, because the
    case this exists for is the one where we never received their code: the
    request timed out and we cannot tell whether it landed.
    """
    return _request("GET", f"/transfer/verify/{reference}")


def resend_transfer_otp(transfer_code: str, reason: str = "resend_otp") -> dict:
    """Ask Paystack to send the confirmation code again."""
    return _request("POST", "/transfer/resend_otp", {
        "transfer_code": transfer_code,
        "reason": reason,
    })


def finalize_transfer(transfer_code: str, otp: str) -> dict:
    """Complete a transfer that Paystack is holding for an OTP.

    Only needed while 'Disable OTP for Transfers' is off on the Paystack
    account. With it on, `initiate_transfer` is the whole story.
    """
    return _request("POST", "/transfer/finalize_transfer", {
        "transfer_code": transfer_code,
        "otp": otp,
    })
