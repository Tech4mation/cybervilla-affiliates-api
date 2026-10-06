"""Odoo v16 XML-RPC client — the catalogue half.

Copied from llm-service (agent/odoo_client.py), which has been talking to this
same store in production. Two deliberate differences:

  * One store, not many. llm-service carries a tenant's credentials and a
    per-tenant catalogue policy through every call; here the credentials come
    from the environment and there is no policy to thread.
  * Reading the catalogue only. The ordering, POS and customer-record methods
    were left behind rather than copied unused — they belong with the ticket
    that settles how an affiliate's code attaches to an order, and copying them
    now would mean maintaining two divergent copies in the meantime.

HOW XML-RPC WORKS (brief primer)
---------------------------------
XML-RPC is a remote procedure call protocol — instead of REST's "hit a URL with
a verb", you POST an XML document to a single endpoint that says "call function X
with these arguments, return me the result as XML".  Python's stdlib ships
xmlrpc.client so there's nothing to pip-install.

Odoo exposes two XML-RPC endpoints:

  /xmlrpc/2/common   — unauthenticated (version info, authenticate)
  /xmlrpc/2/object   — authenticated model operations

Authentication:
  uid = common.authenticate(db, username, api_key, {})
  -> returns an integer user-id, or False on failure.

Every subsequent call proves identity by re-sending (db, uid, api_key):
  models.execute_kw(db, uid, api_key, model, method, positional_args, keyword_args)

This is equivalent to calling model.method(*positional_args, **keyword_args) inside
Odoo, so every ORM method (search_read, read, create, write, unlink, ...) is available.
"""

import logging
import threading
import xmlrpc.client

log = logging.getLogger(__name__)

# Stock is deliberately absent. Nobody outside the store is allowed to be told
# whether an item is in stock, so the figure is not fetched in the first place.
_PRODUCT_FIELDS = [
    "id", "name", "list_price", "description_sale",
    "categ_id", "currency_id",
]


def currency_code(value: object) -> str | None:
    """
    Pull an ISO currency code out of whatever Odoo returned for currency_id.

    Odoo XML-RPC returns a many-to-one field as [id, "Display Name"], so a
    currency arrives as [1, "NGN"].  A plain string is accepted too, and False
    (Odoo's empty many-to-one) yields None.
    """
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        value = value[1]
    if isinstance(value, str) and value.strip():
        return value.strip().upper()
    return None


def many2one(value: object) -> tuple[int | None, str | None]:
    """Split Odoo's [id, "Display Name"] into its two halves.

    False — Odoo's empty many-to-one — yields (None, None), so a product with
    no category reads the same as one whose category we could not parse.
    """
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            return int(value[0]), str(value[1])
        except (TypeError, ValueError):
            return None, None
    return None, None


class OdooClient:
    """
    Thin wrapper around Odoo's XML-RPC external API.

    Authenticates lazily on first use and caches the uid for the lifetime of
    this object.

    Credentials come from the environment (see config.Config):
      url       — base URL, e.g. https://www.cybervilla.io
      database  — Odoo database name
      username  — Odoo login (usually an email)
      api_key   — generated under Settings > Technical > API Keys
    """

    # The size we serve. image_1920 is the original upload and can be several
    # megabytes; image_512 is Odoo's own resize and lands around 16KB, which is
    # plenty for a product card and cheap to pass around.
    IMAGE_FIELD = "image_512"

    def __init__(self, url: str, database: str, username: str, api_key: str):
        self.url = (url or "").rstrip("/")
        self.database = database
        self.username = username
        self.api_key = api_key
        self._uid: int | None = None
        self._company_currency: str | None = None
        # One lock for both proxies. xmlrpc.client keeps a connection on the
        # transport and is not safe to use from two threads at once, and a web
        # server runs requests in parallel by default.
        self._lock = threading.RLock()

        # ServerProxy objects are created once — they hold no persistent connection
        # (XML-RPC is stateless; each call opens and closes an HTTP connection)
        self._common = xmlrpc.client.ServerProxy(
            f"{self.url}/xmlrpc/2/common", allow_none=True
        )
        self._models = xmlrpc.client.ServerProxy(
            f"{self.url}/xmlrpc/2/object", allow_none=True
        )

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _get_uid(self) -> int:
        with self._lock:
            return self._authenticate()

    def _authenticate(self) -> int:
        if self._uid is None:
            uid = self._common.authenticate(
                self.database, self.username, self.api_key, {}
            )
            if not uid:
                raise ValueError(
                    "Odoo authentication failed — verify url, database, username, and api_key."
                )
            self._uid = uid
            log.debug("Odoo authenticated as uid=%s", uid)
        return self._uid

    def _call(self, model: str, method: str, args: list, kwargs: dict | None = None) -> object:
        """
        Wraps execute_kw — the single XML-RPC method that dispatches to any ORM method.

        Equivalent to:  odoo_env[model].method(*args, **kwargs)
        """
        with self._lock:
            uid = self._get_uid()
            return self._models.execute_kw(
                self.database, uid, self.api_key,
                model, method, args, kwargs or {},
            )

    # ------------------------------------------------------------------ #
    # Currency                                                             #
    # ------------------------------------------------------------------ #

    def get_company_currency(self) -> str | None:
        """
        The currency the store keeps its books in.  Cached for the lifetime of
        this client, so it costs at most one extra call.

        Used as a fallback only: a product that states its own currency is
        believed over this.
        """
        if self._company_currency is None:
            try:
                companies = self._call("res.company", "search_read", [[]], {
                    "fields": ["currency_id"],
                    "limit": 1,
                })
                if companies:
                    self._company_currency = currency_code(companies[0].get("currency_id"))
            except Exception:
                log.warning("Could not read the Odoo company currency", exc_info=True)
        return self._company_currency

    # ------------------------------------------------------------------ #
    # Products                                                             #
    # ------------------------------------------------------------------ #

    def iter_products(self, batch_size: int = 200):
        """Every product an affiliate can actually send a customer to.

        `is_published` is the important one: it means the product is on the
        website. Without it we mirrored everything saleable — about 5,200
        products — while only ~665 were on the storefront, so an affiliate
        could promote something a customer had no way to buy. A storewide link
        failed silently (the item simply never appears in the shop) and a
        product link failed loudly (the page answers 403).

        Ordered by id so that the boundary between one batch and the next is
        stable. Ordering by name would let a product renamed mid-sync move
        across a boundary and be copied twice or skipped.

        A generator rather than a list because the catalogue is read in full on
        every sync and there is no reason to hold all of it in memory at once.
        """
        domain: list = [
            ["sale_ok", "=", True],
            ["active", "=", True],
            ["is_published", "=", True],
        ]
        offset = 0
        batch_size = max(1, min(int(batch_size), 500))
        while True:
            batch = self._call("product.product", "search_read", [domain], {
                "fields": _PRODUCT_FIELDS,
                "limit": batch_size,
                "offset": offset,
                "order": "id asc",
            })
            if not batch:
                return
            yield batch
            if len(batch) < batch_size:
                return
            offset += len(batch)

    def get_categories(self) -> list:
        return self._call("product.category", "search_read", [[]], {
            "fields": ["id", "name", "complete_name"],
            "order": "complete_name asc",
            "limit": 100,
        })

    def product_images(self, product_ids: list) -> dict:
        """The picture bytes for these products, keyed by id.

        Only products that actually have one appear in the result, so a caller
        can tell "no picture" from "picture we could not read" by what is
        missing rather than by a sentinel.

        Note for anyone tempted to filter these in the Odoo domain instead:
        searching on image_512 does not work. It is computed rather than
        stored, and a domain of image_512 != False quietly matches every
        product in the catalogue. The only honest test is reading the field.
        """
        ids = [int(pid) for pid in product_ids if pid is not None]
        if not ids:
            return {}
        rows = self._call("product.product", "read", [ids],
                          {"fields": ["id", self.IMAGE_FIELD]})
        found = {}
        for row in rows:
            raw = row.get(self.IMAGE_FIELD)
            if raw:
                found[row["id"]] = raw
        return found

    # ------------------------------------------------------------------ #
    # Affiliates                                                           #
    # ------------------------------------------------------------------ #
    # These reach the cybervilla_affiliate module installed on the store. The
    # dashboard owns who an affiliate is and what a link's markup is; these two
    # calls push that down so the store's checkout can price and attribute a
    # sale. Both are idempotent on the backend_ref, so a repeated push is safe.

    def upsert_affiliate(self, vals: dict) -> int:
        """Create or update the store's copy of an affiliate. Returns its Odoo id."""
        return int(self._call(
            "cybervilla.affiliate", "upsert_from_backend", [vals]
        ))

    def template_id_for(self, product_id: int) -> int | None:
        """The product.template a product.product belongs to.

        Our catalogue mirrors product.product (variants) and stores those ids,
        but a link lands on a product.template. The two are separate tables
        with separate id sequences, so passing one where the other is expected
        silently points at an unrelated product rather than failing.
        """
        rows = self._call("product.product", "read", [[int(product_id)]],
                          {"fields": ["product_tmpl_id"]})
        if not rows:
            return None
        found = rows[0].get("product_tmpl_id")
        # Odoo returns a many2one as [id, display_name].
        return int(found[0]) if found else None

    def upsert_affiliate_link(self, vals: dict) -> dict:
        """Create or update a link and its markup pricelist.

        Returns {link_id, affiliate_id, pricelist_id, markup_percent}; the
        markup is whatever the store actually applied after clamping to its
        ceiling, which may be lower than what was asked for.
        """
        return self._call(
            "cybervilla.affiliate.link", "upsert_from_backend", [vals]
        )
