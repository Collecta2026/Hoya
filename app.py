"""HelioOps — Heliolink delivery operations platform.

A single deployable Flask app covering:
  - dispatcher control board       - multi-driver & vehicle management
  - order management & import      - customer self-order portal + tracking
  - route planning & optimisation  - driver PWA (route, POD, imaging)
  - customer-specific rates        - automated customer SMS
  - reporting

Runs on SQLite locally (no setup) and Postgres on Render (set DATABASE_URL).
"""
import os
import io
import base64
import secrets
from datetime import date, datetime
from functools import wraps

from flask import (Flask, render_template, request, redirect, url_for,
                   session, flash, abort, send_file, jsonify)
from jinja2 import ChoiceLoader, FileSystemLoader

from models import (db, User, Driver, Vehicle, Customer, Route, Order,
                    Stop, POD, SmsLog, Item, Scan, Surcharge, Invoice,
                    InvoiceLine, RateSettings, Zone)
from optimise import (optimise_sequence, geocode, haversine, DEPOT_DEFAULT,
                      region_of, REGIONS)
from sms import send_sms

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ASSET_EXT = (".css", ".js", ".json", ".svg", ".png", ".ico",
             ".webmanifest", ".jpg", ".jpeg", ".gif", ".txt")


def find_asset(filename):
    """Locate a static asset in static/ or (if uploads got flattened) the
    repo root. Extension-whitelisted so source files are never served."""
    if not filename.lower().endswith(ASSET_EXT):
        return None
    for base in (os.path.join(BASE_DIR, "static"), BASE_DIR):
        full = os.path.normpath(os.path.join(base, filename))
        if full.startswith(BASE_DIR) and os.path.isfile(full):
            return full
    return None


def create_app():
    app = Flask(__name__, static_folder=None)
    # Resolve templates from templates/ first, then the repo root. This keeps
    # the app working even if a GitHub folder upload flattened the files.
    app.jinja_loader = ChoiceLoader([
        FileSystemLoader(os.path.join(BASE_DIR, "templates")),
        FileSystemLoader(BASE_DIR),
    ])
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-change-me")

    db_url = os.environ.get("DATABASE_URL", "sqlite:///helioops.db")
    # Render/Heroku hand out postgres:// which SQLAlchemy wants as postgresql://
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024  # 12MB uploads

    db.init_app(app)
    with app.app_context():
        db.create_all()
        seed_if_empty()

    register_routes(app)
    return app


# --------------------------------------------------------------------------
#  Auth helpers
# --------------------------------------------------------------------------
def current_user():
    uid = session.get("uid")
    return db.session.get(User, uid) if uid else None


def login_required(*roles):
    def deco(fn):
        @wraps(fn)
        def wrap(*a, **kw):
            u = current_user()
            if not u:
                return redirect(url_for("login", next=request.path))
            if roles and u.role not in roles:
                abort(403)
            return fn(*a, **kw)
        return wrap
    return deco


def next_ref(prefix, model, field):
    n = db.session.query(model).count() + 1
    ref = f"{prefix}{n:05d}"
    while db.session.query(model).filter(getattr(model, field) == ref).first():
        n += 1
        ref = f"{prefix}{n:05d}"
    return ref


# --------------------------------------------------------------------------
#  Rates
# --------------------------------------------------------------------------
DEPOT_ADDRESS = "Heliolink Ltd, Ashton-on-Ribble"
DEPOT_POSTCODE = "PR2 2TE"
LITRES_PER_GALLON = 4.54609


def get_settings():
    s = db.session.get(RateSettings, 1)
    if not s:
        s = RateSettings(id=1)
        db.session.add(s)
        db.session.commit()
    return s


def zone_multiplier(postcode):
    z = Zone.query.filter_by(region=region_of(postcode)).first()
    return z.multiplier if z else 1.0


def mpg_for(vtype, settings):
    return {"panel van": settings.mpg_panel, "2.5t": settings.mpg_25t,
            "7.5t": settings.mpg_75t}.get(vtype, settings.mpg_panel)


def vtype_for_pallets(n):
    if n <= 2:
        return "panel van"
    if n <= 4:
        return "2.5t"
    return "7.5t"


def recommend_rate(postcode, pallets, weight_per_pallet=0, service="48",
                   vtype=None, settings=None):
    """Cost-based standard rate. Returns a full breakdown dict."""
    s = settings or get_settings()
    n = max(int(pallets or 0), 1)
    vtype = vtype or vtype_for_pallets(n)
    a = geocode(DEPOT_POSTCODE) or DEPOT_DEFAULT
    b = geocode(postcode) or a
    one_way = haversine(a, b)
    miles = one_way * (2 if s.round_trip else 1)

    mpg = mpg_for(vtype, s) or 25.0
    litres = (miles / mpg) * LITRES_PER_GALLON if mpg else 0
    fuel_cost = litres * (s.fuel_price_per_litre or 0)

    drive_hours = miles / (s.avg_speed_mph or 40)
    handling_hours = n * (s.handling_min_per_pallet or 0) / 60.0
    labour_hours = drive_hours + handling_hours
    driver_cost = labour_hours * (s.driver_rate_per_hour or 0)

    base_cost = fuel_cost + driver_cost + (s.fixed_cost_per_job or 0)
    zmult = zone_multiplier(postcode)
    cost = base_cost * zmult
    if str(service) == "24":
        cost *= (1 + (s.service_uplift_24h_pct or 0) / 100.0)
    recommended = cost * (1 + (s.margin_pct or 0) / 100.0)

    return {
        "vtype": vtype, "region": region_of(postcode),
        "one_way_miles": round(one_way, 1), "miles": round(miles, 1),
        "mpg": mpg, "litres": round(litres, 1), "fuel_cost": round(fuel_cost, 2),
        "drive_hours": round(drive_hours, 2), "handling_hours": round(handling_hours, 2),
        "driver_cost": round(driver_cost, 2), "fixed": round(s.fixed_cost_per_job or 0, 2),
        "zone_mult": zmult, "service": str(service),
        "cost": round(cost, 2), "margin_pct": s.margin_pct,
        "recommended": round(recommended, 2),
        "weight_per_pallet": weight_per_pallet, "total_weight": n * (weight_per_pallet or 0),
    }


def quote_order(customer, postcode, pallets, weight=0, start="PR2 2TE"):
    """Carriage price. Standard-mode customers use the cost engine (less their
    discount); flat-mode customers use their bespoke £ rate card."""
    if not customer:
        return 0.0
    if (customer.pricing_mode or "flat") == "standard":
        rec = recommend_rate(postcode, pallets,
                             (weight or 0) / max(int(pallets or 1), 1))["recommended"]
        price = rec * (1 - (customer.discount_pct or 0) / 100.0)
        return round(max(price, customer.min_charge or 0), 2)
    a = geocode(start) or DEPOT_DEFAULT
    b = geocode(postcode) or a
    miles = haversine(a, b)
    price = (customer.rate_base or 0) \
        + (customer.rate_per_mile or 0) * miles \
        + (customer.rate_per_pallet or 0) * (pallets or 0) \
        + (customer.rate_per_kg or 0) * (weight or 0)
    price = max(price, customer.min_charge or 0)
    return round(price, 2)


def set_legs(order, customer, job, addr, pc):
    """Populate collection + delivery legs. The non-entered leg is the depot."""
    if job == "collection":
        order.collection_address = addr
        order.collection_postcode = pc
        order.delivery_address = DEPOT_ADDRESS
        order.delivery_postcode = DEPOT_POSTCODE
    else:
        order.collection_address = DEPOT_ADDRESS
        order.collection_postcode = DEPOT_POSTCODE
        order.delivery_address = addr
        order.delivery_postcode = pc


def order_surcharge_total(order, carriage):
    """Sum of applied accessorial surcharges (excludes fuel)."""
    total = 0.0
    detail = []
    for s in order.surcharges:
        amt = s.compute(carriage, order.pallets)
        total += amt
        detail.append(f"{s.name} £{amt:.2f}")
    return round(total, 2), "; ".join(detail)


def ship_date(order):
    if order.stop and order.stop.completed_at:
        return order.stop.completed_at.date()
    return order.created_at.date()


def generate_invoice(customer, start, end):
    """Build a weekly invoice for one customer from its completed, un-invoiced
    shipments in the period. Returns the Invoice, or None if nothing to bill."""
    orders = [o for o in customer.orders
              if o.status in ("delivered", "collected")
              and o.invoice_id is None
              and start <= ship_date(o) <= end]
    if not orders:
        return None

    inv = Invoice(ref=next_ref("INV", Invoice, "ref"), customer=customer,
                  period_start=start, period_end=end, issue_date=date.today(),
                  fuel_surcharge_pct=customer.fuel_surcharge_pct or 0)
    db.session.add(inv)
    db.session.flush()

    carriage_sum = surcharge_sum = 0.0
    for o in sorted(orders, key=ship_date):
        carriage = o.price or 0.0
        sur_amt, sur_detail = order_surcharge_total(o, carriage)
        line = InvoiceLine(
            invoice=inv, order_ref=o.ref, ship_date=ship_date(o),
            collection=o.collection_address, collection_pc=o.collection_postcode,
            delivery=o.delivery_address, delivery_pc=o.delivery_postcode,
            pallets=o.pallet_count, weight_kg=o.weight_kg,
            carriage=carriage, surcharge_detail=sur_detail,
            surcharge_amount=sur_amt, line_total=round(carriage + sur_amt, 2),
            pod_ref=(o.ref if o.pod and o.pod.photo else None))
        db.session.add(line)
        o.invoice_id = inv.id
        carriage_sum += carriage
        surcharge_sum += sur_amt

    fuel = round(carriage_sum * (inv.fuel_surcharge_pct or 0) / 100.0, 2)
    subtotal = round(carriage_sum + surcharge_sum + fuel, 2)
    vat = round(subtotal * 0.20, 2)
    inv.carriage = round(carriage_sum, 2)
    inv.surcharge_total = round(surcharge_sum, 2)
    inv.fuel_surcharge = fuel
    inv.subtotal = subtotal
    inv.vat = vat
    inv.total = round(subtotal + vat, 2)
    return inv


def build_items(order, parcels, pallets, barcodes=None):
    """Create scannable Item rows for an order. Auto-generates barcodes
    (e.g. HL00007-P1, HL00007-L1) unless explicit barcodes are supplied."""
    barcodes = barcodes or []
    bc = iter(barcodes)
    for n in range(1, int(parcels or 0) + 1):
        code = next(bc, None) or f"{order.ref}-P{n}"
        db.session.add(Item(order=order, barcode=code, kind="parcel"))
    for n in range(1, int(pallets or 0) + 1):
        code = next(bc, None) or f"{order.ref}-L{n}"
        db.session.add(Item(order=order, barcode=code, kind="pallet"))


# --------------------------------------------------------------------------
#  Scheduling / SLA helpers
# --------------------------------------------------------------------------
from datetime import time as dtime, timedelta
try:
    from zoneinfo import ZoneInfo
    UK = ZoneInfo("Europe/London")
except Exception:
    UK = None

VAN_SIZES = ["panel van", "2.5t", "7.5t"]


def uk_now():
    return datetime.now(UK).replace(tzinfo=None) if UK else datetime.now()


def apply_scheduling(o, form):
    """Read collection/delivery scheduling from a form onto an order."""
    cd = form.get("collection_date")
    dd = form.get("delivery_date")
    timing = form.get("timing", "booked")
    today = uk_now().date()
    o.collection_date = datetime.strptime(cd, "%Y-%m-%d").date() if cd else today
    o.collection_time = form.get("collection_time") or None
    o.timing = timing
    if dd:
        o.delivery_date = datetime.strptime(dd, "%Y-%m-%d").date()
    elif timing == "same-day":
        o.delivery_date = o.collection_date
    elif timing == "48h":
        o.delivery_date = o.collection_date + timedelta(days=2)
    else:
        o.delivery_date = o.collection_date


def same_day_cutoff_warning(o):
    if o.timing == "same-day" and o.collection_date == uk_now().date() \
            and uk_now().hour >= 11:
        return "Past the 11:00 same-day cut-off — booked for next available."
    return None


def sla_for(o):
    """(css_class, label) for the SLA countdown: amber within 4h, red overdue."""
    if o.status in ("delivered", "collected", "failed"):
        return "", "done"
    if o.job_type == "collection":
        d = o.collection_date or o.created_at.date()
        t = o.collection_time or "17:00"
    else:
        d = o.delivery_date or o.collection_date or o.created_at.date()
        t = "17:00"
    try:
        hh, mm = [int(x) for x in t.split(":")]
    except Exception:
        hh, mm = 17, 0
    deadline = datetime.combine(d, dtime(hh, mm))
    now = uk_now()
    if now > deadline:
        return "sla-red", "overdue"
    if (deadline - now) <= timedelta(hours=4):
        return "sla-amber", "due soon"
    return "", "on time"


# --------------------------------------------------------------------------
#  Routes
# --------------------------------------------------------------------------
def register_routes(app):

    @app.context_processor
    def inject():
        return {"user": current_user(), "today": date.today()}

    # ---- auth ----
    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            u = User.query.filter_by(email=request.form["email"].strip().lower()).first()
            if u and u.active and u.check_password(request.form["password"]):
                session["uid"] = u.id
                dest = request.args.get("next") or url_for("home")
                return redirect(dest)
            flash("Email or password not recognised.", "error")
        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/")
    def home():
        u = current_user()
        if not u:
            return redirect(url_for("login"))
        if u.role == "driver":
            return redirect(url_for("driver_home"))
        return redirect(url_for("dashboard"))

    # ---- dispatcher control board ----
    @app.route("/dashboard")
    @login_required("admin", "dispatcher")
    def dashboard():
        today = date.today()
        routes = Route.query.filter_by(run_date=today).all()
        unassigned = Order.query.filter_by(status="unassigned").all()
        counts = {
            "unassigned": Order.query.filter_by(status="unassigned").count(),
            "out": Order.query.filter_by(status="out").count(),
            "delivered": Order.query.filter(Order.status == "delivered",
                                             db.func.date(Order.created_at) == today).count(),
            "failed": Order.query.filter_by(status="failed").count(),
        }
        return render_template("dashboard.html", routes=routes,
                               unassigned=unassigned, counts=counts)

    # ---- orders ----
    @app.route("/orders")
    @login_required("admin", "dispatcher")
    def orders():
        status = request.args.get("status")
        q = Order.query.order_by(Order.created_at.desc())
        if status:
            q = q.filter_by(status=status)
        return render_template("orders.html", orders=q.all(), status=status)

    @app.route("/orders/new", methods=["GET", "POST"])
    @login_required("admin", "dispatcher")
    def order_new():
        customers = Customer.query.filter_by(active=True).order_by(Customer.name).all()
        if request.method == "POST":
            cust = db.session.get(Customer, int(request.form["customer_id"]))
            parcels = int(request.form.get("parcels") or 0)
            pallets = int(request.form.get("pallets") or 0)
            weight = float(request.form.get("weight_kg") or 0)
            job = request.form.get("job_type", "delivery")
            addr = request.form.get("address")
            pc = request.form.get("postcode", "").upper().strip()
            o = Order(
                ref=next_ref("HL", Order, "ref"),
                customer=cust,
                job_type=job,
                recipient=request.form.get("recipient"),
                phone=request.form.get("phone"),
                address=addr,
                postcode=pc,
                pallets=pallets,
                weight_kg=weight,
                service=request.form.get("service", "next-day"),
                notes=request.form.get("notes"),
            )
            set_legs(o, cust, job, addr, pc)
            apply_scheduling(o, request.form)
            o.pallet_length = float(request.form.get("pallet_length") or 0)
            o.pallet_width = float(request.form.get("pallet_width") or 0)
            o.pallet_height = float(request.form.get("pallet_height") or 0)
            o.weight_per_pallet = float(request.form.get("weight_per_pallet") or 0)
            if o.weight_per_pallet and not weight:
                o.weight_kg = o.weight_per_pallet * (pallets or 0)
            o.price = quote_order(cust, pc, pallets, o.weight_kg)
            db.session.add(o)
            db.session.flush()
            barcodes = [b.strip() for b in
                        (request.form.get("barcodes") or "").splitlines() if b.strip()]
            build_items(o, parcels, pallets, barcodes)
            db.session.commit()
            cutoff = same_day_cutoff_warning(o)
            if cutoff:
                flash(cutoff, "error")
            flash(f"{o.job_type.title()} {o.ref} created — "
                  f"{parcels} parcel(s), {pallets} pallet(s), £{o.price:.2f}.", "ok")
            return redirect(url_for("pending_board"))
        return render_template("order_new.html", customers=customers)

    @app.route("/api/quote")
    @login_required("admin", "dispatcher")
    def api_quote():
        cust = db.session.get(Customer, int(request.args.get("customer_id", 0)))
        price = quote_order(cust, request.args.get("postcode", ""),
                            int(request.args.get("pallets") or 1),
                            float(request.args.get("weight") or 0))
        return jsonify(price=price)

    # ---- route planning ----
    @app.route("/routes")
    @login_required("admin", "dispatcher")
    def routes_list():
        rs = Route.query.order_by(Route.run_date.desc(), Route.id.desc()).all()
        return render_template("routes.html", routes=rs)

    @app.route("/routes/plan", methods=["GET", "POST"])
    @login_required("admin", "dispatcher")
    def route_plan():
        drivers = Driver.query.filter_by(active=True).all()
        vehicles = Vehicle.query.filter_by(active=True).all()
        unassigned = Order.query.filter_by(status="unassigned").all()
        if request.method == "POST":
            ids = request.form.getlist("order_ids")
            if not ids:
                flash("Pick at least one order for the route.", "error")
                return redirect(url_for("route_plan"))
            r = Route(
                ref=next_ref("R", Route, "ref"),
                name=request.form.get("name") or f"Round {date.today():%d %b}",
                run_date=datetime.strptime(request.form["run_date"], "%Y-%m-%d").date()
                if request.form.get("run_date") else date.today(),
                start_postcode=request.form.get("start_postcode") or "PR2 2TE",
            )
            if request.form.get("driver_id"):
                r.driver_id = int(request.form["driver_id"])
            if request.form.get("vehicle_id"):
                r.vehicle_id = int(request.form["vehicle_id"])
            db.session.add(r)
            db.session.flush()
            for oid in ids:
                o = db.session.get(Order, int(oid))
                o.status = "assigned"
                db.session.add(Stop(route=r, order=o))
            db.session.flush()
            _sequence_route(r)
            db.session.commit()
            flash(f"Route {r.ref} planned with {len(ids)} stops.", "ok")
            return redirect(url_for("route_detail", rid=r.id))
        return render_template("route_plan.html", drivers=drivers,
                               vehicles=vehicles, unassigned=unassigned)

    @app.route("/routes/<int:rid>")
    @login_required("admin", "dispatcher")
    def route_detail(rid):
        r = db.session.get(Route, rid) or abort(404)
        drivers = Driver.query.filter_by(active=True).all()
        vehicles = Vehicle.query.filter_by(active=True).all()
        return render_template("route_detail.html", r=r, drivers=drivers,
                               vehicles=vehicles)

    @app.route("/routes/<int:rid>/optimise", methods=["POST"])
    @login_required("admin", "dispatcher")
    def route_optimise(rid):
        r = db.session.get(Route, rid) or abort(404)
        _sequence_route(r)
        db.session.commit()
        flash("Stops re-sequenced for shortest round.", "ok")
        return redirect(url_for("route_detail", rid=rid))

    @app.route("/routes/<int:rid>/assign", methods=["POST"])
    @login_required("admin", "dispatcher")
    def route_assign(rid):
        r = db.session.get(Route, rid) or abort(404)
        r.driver_id = int(request.form["driver_id"]) if request.form.get("driver_id") else None
        r.vehicle_id = int(request.form["vehicle_id"]) if request.form.get("vehicle_id") else None
        db.session.commit()
        flash("Driver and vehicle updated.", "ok")
        return redirect(url_for("route_detail", rid=rid))

    @app.route("/routes/<int:rid>/dispatch", methods=["POST"])
    @login_required("admin", "dispatcher")
    def route_dispatch(rid):
        r = db.session.get(Route, rid) or abort(404)
        if not r.driver_id:
            flash("Assign a driver before dispatching.", "error")
            return redirect(url_for("route_detail", rid=rid))
        r.status = "dispatched"
        sent = 0
        for s in r.stops:
            s.order.status = "out"
            if s.order.phone:
                if s.order.job_type == "collection":
                    line = (f"your Heliolink collection {s.order.ref} is "
                            f"scheduled today.")
                else:
                    line = (f"your Heliolink delivery {s.order.ref} is out "
                            f"for delivery today.")
                send_sms(s.order.phone,
                         f"Hi {s.order.recipient or 'there'}, {line} "
                         f"Track: {request.host_url}track/{s.order.ref}",
                         order_ref=s.order.ref)
                sent += 1
        db.session.commit()
        flash(f"Route dispatched. {sent} customer text(s) sent.", "ok")
        return redirect(url_for("route_detail", rid=rid))

    # ---- driver PWA ----
    @app.route("/driver")
    @login_required("driver", "admin")
    def driver_home():
        u = current_user()
        drv = u.driver_profile
        rs = []
        allocated = []
        if drv:
            rs = Route.query.filter(Route.driver_id == drv.id,
                                    Route.run_date >= date.today())\
                .order_by(Route.run_date).all()
            allocated = [o for o in Order.query.filter_by(assigned_driver_id=drv.id).all()
                         if o.status not in ("delivered", "collected", "failed")]
        return render_template("driver_home.html", routes=rs, allocated=allocated)

    @app.route("/driver/route/<int:rid>")
    @login_required("driver", "admin")
    def driver_route(rid):
        r = db.session.get(Route, rid) or abort(404)
        return render_template("driver_route.html", r=r)

    @app.route("/driver/stop/<int:sid>", methods=["GET", "POST"])
    @login_required("driver", "admin")
    def driver_stop(sid):
        s = db.session.get(Stop, sid) or abort(404)
        mode = "collect" if s.order.job_type == "collection" else "deliver"
        items = s.order.items
        total = len(items)
        scanned = s.order.scanned_count
        if request.method == "POST":
            outcome = request.form.get("outcome", "delivered")
            override = request.form.get("override_missing") == "on"

            # Scan gate: for a successful delivery/collection every box must be
            # scanned, unless the driver explicitly confirms items are missing.
            if outcome in ("delivered", "collected") and total and scanned < total \
                    and not override:
                miss = total - scanned
                flash(f"{miss} of {total} box(es) not scanned. Scan the rest, "
                      f"or tick 'complete with missing items' to close short.",
                      "error")
                return render_template("driver_stop.html", s=s, mode=mode,
                                       total=total, scanned=scanned, warn=True)

            pod = s.pod or POD(stop=s)
            pod.signed_by = request.form.get("signed_by")
            pod.notes = request.form.get("notes")
            pod.gps = request.form.get("gps")
            pod.outcome = outcome

            photo = request.files.get("photo")
            if photo and photo.filename:
                pod.photo = photo.read()
                pod.photo_mime = photo.mimetype or "image/jpeg"

            sig = request.form.get("signature_data")
            if sig and sig.startswith("data:image"):
                pod.signature = base64.b64decode(sig.split(",", 1)[1])

            db.session.add(pod)
            # any unscanned boxes on a short close are flagged missing
            if outcome in ("delivered", "collected") and override:
                for it in items:
                    if it.status not in ("delivered", "collected"):
                        it.status = "missing"
            s.status = outcome
            s.completed_at = datetime.utcnow()
            s.order.status = outcome
            db.session.flush()

            # customer confirmation text
            if s.order.phone:
                job = s.order.job_type
                if outcome in ("delivered", "collected"):
                    verb = "collected" if job == "collection" else "delivered"
                    msg = (f"Your Heliolink {job} {s.order.ref} has been "
                           f"{verb}. Thank you.")
                else:
                    msg = (f"We attempted your Heliolink {job} {s.order.ref} "
                           f"but could not complete it. We'll be in touch to "
                           f"rearrange.")
                send_sms(s.order.phone, msg, order_ref=s.order.ref)

            if all(st.status in ("delivered", "failed", "collected")
                   for st in s.route.stops):
                s.route.status = "completed"
            db.session.commit()

            # auto-advance to the next pending stop on the optimised route
            nxt = next((st for st in s.route.stops
                        if st.sequence > s.sequence
                        and st.status not in ("delivered", "failed", "collected")),
                       None)
            if nxt:
                flash(f"{s.order.ref} done. Next stop {nxt.sequence}: "
                      f"{nxt.order.recipient or nxt.order.customer.name}.", "ok")
                return redirect(url_for("driver_stop", sid=nxt.id))
            flash("That was the last stop — route complete.", "ok")
            return redirect(url_for("driver_route", rid=s.route_id))
        return render_template("driver_stop.html", s=s, mode=mode,
                               total=total, scanned=scanned)

    @app.route("/driver/stop/<int:sid>/arrive", methods=["POST"])
    @login_required("driver", "admin")
    def driver_arrive(sid):
        s = db.session.get(Stop, sid) or abort(404)
        s.status = "arrived"
        db.session.commit()
        return redirect(url_for("driver_stop", sid=sid))

    # ---- barcode scanning (delivery, collection, and van load) ----
    @app.route("/driver/stop/<int:sid>/scan")
    @login_required("driver", "admin")
    def driver_scan(sid):
        s = db.session.get(Stop, sid) or abort(404)
        mode = "collect" if s.order.job_type == "collection" else "deliver"
        return render_template("driver_scan.html", s=s, mode=mode,
                               items=s.order.items)

    @app.route("/driver/route/<int:rid>/load")
    @login_required("driver", "admin")
    def driver_load(rid):
        r = db.session.get(Route, rid) or abort(404)
        items = [i for st in r.stops for i in st.order.items]
        return render_template("driver_load.html", r=r, items=items)

    @app.route("/api/scan", methods=["POST"])
    @login_required("driver", "admin")
    def api_scan():
        data = request.get_json(silent=True) or {}
        barcode = (data.get("barcode") or "").strip()
        scan_type = data.get("scan_type") or "deliver"
        gps = data.get("gps")
        if not barcode:
            return jsonify(result="empty"), 400

        # scope of the scan: a single stop (deliver/collect) or a whole route (load)
        stop = db.session.get(Stop, int(data["stop_id"])) if data.get("stop_id") else None
        route = db.session.get(Route, int(data["route_id"])) if data.get("route_id") else None
        if stop and not route:
            route = stop.route

        if stop:
            pool = list(stop.order.items)
        elif route:
            pool = [i for st in route.stops for i in st.order.items]
        else:
            return jsonify(result="no-scope"), 400

        item = next((i for i in pool if i.barcode == barcode), None)
        result = "matched"
        target = "loaded" if scan_type == "load" else \
                 ("collected" if scan_type == "collect" else "delivered")

        if item:
            if item.status in ("delivered", "collected") and scan_type != "load":
                result = "duplicate"
            else:
                item.status = target
                item.scanned_at = datetime.utcnow()
        else:
            result = "unknown"
            # on a collection, an unrecognised barcode is a new item being picked up
            if stop and scan_type == "collect":
                item = Item(order=stop.order, barcode=barcode, kind="parcel",
                            status="collected", scanned_at=datetime.utcnow())
                db.session.add(item)
                db.session.flush()
                result = "added"

        drv = current_user().driver_profile
        db.session.add(Scan(barcode=barcode, item_id=item.id if item else None,
                            stop_id=stop.id if stop else None,
                            route_id=route.id if route else None,
                            driver_id=drv.id if drv else None,
                            scan_type=scan_type, result=result, gps=gps))
        db.session.commit()

        done = sum(1 for i in pool if i.status in (target, "delivered", "collected"))
        return jsonify(result=result, barcode=barcode, done=done,
                       total=len(pool), kind=(item.kind if item else None))

    # ---- POD image serving ----
    @app.route("/pod/<int:pid>/photo")
    @login_required("admin", "dispatcher", "driver")
    def pod_photo(pid):
        p = db.session.get(POD, pid) or abort(404)
        if not p.photo:
            abort(404)
        return send_file(io.BytesIO(p.photo),
                         mimetype=p.photo_mime or "image/jpeg")

    @app.route("/pod/<int:pid>/signature")
    @login_required("admin", "dispatcher", "driver")
    def pod_signature(pid):
        p = db.session.get(POD, pid) or abort(404)
        if not p.signature:
            abort(404)
        return send_file(io.BytesIO(p.signature), mimetype="image/png")

    # ---- customer self-order portal (no login, access code) ----
    @app.route("/portal", methods=["GET", "POST"])
    def portal():
        if request.method == "POST":
            c = Customer.query.filter_by(
                portal_code=request.form["code"].strip()).first()
            if c:
                session["portal_customer"] = c.id
                return redirect(url_for("portal_home"))
            flash("Access code not recognised.", "error")
        return render_template("portal_login.html")

    @app.route("/portal/home", methods=["GET", "POST"])
    def portal_home():
        cid = session.get("portal_customer")
        c = db.session.get(Customer, cid) if cid else None
        if not c:
            return redirect(url_for("portal"))
        if request.method == "POST":
            parcels = int(request.form.get("parcels") or 0)
            pallets = int(request.form.get("pallets") or 0)
            weight = float(request.form.get("weight_kg") or 0)
            job = request.form.get("job_type", "delivery")
            addr = request.form.get("address")
            pc = request.form.get("postcode", "").upper().strip()
            o = Order(
                ref=next_ref("HL", Order, "ref"),
                customer=c,
                job_type=job,
                recipient=request.form.get("recipient"),
                phone=request.form.get("phone"),
                address=addr,
                postcode=pc,
                pallets=pallets,
                weight_kg=weight,
                service=request.form.get("service", "next-day"),
                notes=request.form.get("notes"),
            )
            set_legs(o, c, job, addr, pc)
            apply_scheduling(o, request.form)
            o.pallet_length = float(request.form.get("pallet_length") or 0)
            o.pallet_width = float(request.form.get("pallet_width") or 0)
            o.pallet_height = float(request.form.get("pallet_height") or 0)
            o.weight_per_pallet = float(request.form.get("weight_per_pallet") or 0)
            if o.weight_per_pallet and not weight:
                o.weight_kg = o.weight_per_pallet * (pallets or 0)
            o.price = quote_order(c, pc, pallets, o.weight_kg)
            db.session.add(o)
            db.session.flush()
            build_items(o, parcels, pallets)
            db.session.commit()
            cutoff = same_day_cutoff_warning(o)
            if cutoff:
                flash(cutoff, "error")
            flash(f"{o.job_type.title()} {o.ref} placed. "
                  f"Estimated charge £{o.price:.2f}.", "ok")
            return redirect(url_for("portal_track"))
        orders = Order.query.filter_by(customer_id=c.id)\
            .order_by(Order.created_at.desc()).limit(50).all()
        return render_template("portal_home.html", c=c, orders=orders)

    @app.route("/portal/logout")
    def portal_logout():
        session.pop("portal_customer", None)
        return redirect(url_for("portal"))

    def _portal_customer():
        cid = session.get("portal_customer")
        return db.session.get(Customer, cid) if cid else None

    @app.route("/portal/track")
    def portal_track():
        c = _portal_customer()
        if not c:
            return redirect(url_for("portal"))
        active = [o for o in c.orders if o.status in ("out", "assigned")]
        recent = sorted([o for o in c.orders], key=lambda o: o.created_at, reverse=True)[:40]
        return render_template("portal_track.html", c=c, active=active, recent=recent)

    @app.route("/portal/invoices")
    def portal_invoices():
        c = _portal_customer()
        if not c:
            return redirect(url_for("portal"))
        inv = sorted(c.invoices, key=lambda i: i.created_at, reverse=True)
        return render_template("portal_invoices.html", c=c, invoices=inv)

    @app.route("/portal/invoice/<int:iid>")
    def portal_invoice(iid):
        c = _portal_customer()
        if not c:
            return redirect(url_for("portal"))
        inv = db.session.get(Invoice, iid) or abort(404)
        if inv.customer_id != c.id:
            abort(403)
        return render_template("invoice_detail.html", inv=inv, host=request.host_url,
                               portal=True)

    @app.route("/portal/label/<int:oid>")
    def portal_label(oid):
        c = _portal_customer()
        if not c:
            return redirect(url_for("portal"))
        o = db.session.get(Order, oid) or abort(404)
        if o.customer_id != c.id:
            abort(403)
        return render_template("label.html", o=o)

    # ---- public tracking ----
    @app.route("/track/<ref>")
    def track(ref):
        o = Order.query.filter_by(ref=ref).first() or abort(404)
        return render_template("track.html", o=o)

    # ---- customer & rate management ----
    @app.route("/customers")
    @login_required("admin", "dispatcher")
    def customers():
        cs = Customer.query.order_by(Customer.name).all()
        return render_template("customers.html", customers=cs)

    @app.route("/customers/new", methods=["GET", "POST"])
    @app.route("/customers/<int:cid>/edit", methods=["GET", "POST"])
    @login_required("admin", "dispatcher")
    def customer_edit(cid=None):
        c = db.session.get(Customer, cid) if cid else None
        if request.method == "POST":
            if not c:
                c = Customer(portal_code=secrets.token_hex(4).upper())
                db.session.add(c)
            c.name = request.form["name"]
            c.contact_name = request.form.get("contact_name")
            c.email = request.form.get("email")
            c.phone = request.form.get("phone")
            c.address = request.form.get("address")
            c.postcode = request.form.get("postcode", "").upper().strip()
            c.rate_base = float(request.form.get("rate_base") or 0)
            c.rate_per_mile = float(request.form.get("rate_per_mile") or 0)
            c.rate_per_pallet = float(request.form.get("rate_per_pallet") or 0)
            c.rate_per_kg = float(request.form.get("rate_per_kg") or 0)
            c.min_charge = float(request.form.get("min_charge") or 0)
            c.fuel_surcharge_pct = float(request.form.get("fuel_surcharge_pct") or 0)
            c.pricing_mode = request.form.get("pricing_mode", "flat")
            c.discount_pct = float(request.form.get("discount_pct") or 0)
            db.session.commit()
            flash("Customer saved.", "ok")
            return redirect(url_for("customers"))
        return render_template("customer_edit.html", c=c)

    @app.route("/customers/rate-template")
    @login_required("admin", "dispatcher")
    def rate_template():
        import csv
        from io import StringIO
        buf = StringIO()
        w = csv.writer(buf)
        w.writerow(["name", "contact_name", "email", "phone", "address",
                    "postcode", "rate_base", "rate_per_mile", "rate_per_pallet",
                    "rate_per_kg", "min_charge", "fuel_surcharge_pct"])
        w.writerow(["Example Ltd", "Jane", "jane@example.com", "07700900000",
                    "1 Some Road", "M1 1AA", "8.00", "0.55", "6.00", "0.05",
                    "25.00", "15"])
        return app.response_class(buf.getvalue(), mimetype="text/csv", headers={
            "Content-Disposition": "attachment; filename=voya_rate_card_template.csv"})

    @app.route("/customers/rate-import", methods=["POST"])
    @login_required("admin", "dispatcher")
    def rate_import():
        import csv
        from io import StringIO
        f = request.files.get("file")
        if not f:
            flash("No file chosen.", "error")
            return redirect(url_for("customers"))
        rows = list(csv.DictReader(StringIO(f.read().decode("utf-8-sig"))))
        added = updated = 0
        for r in rows:
            name = (r.get("name") or "").strip()
            if not name:
                continue
            c = Customer.query.filter_by(name=name).first()
            if not c:
                c = Customer(name=name, portal_code=secrets.token_hex(4).upper())
                db.session.add(c)
                added += 1
            else:
                updated += 1
            for fld in ("contact_name", "email", "phone", "address", "postcode"):
                if r.get(fld):
                    setattr(c, fld, r[fld].strip())
            for fld in ("rate_base", "rate_per_mile", "rate_per_pallet",
                        "rate_per_kg", "min_charge", "fuel_surcharge_pct"):
                if r.get(fld):
                    try:
                        setattr(c, fld, float(r[fld]))
                    except ValueError:
                        pass
        db.session.commit()
        flash(f"Rate cards imported — {added} added, {updated} updated.", "ok")
        return redirect(url_for("customers"))

    # ---- standard rate card (parameters) ----
    @app.route("/rates/settings", methods=["GET", "POST"])
    @login_required("admin", "dispatcher")
    def rate_settings():
        s = get_settings()
        if request.method == "POST":
            for f in ("fuel_price_per_litre", "driver_rate_per_hour", "avg_speed_mph",
                      "handling_min_per_pallet", "fixed_cost_per_job", "margin_pct",
                      "service_uplift_24h_pct", "mpg_panel", "mpg_25t", "mpg_75t"):
                if request.form.get(f) not in (None, ""):
                    setattr(s, f, float(request.form[f]))
            s.round_trip = request.form.get("round_trip") == "on"
            db.session.commit()
            flash("Standard rate parameters saved.", "ok")
            return redirect(url_for("rate_settings"))
        # sample recommendation so the effect is visible
        sample = recommend_rate("M1 1AA", 2, 300, "48", settings=s)
        return render_template("rate_settings.html", s=s, sample=sample)

    @app.route("/rates/regions", methods=["GET", "POST"])
    @login_required("admin", "dispatcher")
    def rate_regions():
        if request.method == "POST":
            for z in Zone.query.all():
                v = request.form.get(f"mult_{z.id}")
                if v not in (None, ""):
                    z.multiplier = float(v)
            db.session.commit()
            flash("Region multipliers saved.", "ok")
            return redirect(url_for("rate_regions"))
        # areas grouped by region for reference
        from optimise import REGION_OF
        by_region = {}
        for area, reg in REGION_OF.items():
            by_region.setdefault(reg, []).append(area)
        zones = Zone.query.order_by(Zone.region).all()
        return render_template("rate_regions.html", zones=zones, by_region=by_region)

    @app.route("/rates/calculator")
    @login_required("admin", "dispatcher")
    def rate_calculator():
        return render_template("rate_calculator.html",
                               customers=Customer.query.order_by(Customer.name).all())

    @app.route("/api/recommend")
    @login_required("admin", "dispatcher")
    def api_recommend():
        pc = request.args.get("postcode", "")
        pallets = int(request.args.get("pallets") or 1)
        wpp = float(request.args.get("weight_per_pallet") or 0)
        service = request.args.get("service", "48")
        vtype = request.args.get("vtype") or None
        discount = float(request.args.get("discount") or 0)
        rec = recommend_rate(pc, pallets, wpp, service, vtype)
        rec["discount_pct"] = discount
        rec["net"] = round(rec["recommended"] * (1 - discount / 100.0), 2)
        return jsonify(rec)

    # ---- fleet ----
    @app.route("/fleet")
    @login_required("admin", "dispatcher")
    def fleet():
        return render_template("fleet.html",
                               drivers=Driver.query.all(),
                               vehicles=Vehicle.query.all())

    @app.route("/fleet/vehicle", methods=["POST"])
    @login_required("admin", "dispatcher")
    def add_vehicle():
        v = Vehicle(reg=request.form["reg"].upper().strip(),
                    make_model=request.form.get("make_model"),
                    vtype=request.form.get("vtype", "panel van"),
                    capacity_pallets=int(request.form.get("capacity_pallets") or 0),
                    mpg=float(request.form.get("mpg") or 0))
        db.session.add(v)
        db.session.commit()
        flash("Vehicle added.", "ok")
        return redirect(url_for("fleet"))

    @app.route("/fleet/driver", methods=["POST"])
    @login_required("admin", "dispatcher")
    def add_driver():
        email = request.form["email"].strip().lower()
        if User.query.filter_by(email=email).first():
            flash("A user with that email already exists.", "error")
            return redirect(url_for("fleet"))
        u = User(name=request.form["name"], email=email, role="driver",
                 phone=request.form.get("phone"))
        u.set_password(request.form.get("password") or "driver123")
        db.session.add(u)
        db.session.flush()
        db.session.add(Driver(user=u, phone=u.phone,
                              licence_no=request.form.get("licence_no")))
        db.session.commit()
        flash("Driver added.", "ok")
        return redirect(url_for("fleet"))

    # ---- reporting ----
    @app.route("/reports")
    @login_required("admin", "dispatcher")
    def reports():
        from datetime import timedelta
        period = request.args.get("period", "7")   # 1 | 7 | 30 | all
        since = None
        if period != "all":
            since = datetime.utcnow() - timedelta(days=int(period))

        def in_window(q, col):
            return q.filter(col >= since) if since else q

        # completed stops in the window
        stops_q = in_window(Stop.query.filter(
            Stop.status.in_(["delivered", "failed", "collected"])), Stop.completed_at)
        stops = stops_q.all()

        drops = sum(1 for s in stops if s.order.job_type == "delivery")
        collections = sum(1 for s in stops if s.order.job_type == "collection")
        success_stops = sum(1 for s in stops if s.status in ("delivered", "collected"))
        failed_stops = sum(1 for s in stops if s.status == "failed")
        completed = len(stops)
        success = round(100 * success_stops / completed, 1) if completed else 0

        # parcels / pallets moved, split by delivery vs collection
        items_q = in_window(
            Item.query.filter(Item.status.in_(["delivered", "collected"])),
            Item.scanned_at)
        items = items_q.all()
        parcels_del = sum(1 for i in items if i.kind == "parcel" and i.status == "delivered")
        pallets_del = sum(1 for i in items if i.kind == "pallet" and i.status == "delivered")
        parcels_col = sum(1 for i in items if i.kind == "parcel" and i.status == "collected")
        pallets_col = sum(1 for i in items if i.kind == "pallet" and i.status == "collected")

        # delivered-in-full: delivery stops where every expected item was scanned
        del_stops = [s for s in stops if s.order.job_type == "delivery"
                     and s.status == "delivered"]
        in_full = sum(1 for s in del_stops
                      if s.order.items and s.order.scanned_count >= len(s.order.items))
        difot = round(100 * in_full / len(del_stops), 1) if del_stops else 0

        routes_run = in_window(Route.query.filter(
            Route.status.in_(["dispatched", "completed"])), Route.created_at).count()

        # per-driver
        driver_rows = []
        for drv in Driver.query.all():
            ds = [s for s in stops if s.route.driver_id == drv.id]
            d_ok = sum(1 for s in ds if s.status in ("delivered", "collected"))
            d_fail = sum(1 for s in ds if s.status == "failed")
            driver_rows.append({"name": drv.user.name if drv.user else "—",
                                "done": d_ok, "failed": d_fail,
                                "rate": round(100 * d_ok / (d_ok + d_fail))
                                if (d_ok + d_fail) else 0})

        revenue = sum(s.order.price for s in stops
                      if s.status in ("delivered", "collected"))

        k = dict(period=period, drops=drops, collections=collections,
                 completed=completed, success=success, failed=failed_stops,
                 difot=difot, routes_run=routes_run,
                 parcels_del=parcels_del, pallets_del=pallets_del,
                 parcels_col=parcels_col, pallets_col=pallets_col,
                 parcels_total=parcels_del + parcels_col,
                 pallets_total=pallets_del + pallets_col,
                 revenue=round(revenue, 2))
        return render_template("reports.html", k=k, driver_rows=driver_rows)

    @app.route("/reports/export")
    @login_required("admin", "dispatcher")
    def reports_export():
        import csv
        from io import StringIO
        buf = StringIO()
        w = csv.writer(buf)
        w.writerow(["Order", "Type", "Customer", "Postcode", "Status",
                    "Parcels", "Pallets", "Scanned", "Price", "Completed"])
        for s in Stop.query.filter(
                Stop.status.in_(["delivered", "failed", "collected"])).all():
            o = s.order
            w.writerow([o.ref, o.job_type, o.customer.name, o.postcode, s.status,
                        o.parcels, o.pallet_count, o.scanned_count, o.price,
                        s.completed_at.strftime("%Y-%m-%d %H:%M") if s.completed_at else ""])
        out = buf.getvalue()
        return app.response_class(out, mimetype="text/csv", headers={
            "Content-Disposition": "attachment; filename=helioops_kpi_export.csv"})

    @app.route("/admin/backup")
    @login_required("admin")
    def admin_backup():
        """Download a full JSON snapshot of the operational data."""
        import json
        snapshot = {"generated": datetime.utcnow().isoformat()}
        for name, model in [("customers", Customer), ("drivers", Driver),
                            ("vehicles", Vehicle), ("orders", Order),
                            ("items", Item), ("routes", Route), ("stops", Stop),
                            ("scans", Scan), ("sms_log", SmsLog)]:
            rows = []
            for r in model.query.all():
                d = {c.name: getattr(r, c.name) for c in r.__table__.columns
                     if c.name not in ("photo", "signature")}
                for kk, vv in d.items():
                    if isinstance(vv, (datetime, date)):
                        d[kk] = vv.isoformat()
                rows.append(d)
            snapshot[name] = rows
        payload = json.dumps(snapshot, indent=2, default=str)
        stamp = datetime.utcnow().strftime("%Y%m%d-%H%M")
        return app.response_class(payload, mimetype="application/json", headers={
            "Content-Disposition": f"attachment; filename=helioops_backup_{stamp}.json"})

    @app.route("/messages")
    @login_required("admin", "dispatcher")
    def messages():
        logs = SmsLog.query.order_by(SmsLog.created_at.desc()).limit(200).all()
        return render_template("messages.html", logs=logs)

    # ---- pending work control board ----
    @app.route("/pending")
    @login_required("admin", "dispatcher")
    def pending_board():
        view = request.args.get("view", "status")
        active = [o for o in Order.query
                  .order_by(Order.collection_date, Order.created_at).all()
                  if o.status not in ("delivered", "collected", "failed")]
        collections = [o for o in active if o.job_type == "collection"]
        deliveries = [o for o in active if o.job_type == "delivery"]

        def totals(lst):
            return {"jobs": len(lst),
                    "pallets": sum(o.pallet_count for o in lst),
                    "parcels": sum(o.parcels for o in lst)}

        drivers = Driver.query.filter_by(active=True).all()
        vehicles = Vehicle.query.filter_by(active=True).all()
        slamap = {o.id: sla_for(o) for o in active}

        groups = None
        if view == "resource":
            groups = [("Unallocated", [o for o in active if not o.assigned_driver_id])]
            for d in drivers:
                groups.append((d.user.name if d.user else "Driver",
                               [o for o in active if o.assigned_driver_id == d.id]))

        tmpl = "pending_fragment.html" if request.args.get("fragment") else "pending_board.html"
        return render_template(tmpl, view=view, collections=collections,
                               deliveries=deliveries, col_tot=totals(collections),
                               del_tot=totals(deliveries), drivers=drivers,
                               vehicles=vehicles, sla=slamap, groups=groups)

    @app.route("/pending/<int:oid>/allocate", methods=["POST"])
    @login_required("admin", "dispatcher")
    def pending_allocate(oid):
        o = db.session.get(Order, oid) or abort(404)
        did = request.form.get("driver_id")
        vid = request.form.get("vehicle_id")
        o.assigned_driver_id = int(did) if did else None
        o.assigned_vehicle_id = int(vid) if vid else None
        if o.assigned_driver_id or o.assigned_vehicle_id:
            if o.status == "unassigned":
                o.status = "assigned"
        elif o.status == "assigned":
            o.status = "unassigned"
        warn = None
        if o.assigned_vehicle and o.pallet_count > (o.assigned_vehicle.capacity_pallets or 0):
            warn = (f"{o.assigned_vehicle.reg} holds "
                    f"{o.assigned_vehicle.capacity_pallets} pallets but {o.ref} "
                    f"has {o.pallet_count}.")
        db.session.commit()
        flash(warn, "error") if warn else flash(f"{o.ref} allocated.", "ok")
        return redirect(request.referrer or url_for("pending_board"))

    @app.route("/pending/<int:oid>/suggest", methods=["POST"])
    @login_required("admin", "dispatcher")
    def pending_suggest(oid):
        o = db.session.get(Order, oid) or abort(404)
        van = (Vehicle.query
               .filter(Vehicle.active == True, Vehicle.status == "available",
                       Vehicle.capacity_pallets >= max(o.pallet_count, 1))
               .order_by(Vehicle.capacity_pallets.asc()).first())
        drv = Driver.query.filter_by(active=True, status="available").first()
        if van:
            o.assigned_vehicle_id = van.id
        if drv:
            o.assigned_driver_id = drv.id
        if (van or drv) and o.status == "unassigned":
            o.status = "assigned"
        db.session.commit()
        if van or drv:
            picks = ", ".join(filter(None, [van.reg if van else None,
                              (drv.user.name if drv and drv.user else None)]))
            flash(f"Suggested for {o.ref}: {picks}.", "ok")
        else:
            flash("No available van/driver to suggest.", "error")
        return redirect(request.referrer or url_for("pending_board"))

    @app.route("/orders/<int:oid>")
    @login_required("admin", "dispatcher")
    def order_detail(oid):
        o = db.session.get(Order, oid) or abort(404)
        return render_template("order_detail.html", o=o,
                               drivers=Driver.query.filter_by(active=True).all(),
                               vehicles=Vehicle.query.filter_by(active=True).all(),
                               all_surcharges=Surcharge.query.filter_by(active=True).all(),
                               sla=sla_for(o))

    # ---- resource table ----
    @app.route("/resources")
    @login_required("admin", "dispatcher")
    def resources():
        drivers = Driver.query.all()
        alloc = {}
        for d in drivers:
            alloc[d.id] = [o for o in Order.query.filter_by(assigned_driver_id=d.id).all()
                           if o.status not in ("delivered", "collected", "failed")]
        return render_template("resources.html", drivers=drivers,
                               vehicles=Vehicle.query.all(), alloc=alloc)

    @app.route("/resources/vehicle/<int:vid>/status", methods=["POST"])
    @login_required("admin", "dispatcher")
    def vehicle_status(vid):
        v = db.session.get(Vehicle, vid) or abort(404)
        v.status = request.form.get("status", "available")
        db.session.commit()
        return redirect(url_for("resources"))

    @app.route("/resources/driver/<int:did>/status", methods=["POST"])
    @login_required("admin", "dispatcher")
    def driver_status(did):
        d = db.session.get(Driver, did) or abort(404)
        d.status = request.form.get("status", "available")
        db.session.commit()
        return redirect(url_for("resources"))

    # ---- surcharge catalogue ----
    @app.route("/surcharges", methods=["GET", "POST"])
    @login_required("admin", "dispatcher")
    def surcharges():
        if request.method == "POST":
            sc = Surcharge(
                code=request.form["code"].upper().strip(),
                name=request.form["name"],
                kind=request.form.get("kind", "flat"),
                amount=float(request.form.get("amount") or 0),
            )
            db.session.add(sc)
            db.session.commit()
            flash("Surcharge added.", "ok")
            return redirect(url_for("surcharges"))
        return render_template("surcharges.html",
                               surcharges=Surcharge.query.order_by(Surcharge.name).all())

    @app.route("/surcharges/<int:sid>/update", methods=["POST"])
    @login_required("admin", "dispatcher")
    def surcharge_update(sid):
        sc = db.session.get(Surcharge, sid) or abort(404)
        sc.amount = float(request.form.get("amount") or 0)
        sc.active = request.form.get("active") == "on"
        db.session.commit()
        flash("Surcharge updated.", "ok")
        return redirect(url_for("surcharges"))

    @app.route("/orders/<int:oid>/surcharges", methods=["POST"])
    @login_required("admin", "dispatcher")
    def order_apply_surcharges(oid):
        o = db.session.get(Order, oid) or abort(404)
        ids = [int(x) for x in request.form.getlist("surcharge_ids")]
        o.surcharges = Surcharge.query.filter(Surcharge.id.in_(ids)).all() if ids else []
        db.session.commit()
        flash("Surcharges updated for the shipment.", "ok")
        return redirect(request.referrer or url_for("orders"))

    # ---- invoicing ----
    @app.route("/invoices")
    @login_required("admin", "dispatcher")
    def invoices():
        inv = Invoice.query.order_by(Invoice.created_at.desc()).all()
        return render_template("invoices.html", invoices=inv)

    @app.route("/invoices/run", methods=["GET", "POST"])
    @login_required("admin", "dispatcher")
    def invoice_run():
        from datetime import timedelta
        if request.method == "POST":
            # default period = the most recent complete week (Mon-Sun)
            if request.form.get("period_start"):
                start = datetime.strptime(request.form["period_start"], "%Y-%m-%d").date()
            else:
                today = date.today()
                start = today - timedelta(days=today.weekday() + 7)
            end = start + timedelta(days=6)
            made = 0
            cust_ids = request.form.getlist("customer_ids")
            custs = (Customer.query.filter(Customer.id.in_([int(c) for c in cust_ids]))
                     if cust_ids else Customer.query).all()
            for c in custs:
                inv = generate_invoice(c, start, end)
                if inv:
                    made += 1
            db.session.commit()
            flash(f"Invoice run complete for {start:%d %b}–{end:%d %b} — "
                  f"{made} invoice(s) created.", "ok")
            return redirect(url_for("invoices"))
        return render_template("invoice_run.html",
                               customers=Customer.query.order_by(Customer.name).all())

    @app.route("/invoices/<int:iid>")
    @login_required("admin", "dispatcher")
    def invoice_detail(iid):
        inv = db.session.get(Invoice, iid) or abort(404)
        return render_template("invoice_detail.html", inv=inv, host=request.host_url)

    @app.route("/invoices/<int:iid>/status", methods=["POST"])
    @login_required("admin", "dispatcher")
    def invoice_status(iid):
        inv = db.session.get(Invoice, iid) or abort(404)
        inv.status = request.form.get("status", inv.status)
        db.session.commit()
        flash(f"Invoice marked {inv.status}.", "ok")
        return redirect(url_for("invoice_detail", iid=iid))

    # ---- barcode labels (4x4) ----
    @app.route("/orders/<int:oid>/label")
    @login_required("admin", "dispatcher", "driver")
    def order_label(oid):
        o = db.session.get(Order, oid) or abort(404)
        return render_template("label.html", o=o)

    # ---- standalone scanner module ----
    @app.route("/scan")
    @login_required("admin", "dispatcher", "driver")
    def scan_module():
        return render_template("scan_module.html")

    @app.route("/api/lookup")
    @login_required("admin", "dispatcher", "driver")
    def api_lookup():
        code = (request.args.get("barcode") or "").strip()
        item = Item.query.filter_by(barcode=code).first()
        o = item.order if item else Order.query.filter_by(ref=code).first()
        if not o:
            return jsonify(found=False)
        return jsonify(found=True, ref=o.ref, customer=o.customer.name,
                       job=o.job_type, status=o.status,
                       delivery=o.delivery_address, delivery_pc=o.delivery_postcode,
                       pallets=o.pallet_count, parcels=o.parcels,
                       weight=o.weight_kg, kind=(item.kind if item else None))

    # ---- public POD image by shipment ref (for invoice hyperlinks) ----
    @app.route("/pod/ref/<ref>")
    def pod_by_ref(ref):
        o = Order.query.filter_by(ref=ref).first() or abort(404)
        if o.pod and o.pod.photo:
            return send_file(io.BytesIO(o.pod.photo),
                             mimetype=o.pod.photo_mime or "image/jpeg")
        abort(404)

    # ---- PWA plumbing ----
    @app.route("/healthz")
    def healthz():
        try:
            db.session.execute(db.text("SELECT 1"))
            return {"status": "ok"}, 200
        except Exception:
            return {"status": "degraded"}, 503

    @app.route("/static/<path:filename>", endpoint="static")
    def static_files(filename):
        p = find_asset(filename)
        if not p:
            abort(404)
        return send_file(p)

    @app.route("/manifest.webmanifest")
    def manifest():
        p = find_asset("manifest.json")
        return send_file(p) if p else ("", 404)

    @app.route("/sw.js")
    def service_worker():
        p = find_asset("sw.js")
        if not p:
            return ("", 404)
        resp = app.make_response(send_file(p))
        resp.headers["Content-Type"] = "application/javascript"
        return resp

    @app.errorhandler(403)
    def forbidden(e):
        return render_template("error.html", code=403,
                               msg="You don't have access to that page."), 403

    @app.errorhandler(404)
    def notfound(e):
        return render_template("error.html", code=404,
                               msg="That page or record wasn't found."), 404


def _sequence_route(r):
    ordered, miles = optimise_sequence(r.stops, r.start_postcode)
    for i, s in enumerate(ordered, start=1):
        s.sequence = i
    return miles


# --------------------------------------------------------------------------
#  Seed data (first run only)
# --------------------------------------------------------------------------
def seed_if_empty():
    # Always ensure an admin login exists. In production set ADMIN_EMAIL and
    # ADMIN_PASSWORD as environment variables so you never go live on defaults.
    admin_email = os.environ.get("ADMIN_EMAIL", "admin@heliolink.co").strip().lower()
    if not User.query.filter_by(email=admin_email).first():
        admin = User(name=os.environ.get("ADMIN_NAME", "Administrator"),
                     email=admin_email, role="admin")
        admin.set_password(os.environ.get("ADMIN_PASSWORD", "Heliolink26"))
        db.session.add(admin)
        db.session.commit()

    # Standard rate settings + region zones — config, seeded once (always).
    if not db.session.get(RateSettings, 1):
        db.session.add(RateSettings(id=1))
    if not Zone.query.first():
        for reg in REGIONS:
            db.session.add(Zone(region=reg, multiplier=1.0))
    db.session.commit()

    # Standard industry surcharge catalogue — seeded once, config not demo data.
    if not Surcharge.query.first():
        catalogue = [
            ("FUEL", "Fuel surcharge (variable)", "percent", 15.0),
            ("TAIL", "Tail-lift delivery", "flat", 12.50),
            ("TIMED", "Timed / booked delivery", "flat", 15.00),
            ("REMOTE", "Remote / offshore area", "per_pallet", 8.00),
            ("WAIT", "Waiting time (per 15 min)", "flat", 7.50),
            ("REDEL", "Re-delivery / failed attempt", "flat", 18.00),
            ("HAZ", "Hazardous / ADR goods", "per_pallet", 10.00),
            ("OOG", "Out-of-gauge / oversize", "per_pallet", 14.00),
            ("MANUAL", "Manual handling / no equipment", "flat", 9.00),
            ("SAT", "Saturday delivery", "flat", 20.00),
        ]
        for code, name, kind, amt in catalogue:
            db.session.add(Surcharge(code=code, name=name, kind=kind, amount=amt))
        db.session.commit()

    # Demo data (sample drivers, vehicles, customers, orders) is only created
    # when SEED_DEMO is not "false". Set SEED_DEMO=false for a clean go-live.
    if os.environ.get("SEED_DEMO", "true").lower() == "false":
        return
    if Customer.query.first():   # demo already seeded
        return

    disp = User(name="Dispatch Desk", email="dispatch@heliolink.co", role="dispatcher")
    disp.set_password("Heliolink26")
    db.session.add(disp)
    db.session.flush()

    d1u = User(name="John Driver", email="john@heliolink.co", role="driver", phone="07700900001")
    d1u.set_password("driver123")
    d2u = User(name="Sam Wheeler", email="sam@heliolink.co", role="driver", phone="07700900002")
    d2u.set_password("driver123")
    db.session.add_all([d1u, d2u])
    db.session.flush()
    d1 = Driver(user=d1u, phone=d1u.phone, licence_no="SALEH061")
    d2 = Driver(user=d2u, phone=d2u.phone, licence_no="WHEEL022")
    db.session.add_all([d1, d2])

    v1 = Vehicle(reg="PR21 HLK", make_model="Ford Transit", vtype="panel van", capacity_pallets=2, mpg=30)
    v2 = Vehicle(reg="PR71 HLK", make_model="Mercedes Sprinter", vtype="2.5t", capacity_pallets=4, mpg=22)
    v3 = Vehicle(reg="PR22 HLK", make_model="DAF LF", vtype="7.5t", capacity_pallets=10, mpg=14)
    db.session.add_all([v1, v2, v3])

    c1 = Customer(name="ESOL Trading Ltd", contact_name="Maria", email="ops@esol.example",
                  phone="07700900100", address="Unit 4, Docklands", postcode="L20 8DB",
                  rate_base=8.0, rate_per_mile=0.55, rate_per_pallet=6.0,
                  rate_per_kg=0.02, min_charge=15.0, fuel_surcharge_pct=15.0,
                  portal_code="ESOL2026")
    c2 = Customer(name="Williams Bakery", contact_name="Tom", email="tom@williams.example",
                  phone="07700900101", address="Moor Lane", postcode="M8 4PX",
                  rate_base=6.0, rate_per_mile=0.50, rate_per_pallet=5.0,
                  rate_per_kg=0.0, min_charge=12.0, fuel_surcharge_pct=15.0,
                  portal_code="WILL2026")
    c3 = Customer(name="ETD Logistics", contact_name="Priya", email="hub@etd.example",
                  phone="07700900102", address="Ashton Ind Est", postcode="ST4 8JG",
                  rate_base=10.0, rate_per_mile=0.60, rate_per_pallet=7.0,
                  rate_per_kg=0.03, min_charge=18.0, fuel_surcharge_pct=15.0,
                  portal_code="ETD2026")
    db.session.add_all([c1, c2, c3])
    db.session.flush()

    # (customer, recipient, phone, postcode, parcels, pallets, job_type, weight)
    demo = [
        (c1, "Northern Foods", "07700900201", "CH64 8TF", 4, 2, "delivery", 480),
        (c1, "Wirral Depot", "07700900202", "CH66 1QW", 2, 1, "delivery", 220),
        (c2, "Congleton Store", "07700900203", "CW12 4RL", 0, 3, "delivery", 640),
        (c2, "Stoke Central", "07700900204", "ST4 8JG", 6, 0, "delivery", 90),
        (c3, "Matlock Yard", "07700900205", "DE4 5FR", 3, 2, "collection", 510),
        (c3, "Derby Hub", "07700900206", "DE21 5DB", 2, 1, "delivery", 260),
    ]
    for cust, recip, phone, pc, parc, pal, jt, wt in demo:
        o = Order(ref=next_ref("HL", Order, "ref"), customer=cust, recipient=recip,
                  phone=phone, address=recip, postcode=pc, pallets=pal,
                  weight_kg=wt, job_type=jt, service="next-day")
        set_legs(o, cust, jt, recip, pc)
        o.collection_date = date.today()
        o.collection_time = "09:00"
        o.timing = "48h" if jt == "delivery" else "same-day"
        o.delivery_date = date.today() + (timedelta(days=2) if jt == "delivery" else timedelta(0))
        o.price = quote_order(cust, pc, pal, wt)
        db.session.add(o)
        db.session.flush()
        build_items(o, parc, pal)

    db.session.commit()


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
