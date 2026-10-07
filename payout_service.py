"""Turning recorded earnings into money an affiliate has actually been paid.

The rules here exist because paying is the one thing this service does that
cannot be undone with a database update. So:

  * An earning is only approved deliberately, and only once the order has had
    time to come back. A refund after payment is a loss; a refund before it is
    just a reversal.
  * A payout names the exact earnings it settles. Nothing is paid twice,
    because being attached to a payout is what marks an earning paid.
  * Nothing is deleted. A failed payout releases its earnings so they can be
    paid again later, and the failed attempt stays on the record.

Money is moved by `send_payout`, and only there. `mark_paid` remains what it
always was: a record that money has moved, with no power to move any. Keeping
those two apart is what means a failure to record cannot send twice, and
recording cannot send at all.
"""

import logging
from datetime import timedelta

from sqlalchemy import func, or_

from config import Config
from models import Affiliate, Earning, Payout, amount_due, db, utcnow
from notification_service import notify_admins, notify_affiliate
from paystack_client import (
    PaystackError,
    create_recipient,
    finalize_transfer,
    initiate_transfer,
    resend_transfer_otp,
    verify_transfer,
)

log = logging.getLogger(__name__)


class PayoutError(Exception):
    """Something about this payout is not allowed; the message explains what."""


# --------------------------------------------------------------------------- #
# Approving earnings
# --------------------------------------------------------------------------- #

def payable_conditions(affiliate_id: int | None = None) -> list:
    """What makes an earning payable, as filter conditions.

    Shared by the balance and by the payout that claims it, so the figure an
    affiliate is shown and the earnings actually taken can never disagree.

    There is no approval step. An order the store has confirmed as paid is
    money owed; a person vets it once, at the point the payout is released,
    which is the step that actually moves anything.

    The waiting period lives here instead. It used to gate approval, and
    with approval gone it has to gate payment or it would quietly stop
    protecting anything: a refund is only harmless while we still hold the
    money.
    """
    conditions = [
        Earning.status == "completed",
        Earning.payout_id.is_(None),
    ]
    if affiliate_id is not None:
        conditions.append(Earning.affiliate_id == affiliate_id)
    if Config.EARNING_HOLD_DAYS > 0:
        cutoff = utcnow() - timedelta(days=Config.EARNING_HOLD_DAYS)
        # An earning with no date cannot be shown to be past the window, so
        # it waits rather than ageing in by default.
        conditions.append(Earning.occurred_at.isnot(None))
        conditions.append(Earning.occurred_at <= cutoff)
    return conditions


def held_back_total(affiliate_id: int) -> float:
    """Money earned but still inside the waiting period.

    Only so the dashboard can say "this is coming" rather than leaving the
    affiliate to wonder where it went.
    """
    if Config.EARNING_HOLD_DAYS <= 0:
        return 0.0
    cutoff = utcnow() - timedelta(days=Config.EARNING_HOLD_DAYS)
    total = (
        db.session.query(func.coalesce(func.sum(amount_due()), 0))
        .filter(
            Earning.affiliate_id == affiliate_id,
            Earning.status == "completed",
            Earning.payout_id.is_(None),
            or_(Earning.occurred_at.is_(None), Earning.occurred_at > cutoff),
        )
        .scalar()
    )
    return float(total or 0)


def payable_balance(affiliate_id: int) -> dict:
    """What this affiliate can be paid now, and if not, why not.

    Returned as a summary rather than a number because the dashboard has to
    explain *why* a payout cannot be requested, not merely refuse.
    """
    rows = (
        db.session.query(
            func.count(Earning.id),
            func.coalesce(func.sum(amount_due()), 0),
            func.max(Earning.currency),
        )
        .filter(*payable_conditions(affiliate_id))
        .one()
    )
    count, total, currency = int(rows[0] or 0), float(rows[1] or 0), rows[2]

    affiliate = Affiliate.query.get(affiliate_id)
    # bank_code is part of being payable, not a detail: without it Paystack
    # cannot be given a destination, so a payout requested without one could
    # be requested and then never sent.
    has_bank = bool(
        affiliate and affiliate.bank_account_number and affiliate.bank_name
        and affiliate.bank_account_name and affiliate.bank_code
    )
    pending_payout = Payout.query.filter(
        Payout.affiliate_id == affiliate_id,
        Payout.status.in_(["requested", "approved", "processing"]),
    ).first()

    waiting = held_back_total(affiliate_id)

    reasons = []
    if not count:
        # Said as one sentence when the money exists but is waiting, because
        # "you have nothing" and "the minimum is 50,000" shown together read
        # as a contradiction to somebody looking at their earnings.
        if waiting:
            reasons.append(
                f"{waiting:,.0f} is still within the {Config.EARNING_HOLD_DAYS}-day "
                "waiting period after a sale."
            )
        else:
            reasons.append("You have no completed earnings yet.")
    elif total < Config.MIN_PAYOUT_AMOUNT:
        reasons.append(
            f"The minimum payout is {Config.MIN_PAYOUT_AMOUNT:,.0f} and you have "
            f"{total:,.0f} ready."
        )
    if not has_bank:
        reasons.append("Add the bank account your payout should go to.")
    if pending_payout:
        reasons.append("You already have a payout in progress.")

    return {
        "amount": total,
        "orderCount": count,
        "currency": currency,
        "minimum": float(Config.MIN_PAYOUT_AMOUNT),
        "waiting": waiting,
        "holdDays": int(Config.EARNING_HOLD_DAYS),
        "canRequest": not reasons,
        "blockedBy": reasons,
    }


# --------------------------------------------------------------------------- #
# Requesting and settling
# --------------------------------------------------------------------------- #

def request_payout(affiliate: Affiliate, note: str = "") -> Payout:
    """Claim every approved, unpaid earning into one payout.

    The earnings are attached here, so from this moment they cannot be rolled
    into a second payout — which is what stops the same sale being paid twice
    if someone presses the button again.
    """
    summary = payable_balance(affiliate.id)
    if not summary["canRequest"]:
        raise PayoutError(" ".join(summary["blockedBy"]))

    earnings = Earning.query.filter(*payable_conditions(affiliate.id)).all()

    payout = Payout(
        affiliate_id=affiliate.id,
        amount=summary["amount"],
        currency=summary["currency"],
        method="manual",
        status="requested",
        bank_account_name=affiliate.bank_account_name,
        bank_account_number=affiliate.bank_account_number,
        bank_name=affiliate.bank_name,
        bank_code=affiliate.bank_code,
        note=(note or "").strip() or None,
    )
    db.session.add(payout)
    db.session.flush()
    payout.reference = f"PO-{payout.id}"
    for earning in earnings:
        earning.payout_id = payout.id
    db.session.commit()
    log.info("Payout %s requested for affiliate %s: %s over %d earning(s)",
             payout.reference, affiliate.backend_ref, payout.amount, len(earnings))
    notify_admins(
        "payout.requested",
        f"{affiliate.name} requested a payout",
        f"{payout.reference} — {payout.amount} over {len(earnings)} order(s). "
        "Open it and choose Send now to release it.",
        "/admin/payouts",
    )
    return payout


def mark_paid(payout: Payout, provider_reference: str = "", note: str = "") -> Payout:
    """Record that this payout has actually been sent.

    This does not move money. It is called after a transfer has been made —
    by hand from the bank, or by whatever sends it — so that the record
    matches reality. Keeping the two apart means a failure to record cannot
    send money twice, and recording cannot send money at all.
    """
    if payout.status == "paid":
        raise PayoutError("This payout is already marked as paid.")
    if payout.status in ("cancelled", "failed"):
        raise PayoutError(f"This payout was {payout.status} and cannot be paid.")

    payout.status = "paid"
    payout.paid_at = utcnow()
    if provider_reference:
        payout.provider_reference = provider_reference
    if note:
        payout.note = note
    for earning in payout.earnings:
        earning.status = "paid"
    db.session.commit()
    log.info("Payout %s marked paid (%d earnings)", payout.reference, len(payout.earnings))
    notify_affiliate(
        payout.affiliate_id, "payout.paid",
        f"You've been paid {payout.amount}",
        f"{payout.reference} has been sent to your {payout.bank_name or 'bank'} account "
        f"ending {(payout.bank_account_number or '')[-4:]}.",
        "/earnings",
    )
    return payout


def fail_payout(payout: Payout, reason: str) -> Payout:
    """Record that a payout did not go through, and free its earnings.

    The earnings go back to unattached so they can be paid in a later
    attempt; the failed payout stays, so the attempt is not lost.
    """
    if payout.status == "paid":
        raise PayoutError("A payout that has been paid cannot be marked failed.")
    payout.status = "failed"
    payout.failure_reason = (reason or "").strip()[:512] or "No reason given."
    for earning in payout.earnings:
        earning.payout_id = None
    db.session.commit()
    log.warning("Payout %s failed: %s", payout.reference, payout.failure_reason)
    notify_affiliate(
        payout.affiliate_id, "payout.failed",
        f"Payout {payout.reference} did not go through",
        f"{payout.failure_reason} Your earnings are payable again, so this can be "
        "retried once anything needed is corrected.",
        "/earnings",
    )
    return payout


# --------------------------------------------------------------------------- #
# Sending the money through Paystack
# --------------------------------------------------------------------------- #
#
# This is the only part of this service that moves money, and it is built on
# one idea: Paystack decides what happened, we only record it. We never infer
# that a transfer succeeded because a call returned, and we never retry by
# generating a new reference.
#
# The reference we send is the payout's own ("PO-12"), permanently. Paystack
# refuses a reference it has already seen, and that refusal is what makes a
# double-click, a retried request or a crashed-and-restarted worker harmless:
# the second attempt cannot create a second transfer. When we see that
# refusal we ask Paystack what it already has rather than trying again.

# Paystack rejects a short transfer reference. Its message says "at least 16
# alphanumeric characters", and that message overstates the real limit — a
# 9-character reference is accepted — but "PO-4" is not, and guessing where
# the true boundary sits would be a silly thing to depend on. So the
# reference Paystack sees is built to be comfortably long and purely
# alphanumeric, and is NOT the payout's own "PO-4", which stays as it is for
# people to read.
#
# It is derived from the payout id and nothing else, which is what makes it
# stable: the same payout always produces the same reference, so a retried
# send is refused by Paystack as a duplicate instead of paying twice. A
# payout that genuinely has to be retried is a new row with a new id, and so
# gets a new reference.
TRANSFER_REF_PREFIX = "CVAPO"


def transfer_reference(payout: Payout) -> str:
    """The reference Paystack knows this payout by."""
    return f"{TRANSFER_REF_PREFIX}{payout.id:012d}"


def payout_for_provider(reference: str = "", transfer_code: str = "") -> Payout | None:
    """The payout a Paystack event is about.

    Tried in order of reliability: Paystack's own transfer code, which we
    store when a transfer is accepted; then our derived reference, which
    covers the case where the response that carried the code never reached
    us; then the payout's human reference, for anything recorded by hand.
    """
    if transfer_code:
        found = Payout.query.filter_by(provider_reference=transfer_code).first()
        if found:
            return found

    ref = (reference or "").strip()
    if ref.startswith(TRANSFER_REF_PREFIX):
        tail = ref[len(TRANSFER_REF_PREFIX):]
        if tail.isdigit():
            found = db.session.get(Payout, int(tail))
            if found:
                return found

    return Payout.query.filter_by(reference=ref).first() if ref else None


# How Paystack's transfer status maps onto ours.
_PROVIDER_PAID = {"success"}
_PROVIDER_FAILED = {"failed", "reversed", "abandoned"}
_PROVIDER_NEEDS_OTP = {"otp"}
# Anything else ("pending", "processing", "queued", "received"…) means it has
# been accepted and the outcome will arrive later, by webhook.


def _apply_transfer_state(payout: Payout, provider_status: str | None,
                          transfer_code: str | None = None) -> Payout:
    """Record what Paystack says this transfer is now, and act on it.

    Every route into this module ends here, so there is one place where a
    provider status becomes a payout status.
    """
    if transfer_code:
        payout.provider_reference = transfer_code
    payout.provider_status = (provider_status or "")[:32]
    state = (provider_status or "").strip().lower()

    if state in _PROVIDER_PAID:
        db.session.commit()
        return mark_paid(payout)

    if state in _PROVIDER_FAILED:
        db.session.commit()
        return fail_payout(payout, f"Paystack reported the transfer as {state}.")

    if state in _PROVIDER_NEEDS_OTP:
        payout.status = "awaiting_otp"
        db.session.commit()
        notify_admins(
            "payout.otp_required",
            f"{payout.reference} needs a confirmation code",
            "Paystack has sent a code to the account owner. Enter it on the payouts "
            "page to release this transfer.",
            "/admin/payouts",
        )
        return payout

    payout.status = "processing"
    db.session.commit()
    log.info("Payout %s is with Paystack (status %s)", payout.reference, state or "unknown")
    return payout


def _adopt_existing_transfer(payout: Payout) -> Payout | None:
    """Whatever Paystack already holds against this payout's reference.

    Called when sending was refused, which is usually because the transfer
    exists already. Returning None means Paystack has nothing and the
    refusal was about something else.
    """
    try:
        data = verify_transfer(transfer_reference(payout))
    except PaystackError:
        return None
    if not data or not data.get("status"):
        return None
    log.warning("Payout %s already existed at Paystack as %s; adopting it rather than resending",
                payout.reference, data.get("status"))
    return _apply_transfer_state(payout, data.get("status"), data.get("transfer_code"))


def send_payout(payout: Payout) -> Payout:
    """Actually send this payout's money.

    Raises PayoutError and changes nothing when the payout is in a state
    where sending would be wrong. The checks are deliberately blunt: there is
    no state from which sending twice is acceptable.
    """
    if payout.status == "paid":
        raise PayoutError("This payout has already been paid.")
    if payout.status in ("failed", "cancelled"):
        raise PayoutError(f"This payout was {payout.status}, so it cannot be sent.")
    if payout.status == "awaiting_otp":
        raise PayoutError("This payout is already waiting for a confirmation code.")
    if payout.status == "processing":
        raise PayoutError("This payout is already with Paystack.")
    if not (payout.bank_account_number and payout.bank_code and payout.bank_account_name):
        raise PayoutError(
            "This payout has no verified bank account to send to. The affiliate needs "
            "to add their bank details first."
        )

    affiliate = payout.affiliate
    recipient = affiliate.paystack_recipient_code if affiliate else None
    if not recipient:
        try:
            recipient = create_recipient(
                payout.bank_account_name, payout.bank_account_number, payout.bank_code,
            )["code"]
        except PaystackError as exc:
            raise PayoutError(f"Paystack would not accept those bank details: {exc}") from exc
        affiliate.paystack_recipient_code = recipient
        db.session.commit()

    payout.method = "paystack"
    try:
        result = initiate_transfer(
            payout.amount,
            recipient,
            f"CyberVilla affiliate payout {payout.reference}",
            transfer_reference(payout),
        )
    except PaystackError as exc:
        existing = _adopt_existing_transfer(payout)
        if existing is not None:
            return existing
        db.session.rollback()
        raise PayoutError(str(exc)) from exc

    return _apply_transfer_state(payout, result.get("status"), result.get("transfer_code"))


def confirm_payout_otp(payout: Payout, otp: str) -> Payout:
    """Release a transfer Paystack is holding, using the code it sent."""
    if payout.status != "awaiting_otp":
        raise PayoutError("This payout is not waiting for a confirmation code.")
    if not payout.provider_reference:
        raise PayoutError("We have no Paystack transfer on record for this payout.")
    code = (otp or "").strip()
    if not code:
        raise PayoutError("Enter the code Paystack sent.")
    try:
        data = finalize_transfer(payout.provider_reference, code)
    except PaystackError as exc:
        # A wrong code is a correctable mistake, not a failed payout: the
        # transfer stays where it is and can be confirmed again.
        raise PayoutError(str(exc)) from exc
    return _apply_transfer_state(payout, data.get("status"), data.get("transfer_code"))


def resend_payout_otp(payout: Payout) -> None:
    """Ask Paystack to send the confirmation code again."""
    if payout.status != "awaiting_otp":
        raise PayoutError("This payout is not waiting for a confirmation code.")
    if not payout.provider_reference:
        raise PayoutError("We have no Paystack transfer on record for this payout.")
    try:
        resend_transfer_otp(payout.provider_reference)
    except PaystackError as exc:
        raise PayoutError(str(exc)) from exc


def reconcile_payout(payout: Payout) -> Payout:
    """Make our record agree with Paystack's.

    Used when the two may have drifted — after a timeout, or a webhook that
    never arrived. Paystack is the authority; this never sends anything.
    """
    if payout.method != "paystack" or not payout.provider_reference:
        raise PayoutError("This payout was not sent through Paystack.")
    try:
        data = verify_transfer(transfer_reference(payout))
    except PaystackError as exc:
        raise PayoutError(str(exc)) from exc
    return _apply_transfer_state(payout, data.get("status"), data.get("transfer_code"))


def reverse_payout(payout: Payout, reason: str) -> Payout:
    """Money that had been sent has come back.

    Distinct from `fail_payout`, which is for a payout that never left: here
    the earnings were already marked paid, so they have to be returned to
    approved as well as detached. Without that they would be stuck — counted
    as paid, attached to nothing, and invisible to the next payout.
    """
    earnings = list(payout.earnings)
    payout.status = "failed"
    payout.paid_at = None
    payout.failure_reason = (reason or "").strip()[:512] or "The transfer was reversed."
    for earning in earnings:
        earning.status = "completed"
        earning.payout_id = None
    db.session.commit()
    log.warning("Payout %s was REVERSED after being paid: %s (%d earnings returned)",
                payout.reference, payout.failure_reason, len(earnings))
    notify_affiliate(
        payout.affiliate_id, "payout.failed",
        f"Payout {payout.reference} was returned",
        f"{payout.failure_reason} The money did not reach your account, so your earnings "
        "are payable again and this can be sent another way.",
        "/earnings",
    )
    notify_admins(
        "payout.failed",
        f"{payout.reference} was reversed after being sent",
        f"{payout.failure_reason} The earnings have been released.",
        "/admin/payouts",
    )
    return payout


def apply_provider_event(reference: str, provider_status: str, reason: str = "",
                         transfer_code: str = "") -> Payout | None:
    """Record what Paystack says has happened to a transfer.

    Safe to call with the same event twice. Paystack retries anything it does
    not get a 200 for, so a repeat must not notify the affiliate a second
    time or disturb a payout that already reflects the outcome.

    Returns None when the reference is not one of ours, which is not an
    error: the same webhook URL receives events for everything the Paystack
    account does.
    """
    payout = payout_for_provider(reference, transfer_code)
    if not payout:
        log.info("Paystack reported transfer %r (%s), which matches no payout of ours",
                 reference, transfer_code or "no code")
        return None

    state = (provider_status or "").strip().lower()

    if state in _PROVIDER_PAID:
        if payout.status == "paid":
            return payout
        payout.provider_status = state[:32]
        db.session.commit()
        return mark_paid(payout)

    if state in _PROVIDER_FAILED:
        if payout.status == "failed":
            return payout
        payout.provider_status = state[:32]
        if payout.status == "paid":
            db.session.commit()
            return reverse_payout(
                payout, reason or f"Paystack {state} this transfer after it had been sent.")
        db.session.commit()
        return fail_payout(payout, reason or f"Paystack reported the transfer as {state}.")

    payout.provider_status = state[:32]
    if payout.status not in ("paid", "failed"):
        payout.status = "processing"
    db.session.commit()
    return payout
