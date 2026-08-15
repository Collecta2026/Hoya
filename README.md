# Voya — Delivery Operations Platform

*Product name: **Voya**. Heliolink Ltd is the launch tenant; the platform is
built multi-tenant so it can be sold to other operators. (Domain/trademark
note: voya.com belongs to Voya Financial — a financial-services brand in
different trademark classes — so launch on e.g. voyahq.com and run a clearance
search in the software/logistics classes for your target markets before scaling.)

A full delivery operations system: dispatcher control board, order management,
route planning & optimisation, multi-driver/vehicle management, a driver mobile
app (installable PWA) with proof-of-delivery imaging and signature capture,
customer self-order portal with tracking, customer-specific rates, automated
customer SMS, and reporting.

Built as a single Flask + Postgres app so it deploys to Render from GitHub in
one step — the same stack the Heliolink cashflow app already runs on.

---

## What's in the box

| Area | What it does |
|------|--------------|
| **Control board** | Live status of today's routes and unassigned orders, with counts for unassigned / out / delivered / failed. |
| **Orders** | Create orders, auto-priced from the customer's rate card. Filter by status. |
| **Route planning** | Tick unassigned orders, assign driver + vehicle, and the stops are auto-sequenced from the depot for the shortest round. Re-optimise any time. |
| **Dispatch** | One button sends every recipient an "out for delivery" text and moves the route live. |
| **Driver app (PWA)** | Phone-friendly. Driver sees their round in order, navigates, marks arrived, then captures photo + signature + notes + GPS as POD. Customer gets an automatic "delivered" text. |
| **Customer portal** | Customers log in with an account code, place their own orders, and track them. |
| **Customers & rates** | Per-customer rate profile: base £/drop + £/mile + £/pallet. Every order priced from it. |
| **Fleet** | Add drivers (with their own login) and vehicles. |
| **Reports** | Delivery success rate, per-driver performance, revenue on completed deliveries. |
| **Messages** | Full SMS log — every text sent or would-be-sent. |
| **Barcode scanning** | Camera scanner (any smartphone) on the driver app. Scan at three points: **van load**, **delivery**, and **collection**. Scans by item across parcels and pallets, flags anything not on the manifest, catches duplicates, and — on collections — lets the driver scan brand-new barcodes to add items picked up on the doorstep. Manual entry always available as a fallback. |
| **Collections** | Every job is a delivery **or** a collection, end to end — order entry, driver wording, scanning, POD, customer texts and KPIs all adapt. |
| **KPIs & reporting** | Period-filtered dashboard (today / 7 / 30 days / all): parcels and pallets handled, drops vs collections, success rate, delivered-in-full (DIFOT-style), failed stops, routes run, revenue, and per-driver performance. CSV export. |
| **Backups** | One-click full JSON backup for admins, plus a nightly Render cron job that snapshots the database and (with a bucket configured) copies it offsite. |

## Sign-in details (seeded on first run)

- **Admin:** `admin@heliolink.co` / `Heliolink26`
- **Dispatcher:** `dispatch@heliolink.co` / `Heliolink26`
- **Drivers:** `john@heliolink.co` / `sam@heliolink.co` — password `driver123`
- **Customer portal codes:** `ESOL2026`, `WILL2026`, `ETD2026`

Change these once you're in (and reset the admin password before going live).

---

> **Note:** this build adds item-level scanning, collections and KPIs, which
> means new database tables. On a brand-new deploy that's automatic. If you're
> re-running over an old local `helioops.db`, delete that file first so the new
> tables are created.

## Run it locally (no setup)

```bash
pip install -r requirements.txt
python app.py
```

Open http://localhost:5000. With no `DATABASE_URL` it uses a local SQLite file,
so it just works. Texts are logged (visible under **Messages**), not sent.

## Deploy to Render (GitHub → live)

1. Create a new GitHub repo and push this folder to it.
2. In Render, click **New + → Blueprint** and select the repo. Render reads
   `render.yaml` and provisions the web service **and** a Postgres database,
   wiring `DATABASE_URL` automatically.
3. First deploy creates the tables and seed data. You're live.

### Backups & offsite copies
The nightly cron in `render.yaml` snapshots everything. For durable **offsite**
copies, create an S3 or Cloudflare R2 bucket and set `BACKUP_BUCKET`,
`BACKUP_ENDPOINT`, `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` on the
`helioops-backup` job. Render's paid Postgres also includes automated daily
backups and point-in-time restore — belt and braces. Admins can also pull a
snapshot any time from **Reports → Download backup**.

### Turn on real texts
In the Render dashboard, on the `helioops` service, set three environment
variables from your Twilio account, then redeploy:

```
TWILIO_SID     ACxxxxxxxx
TWILIO_TOKEN   your-auth-token
TWILIO_FROM    +44...    (your Twilio number)
```

Without them, the system runs fully — it just records texts in the log instead
of sending them.

## Install the driver app to a phone

On the driver's phone, open the site in Chrome/Safari, sign in, then
**Add to Home Screen**. It installs as a standalone app icon.

---

## Notes & sensible next steps

- **POD images** are stored in the database so they survive Render's redeploys
  (its disk is temporary). At higher volume, move them to object storage
  (S3 / Cloudflare R2) — the storage call is isolated in `models.py`/`app.py`.
- **Route optimisation** uses a nearest-neighbour pass over UK outward-code
  centroids (in `optimise.py`) — no API key needed, removes backtracking. Swap
  in a live routing/geocoding API by replacing `optimise_sequence` and
  `geocode` for road-accurate distances and drive times.
- Natural additions: CSV order import, driver live GPS on a dispatcher map,
  time-window/ETA calculation, period filters and CSV export on reports,
  and back-to-back liability fields on the customer record.

## File map

```
app.py         all routes + app factory + seed data
models.py      database tables
optimise.py    geocoding + route sequencing
sms.py         Twilio (or log-only) texting
templates/     every screen
static/        stylesheet, PWA manifest, service worker, icon
render.yaml    Render blueprint (web + postgres)
```
