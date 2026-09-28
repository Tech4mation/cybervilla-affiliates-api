# cybervilla-affiliates-api

The backend for the CyberVilla affiliate dashboard (`cybervilla-affiliates`).

Today it does one thing: it keeps a copy of the CyberVilla product catalogue and
serves it to the dashboard. Affiliate accounts, tracking links, commissions and
payouts are not built yet.

## Why there is a copy of the catalogue

The catalogue lives in Odoo, the CyberVilla store system, and is read over
XML-RPC — an older remote-call protocol that is slow, handles one call at a time
per connection, and is sometimes unavailable. Nothing a person waits for is
allowed to depend on that. So a scheduled job copies the catalogue into this
service's own database, and every request the dashboard makes is answered from
the copy.

When the copy is out of date, the dashboard is told how old it is rather than
being shown an empty page. When the store cannot be reached, the previous
catalogue stays exactly where it was.

## Running it locally

Requires Python 3.11 or newer, and Docker for the database.

```bash
docker compose up -d          # PostgreSQL on port 5434

python -m venv .venv
.venv/Scripts/activate        # Windows;  source .venv/bin/activate elsewhere
pip install -r requirements.txt

cp .env.example .env          # then fill it in — DATABASE_URL at minimum

export FLASK_APP=app.py       # $env:FLASK_APP = "app.py" in PowerShell
flask db upgrade              # create the tables
flask run --port 5000
```

Port 5434 rather than 5432 because this machine already has other PostgreSQL
containers on 5432 and 5433. A port clash there fails in the worst way: you
connect successfully, to somebody else's database.

If you would rather use a PostgreSQL you already have, skip the Docker step and
point `DATABASE_URL` at it.

Check it came up:

```bash
curl http://localhost:5000/health
```

A healthy answer is `200` with `"database": "ok"`. If the database is
unreachable the answer is `503` — deliberately, so that whatever is watching
the service notices.

## Where the store credentials come from

The four `ODOO_*` values are not in any file. They live in the `llm-service`
database, in the `connector_config` table, encrypted with that service's
`ENCRYPTION_KEY`. To read them out, from a checkout of `llm-service` with its
own `.env` in place:

```bash
python -c "
from app import app
from models import ConnectorConfig
from crypto import decrypt_credentials
with app.app_context():
    for cfg in ConnectorConfig.query.filter_by(connector_type='odoo').order_by(
            ConnectorConfig.is_primary.desc()).all():
        print(cfg.org_id, cfg.label, decrypt_credentials(cfg.credentials_enc))
"
```

That prints live credentials to your terminal, so run it where nobody is
watching and clear the scrollback afterwards. One organisation can hold several
Odoo connectors; the CyberVilla one is the store at `store.cybervilla.com`.

Copy `url`, `database`, `username` and `api_key` into this service's `.env`.
They are the same credentials, so revoking the API key in Odoo stops both
services at once — worth knowing before anyone rotates it.

## Filling the catalogue

With the four `ODOO_*` values set in `.env`:

```bash
flask sync-catalogue
```

Safe to re-run: products are matched on the store's own product id, so a second
run updates rather than duplicates. Products the store no longer offers are
marked unavailable rather than deleted, because affiliate links and past orders
will point at them long after the store has forgotten them.

The same command also fetches product pictures, fifty at a time. That is the
slow part of a first run — about 5,200 pictures at roughly 40 kB each — so it
fetches only pictures it does not already have, and a later run costs almost
nothing. Two ways to keep it out of the way:

```bash
flask sync-catalogue --no-images   # prices now, pictures later
flask warm-images                  # the pictures, whenever suits
flask warm-images --limit 200      # or just enough to look at
```

Pictures are fetched in bulk rather than when a card is first shown because
signing in to the store costs about two seconds, and a page of two dozen cards
would otherwise pay that two dozen times. Fifty pictures in one call cost about
the same as one.

In production this runs on a schedule (cron, hourly to start with). The same
job can be triggered over HTTP for a scheduler that prefers it:

```bash
curl -X POST http://localhost:5000/internal/catalogue/refresh \
     -H "X-Internal-Key: $INTERNAL_API_KEY"
```

With no `INTERNAL_API_KEY` set, that endpoint refuses every call.

| Address | What it gives you |
|---|---|
| `GET /health` | Service and database state, and how current the catalogue is |
| `GET /products` | A page of products. `search`, `category`, `page`, `perPage` |
| `GET /products/<id>` | One product, by the store's product id |
| `GET /products/<id>/image` | That product's photograph |
| `GET /categories` | Categories that actually have products in them |
| `POST /auth/signup` | Affiliate sign-up. Signs up with status `pending` (awaiting admin approval) |
| `POST /auth/signin` | Sign in with email + password. Returns JWT token and status |
| `GET /auth/me` | Current authenticated user and affiliate profile |
| `GET /signup` | Built-in browser sign-up page |
| `GET /signin` | Built-in browser sign-in page |
| `GET /admin/approvals` | Built-in browser admin approval portal |
| `GET /admin/affiliates` | List all affiliates and pending applications (admin only) |
| `POST /admin/affiliates/<id>/approve` | Approve a pending affiliate into the program & sync to store (admin only) |
| `POST /admin/affiliates/<id>/reject` | Reject an affiliate application with reason (admin only) |
| `GET /affiliate/profile` | Signed-in approved affiliate profile |
| `GET /affiliate/links` | Signed-in approved affiliate links |
| `POST /affiliate/links` | Create a new affiliate link (approved members only) |
| `POST /internal/catalogue/refresh` | Copy the catalogue across now. Needs `X-Internal-Key` |
| `POST /internal/affiliates` | Create an affiliate and push it to the store. Needs `X-Internal-Key` |
| `POST /internal/affiliates/<ref>/links` | Create a link (priced at its markup) and push it. Needs `X-Internal-Key` |
| `POST /internal/affiliates/resync` | Retry pushing anything the store has not accepted yet. Needs `X-Internal-Key` |
| `GET /internal/earnings` | Recorded earnings, for checking the webhook. Needs `X-Internal-Key` |
| `POST /odoo/webhook` | Where the store reports a paid/cancelled affiliate order. Signed, not keyed |

## Authentication and Admin Approval

Affiliates register through the sign-up page (`POST /auth/signup` or `/signup`).

> [!IMPORTANT]
> **Approval Required:** Sign-ups do **not** become members of the affiliate program until approved by the admin.
> Upon sign-up, accounts are placed in `pending` status and are **not** pushed to the Odoo store or granted access to generate links and earn commissions.

### Admin Account

A single admin account is automatically created on startup using settings from `.env`:
* `ADMIN_EMAIL` (default: `admin@cybervilla.io`)
* `ADMIN_PASSWORD` (default: `<the ADMIN_PASSWORD you set>`)
* `ADMIN_NAME` (default: `CyberVilla Admin`)

You can also run or re-run the CLI command to ensure the admin account:

```bash
flask create-admin
# or with custom credentials:
flask create-admin --email admin@cybervilla.io --password MySecurePassword123! --name "CyberVilla Admin"
```

### Reviewing and Approving Affiliates

The admin can review applicants via the API or browser interface:
1. View pending sign-ups: `GET /admin/affiliates?status=pending` (or `/admin/approvals` in the browser).
2. Approve applicant: `POST /admin/affiliates/<id>/approve`. This sets status to `approved`, creates their stable `AFF-<id>` reference, and pushes the affiliate to the Odoo store (`_push_affiliate`). The user is now a full member of the affiliate program.
3. Reject applicant: `POST /admin/affiliates/<id>/reject` with optional JSON body `{"reason": "..."}`.

## Still to come

* The **earning state machine and payouts** — moving an earning from `pending`
  through `payable` to `paid`, and the Paystack Transfers run that pays it. New
  earnings sit `pending` until then.
* **Registration and bank-detail verification** at approval (the `Affiliate`
  fields exist; the flow does not yet).

