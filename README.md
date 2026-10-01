# Heliolink — Delivery Ops

Heliolink's dispatch & delivery operations platform. **Flask + SQLAlchemy**,
SQLite locally, **Postgres (Neon)** in production, deployable on **Render** —
the same architecture as the original app.

## What it does

- **Schedule** — week-by-week delivery calendar (the landing page).
- **Bookings** — create jobs with automatic pricing:
  - **Pallet** customers: delivery postcode → distance **Band A–G** → per-pallet
    rate by **account tier** (Standard/Trade/Key/Major), + 16% fuel surcharge + VAT,
    + same-day charge where it applies.
  - **Wavelength** (events) customers: round-trip miles × vehicle → distance quote.
  - **Custom / multi-drop**: a manually agreed net price + VAT.
  - A **manual override** on any booking.
- **Auto on confirm** — labels + POD are generated and an email is sent to
  `sales@heliolink.co.uk` (also on status changes).
- **Billing** — every booking with net/VAT/total and Quoted → Invoiced → Paid.
- **Route planner** — a driver's run in time order; multi-drop rounds where you
  insert the drop postcodes.
- **Print labels** — 4″×4″ barcode labels (one barcode per order).
- **POD** — A4 Proof of Delivery / Collection Note (RHA 2009 wording), collection
  and delivery copies, same order barcode.
- **Customers / Fleet / Employees** — full add / edit / remove.

## Quick start (local)

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open http://localhost:5000. First run prints the admin login
(`ali@heliolink.co.uk` / `Heliolink26` by default — override with `ADMIN_EMAIL`
/ `ADMIN_PASSWORD`). Demo customers/drivers are seeded unless `SEED_DEMO=false`.

## Email

Emails to sales@ send over SMTP when configured, otherwise they're logged to the
console (log-only fallback), so the app runs fully without credentials. Set
`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `MAIL_FROM`, `SALES_EMAIL`
to send for real. The email links to the printable labels and POD.

## Deploy (GitHub → Render → Neon)

1. **Neon** — create a Postgres project, copy the pooled connection string.
2. **GitHub** — push this folder (keep `templates/` and `static/` intact).
3. **Render** — New → Blueprint → point at the repo (`render.yaml` provisions it).
   Then set env vars: `DATABASE_URL` (Neon pooled), `ADMIN_EMAIL`,
   `ADMIN_PASSWORD`, `SEED_DEMO=false`, and the `SMTP_*` vars for real email.
4. Visit `/healthz`, then sign in and start adding customers, fleet and bookings.

## Files

```
app.py        routes, factory, seed, email glue
models.py     SQLAlchemy models (Employee, Customer, Driver, Vehicle, Order, OrderDrop)
pricing.py    rate card + band lookup + Wavelength + custom pricing
emailer.py    SMTP send with log-only fallback
templates/    Jinja2 templates
static/       CSS (white/orange + brand-navy dark) and logos
```
