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

### Upgrading to the rate-card pricing / multidrop / schedule update

This update replaced the generic cost-based rate engine with Heliolink's
published rate card (postcode band x account tier for pallets, a
"Wavelength" events/distance mode, and a custom/manual mode), added
multi-drop rounds, flexible quantity entry, a weekly schedule board, and
email notifications. Run this once in Neon's SQL Editor before redeploying:

```sql
ALTER TABLE customers ADD COLUMN IF NOT EXISTS main_product VARCHAR(120);
ALTER TABLE customers ADD COLUMN IF NOT EXISTS tier VARCHAR(20) DEFAULT 'Standard';
UPDATE customers SET pricing_mode = 'pallet' WHERE pricing_mode = 'standard';

ALTER TABLE orders ADD COLUMN IF NOT EXISTS full_pallets INTEGER DEFAULT 0;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS half_pallets INTEGER DEFAULT 0;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS goods_category VARCHAR(120);
ALTER TABLE orders ADD COLUMN IF NOT EXISTS goods_description TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS quantity_summary VARCHAR(255);
ALTER TABLE orders ADD COLUMN IF NOT EXISTS wl_vehicle VARCHAR(20);
ALTER TABLE orders ADD COLUMN IF NOT EXISTS wl_miles FLOAT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS wl_after6 BOOLEAN DEFAULT FALSE;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS manual_net FLOAT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS price_net FLOAT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS price_vat FLOAT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS price_basis VARCHAR(120);

CREATE TABLE IF NOT EXISTS order_drops (
    id SERIAL PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    postcode VARCHAR(20) NOT NULL,
    name VARCHAR(255),
    seq INTEGER DEFAULT 0
);
```

Existing customers default to the "pallet" pricing mode and "Standard"
tier after this migration — check each real customer's actual tier
(Standard/Trade/Key/Major) on the Customers page afterwards. Existing
orders keep their old `quoted_price`; only new orders get the full
net/VAT/basis breakdown. No existing data is deleted.

**What's new:**
- **Pricing** (Customers page, "Edit pricing"): each customer is now
  priced by **Pallet rate card** (postcode band A-G x account tier,
  Standard/Trade/Key/Major, with the 16% fuel surcharge and 20% VAT
  built into the quote), **Wavelength** (events/distance: vehicle mpg x
  round-trip miles, for one-off jobs like equipment moves), **Custom**
  (a manually agreed net price), or **Flat** (the original fixed £ per
  pallet, kept for continuity). A customer discount % still applies on
  top of whichever mode is used. The old generic cost-model knobs on
  **Rates & Settings** now only affect the legacy Flat mode's VAT rate.
- **Multi-drop rounds**: choose "Multi-drop round" as the job type on
  New Order, then add/remove the round's drop postcodes from **Route
  Planner → Multi-drop rounds**. Priced as a custom/manual net charge.
- **Flexible quantities**: New Order now has an "Add line" quantity
  builder (pick any mix of pallet/half-pallet/box/parcel/basket/flight
  case/cage/crate/item) instead of fixed pallet/parcel boxes — this
  still feeds the same barcode/label/scanning system underneath.
- **Schedule** (top nav): a weekly calendar board of every booking by
  day, colour-coded by status, with one-click "+ add" into a given day.
- **Email notifications**: every booking confirmation and status change
  now also emails `SALES_EMAIL` (default `sales@heliolink.co.uk`) in
  addition to the existing customer SMS. Configure `SMTP_HOST`,
  `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `MAIL_FROM`, `SALES_EMAIL` in
  Render's environment — with no SMTP configured it logs the email to
  the console instead of failing, same pattern as the SMS log fallback.

### Note on Python version

Render can default to a very new Python where `psycopg2-binary` has no
prebuilt wheel yet. `runtime.txt` and `.python-version` pin the build to
3.12.7 to avoid this (the same fix used previously on this platform).

## Testing the driver app / customer portal on a real phone

Once Hoya is deployed to Render (or even just running locally on your
laptop and reachable on the same wifi), you don't need an app store —
it's a normal website, so any phone browser works:

1. **Get the URL.**
   - Live/deployed: your Render URL, e.g. `https://hoya-dnvg.onrender.com`.
   - Local, on the same wifi as your phone: find your laptop's LAN IP
     (Windows: `ipconfig`, look for IPv4 Address, e.g. `192.168.1.42`),
     run `python app.py`, then on the phone browse to
     `http://192.168.1.42:5000` (not `localhost` — that means the phone
     itself). Your laptop's firewall may need to allow the connection.
2. **Sign in as a driver** at `/driver/login` (or tap "Driver App" in the
   nav if you're testing from a staff login) using a driver's PIN — set
   this on the Fleet page when you add a driver. From here you can view
   assigned stops, scan barcodes with the phone's camera, capture a
   signature by finger, take a POD photo with the phone's camera, and
   view/print the delivery note.
3. **Add it to the home screen so it behaves like an app:**
   - **iPhone (Safari):** open the driver URL → tap the Share icon →
     "Add to Home Screen". It launches full-screen, no browser bar.
   - **Android (Chrome):** open the driver URL → tap the ⋮ menu →
     "Add to Home screen" / "Install app".
   This uses the PWA manifest at `static/manifest.json` — it's a
   shortcut with an app-like frame, not an offline-capable native app
   (Hoya still needs a signal to load and submit each stop).
4. **Test the customer contactless-signing link** the same way: send
   yourself a test SMS (or check the terminal/Render logs if Twilio
   isn't configured — the link is logged there) and open it on your
   phone to confirm the signature pad and COD checkbox work with touch.
5. **Test the customer portal** at `/portal/login` with a customer's
   portal email/password (set up on the Customers page — see below) to
   confirm order placement and tracking work on a small screen.
6. **Camera/location permissions:** the browser will prompt for camera
   access (barcode scanning, POD photo) and location access (one-off
   "last known location" ping) the first time each is used — allow both
   for full functionality. Camera scanning requires HTTPS (Render gives
   you this automatically) or `localhost`; it will not work over plain
   `http://` on a LAN IP, so use manual barcode entry when testing that way.

## Customer portal logins

Customer portal accounts (email + password for `/portal/login`, so a
customer can place and track their own orders) are managed from the
**Customers** page (Operations menu, admin/dispatcher only):

- **New customer:** the "Add customer" form has optional "Portal login
  email" / "Portal password" fields — fill these in when creating the
  customer and the login is created at the same time.
- **Existing customer:** each row in the customer table has a "Set up
  login" / "Manage login" button that expands a small form to set or
  reset that customer's email and password at any time, and a "Disable"
  button to revoke portal access without deleting the customer or their
  order history.
- If no password is entered, it defaults to `changeme123` — tell the
  customer to change it, or set a real one yourself when creating the login.

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
| Published rate-card pricing by account tier / distance | ✅ Pallet rate card (postcode band x tier) + Wavelength (events/distance) + Custom/manual modes |
| Multi-drop rounds | ✅ "Multi-drop round" job type + drop list managed from Route Planner |
| Weekly schedule / calendar view | ✅ Schedule page (top nav) |
| Email notifications alongside SMS | ✅ Booking confirmation + status-change emails to sales@, SMTP with log-only fallback |
| Manage/remove drivers & vehicles | ✅ Fleet page - remove is blocked while a driver/van has an active job, historical orders keep their record but are unlinked |
| POD photos visible to dispatch/admin | ✅ Dashboard shows a recent proof-of-delivery photo gallery, linking back to each order |
| KPI reporting / exports | ✅ Insights dashboard + CSV export |
| Live GPS tracking on a map | ⚠️ Partial — one-off "last known location" ping per stop (Google Maps link), not a continuous live-tracking map |
| Route distances via real road network | ⚠️ Partial — straight-line distance with a routing-inefficiency correction factor, not turn-by-turn road routing (avoids needing a paid mapping API) |
| Native offline mobile app | ❌ Driver app is an installable web app (PWA-style); needs a signal, doesn't queue actions offline |
| Inbound API/webhooks, bulk CSV import of orders | ❌ Not built — orders go in via the dispatcher form or customer portal |

The ⚠️/❌ rows are the realistic gaps against a mature paid platform like
Detrack; everything else above is implemented and tested end to end.

### Latest fix: /schedule crash + automatic column migration

- **Fixed** `AttributeError: 'Order' object has no attribute 'delivery_time'` on `/schedule`.
  `Order` now has an optional `delivery_time` ("HH:MM") column, read from the order form
  if a `delivery_time` field is present; the schedule sorts by it, then by collection time.
- **Added `ensure_schema()`** (runs at startup after `create_all()`): it adds any model column
  that is missing from an existing table with `ALTER TABLE ... ADD COLUMN`. It never drops,
  renames or retypes anything, so it is safe on every boot and prevents the
  `psycopg2.errors.UndefinedColumn` startup crash for *new columns*. It does **not** handle
  renames (e.g. `pricing_mode` -> `pricing_type`); those still need a manual `ALTER TABLE`.
- `render.yaml` now declares `SALES_EMAIL`, `MAIL_FROM` and the `SMTP_*` variables.
- `static/logo-orange.png` (Heliolink logo) added for use in templates.

**Important:** Hoya must run Hoya's own `models.py` (column `customers.pricing_mode`).
Heliolink's models use `pricing_type`; deploying those against the Hoya database is what
caused the `customers.pricing_type does not exist` crash.
