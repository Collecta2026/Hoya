# Voya — Go-Live Guide (Heliolink)

A plain-English, click-by-click guide to take Voya from a zip file on your PC to
a live system your dispatcher and drivers use every day — fast, stable, and
backed up. No prior server experience assumed.

---

## Is it ready to go live?

**Yes — for Heliolink's own operations.** Voya is a working system: dispatcher
control board, orders, route planning and optimisation, driver app with barcode
scanning and POD, deliveries and collections, customer SMS, KPIs and backups.
This guide stands it up as **Heliolink's own production instance**.

Two honest notes:

- **This is a single-operator deployment.** Turning it into a multi-tenant
  product you sell to *other* companies (separate logins, data walls between
  tenants, billing) is a later phase. Nothing here is wasted — it's the same
  system — but today's job is Heliolink live, not reselling yet.
- A few Track-POD features aren't built (native app, live map, PDF ePOD). None
  block daily operations; they're on the roadmap in the comparison document.

Total time to live: **about 45–60 minutes**, most of it waiting for things to
build. Running cost once live: roughly **£20–30/month** (see the last section).

---

## The software you'll use (and why)

You don't install servers yourself — a hosting platform (Render) runs everything.
Here's the full stack and what each piece is for:

| Piece | What it's for | Cost |
|---|---|---|
| **GitHub** | Stores your code; Render deploys from it | Free |
| **Render** | The host — runs the web app, the database, and the backup job | ~£20/mo (paid tier = always-on, fast) |
| **Neon (Postgres)** | The database — all your orders, PODs, KPIs — same stack as your cashflow app | Free tier; ~£19/mo paid when you scale |
| **Gunicorn** | The production web server inside the app (already configured) | Free (in the code) |
| **Twilio** | Sends the real customer text messages | Pay-as-you-go (~2–4p/text) |
| **Cloudflare R2** *(or AWS S3)* | Off-site copy of your backups | Free tier covers you |
| **Your domain** (e.g. voyahq.com) | The web address drivers/customers use | ~£10/year |
| **UptimeRobot** | Pings the site every 5 min, emails you if it's down | Free |
| **Sentry** *(optional)* | Emails you if the app hits an error | Free tier |

You already have most of the muscle for **speed and stability** built in: the app
runs under **Gunicorn with 3 workers**, uses a **managed Postgres** database, has a
**/healthz** health check Render watches to auto-restart if anything hangs, and is
pinned to the **Frankfurt (EU) region** so it's physically close to the UK — that
last point is the same latency lesson from the cashflow app, handled up front.

---

## Before you start — create these free accounts

1. **GitHub** — https://github.com (sign up if you don't have one)
2. **Render** — https://render.com (sign up; you can use your GitHub login)
3. **Neon** — https://neon.tech (the database; you likely already have an account from the cashflow app)
4. **Twilio** — https://www.twilio.com (for live SMS; you can do this after go-live)
5. **A domain** — buy `voyahq.com` (or your chosen name) from any registrar
   (Cloudflare, Namecheap, GoDaddy). *Don't* try for voya.com — it's taken.

Have the `voya.zip` file from this chat unzipped into a folder on your PC.

---

## Step 1 — Put the code on GitHub (10 min)

**The easy way (no commands):**

1. On github.com, click the **+** (top right) → **New repository**.
2. Name it `voya`, set it **Private**, click **Create repository**.
3. On the next page click **uploading an existing file**.
4. Open your unzipped `voya` folder, select **everything inside it** (including
   the hidden `.github` folder if your system shows it), and drag it all into
   the browser's upload box.
5. Wait for the files to list, then click **Commit changes**.

That's your code on GitHub. You'll only touch this again when you want to update
the app.

> If you prefer Git on the command line and have it installed:
> `git init && git add . && git commit -m "Voya" && git branch -M main`
> `git remote add origin https://github.com/<you>/voya.git && git push -u origin main`

---

## Step 2 — Create the Neon database (5 min)

Same as your cashflow app's database, so this will feel familiar.

1. Log in to https://neon.tech and click **New Project**.
2. Name it `voya`, and **choose a London / EU region** (keeps it close to the
   app for speed — the same reason we moved the cashflow DB to London).
3. Once created, open **Connection Details** and copy the **Pooled** connection
   string (the host has `-pooler` in it — use that one; it handles the app's
   multiple workers cleanly). It looks like
   `postgresql://user:pass@ep-xxx-pooler.eu-...neon.tech/voya?sslmode=require`.
4. Keep that string handy — you'll paste it into Render in the next step.

## Step 3 — Deploy on Render (10 min, mostly waiting)

1. Log in to https://dashboard.render.com.
2. Click **New +** (top right) → **Blueprint**.
3. Connect your GitHub and pick the **voya** repository.
4. Render reads the `render.yaml` file and shows it will create two things:
   a **web service** (voya) and a **backup job** (voya-backup). The database
   isn't here because it's your **Neon** project. Click **Apply**.
5. It now asks you to fill the "sync: false" values it left blank. Set:
   - **DATABASE_URL** → paste the **Neon pooled connection string** from Step 2
     (set it on **both** the web service and the backup job).
   - **ADMIN_EMAIL** → your real email (this becomes your login)
   - **ADMIN_PASSWORD** → a strong password you'll remember
   - Leave the Twilio and backup-bucket ones blank for now — you'll add them later.
6. Click **Apply / Create**. Render builds everything. Give it a few minutes;
   when the web service shows **Live** (green), it's up.

Your app is now at a URL like `https://voya.onrender.com`. Open it — you should
see the Voya sign-in page. Log in with the admin email and password you just set.

Because `SEED_DEMO` is set to **false** in the blueprint, you start with a
**clean system** — no sample customers or orders. Just your admin account.

---

## Step 4 — Point your own domain at it (10 min + DNS wait)

1. In Render, open the **voya** web service → **Settings** → **Custom Domains**
   → **Add Custom Domain**. Enter `app.voyahq.com` (a subdomain is cleanest).
2. Render shows you a **CNAME** record to add.
3. Go to your domain registrar's DNS settings and add that CNAME exactly as
   shown (name: `app`, value: the `...onrender.com` target Render gives you).
4. Back in Render, click **Verify**. Within minutes to an hour it goes green and
   Render issues a free HTTPS certificate automatically.

Drivers and customers now use `https://app.voyahq.com`.

---

## Step 5 — Turn on real customer texts (10 min)

Until you do this, texts are **logged** (visible under **Messages** in the app)
but not actually sent — useful for testing, not for live.

1. In Twilio, buy a phone number (a UK number, or an Alphanumeric Sender ID for
   UK if you don't need replies).
2. From the Twilio console copy your **Account SID**, **Auth Token**, and the
   **From** number.
3. In Render → **voya** service → **Environment**, set:
   - `TWILIO_SID` = your Account SID
   - `TWILIO_TOKEN` = your Auth Token
   - `TWILIO_FROM` = your Twilio number (e.g. `+44…`)
4. Click **Save Changes** — Render redeploys. Now dispatch and delivery texts go
   out for real. Send yourself a test order to confirm.

---

## Step 6 — Set up frequent, safe backups (15 min)

You get **two layers**, which is what you want:

**Layer 1 — Neon's own history/restore (automatic).**
Neon continuously keeps a restore history, so you can roll the database back to
an earlier point in time from the Neon console (**Restore** / branch from
history). On the free tier this window is short; a paid Neon plan extends it.
Nothing to set up — but open the Neon console once so you know where it is.

**Layer 2 — your own off-site snapshots every 6 hours (set this up).**
The blueprint includes a **backup job** that snapshots the whole database four
times a day and can push it to off-site storage so a copy exists outside Render:

1. Create a **Cloudflare R2** bucket (free) — or an AWS S3 bucket.
   In R2: create a bucket called `voya-backups`, then create an **API token**
   with read/write to it. Note the **Access Key ID**, **Secret**, and the
   **endpoint URL** (looks like `https://<account>.r2.cloudflarestorage.com`).
2. In Render → **voya-backup** job → **Environment**, set:
   - `BACKUP_BUCKET` = `voya-backups`
   - `BACKUP_ENDPOINT` = your R2 endpoint URL
   - `AWS_ACCESS_KEY_ID` = the access key
   - `AWS_SECRET_ACCESS_KEY` = the secret
3. Save. To test it now without waiting, open the job and click **Trigger Run**.
   Check the R2 bucket — a `voya_backup_<timestamp>.json` file should appear.

**Layer 3 — the manual button.** Any time, log in as admin and go to
**Reports → Download backup** for an instant full snapshot to your own PC. Do
this before any big change.

> **Do a restore drill once.** A backup you've never restored isn't a backup.
> Once you're comfortable, restore the voya-db to a fresh test database from a
> Render backup so you've seen it work. Ten minutes now saves a very bad day later.

---

## Step 7 — Speed & stability checklist (already handled, but verify)

Most of this is baked in; just confirm:

- ✅ **Always-on** — the web service is on the **Starter** paid plan, so it never
  sleeps (the free tier sleeps after 15 min and is slow to wake).
- ✅ **Close to users** — both the app and database are in **Frankfurt (EU)**.
- ✅ **Multiple workers** — Gunicorn runs 3 workers, so several people can use it
  at once without slowing each other down.
- ✅ **Auto-restart** — Render watches `/healthz`; if the app stops responding it
  restarts it automatically.
- **Add uptime alerts (5 min):** in **UptimeRobot**, add a monitor for
  `https://app.voyahq.com/healthz` checking every 5 minutes, with your email as
  the alert. Now you know before your drivers do if anything's wrong.
- **Optional — error alerts:** create a free **Sentry** project (Python/Flask),
  and it'll email you the moment the app throws an error. (Wiring is a two-line
  add if you want it — ask and I'll include it.)

---

## Step 8 — Load Heliolink's real data (20 min)

Logged in as admin:

1. **Fleet** → add your **vehicles** (reg, type, pallet capacity) and your
   **drivers** (each gets their own login — give them the email + password).
2. **Customers** → add your real customers and their **rate profile**
   (base £/drop + £/mile + £/pallet). Each customer gets a **portal code** you
   can hand out so they can self-order.
3. Create a couple of test **orders**, build a **route**, **optimise** and
   **dispatch** it, and walk one stop through on your own phone to see the full
   loop — scan, POD photo, signature, and the customer text — before you rely on
   it for a real round.

---

## Step 9 — Get the app onto drivers' phones (5 min each)

Voya installs like an app without any app store:

1. On the driver's phone, open `https://app.voyahq.com` in **Chrome** (Android)
   or **Safari** (iPhone) and sign in with their driver login.
2. **Android:** menu (⋮) → **Add to Home screen**.
   **iPhone:** Share button → **Add to Home Screen**.
3. It now sits as a **Voya icon** on their home screen and opens full-screen like
   a normal app. Camera scanning and signature capture work straight away.

---

## Optional — hardware barcode scanners

Voya scans with the **phone camera** out of the box (free, works today). For
high-volume rounds you can pair a **Bluetooth scanner** to the driver's phone —
it types the barcode straight into Voya's scan box (the box now accepts the
scanner's Enter keystroke, so there's nothing to configure). Buy **2D imagers**
(they read courier QR and DataMatrix labels, not just old barcodes):

- **Budget / best value:** NETUM 3-in-1 (Bluetooth + 2.4G + USB), ~£80–130.
- **Rugged / all-day:** Zebra CS60 or Socket Mobile DuraScan, ~£200–350.

Pair it once via the phone's Bluetooth settings, open a stop's scan screen, tap
the barcode box, and scan — each read logs exactly like a camera scan.

## Go-live checklist

- [ ] Admin password is your own strong one (not the default)
- [ ] `SEED_DEMO` is false — no sample data in the live system
- [ ] Custom domain live with HTTPS (padlock shows)
- [ ] Twilio connected and a test text received
- [ ] Database backups visible in Render **and** off-site copies in R2
- [ ] One backup restore drill done
- [ ] UptimeRobot monitor active on `/healthz`
- [ ] Real vehicles, drivers and customers loaded
- [ ] One full test round completed on a real phone
- [ ] Each driver has the app on their home screen

When every box is ticked, you're live.

---

## Updating the app later

When I send you an improved version (CSV import, PDF ePOD, etc.), you upload the
changed files to the same GitHub repo (or `git push`), and Render **redeploys
automatically** — usually with zero downtime. Your data is untouched; only the
code changes.

## Running costs (rough, monthly)

| Item | Approx. |
|---|---|
| Render web service (Starter) | £14 |
| Render Postgres (Basic 256MB) | £6 |
| Render backup job | pennies (runs briefly) |
| Cloudflare R2 backups | £0 (free tier) |
| Domain | ~£1 (annual ÷ 12) |
| Twilio texts | usage — ~£3–4 per 100 texts |
| **Total baseline** | **~£20–25/mo + texts** |

Compare that to Track-POD's per-driver subscription — with a handful of drivers
you're already ahead, and you own the system.

---

*Stuck on any step? Tell me which number and what you see on screen, and I'll
walk you through it — the same way we got the cashflow app running.*
