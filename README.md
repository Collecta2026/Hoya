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

Open http://localhost:5000. On first run it seeds:

- **Admin login:** `admin@heliolink.co` / `Heliolink26` (override with
  `ADMIN_EMAIL` / `ADMIN_PASSWORD` env vars)
- **Demo customer portal login:** `ops@williamshandbaked.co.uk` / `customer123`
- **Demo drivers (PIN login):** Jamie Ellis (1111), Priya Anand (2222),
  Marcus Reid (3333)
- Sample vehicles, and 5 sample orders across 2 demo customers

Set `SEED_DEMO=false` to skip demo data (used automatically on the
production deploy below).

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
   (`db.create_all()`), so no manual migration step is needed for a first
   deploy — for schema changes later, re-deploying with new columns/tables
   will add them, but existing data is preserved (SQLAlchemy doesn't drop
   tables on `create_all()`).
5. Visit `/healthz` to confirm the app is live, then log in with your admin
   credentials and start adding real customers, fleet and orders.

### Note on Python version

Render can default to a very new Python where `psycopg2-binary` has no
prebuilt wheel yet. `runtime.txt` and `.python-version` pin the build to
3.12.7 to avoid this (the same fix used previously on this platform).

## Known gaps vs a full commercial platform (e.g. Detrack/TrackPod)

- No native offline driver app (this is an installable PWA-style web app —
  works great with a signal, doesn't queue actions while fully offline)
- No live GPS tracking map (stop sequencing is postcode-based, not GPS-based)
- Route distances are straight-line-based with a routing-inefficiency
  correction factor, not real road-network routing (no paid API key needed)
- No inbound API/webhooks or bulk CSV import yet (orders are entered via
  the dispatcher form, the customer portal, or could be scripted against
  the existing routes)

These are the same gaps documented on the platform's history and can be
prioritised as a follow-up phase.
