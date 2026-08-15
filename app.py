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

from models import (db, User, Driver, Vehicle, Customer, Route, Order,
                    Stop, POD, SmsLog, Item, Scan)
from optimise import optimise_sequence, geocode, haversine, DEPOT_DEFAULT
from sms import send_sms


def create_app():
    app = Flask(__name__)
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
def quote_order(customer, postcode, pallets, start="PR2 2TE"):
    """Price a delivery using the customer's rate profile."""
    if not customer:
        return 0.0
    a = geocode(start) or DEPOT_DEFAULT
    b = geocode(postcode) or a
    miles = haversine(a, b)
    price = (customer.rate_base or 0) \
        + (customer.rate_per_mile or 0) * miles \
        + (customer.rate_per_pallet or 0) * (pallets or 1)
    return round(price, 2)


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
            o = Order(
                ref=next_ref("HL", Order, "ref"),
                customer=cust,
                job_type=request.form.get("job_type", "delivery"),
                recipient=request.form.get("recipient"),
                phone=request.form.get("phone"),
                address=request.form.get("address"),
                postcode=request.form.get("postcode", "").upper().strip(),
                pallets=pallets,
                weight_kg=float(request.form.get("weight_kg") or 0),
                service=request.form.get("service", "next-day"),
                notes=request.form.get("notes"),
            )
            o.price = quote_order(cust, o.postcode, pallets)
            db.session.add(o)
            db.session.flush()
            barcodes = [b.strip() for b in
                        (request.form.get("barcodes") or "").splitlines() if b.strip()]
            build_items(o, parcels, pallets, barcodes)
            db.session.commit()
            flash(f"{o.job_type.title()} {o.ref} created — "
                  f"{parcels} parcel(s), {pallets} pallet(s), £{o.price:.2f}.", "ok")
            return redirect(url_for("orders"))
        return render_template("order_new.html", customers=customers)

    @app.route("/api/quote")
    @login_required("admin", "dispatcher")
    def api_quote():
        cust = db.session.get(Customer, int(request.args.get("customer_id", 0)))
        price = quote_order(cust, request.args.get("postcode", ""),
                            int(request.args.get("pallets") or 1))
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
        if drv:
            rs = Route.query.filter(Route.driver_id == drv.id,
                                    Route.run_date >= date.today())\
                .order_by(Route.run_date).all()
        return render_template("driver_home.html", routes=rs)

    @app.route("/driver/route/<int:rid>")
    @login_required("driver", "admin")
    def driver_route(rid):
        r = db.session.get(Route, rid) or abort(404)
        return render_template("driver_route.html", r=r)

    @app.route("/driver/stop/<int:sid>", methods=["GET", "POST"])
    @login_required("driver", "admin")
    def driver_stop(sid):
        s = db.session.get(Stop, sid) or abort(404)
        if request.method == "POST":
            outcome = request.form.get("outcome", "delivered")
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

            # close the route if every stop is done
            if all(st.status in ("delivered", "failed", "collected")
                   for st in s.route.stops):
                s.route.status = "completed"
            db.session.commit()
            flash("Stop completed.", "ok")
            return redirect(url_for("driver_route", rid=s.route_id))
        mode = "collect" if s.order.job_type == "collection" else "deliver"
        return render_template("driver_stop.html", s=s, mode=mode)

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
            o = Order(
                ref=next_ref("HL", Order, "ref"),
                customer=c,
                job_type=request.form.get("job_type", "delivery"),
                recipient=request.form.get("recipient"),
                phone=request.form.get("phone"),
                address=request.form.get("address"),
                postcode=request.form.get("postcode", "").upper().strip(),
                pallets=pallets,
                weight_kg=float(request.form.get("weight_kg") or 0),
                service=request.form.get("service", "next-day"),
                notes=request.form.get("notes"),
            )
            o.price = quote_order(c, o.postcode, pallets)
            db.session.add(o)
            db.session.flush()
            build_items(o, parcels, pallets)
            db.session.commit()
            flash(f"{o.job_type.title()} {o.ref} placed. "
                  f"Estimated charge £{o.price:.2f}.", "ok")
            return redirect(url_for("portal_home"))
        orders = Order.query.filter_by(customer_id=c.id)\
            .order_by(Order.created_at.desc()).limit(50).all()
        return render_template("portal_home.html", c=c, orders=orders)

    @app.route("/portal/logout")
    def portal_logout():
        session.pop("portal_customer", None)
        return redirect(url_for("portal"))

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
            db.session.commit()
            flash("Customer saved.", "ok")
            return redirect(url_for("customers"))
        return render_template("customer_edit.html", c=c)

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
                    vtype=request.form.get("vtype", "van"),
                    capacity_pallets=int(request.form.get("capacity_pallets") or 0))
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

    # ---- PWA plumbing ----
    @app.route("/healthz")
    def healthz():
        try:
            db.session.execute(db.text("SELECT 1"))
            return {"status": "ok"}, 200
        except Exception:
            return {"status": "degraded"}, 503

    @app.route("/manifest.webmanifest")
    def manifest():
        return app.send_static_file("manifest.json")

    @app.route("/sw.js")
    def service_worker():
        resp = app.make_response(app.send_static_file("sw.js"))
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

    v1 = Vehicle(reg="PR21 HLK", make_model="Ford Transit", vtype="van", capacity_pallets=6)
    v2 = Vehicle(reg="PR71 HLK", make_model="DAF LF 7.5t", vtype="7.5t", capacity_pallets=10)
    db.session.add_all([v1, v2])

    c1 = Customer(name="ESOL Trading Ltd", contact_name="Maria", email="ops@esol.example",
                  phone="07700900100", address="Unit 4, Docklands", postcode="L20 8DB",
                  rate_base=8.0, rate_per_mile=0.55, rate_per_pallet=6.0,
                  portal_code="ESOL2026")
    c2 = Customer(name="Williams Bakery", contact_name="Tom", email="tom@williams.example",
                  phone="07700900101", address="Moor Lane", postcode="M8 4PX",
                  rate_base=6.0, rate_per_mile=0.50, rate_per_pallet=5.0,
                  portal_code="WILL2026")
    c3 = Customer(name="ETD Logistics", contact_name="Priya", email="hub@etd.example",
                  phone="07700900102", address="Ashton Ind Est", postcode="ST4 8JG",
                  rate_base=10.0, rate_per_mile=0.60, rate_per_pallet=7.0,
                  portal_code="ETD2026")
    db.session.add_all([c1, c2, c3])
    db.session.flush()

    # (customer, recipient, phone, postcode, parcels, pallets, job_type)
    demo = [
        (c1, "Northern Foods", "07700900201", "CH64 8TF", 4, 2, "delivery"),
        (c1, "Wirral Depot", "07700900202", "CH66 1QW", 2, 1, "delivery"),
        (c2, "Congleton Store", "07700900203", "CW12 4RL", 0, 3, "delivery"),
        (c2, "Stoke Central", "07700900204", "ST4 8JG", 6, 0, "delivery"),
        (c3, "Matlock Yard", "07700900205", "DE4 5FR", 3, 2, "collection"),
        (c3, "Derby Hub", "07700900206", "DE21 5DB", 2, 1, "delivery"),
    ]
    for cust, recip, phone, pc, parc, pal, jt in demo:
        o = Order(ref=next_ref("HL", Order, "ref"), customer=cust, recipient=recip,
                  phone=phone, address=recip, postcode=pc, pallets=pal,
                  job_type=jt, service="next-day")
        o.price = quote_order(cust, pc, pal)
        db.session.add(o)
        db.session.flush()
        build_items(o, parc, pal)

    db.session.commit()


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
