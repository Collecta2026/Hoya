# Hoya — Courier Dispatch & Last-Mile Delivery Platform

Hoya (formerly HelioOps/Voya) is Heliolink's dispatch and last-mile delivery
operations platform, rebuilt here on the same architecture as Collecta:
**Flask + SQLAlchemy**, SQLite locally, **Postgres (Neon)** in production,
hosted on **Render**, code in **GitHub**.

Targets feature parity with Detrack-style last-mile platforms: order intake,
driver/vehicle management, route optimisation, barcode scan-gated proof of
delivery, a customer self-service portal, a cost-based rate engine with
invoicing, a live pending-work control board, and a KPI dashboard.

## Quick start (local)

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Open http://localhost:5000. On first run it creates one admin login
(`admin@heliolink.co` / `Heliolink26` by default — override with the
`ADMIN_EMAIL` / `ADMIN_PASSWORD` env vars) plus the default rate settings,
UK zone multipliers, and a standard surcharge catalogue. **No demo
customers, drivers, vehicles or orders are created** — the app starts
clean and everything else is entered by an admin (see Employees, Fleet and
Customers below).

If you want sample data for local testing/demos, set `SEED_DEMO=true`.

## Architecture

| Layer | Choice | Why |
|---|---|---|
| App | Flask + SQLAlchemy + Flask-Login | Matches Collecta's stack |
| Database | SQLite (dev) / Postgres via Neon (prod) | Matches Collecta's Neon setup |
| Hosting | Render (web service, gunicorn) | Matches Collecta's host |
| Code | GitHub | Matches Collecta's source-of-truth |
| Route sequencing | Nearest-neighbour over UK postcode-area centroids | No mapping API key needed |
| POD images/signatures | Stored as base64 in the database | Survives Render's ephemeral disk |
| SMS | Twilio, with a log-only fallback | Works fully offline without credentials |

## Project layout

```
app.py          All routes (dispatcher, driver, customer portal, API)
models.py       SQLAlchemy models
optimise.py     Postcode-based route sequencing helpers
rates.py        Cost-based rate engine
sms.py          Twilio wrapper with log-only fallback
templates/      Jinja2 templates (Bootstrap 5 via CDN)
static/         CSS + PWA manifest
requirements.txt, render.yaml, Procfile, runtime.txt, .python-version
```

## Roles & sign-in

- **Staff** (`/login`) — admin or dispatcher: full operations console.
- **Driver** (`/driver/login`) — PIN-based, mobile-friendly route/scan/POD app.
- **Customer** (`/portal/login`) — self-service ordering, tracking, invoices.

Full feature walkthrough for every role is in the in-app **Help** page
(top-right nav once signed in) — this covers orders, route planning, the
pending work board, fleet/resources, rates, invoicing, KPIs, scanning, the
driver app, and the customer portal in plain language.

## Deploying (GitHub → Render → Neon), same as Collecta

1. **Neon**: create a Postgres project (London/EU region to match Collecta).
   Copy the **pooled** connection string.
2. **GitHub**: push this folder to a repo (e.g. `Collecta2026/Hoya`), keeping
   the `templates/` and `static/` folders intact (a flattened upload will
   break template/static loading).
3. **Render**: New → Blueprint → point at the repo. `render.yaml` provisions
   a web service (Frankfurt region, gunicorn, health check at `/healthz`).
   After first deploy, set these env vars in the Render dashboard:
   - `DATABASE_URL` — the Neon pooled connection string
   - `ADMIN_EMAIL`, `ADMIN_PASSWORD` — your real admin login
   - `SEED_DEMO=false` — already set in render.yaml, keeps prod clean
   - `DEPOT_POSTCODE` — your actual depot postcode, for accurate mileage costing
   - `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` — optional,
     for real SMS instead of log-only
4. Redeploy. Tables are created automatically on first request
   (`db.create_all()`) — this only works for **brand new tables that don't
   exist yet**. It does **not** add new columns to a table that already
   exists. So: a first-ever deploy against an empty database needs nothing
   extra, but updating an already-deployed database to a newer version of
   this app (one that added columns to an existing table, like this
   version did) needs a manual `ALTER TABLE` — see "Upgrading an existing
   deployment" below. Skipping this shows up as
   `psycopg2.errors.UndefinedColumn` on startup.
5. Visit `/healthz` to confirm the app is live, then log in with your admin
   credentials and start adding real employees, fleet, customers and orders.

### Upgrading an existing deployment to this version

This version added cash-on-delivery and last-known-location fields to the
`orders` table. If you're updating a database that was already running an
earlier version of Hoya (rather than starting fresh), run this once in
Neon's SQL Editor **before** redeploying, so the existing `orders` table
gains the new columns instead of crashing on startup:

```sql
ALTER TABLE orders ADD COLUMN IF NOT EXISTS cod_amount FLOAT DEFAULT 0.0;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS cod_collected BOOLEAN DEFAULT FALSE;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS last_known_lat FLOAT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS last_known_lng FLOAT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS last_location_at TIMESTAMP;
```

This only touches the `orders` table and adds columns — it doesn't delete
or change anything else, so existing customers, employees, drivers and
orders are untouched. If a future update adds more columns, the same
pattern applies: check the diff, write the matching `ALTER TABLE`
statements, run them first, then redeploy.

### Note on Python version

Render can default to a very new Python where `psycopg2-binary` has no
prebuilt wheel yet. `runtime.txt` and `.python-version` pin the build to
3.12.7 to avoid this (the same fix used previously on this platform).

## Feature checklist vs Detrack (researched against Detrack's own docs)

| Detrack feature | Status in Hoya |
|---|---|
| Job/order creation, dispatch, status tracking | ✅ Dispatch board, order detail, event history |
| Driver & vehicle management | ✅ Fleet page, live status (available/on route/off) |
| Employee/staff accounts, role-based access | ✅ Employees page (admin-only), dispatcher vs admin roles |
| Route sequencing / planning | ✅ Nearest-neighbour over UK postcode areas (no paid API key) |
| Item-level barcode scanning (load, delivery, collection) | ✅ Scan-gated stop completion, standalone scanner page |
| Shipping label printing (single order) | ✅ Order detail → Print Labels |
| Bulk shipping label export for a date | ✅ Bulk Print Labels (Operations menu) |
| Printable run sheet for a driver | ✅ Print Run Sheet (Operations menu) |
| Proof of delivery: signature, photo, notes, partial items | ✅ Driver app POD form + box-level scan confirmation |
| Contactless SMS-link POD (customer signs on own device) | ✅ Auto-texted when a job goes In Transit; also on order detail |
| Cash on delivery (COD) capture | ✅ Order-level COD amount + collected flag (driver or customer can confirm) |
| Customer notifications (SMS on status change) | ✅ Twilio, with a log-only fallback when not configured |
| Customer self-service portal (place/track orders, invoices) | ✅ Portal login, order placement, tracking, invoices |
| Zone-based / automated job assignment | ✅ Pending Work Board's "Suggest" (smallest fitting van + free driver) |
| KPI reporting / exports | ✅ Insights dashboard + CSV export |
| Live GPS tracking on a map | ⚠️ Partial — one-off "last known location" ping per stop (Google Maps link), not a continuous live-tracking map |
| Route distances via real road network | ⚠️ Partial — straight-line distance with a routing-inefficiency correction factor, not turn-by-turn road routing (avoids needing a paid mapping API) |
| Native offline mobile app | ❌ Driver app is an installable web app (PWA-style); needs a signal, doesn't queue actions offline |
| Inbound API/webhooks, bulk CSV import of orders | ❌ Not built — orders go in via the dispatcher form or customer portal |

The ⚠️/❌ rows are the realistic gaps against a mature paid platform like
Detrack; everything else above is implemented and tested end to end.
