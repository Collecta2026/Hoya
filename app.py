"""
Hoya - courier dispatch & last-mile delivery ops platform.
Flask + SQLAlchemy, same architecture as Collecta: SQLite locally,
Postgres (Neon) in production via the DATABASE_URL env var.

Run locally:
    pip install -r requirements.txt
    python app.py
Then open http://localhost:5000  (seeded admin login printed on first run).
"""
import os
import io
import csv
import random
import string
from datetime import datetime, date, timedelta

from flask import (Flask, render_template, redirect, url_for, request, flash,
                    jsonify, Response, abort)
from flask_login import (LoginManager, login_user, logout_user, login_required,
                          current_user)

from models import (db, User, Driver, Vehicle, Customer, Order, OrderBox,
                     OrderEvent, ProofOfDelivery, Zone, RateSettings,
                     Surcharge, OrderSurcharge, Invoice)
from optimise import optimise_stops, region_of
from rates import cost_breakdown, quote_order

try:
    from sms import send_sms
except Exception:  # pragma: no cover - sms module always present, but be defensive
    def send_sms(*a, **k):
        print("[sms] skipped:", a, k)


# ---------------------------------------------------------------------------
# App factory / config
# ---------------------------------------------------------------------------

def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")

    db_url = os.environ.get("DATABASE_URL", "")
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    if not db_url:
        db_url = "sqlite:///" + os.path.join(os.path.dirname(__file__), "hoya.db")
    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True}
    app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024  # 12MB, allows POD photos

    db.init_app(app)

    login_manager = LoginManager()
    login_manager.login_view = "login"
    login_manager.init_app(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    register_routes(app)

    with app.app_context():
        db.create_all()
        seed_if_needed()

    return app


DEPOT_POSTCODE = os.environ.get("DEPOT_POSTCODE", "M1 1AA")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def gen_reference():
    return "HOY-" + datetime.utcnow().strftime("%y%m%d") + "-" + "".join(
        random.choices(string.ascii_uppercase + string.digits, k=4))


def gen_barcode(order_ref, kind, index):
    prefix = "PAL" if kind == "pallet" else "PCL"
    return f"{order_ref}-{prefix}{index:02d}"


def log_event(order, status, note=None):
    db.session.add(OrderEvent(order_id=order.id, status=status, note=note))


def require_role(*roles):
    if not current_user.is_authenticated or current_user.role not in roles:
        abort(403)


def sla_tag(order):
    """amber if <=4h to the delivery deadline, red if overdue, else None."""
    if not order.delivery_date or order.status in ("delivered", "failed", "returned", "collected"):
        return None
    deadline = datetime.combine(order.delivery_date, datetime.min.time()) + timedelta(hours=18)
    remaining = deadline - datetime.utcnow()
    if remaining.total_seconds() < 0:
        return "red"
    if remaining.total_seconds() < 4 * 3600:
        return "amber"
    return None


def register_routes(app):

    # -----------------------------------------------------------------
    # Auth
    # -----------------------------------------------------------------

    @app.route("/")
    def index():
        if current_user.is_authenticated:
            if current_user.role == "driver":
                return redirect(url_for("driver_home"))
            if current_user.role == "customer":
                return redirect(url_for("portal_home"))
            return redirect(url_for("dashboard"))
        return redirect(url_for("login"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            user = User.query.filter_by(email=email).first()
            if user and user.role in ("admin", "dispatcher") and user.check_password(password):
                login_user(user)
                return redirect(url_for("dashboard"))
            flash("Invalid email or password.", "error")
        return render_template("login.html")

    @app.route("/driver/login", methods=["GET", "POST"])
    def driver_login():
        if request.method == "POST":
            driver_id = request.form.get("driver_id")
            pin = request.form.get("pin", "")
            driver = Driver.query.get(driver_id) if driver_id else None
            if driver and driver.pin == pin:
                user = User.query.filter_by(driver_id=driver.id).first()
                if not user:
                    user = User(email=f"driver{driver.id}@hoya.local", name=driver.name,
                                 role="driver", driver_id=driver.id)
                    user.set_password(pin)
                    db.session.add(user)
                    db.session.commit()
                login_user(user)
                return redirect(url_for("driver_home"))
            flash("Incorrect PIN.", "error")
        drivers = Driver.query.order_by(Driver.name).all()
        return render_template("driver_login.html", drivers=drivers)

    @app.route("/portal/login", methods=["GET", "POST"])
    def portal_login():
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            user = User.query.filter_by(email=email, role="customer").first()
            if user and user.check_password(password):
                login_user(user)
                return redirect(url_for("portal_home"))
            flash("Invalid email or password.", "error")
        return render_template("portal_login.html")

    @app.route("/logout")
    @login_required
    def logout():
        logout_user()
        return redirect(url_for("login"))

    @app.route("/healthz")
    def healthz():
        return jsonify({"ok": True, "time": datetime.utcnow().isoformat()})

    # -----------------------------------------------------------------
    # Dispatcher: dashboard
    # -----------------------------------------------------------------

    @app.route("/dashboard")
    @login_required
    def dashboard():
        require_role("admin", "dispatcher")
        today = date.today()
        active_statuses = ["pending", "assigned", "picked_up", "in_transit"]
        active_count = Order.query.filter(Order.status.in_(active_statuses)).count()
        delivered_today = Order.query.filter(
            Order.status.in_(["delivered", "collected"]),
            Order.updated_at >= datetime.combine(today, datetime.min.time())
        ).count()
        failed_count = Order.query.filter_by(status="failed").count()
        available_drivers = Driver.query.filter_by(status="available").count()
        available_vans = Vehicle.query.filter_by(status="available").count()
        recent = Order.query.order_by(Order.created_at.desc()).limit(8).all()
        return render_template("dashboard.html", active_count=active_count,
                                delivered_today=delivered_today, failed_count=failed_count,
                                available_drivers=available_drivers, available_vans=available_vans,
                                recent=recent)

    # -----------------------------------------------------------------
    # Dispatcher: orders / dispatch board
    # -----------------------------------------------------------------

    STATUSES = ["pending", "assigned", "picked_up", "in_transit", "delivered", "collected", "failed", "returned"]
    STATUS_LABELS = {
        "pending": "Pending", "assigned": "Assigned", "picked_up": "Picked Up",
        "in_transit": "In Transit", "delivered": "Delivered", "collected": "Collected",
        "failed": "Failed / Exception", "returned": "Returned",
    }

    @app.route("/orders")
    @login_required
    def orders_board():
        require_role("admin", "dispatcher")
        board_statuses = ["pending", "assigned", "picked_up", "in_transit", "delivered", "failed"]
        orders = Order.query.order_by(Order.created_at.desc()).all()
        columns = {s: [o for o in orders if o.status == s] for s in board_statuses}
        return render_template("orders_board.html", columns=columns,
                                status_labels=STATUS_LABELS, board_statuses=board_statuses)

    @app.route("/orders/new", methods=["GET", "POST"])
    @login_required
    def order_new():
        require_role("admin", "dispatcher")
        customers = Customer.query.order_by(Customer.name).all()
        if request.method == "POST":
            order = _create_order_from_form(request.form)
            flash(f"Order {order.reference} created.", "success")
            return redirect(url_for("order_detail", order_id=order.id))
        return render_template("order_form.html", customers=customers, today=date.today().isoformat())

    def _create_order_from_form(form):
        pallets = int(form.get("pallets") or 0)
        order = Order(
            reference=gen_reference(),
            customer_id=int(form["customer_id"]),
            job_type=form.get("job_type", "delivery"),
            collection_address=form.get("collection_address"),
            collection_postcode=(form.get("collection_postcode") or "").upper(),
            delivery_address=form["delivery_address"],
            delivery_postcode=form["delivery_postcode"].upper(),
            contact_name=form.get("contact_name"),
            contact_phone=form.get("contact_phone"),
            notes=form.get("notes"),
            pallets=pallets,
            parcels=int(form.get("parcels") or 0),
            weight_per_pallet_kg=float(form.get("weight_per_pallet_kg") or 0),
            pallet_length_cm=float(form.get("pallet_length_cm") or 120),
            pallet_width_cm=float(form.get("pallet_width_cm") or 100),
            pallet_height_cm=float(form.get("pallet_height_cm") or 150),
            collection_date=_parse_date(form.get("collection_date")),
            collection_time=form.get("collection_time"),
            delivery_date=_parse_date(form.get("delivery_date")),
            timing=form.get("timing", "48h"),
            priority=form.get("priority", "standard"),
        )
        db.session.add(order)
        db.session.flush()
        log_event(order, "pending", "Order created")

        parcel_count = order.parcels or 0
        pallet_count = order.pallets or 0
        for i in range(1, pallet_count + 1):
            db.session.add(OrderBox(order_id=order.id, kind="pallet", label_index=i,
                                     barcode=gen_barcode(order.reference, "pallet", i)))
        for i in range(1, parcel_count + 1):
            db.session.add(OrderBox(order_id=order.id, kind="parcel", label_index=i,
                                     barcode=gen_barcode(order.reference, "parcel", i)))

        customer = Customer.query.get(order.customer_id)
        settings = RateSettings.query.first()
        zones = Zone.query.all()
        try:
            order.quoted_price = quote_order(order, customer, settings, zones, DEPOT_POSTCODE)
        except Exception:
            order.quoted_price = None

        db.session.commit()
        return order

    def _parse_date(value):
        if not value:
            return None
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            return None

    @app.route("/orders/<int:order_id>")
    @login_required
    def order_detail(order_id):
        require_role("admin", "dispatcher")
        order = Order.query.get_or_404(order_id)
        surcharges = Surcharge.query.all()
        drivers = Driver.query.order_by(Driver.name).all()
        vehicles = Vehicle.query.order_by(Vehicle.registration).all()
        return render_template("order_detail.html", order=order, surcharges=surcharges,
                                status_labels=STATUS_LABELS, statuses=STATUSES,
                                drivers=drivers, vehicles=vehicles)

    @app.route("/orders/<int:order_id>/status", methods=["POST"])
    @login_required
    def order_set_status(order_id):
        require_role("admin", "dispatcher")
        order = Order.query.get_or_404(order_id)
        status = request.form.get("status")
        if status not in STATUSES:
            abort(400)
        order.status = status
        log_event(order, status, request.form.get("note"))
        db.session.commit()
        send_sms(order.contact_phone, f"Your order {order.reference} is now {STATUS_LABELS.get(status, status)}.")
        return redirect(request.referrer or url_for("orders_board"))

    @app.route("/orders/<int:order_id>/assign", methods=["POST"])
    @login_required
    def order_assign(order_id):
        require_role("admin", "dispatcher")
        order = Order.query.get_or_404(order_id)
        driver_id = request.form.get("driver_id") or None
        vehicle_id = request.form.get("vehicle_id") or None
        order.driver_id = int(driver_id) if driver_id else None
        order.vehicle_id = int(vehicle_id) if vehicle_id else None
        if order.driver_id and order.status == "pending":
            order.status = "assigned"
        driver_name = Driver.query.get(order.driver_id).name if order.driver_id else "nobody"
        log_event(order, order.status, f"Assigned to {driver_name}")
        db.session.commit()
        return redirect(request.referrer or url_for("orders_board"))

    @app.route("/orders/<int:order_id>/surcharge", methods=["POST"])
    @login_required
    def order_add_surcharge(order_id):
        require_role("admin", "dispatcher")
        order = Order.query.get_or_404(order_id)
        surcharge_id = int(request.form["surcharge_id"])
        surcharge = Surcharge.query.get_or_404(surcharge_id)
        amount = surcharge.default_amount
        if surcharge.is_percent and order.quoted_price:
            amount = round(order.quoted_price * surcharge.default_amount / 100.0, 2)
        db.session.add(OrderSurcharge(order_id=order.id, surcharge_id=surcharge.id, amount=amount))
        db.session.commit()
        return redirect(url_for("order_detail", order_id=order.id))

    @app.route("/orders/<int:order_id>/label")
    @login_required
    def order_label(order_id):
        require_role("admin", "dispatcher")
        order = Order.query.get_or_404(order_id)
        return render_template("labels.html", order=order)

    # -----------------------------------------------------------------
    # Dispatcher: fleet (drivers & vehicles)
    # -----------------------------------------------------------------

    @app.route("/fleet")
    @login_required
    def fleet():
        require_role("admin", "dispatcher")
        drivers = Driver.query.order_by(Driver.name).all()
        vehicles = Vehicle.query.order_by(Vehicle.registration).all()
        return render_template("fleet.html", drivers=drivers, vehicles=vehicles)

    @app.route("/fleet/drivers/new", methods=["POST"])
    @login_required
    def driver_new():
        require_role("admin", "dispatcher")
        d = Driver(name=request.form["name"], phone=request.form.get("phone"),
                    pin=request.form.get("pin") or "1234")
        db.session.add(d)
        db.session.commit()
        flash(f"Driver {d.name} added. PIN: {d.pin}", "success")
        return redirect(url_for("fleet"))

    @app.route("/fleet/drivers/<int:driver_id>/status", methods=["POST"])
    @login_required
    def driver_set_status(driver_id):
        require_role("admin", "dispatcher")
        d = Driver.query.get_or_404(driver_id)
        d.status = request.form["status"]
        db.session.commit()
        return redirect(request.referrer or url_for("fleet"))

    @app.route("/fleet/vehicles/new", methods=["POST"])
    @login_required
    def vehicle_new():
        require_role("admin", "dispatcher")
        size = request.form.get("size", "panel_van")
        v = Vehicle(registration=request.form["registration"], size=size,
                    capacity_pallets=Vehicle.SIZE_CAPACITY.get(size, 4),
                    mpg=Vehicle.SIZE_MPG_DEFAULT.get(size, 25.0))
        db.session.add(v)
        db.session.commit()
        flash(f"Vehicle {v.registration} added.", "success")
        return redirect(url_for("fleet"))

    @app.route("/fleet/vehicles/<int:vehicle_id>/status", methods=["POST"])
    @login_required
    def vehicle_set_status(vehicle_id):
        require_role("admin", "dispatcher")
        v = Vehicle.query.get_or_404(vehicle_id)
        v.status = request.form["status"]
        db.session.commit()
        return redirect(request.referrer or url_for("fleet"))

    # Backward/alt naming used by the resources page toggles
    @app.route("/resources")
    @login_required
    def resources():
        require_role("admin", "dispatcher")
        drivers = Driver.query.order_by(Driver.name).all()
        vehicles = Vehicle.query.order_by(Vehicle.registration).all()
        return render_template("resources.html", drivers=drivers, vehicles=vehicles)

    # -----------------------------------------------------------------
    # Dispatcher: customers
    # -----------------------------------------------------------------

    @app.route("/customers")
    @login_required
    def customers_list():
        require_role("admin", "dispatcher")
        customers = Customer.query.order_by(Customer.name).all()
        zones = Zone.query.all()
        return render_template("customers.html", customers=customers, zones=zones)

    @app.route("/customers/new", methods=["POST"])
    @login_required
    def customer_new():
        require_role("admin", "dispatcher")
        c = Customer(
            name=request.form["name"], email=request.form.get("email"),
            phone=request.form.get("phone"), billing_address=request.form.get("billing_address"),
            pricing_mode=request.form.get("pricing_mode", "standard"),
            discount_pct=float(request.form.get("discount_pct") or 0),
            flat_rate_per_pallet=float(request.form.get("flat_rate_per_pallet") or 25),
            zone_id=int(request.form["zone_id"]) if request.form.get("zone_id") else None,
        )
        db.session.add(c)
        db.session.commit()

        portal_email = request.form.get("portal_email")
        if portal_email:
            user = User(email=portal_email.strip().lower(), name=c.name, role="customer", customer_id=c.id)
            user.set_password(request.form.get("portal_password") or "changeme123")
            db.session.add(user)
            db.session.commit()
            flash(f"Customer {c.name} added with portal login {portal_email}.", "success")
        else:
            flash(f"Customer {c.name} added.", "success")
        return redirect(url_for("customers_list"))

    # -----------------------------------------------------------------
    # Dispatcher: route planner
    # -----------------------------------------------------------------

    @app.route("/routes")
    @login_required
    def routes_list():
        require_role("admin", "dispatcher")
        drivers = Driver.query.order_by(Driver.name).all()
        unassigned = Order.query.filter_by(status="pending").order_by(Order.delivery_date).all()
        active_by_driver = {}
        for d in drivers:
            active_by_driver[d.id] = Order.query.filter(
                Order.driver_id == d.id,
                Order.status.in_(["assigned", "picked_up", "in_transit"])
            ).order_by(Order.updated_at).all()
        return render_template("routes.html", drivers=drivers, unassigned=unassigned,
                                active_by_driver=active_by_driver, today=date.today().isoformat())

    @app.route("/routes/build", methods=["POST"])
    @login_required
    def routes_build():
        require_role("admin", "dispatcher")
        driver_id = int(request.form["driver_id"])
        order_ids = request.form.getlist("order_ids")
        driver = Driver.query.get_or_404(driver_id)
        orders = Order.query.filter(Order.id.in_(order_ids)).all()
        ordered = optimise_stops(orders, DEPOT_POSTCODE)
        for order in ordered:
            order.driver_id = driver.id
            order.vehicle_id = driver.current_vehicle_id
            order.status = "assigned"
            log_event(order, "assigned", f"Route built for {driver.name}")
        driver.status = "on_route"
        db.session.commit()
        flash(f"Built optimised route for {driver.name}: {len(ordered)} stop(s).", "success")
        return redirect(url_for("routes_list"))

    @app.route("/routes/<int:driver_id>/reoptimise", methods=["POST"])
    @login_required
    def routes_reoptimise(driver_id):
        require_role("admin", "dispatcher")
        driver = Driver.query.get_or_404(driver_id)
        orders = Order.query.filter(
            Order.driver_id == driver.id,
            Order.status.in_(["assigned", "picked_up", "in_transit"])
        ).all()
        ordered = optimise_stops(orders, DEPOT_POSTCODE)
        base = datetime.utcnow()
        for i, order in enumerate(ordered):
            order.updated_at = base + timedelta(seconds=i)  # encodes new sequence order
        db.session.commit()
        flash(f"Re-optimised {len(ordered)} stop(s) for {driver.name}.", "success")
        return redirect(url_for("routes_list"))

    # -----------------------------------------------------------------
    # Dispatcher: pending work control board
    # -----------------------------------------------------------------

    @app.route("/pending")
    @login_required
    def pending_board():
        require_role("admin", "dispatcher")
        view = request.args.get("view", "status")
        active = Order.query.filter(Order.status.in_(
            ["pending", "assigned", "picked_up", "in_transit"])).all()

        awaiting_collection = [o for o in active if o.job_type == "collection" and o.status == "pending"]
        awaiting_delivery = [o for o in active if o.job_type == "delivery" and o.status == "pending"]

        by_driver = {}
        for o in active:
            key = o.driver.name if o.driver else "Unassigned"
            by_driver.setdefault(key, []).append(o)

        summary = {
            "collections": len([o for o in active if o.job_type == "collection"]),
            "deliveries": len([o for o in active if o.job_type == "delivery"]),
            "pallets": sum(o.pallets or 0 for o in active),
            "parcels": sum(o.parcels or 0 for o in active),
        }

        drivers = Driver.query.order_by(Driver.name).all()
        vehicles = Vehicle.query.order_by(Vehicle.registration).all()
        tags = {o.id: sla_tag(o) for o in active}

        return render_template("pending.html", view=view, awaiting_collection=awaiting_collection,
                                awaiting_delivery=awaiting_delivery, by_driver=by_driver,
                                summary=summary, drivers=drivers, vehicles=vehicles, tags=tags)

    @app.route("/pending/<int:order_id>/allocate", methods=["POST"])
    @login_required
    def pending_allocate(order_id):
        require_role("admin", "dispatcher")
        order = Order.query.get_or_404(order_id)
        driver_id = request.form.get("driver_id") or None
        vehicle_id = request.form.get("vehicle_id") or None
        order.driver_id = int(driver_id) if driver_id else None
        order.vehicle_id = int(vehicle_id) if vehicle_id else None

        warning = None
        if order.vehicle_id:
            vehicle = Vehicle.query.get(order.vehicle_id)
            load_on_van = db.session.query(db.func.coalesce(db.func.sum(Order.pallets), 0)).filter(
                Order.vehicle_id == vehicle.id,
                Order.status.in_(["assigned", "picked_up", "in_transit"])
            ).scalar()
            if load_on_van + (order.pallets or 0) > vehicle.capacity_pallets:
                warning = f"Warning: {vehicle.registration} capacity ({vehicle.capacity_pallets} pallets) may be exceeded."

        if order.driver_id and order.status == "pending":
            order.status = "assigned"
        log_event(order, order.status, "Allocated via pending board")
        db.session.commit()
        if warning:
            flash(warning, "warning")
        return redirect(url_for("pending_board"))

    @app.route("/pending/<int:order_id>/suggest", methods=["POST"])
    @login_required
    def pending_suggest(order_id):
        require_role("admin", "dispatcher")
        order = Order.query.get_or_404(order_id)
        vehicles = Vehicle.query.filter_by(status="available").order_by(Vehicle.capacity_pallets).all()
        chosen_vehicle = next((v for v in vehicles if v.capacity_pallets >= (order.pallets or 0)), None)
        chosen_driver = Driver.query.filter_by(status="available").order_by(Driver.name).first()

        if chosen_vehicle:
            order.vehicle_id = chosen_vehicle.id
        if chosen_driver:
            order.driver_id = chosen_driver.id
            if order.status == "pending":
                order.status = "assigned"
        log_event(order, order.status, "Auto-suggested allocation")
        db.session.commit()
        if not chosen_vehicle or not chosen_driver:
            flash("No fully available driver/van combination found — allocate manually.", "warning")
        else:
            flash(f"Suggested {chosen_driver.name} in {chosen_vehicle.registration}.", "success")
        return redirect(url_for("pending_board"))

    # -----------------------------------------------------------------
    # Driver app
    # -----------------------------------------------------------------

    @app.route("/driver")
    @login_required
    def driver_home():
        require_role("driver")
        driver = Driver.query.get(current_user.driver_id)
        stops = Order.query.filter(
            Order.driver_id == driver.id,
            Order.status.in_(["assigned", "picked_up", "in_transit"])
        ).order_by(Order.updated_at).all()
        completed_today = Order.query.filter(
            Order.driver_id == driver.id,
            Order.status.in_(["delivered", "collected", "failed"]),
            Order.updated_at >= datetime.combine(date.today(), datetime.min.time())
        ).count()
        next_stop = stops[0] if stops else None
        return render_template("driver_home.html", driver=driver, stops=stops,
                                next_stop=next_stop, completed_today=completed_today)

    @app.route("/driver/stop/<int:order_id>")
    @login_required
    def driver_stop(order_id):
        require_role("driver")
        order = Order.query.get_or_404(order_id)
        if order.driver_id != current_user.driver_id:
            abort(403)
        scanned = sum(1 for b in order.boxes if b.scanned)
        total = len(order.boxes)
        return render_template("driver_stop.html", order=order, scanned=scanned, total=total)

    @app.route("/driver/stop/<int:order_id>/scan", methods=["POST"])
    @login_required
    def driver_scan(order_id):
        require_role("driver")
        order = Order.query.get_or_404(order_id)
        if order.driver_id != current_user.driver_id:
            abort(403)
        code = request.form.get("barcode", "").strip()
        box = OrderBox.query.filter_by(order_id=order.id, barcode=code).first()
        result = {"ok": False, "message": "Barcode not recognised for this order."}
        if box:
            if box.scanned:
                result = {"ok": True, "message": f"{box.barcode} already scanned.", "duplicate": True}
            else:
                box.scanned = True
                box.scanned_at = datetime.utcnow()
                db.session.commit()
                result = {"ok": True, "message": f"{box.barcode} scanned."}
        scanned = sum(1 for b in order.boxes if b.scanned)
        total = len(order.boxes)
        result.update({"scanned": scanned, "total": total, "all_scanned": scanned == total})
        if request.headers.get("X-Requested-With") == "fetch" or request.accept_mimetypes.best == "application/json":
            return jsonify(result)
        flash(result["message"], "success" if result["ok"] else "error")
        return redirect(url_for("driver_stop", order_id=order.id))

    @app.route("/driver/stop/<int:order_id>/complete", methods=["POST"])
    @login_required
    def driver_complete(order_id):
        require_role("driver")
        order = Order.query.get_or_404(order_id)
        if order.driver_id != current_user.driver_id:
            abort(403)

        exception_reason = request.form.get("exception_reason", "").strip()
        override_missing = request.form.get("override_missing") == "on"
        scanned = sum(1 for b in order.boxes if b.scanned)
        total = len(order.boxes)

        if total > 0 and scanned < total and not override_missing and not exception_reason:
            flash(f"Only {scanned}/{total} items scanned. Scan the rest, or tick "
                  f"'complete with items missing' to override.", "error")
            return redirect(url_for("driver_stop", order_id=order.id))

        for b in order.boxes:
            if not b.scanned:
                b.missing = True

        final_status = "failed" if exception_reason else (
            "collected" if order.job_type == "collection" else "delivered")

        pod = ProofOfDelivery.query.filter_by(order_id=order.id).first()
        if not pod:
            pod = ProofOfDelivery(order_id=order.id)
            db.session.add(pod)
        pod.recipient_name = request.form.get("recipient_name")
        pod.signature_data_url = request.form.get("signature_data_url")
        pod.photo_data_url = request.form.get("photo_data_url") or pod.photo_data_url
        pod.notes = request.form.get("notes")
        pod.exception_reason = exception_reason or None
        pod.boxes_confirmed = scanned
        pod.boxes_missing = total - scanned
        pod.delivered_at = datetime.utcnow()
        order.status = final_status
        note = f"Exception: {exception_reason}" if exception_reason else (
            f"Confirmed by {pod.recipient_name or 'recipient'} ({scanned}/{total} items scanned)")
        log_event(order, final_status, note)
        db.session.commit()

        send_sms(order.contact_phone, f"Order {order.reference}: {STATUS_LABELS.get(final_status, final_status)}.")

        remaining = Order.query.filter(
            Order.driver_id == current_user.driver_id,
            Order.status.in_(["assigned", "picked_up", "in_transit"])
        ).order_by(Order.updated_at).first()

        if not remaining:
            driver = Driver.query.get(current_user.driver_id)
            driver.status = "available"
            db.session.commit()
            flash("All stops complete for now.", "success")
            return redirect(url_for("driver_home"))

        flash("Stop completed. Moving to next stop.", "success")
        return redirect(url_for("driver_stop", order_id=remaining.id))

    @app.route("/driver/stop/<int:order_id>/pickup", methods=["POST"])
    @login_required
    def driver_pickup(order_id):
        """Mark 'picked up' / 'in transit' without closing the stop - used when
        a stop has two phases (arrive & load, then depart)."""
        require_role("driver")
        order = Order.query.get_or_404(order_id)
        if order.driver_id != current_user.driver_id:
            abort(403)
        order.status = request.form.get("status", "in_transit")
        log_event(order, order.status, "Driver update")
        db.session.commit()
        return redirect(url_for("driver_stop", order_id=order.id))

    # -----------------------------------------------------------------
    # Customer portal
    # -----------------------------------------------------------------

    @app.route("/portal")
    @login_required
    def portal_home():
        require_role("customer")
        customer = Customer.query.get(current_user.customer_id)
        orders = Order.query.filter_by(customer_id=customer.id).order_by(Order.created_at.desc()).limit(20).all()
        return render_template("portal_home.html", customer=customer, orders=orders)

    @app.route("/portal/order/new", methods=["GET", "POST"])
    @login_required
    def portal_order_new():
        require_role("customer")
        customer = Customer.query.get(current_user.customer_id)
        if request.method == "POST":
            form = request.form.copy()
            form["customer_id"] = str(customer.id)
            order = _create_order_from_form(form)
            flash(f"Order {order.reference} placed.", "success")
            return redirect(url_for("portal_track", reference=order.reference))
        return render_template("portal_order_form.html", customer=customer, today=date.today().isoformat())

    @app.route("/portal/track/<reference>")
    @login_required
    def portal_track(reference):
        require_role("customer")
        order = Order.query.filter_by(reference=reference).first_or_404()
        if order.customer_id != current_user.customer_id:
            abort(403)
        return render_template("portal_track.html", order=order, status_labels=STATUS_LABELS)

    @app.route("/portal/invoices")
    @login_required
    def portal_invoices():
        require_role("customer")
        invoices = Invoice.query.filter_by(customer_id=current_user.customer_id).order_by(
            Invoice.created_at.desc()).all()
        return render_template("portal_invoices.html", invoices=invoices)

    @app.route("/portal/invoices/<int:invoice_id>")
    @login_required
    def portal_invoice_detail(invoice_id):
        require_role("customer")
        invoice = Invoice.query.get_or_404(invoice_id)
        if invoice.customer_id != current_user.customer_id:
            abort(403)
        return render_template("invoice_detail.html", invoice=invoice)

    @app.route("/pod/ref/<reference>")
    def pod_public(reference):
        """Public (unauthenticated, unguessable-reference) POD view, linked
        from invoices so customers/finance can check proof of delivery."""
        order = Order.query.filter_by(reference=reference).first_or_404()
        return render_template("pod_public.html", order=order)

    # -----------------------------------------------------------------
    # Rates & invoicing
    # -----------------------------------------------------------------

    @app.route("/rates/settings", methods=["GET", "POST"])
    @login_required
    def rate_settings():
        require_role("admin", "dispatcher")
        settings = RateSettings.query.first()
        if request.method == "POST":
            for field in ["fuel_price_per_litre", "driver_rate_per_hour", "avg_speed_mph",
                          "handling_min_per_pallet", "fixed_cost_per_job", "margin_pct",
                          "service_uplift_24h_pct", "fuel_surcharge_pct", "vat_pct"]:
                setattr(settings, field, float(request.form[field]))
            settings.round_trip = request.form.get("round_trip") == "on"
            db.session.commit()
            flash("Rate settings updated.", "success")
            return redirect(url_for("rate_settings"))
        example_order = Order.query.first()
        example = None
        if example_order:
            example = cost_breakdown(example_order, settings, Zone.query.all(), depot_postcode=DEPOT_POSTCODE)
        return render_template("rate_settings.html", settings=settings, example=example)

    @app.route("/rates/regions", methods=["GET", "POST"])
    @login_required
    def rate_regions():
        require_role("admin", "dispatcher")
        if request.method == "POST":
            z = Zone(name=request.form["name"], outward_prefixes=request.form["outward_prefixes"],
                      multiplier=float(request.form["multiplier"]))
            db.session.add(z)
            db.session.commit()
            flash(f"Zone {z.name} added.", "success")
            return redirect(url_for("rate_regions"))
        zones = Zone.query.all()
        return render_template("rate_regions.html", zones=zones)

    @app.route("/rates/calculator")
    @login_required
    def rate_calculator():
        require_role("admin", "dispatcher")
        vehicles = Vehicle.SIZE_CAPACITY.keys()
        return render_template("rate_calculator.html", vehicle_sizes=vehicles)

    @app.route("/api/recommend", methods=["POST"])
    @login_required
    def api_recommend():
        require_role("admin", "dispatcher")
        data = request.get_json(force=True)

        class Stub:
            pass
        stub = Stub()
        stub.job_type = "delivery"
        stub.delivery_postcode = data.get("postcode", "M1")
        stub.collection_postcode = data.get("postcode", "M1")
        stub.pallets = int(data.get("pallets") or 1)
        stub.timing = data.get("timing", "48h")

        settings = RateSettings.query.first()
        zones = Zone.query.all()
        breakdown = cost_breakdown(stub, settings, zones, vehicle_size=data.get("vehicle", "panel_van"),
                                    depot_postcode=DEPOT_POSTCODE)
        return jsonify(breakdown)

    @app.route("/invoices")
    @login_required
    def invoices_list():
        require_role("admin", "dispatcher")
        invoices = Invoice.query.order_by(Invoice.created_at.desc()).all()
        uninvoiced_count = Order.query.filter(
            Order.status.in_(["delivered", "collected"]), Order.invoiced == False  # noqa: E712
        ).count()
        return render_template("invoices.html", invoices=invoices, uninvoiced_count=uninvoiced_count)

    @app.route("/invoices/run", methods=["POST"])
    @login_required
    def invoices_run():
        require_role("admin", "dispatcher")
        settings = RateSettings.query.first()
        completed = Order.query.filter(
            Order.status.in_(["delivered", "collected"]), Order.invoiced == False  # noqa: E712
        ).all()

        by_customer = {}
        for o in completed:
            by_customer.setdefault(o.customer_id, []).append(o)

        created = 0
        for customer_id, order_list in by_customer.items():
            customer = Customer.query.get(customer_id)
            subtotal = 0.0
            for o in order_list:
                price = o.quoted_price or 0.0
                price += sum(s.amount for s in o.surcharges)
                subtotal += price
            fuel_surcharge = round(subtotal * settings.fuel_surcharge_pct / 100.0, 2)
            vat = round((subtotal + fuel_surcharge) * settings.vat_pct / 100.0, 2)
            total = round(subtotal + fuel_surcharge + vat, 2)

            invoice = Invoice(
                invoice_number="INV-" + datetime.utcnow().strftime("%y%m%d") + "-" + str(customer_id).zfill(3),
                customer_id=customer_id, period_start=date.today() - timedelta(days=7),
                period_end=date.today(), subtotal=round(subtotal, 2),
                fuel_surcharge=fuel_surcharge, vat=vat, total=total,
            )
            db.session.add(invoice)
            db.session.flush()
            for o in order_list:
                o.invoiced = True
                o.invoice_id = invoice.id
            created += 1

        db.session.commit()
        flash(f"Generated {created} invoice(s) from {len(completed)} completed order(s).", "success")
        return redirect(url_for("invoices_list"))

    @app.route("/invoices/<int:invoice_id>")
    @login_required
    def invoice_detail(invoice_id):
        require_role("admin", "dispatcher")
        invoice = Invoice.query.get_or_404(invoice_id)
        return render_template("invoice_detail.html", invoice=invoice)

    @app.route("/invoices/<int:invoice_id>/status", methods=["POST"])
    @login_required
    def invoice_set_status(invoice_id):
        require_role("admin", "dispatcher")
        invoice = Invoice.query.get_or_404(invoice_id)
        invoice.status = request.form["status"]
        db.session.commit()
        return redirect(url_for("invoice_detail", invoice_id=invoice.id))

    # -----------------------------------------------------------------
    # KPI dashboard & reporting
    # -----------------------------------------------------------------

    @app.route("/kpi")
    @login_required
    def kpi_dashboard():
        require_role("admin", "dispatcher")
        period = request.args.get("period", "7")
        days = int(period)
        since = datetime.utcnow() - timedelta(days=days)
        orders = Order.query.filter(Order.created_at >= since).all()

        total_pallets = sum(o.pallets or 0 for o in orders)
        total_parcels = sum(o.parcels or 0 for o in orders)
        deliveries = [o for o in orders if o.job_type == "delivery"]
        collections = [o for o in orders if o.job_type == "collection"]
        delivered = [o for o in orders if o.status in ("delivered", "collected")]
        failed = [o for o in orders if o.status == "failed"]
        success_rate = round(len(delivered) / len(orders) * 100, 1) if orders else 0.0
        difot = round(len([o for o in delivered if not o.pod or not o.pod.exception_reason]) /
                       len(delivered) * 100, 1) if delivered else 0.0

        per_driver = {}
        for o in orders:
            if not o.driver:
                continue
            key = o.driver.name
            per_driver.setdefault(key, {"jobs": 0, "delivered": 0, "failed": 0, "pallets": 0})
            per_driver[key]["jobs"] += 1
            per_driver[key]["pallets"] += o.pallets or 0
            if o.status in ("delivered", "collected"):
                per_driver[key]["delivered"] += 1
            elif o.status == "failed":
                per_driver[key]["failed"] += 1

        return render_template("kpi.html", period=period, total_pallets=total_pallets,
                                total_parcels=total_parcels, deliveries=len(deliveries),
                                collections=len(collections), success_rate=success_rate,
                                difot=difot, per_driver=per_driver, total_jobs=len(orders),
                                failed_count=len(failed))

    @app.route("/kpi/export.csv")
    @login_required
    def kpi_export():
        require_role("admin", "dispatcher")
        period = request.args.get("period", "7")
        since = datetime.utcnow() - timedelta(days=int(period))
        orders = Order.query.filter(Order.created_at >= since).order_by(Order.created_at).all()

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["Reference", "Customer", "Job Type", "Status", "Driver", "Pallets",
                          "Parcels", "Postcode", "Created", "Delivered/Failed At"])
        for o in orders:
            writer.writerow([
                o.reference, o.customer.name if o.customer else "", o.job_type, o.status,
                o.driver.name if o.driver else "", o.pallets, o.parcels, o.delivery_postcode,
                o.created_at.strftime("%Y-%m-%d %H:%M"),
                o.updated_at.strftime("%Y-%m-%d %H:%M") if o.status in ("delivered", "collected", "failed") else "",
            ])
        return Response(buf.getvalue(), mimetype="text/csv",
                         headers={"Content-Disposition": "attachment; filename=hoya_kpi_export.csv"})

    # -----------------------------------------------------------------
    # Standalone scanner & help
    # -----------------------------------------------------------------

    @app.route("/scan")
    @login_required
    def scan_standalone():
        return render_template("scan.html")

    @app.route("/scan/lookup", methods=["POST"])
    @login_required
    def scan_lookup():
        code = request.form.get("barcode", "").strip()
        box = OrderBox.query.filter_by(barcode=code).first()
        if not box:
            return jsonify({"ok": False, "message": "Unknown barcode."})
        return jsonify({"ok": True, "order_reference": box.order.reference,
                         "order_id": box.order_id, "kind": box.kind,
                         "scanned": box.scanned, "customer": box.order.customer.name})

    @app.route("/help")
    @login_required
    def help_page():
        role = current_user.role
        return render_template("help.html", role=role)


# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------

def seed_if_needed():
    if RateSettings.query.first() is None:
        db.session.add(RateSettings())

    if Zone.query.count() == 0:
        default_zones = [
            ("London & South East", "E,EC,N,NW,SE,SW,W,WC,BR,CR,DA,EN,HA,IG,KT,RM,SM,TW,UB,WD,GU,RG,SL,MK,LU,AL", 1.2),
            ("Midlands", "B,CV,DE,LE,NG,NN,ST,WR,WS,WV,OX", 1.0),
            ("North West", "M,L,WA,PR,BB,BL,OL,SK,CH,WN,LA", 1.0),
            ("Yorkshire & North East", "LS,S,BD,HD,HX,WF,YO,HU,DN,NE,SR,DH,TS,DL", 1.05),
            ("Scotland", "EH,G,KA,PA,ML,FK,DD,AB,IV,KY,TD,DG", 1.35),
            ("Wales", "CF,NP,LD,SY,LL,SA", 1.25),
            ("South West", "BS,GL,HR,PL,EX,TR,SN", 1.15),
            ("East of England", "IP,CO,SS,CM,CB,NR,PE", 1.1),
        ]
        for name, prefixes, mult in default_zones:
            db.session.add(Zone(name=name, outward_prefixes=prefixes, multiplier=mult))

    if Surcharge.query.count() == 0:
        default_surcharges = [
            ("Residential delivery", 5.0, False), ("Tail lift required", 15.0, False),
            ("Timed delivery slot", 10.0, False), ("Weekend delivery", 20.0, False),
            ("Fragile / high value", 8.0, False), ("Long carry (>20m)", 6.0, False),
            ("Two-person handling", 18.0, False), ("Redelivery", 12.0, False),
            ("Storage per day", 4.0, False), ("Failed collection re-attempt", 10.0, False),
        ]
        for name, amount, is_pct in default_surcharges:
            db.session.add(Surcharge(name=name, default_amount=amount, is_percent=is_pct))

    db.session.commit()

    admin_email = os.environ.get("ADMIN_EMAIL", "admin@heliolink.co")
    admin_password = os.environ.get("ADMIN_PASSWORD", "Heliolink26")
    if not User.query.filter_by(email=admin_email).first():
        admin = User(email=admin_email, name="Admin", role="admin")
        admin.set_password(admin_password)
        db.session.add(admin)
        db.session.commit()
        print(f"[seed] Admin login created: {admin_email} / {admin_password}")

    if os.environ.get("SEED_DEMO", "true").lower() == "true" and Customer.query.count() == 0:
        _seed_demo_data()


def _seed_demo_data():
    c1 = Customer(name="Williams Handbaked", email="ops@williamshandbaked.co.uk", phone="01204 555111",
                   billing_address="Unit 4, Bakery Park, Bolton", pricing_mode="standard", discount_pct=10)
    c2 = Customer(name="Fairgreen International School", email="logistics@fairgreen.edu", phone="0161 555222",
                   billing_address="Fairgreen Campus, Manchester", pricing_mode="flat", flat_rate_per_pallet=22)
    db.session.add_all([c1, c2])
    db.session.commit()

    portal_user = User(email="ops@williamshandbaked.co.uk", name="Williams Handbaked", role="customer",
                        customer_id=c1.id)
    portal_user.set_password("customer123")
    db.session.add(portal_user)

    v1 = Vehicle(registration="HY17 ABC", size="panel_van", capacity_pallets=4, mpg=34, status="available")
    v2 = Vehicle(registration="HY18 DEF", size="2.5t", capacity_pallets=8, mpg=22, status="available")
    v3 = Vehicle(registration="HY19 GHI", size="7.5t", capacity_pallets=16, mpg=14, status="off_road")
    db.session.add_all([v1, v2, v3])
    db.session.commit()

    d1 = Driver(name="Jamie Ellis", phone="07700 900123", pin="1111", status="available",
                current_vehicle_id=v1.id)
    d2 = Driver(name="Priya Anand", phone="07700 900456", pin="2222", status="available",
                current_vehicle_id=v2.id)
    d3 = Driver(name="Marcus Reid", phone="07700 900789", pin="3333", status="off",
                current_vehicle_id=v3.id)
    db.session.add_all([d1, d2, d3])
    db.session.commit()

    settings = RateSettings.query.first()
    zones = Zone.query.all()

    sample_orders = [
        (c1.id, "delivery", "45 Camden High St, London", "NW1 7JR", "Alice Turner", "standard", 2, 0),
        (c1.id, "delivery", "9 Islington Green, London", "N1 2XH", "Ben Hughes", "same_day", 1, 3),
        (c2.id, "delivery", "78 Deansgate, Manchester", "M3 2FW", "Chloe Adams", "48h", 3, 0),
        (c2.id, "collection", "22 Portland St, Manchester", "M1 4GX", "Daniel Foster", "48h", 1, 5),
        (c1.id, "delivery", "12 Corporation St, Birmingham", "B2 4LP", "Emma Clarke", "48h", 4, 0),
    ]
    for customer_id, job_type, address, postcode, contact, timing, pallets, parcels in sample_orders:
        order = Order(
            reference=gen_reference(), customer_id=customer_id, job_type=job_type,
            delivery_address=address, delivery_postcode=postcode,
            collection_address="Hoya Depot, Trafford Park, Manchester", collection_postcode=DEPOT_POSTCODE,
            contact_name=contact, contact_phone="07911 000000", pallets=pallets, parcels=parcels,
            weight_per_pallet_kg=180, timing=timing, delivery_date=date.today() + timedelta(days=1),
        )
        db.session.add(order)
        db.session.flush()
        db.session.add(OrderEvent(order_id=order.id, status="pending", note="Order created (seed)"))
        for i in range(1, pallets + 1):
            db.session.add(OrderBox(order_id=order.id, kind="pallet", label_index=i,
                                     barcode=gen_barcode(order.reference, "pallet", i)))
        for i in range(1, parcels + 1):
            db.session.add(OrderBox(order_id=order.id, kind="parcel", label_index=i,
                                     barcode=gen_barcode(order.reference, "parcel", i)))
        customer = Customer.query.get(customer_id)
        try:
            order.quoted_price = quote_order(order, customer, settings, zones, DEPOT_POSTCODE)
        except Exception:
            order.quoted_price = 45.0
    db.session.commit()
    print("[seed] Demo customers, fleet, drivers and orders created.")


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "true").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
