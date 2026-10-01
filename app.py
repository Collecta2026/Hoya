"""
Heliolink Delivery Ops - Flask + SQLAlchemy.

Run locally:
    pip install -r requirements.txt
    python app.py
Then open http://localhost:5000  (admin login printed on first run).
"""
import os
import uuid
from datetime import datetime, date, timedelta

from flask import (Flask, render_template, redirect, url_for, request, flash,
                   jsonify, abort)
from flask_login import (LoginManager, login_user, logout_user, login_required,
                         current_user)

from models import db, Employee, Vehicle, Driver, Customer, Order, OrderDrop
import pricing
from emailer import send_email, SALES_EMAIL

STATUS_LABELS = {"pending": "Pending", "assigned": "Assigned", "in_transit": "In transit",
                 "delivered": "Delivered", "collected": "Collected", "failed": "Failed / Exception"}
STATUS_OPTIONS = ["pending", "assigned", "in_transit", "delivered", "failed"]
UNITS = ["Pallet", "Half pallet", "Box", "Parcel", "Basket", "Flight case", "Cage", "Crate", "Item"]
PRIORITIES = ["Next Day", "Same Day", "Economy (2-3 days)", "Standard"]


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------
def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    db_url = os.environ.get("DATABASE_URL", "")
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    if not db_url:
        db_url = "sqlite:///" + os.path.join(os.path.dirname(__file__), "heliolink.db")
    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True}

    db.init_app(app)
    lm = LoginManager()
    lm.login_view = "login"
    lm.init_app(app)

    @lm.user_loader
    def load_user(uid):
        return Employee.query.get(int(uid))

    register_routes(app)
    with app.app_context():
        db.create_all()
        seed_if_needed()
    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def require(*roles):
    if not current_user.is_authenticated or current_user.access not in roles:
        abort(403)


def monday_of(d):
    return d - timedelta(days=d.weekday())


def parse_date(v):
    try:
        return datetime.strptime(v, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def gen_reference(order):
    return "HL-" + order.created_at.strftime("%y%m%d") + "-" + str(order.id).zfill(4)


def parse_quantities(form):
    """Read qty_value[]/qty_unit[] -> (full_pallets, half_pallets, parcels, summary)."""
    values = form.getlist("qty_value")
    units = form.getlist("qty_unit")
    full = half = parcels = 0
    parts = []
    for raw, unit in zip(values, units):
        try:
            n = int(raw)
        except (TypeError, ValueError):
            continue
        if n <= 0 or not unit:
            continue
        label = plural(n, unit)
        parts.append(label)
        if unit == "Pallet":
            full += n
        elif unit == "Half pallet":
            half += n
        else:
            parcels += n
    return full, half, parcels, ", ".join(parts)


def plural(n, unit):
    if n == 1:
        return "%d %s" % (n, unit)
    if unit == "Flight case":
        return "%d flight cases" % n
    if unit == "Half pallet":
        return "%d half pallets" % n
    return "%d %ss" % (n, unit)


def read_surcharges(form):
    labels = form.getlist("sur_label")
    amounts = form.getlist("sur_amount")
    out = []
    for lab, amt in zip(labels, amounts):
        try:
            a = float(amt)
        except (TypeError, ValueError):
            continue
        if a > 0:
            out.append({"label": lab.strip() or "Surcharge", "amount": a})
    return out


def compute_quote(data, customer):
    """data: dict-like with pricing inputs. Returns a pricing breakdown."""
    job_type = data.get("job_type", "delivery")
    override = str(data.get("override", "")).lower() in ("1", "true", "on", "yes")
    multidrop = job_type == "multidrop"
    surcharges = data.get("surcharges") or []
    if override or multidrop:
        drops = int(data.get("drops") or 0)
        return pricing.price_custom(data.get("manual_net"), drops=drops, multidrop=multidrop)
    if customer and customer.pricing_type == "wavelength":
        return pricing.price_wavelength(data.get("vehicle", "luton"),
                                        float(data.get("miles") or 0),
                                        str(data.get("after6", "")).lower() in ("1", "true", "on", "yes"),
                                        surcharges)
    if customer and customer.pricing_type == "custom":
        return pricing.price_custom(data.get("manual_net"))
    tier = data.get("tier") or (customer.tier if customer else "Standard")
    return pricing.price_pallet(tier, data.get("postcode", ""),
                                int(data.get("full") or 0), int(data.get("half") or 0),
                                same_day=str(data.get("same_day", "")).lower() in ("1", "true", "on", "yes"),
                                surcharges=surcharges)


def email_booking(order, event="confirmed"):
    c = order.customer
    labels_url = url_for("order_labels", order_id=order.id, _external=True)
    pod_links = " | ".join('<a href="%s">%s POD</a>' % (
        url_for("order_pod", order_id=order.id, copy=cp, _external=True), cp.title())
        for cp in order.pod_copies())
    dest = (order.delivery_address or "")
    if order.delivery_postcode and order.delivery_postcode != "MULTIPLE":
        dest += " " + order.delivery_postcode
    elif order.is_multidrop:
        dest += " (multiple drops)"
    subject = "Booking %s %s - %s" % (order.reference, event, c.name if c else "")
    body = """
    <h3>Booking {ref} {event}</h3>
    <ul>
      <li><b>Reference:</b> {ref}</li>
      <li><b>Customer:</b> {cust}</li>
      <li><b>Job type:</b> {jt}</li>
      <li><b>Destination:</b> {dest}</li>
      <li><b>Date:</b> {d} {t}</li>
      <li><b>Goods:</b> {goods} - {qty}</li>
      <li><b>Driver:</b> {drv}</li>
      <li><b>Status:</b> {status}</li>
      <li><b>Price:</b> &pound;{total:.2f} inc VAT (&pound;{net:.2f} net)</li>
    </ul>
    <p>Labels: <a href="{labels}">print labels</a><br>POD: {pods}</p>
    <p style="color:#666">All documents carry barcode {ref}.</p>
    """.format(ref=order.reference, event=event, cust=c.name if c else "", jt=order.job_type,
               dest=dest, d=order.delivery_date or "", t=order.delivery_time or "",
               goods=order.goods_category or "", qty=order.quantity_summary or "",
               drv=order.driver.name if order.driver else "Unassigned", status=STATUS_LABELS.get(order.status, order.status),
               total=order.price_total or 0, net=order.price_net or 0, labels=labels_url, pods=pod_links)
    send_email(SALES_EMAIL, subject, body)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
def register_routes(app):

    @app.context_processor
    def inject():
        return dict(STATUS_LABELS=STATUS_LABELS, STATUS_OPTIONS=STATUS_OPTIONS,
                    UNITS=UNITS, PRIORITIES=PRIORITIES, TIERS=pricing.TIERS, SALES_EMAIL=SALES_EMAIL)

    @app.route("/healthz")
    def healthz():
        return jsonify({"ok": True, "time": datetime.utcnow().isoformat()})

    # ---- auth ----
    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            u = Employee.query.filter_by(email=email).first()
            if u and u.active and u.check_password(request.form.get("password", "")):
                login_user(u)
                return redirect(url_for("schedule"))
            flash("Invalid email or password.", "error")
        return render_template("login.html")

    @app.route("/logout")
    @login_required
    def logout():
        logout_user()
        return redirect(url_for("login"))

    @app.route("/")
    def index():
        return redirect(url_for("schedule") if current_user.is_authenticated else url_for("login"))

    # ---- schedule (weekly calendar) ----
    @app.route("/schedule")
    @login_required
    def schedule():
        require("admin", "dispatcher")
        ref = parse_date(request.args.get("week")) or date.today()
        start = monday_of(ref)
        days = [start + timedelta(days=i) for i in range(7)]
        orders = Order.query.filter(Order.delivery_date >= start,
                                    Order.delivery_date <= start + timedelta(days=6)).all()
        by_day = {d: [] for d in days}
        for o in orders:
            by_day.get(o.delivery_date, []).append(o)
        for d in by_day:
            by_day[d].sort(key=lambda o: (o.delivery_time or "99:99"))
        return render_template("schedule.html", days=days, by_day=by_day, today=date.today(),
                               week_start=start, week_end=start + timedelta(days=6),
                               prev_week=(start - timedelta(days=7)).isoformat(),
                               next_week=(start + timedelta(days=7)).isoformat(),
                               this_week=date.today().isoformat())

    # ---- booking ----
    @app.route("/booking", methods=["GET", "POST"])
    @login_required
    def booking():
        require("admin", "dispatcher")
        if request.method == "POST":
            order = create_order(request.form)
            email_booking(order, "confirmed")
            flash("Booking %s confirmed - sales@ notified, labels & POD generated." % order.reference, "success")
            return redirect(url_for("schedule", week=(order.delivery_date or date.today()).isoformat()))
        customers = Customer.query.order_by(Customer.name).all()
        drivers = Driver.query.order_by(Driver.name).all()
        vehicles = Vehicle.query.order_by(Vehicle.reg).all()
        return render_template("booking.html", customers=customers, drivers=drivers, vehicles=vehicles,
                               today=date.today().isoformat(),
                               preselect_date=request.args.get("date") or date.today().isoformat(),
                               preselect_customer=request.args.get("customer_id", type=int))

    def create_order(form):
        customer = Customer.query.get(int(form["customer_id"]))
        full, half, parcels, summary = parse_quantities(form)
        job_type = form.get("job_type", "delivery")
        multidrop = job_type == "multidrop"
        goods = (form.get("goods_category") or "").strip() or (customer.main_product if customer else "")
        order = Order(
            reference="TMP-" + uuid.uuid4().hex[:8], customer_id=customer.id, job_type=job_type,
            goods_category=goods or None, goods_description=(form.get("goods_description") or "").strip() or None,
            quantity_summary=summary or None, full_pallets=full, half_pallets=half, parcels=parcels,
            delivery_address=(form.get("delivery_address") or "").strip(),
            delivery_postcode="MULTIPLE" if multidrop else (form.get("delivery_postcode") or "").strip().upper(),
            delivery_date=parse_date(form.get("delivery_date")), delivery_time=(form.get("delivery_time") or "").strip() or None,
            priority=form.get("priority", "Next Day"), same_day=form.get("same_day") == "on",
            notes=(form.get("notes") or "").strip() or None,
        )
        did = form.get("driver_id") or None
        vid = form.get("vehicle_id") or None
        order.driver_id = int(did) if did else None
        order.vehicle_id = int(vid) if vid else None
        order.status = "assigned" if order.driver_id else "pending"

        q = compute_quote({
            "job_type": job_type, "override": form.get("override"), "manual_net": form.get("manual_net"),
            "tier": form.get("tier"), "postcode": order.delivery_postcode, "full": full, "half": half,
            "same_day": form.get("same_day"), "vehicle": form.get("wl_vehicle"), "miles": form.get("wl_miles"),
            "after6": form.get("wl_after6"), "surcharges": read_surcharges(form),
        }, customer)
        if q.get("ok"):
            order.price_net, order.price_vat, order.price_total, order.price_basis = \
                q["net"], q["vat"], q["total"], q["basis"]

        db.session.add(order)
        db.session.flush()
        order.reference = gen_reference(order)
        db.session.commit()
        return order

    @app.route("/api/quote", methods=["POST"])
    @login_required
    def api_quote():
        require("admin", "dispatcher")
        data = request.get_json(force=True)
        customer = Customer.query.get(data.get("customer_id")) if data.get("customer_id") else None
        q = compute_quote(data, customer)
        if not q.get("ok"):
            return jsonify({"ok": False, "reason": q.get("reason", "Add details to price."), "band": q.get("band")})
        return jsonify({"ok": True, "band": q.get("band"), "basis": q["basis"],
                        "net": q["net"], "vat": q["vat"], "total": q["total"],
                        "lines": [{"k": l[0], "v": l[1], "sub": l[2]} for l in q["lines"]]})

    # ---- order detail + status ----
    @app.route("/orders/<int:order_id>")
    @login_required
    def order_detail(order_id):
        require("admin", "dispatcher")
        order = Order.query.get_or_404(order_id)
        return render_template("order_detail.html", order=order)

    @app.route("/orders/<int:order_id>/status", methods=["POST"])
    @login_required
    def order_status(order_id):
        require("admin", "dispatcher")
        order = Order.query.get_or_404(order_id)
        st = request.form.get("status")
        if st in STATUS_LABELS:
            order.status = st
            db.session.commit()
            email_booking(order, "updated to %s" % STATUS_LABELS[st])
            flash("Status -> %s - %s notified." % (STATUS_LABELS[st], SALES_EMAIL), "success")
        return redirect(request.referrer or url_for("order_detail", order_id=order.id))

    # ---- billing ----
    @app.route("/billing")
    @login_required
    def billing():
        require("admin", "dispatcher")
        f = request.args.get("filter", "all")
        orders = Order.query.order_by(Order.delivery_date.desc().nullslast(), Order.id.desc()).all()
        shown = [o for o in orders if f == "all" or o.bill_status == f]
        totals = {
            "count": len(orders),
            "total": sum(o.price_total or 0 for o in orders),
            "outstanding": sum(o.price_total or 0 for o in orders if o.bill_status != "paid"),
            "paid": sum(o.price_total or 0 for o in orders if o.bill_status == "paid"),
        }
        return render_template("billing.html", orders=shown, totals=totals, flt=f)

    @app.route("/billing/<int:order_id>/cycle", methods=["POST"])
    @login_required
    def billing_cycle(order_id):
        require("admin", "dispatcher")
        order = Order.query.get_or_404(order_id)
        seq = ["quoted", "invoiced", "paid"]
        order.bill_status = seq[(seq.index(order.bill_status) + 1) % 3]
        db.session.commit()
        return redirect(url_for("billing", filter=request.args.get("filter", "all")))

    # ---- route planner ----
    @app.route("/routes")
    @login_required
    def routes():
        require("admin", "dispatcher")
        drivers = Driver.query.order_by(Driver.name).all()
        did = request.args.get("driver_id", type=int) or (drivers[0].id if drivers else None)
        run_date = parse_date(request.args.get("date")) or date.today()
        jobs = Order.query.filter(Order.driver_id == did, Order.delivery_date == run_date)\
            .order_by(Order.delivery_time).all() if did else []
        entries = []
        for j in jobs:
            if j.is_multidrop and j.drops:
                for dp in j.drops:
                    entries.append({"job": j, "drop": dp})
            else:
                entries.append({"job": j, "drop": None})
        md_jobs = Order.query.filter_by(job_type="multidrop").order_by(Order.id.desc()).all()
        sel_md = request.args.get("md", type=int) or (md_jobs[0].id if md_jobs else None)
        sel_md_job = Order.query.get(sel_md) if sel_md else None
        return render_template("routes.html", drivers=drivers, did=did, run_date=run_date,
                               entries=entries, md_jobs=md_jobs, sel_md=sel_md, sel_md_job=sel_md_job)

    @app.route("/routes/<int:order_id>/drop", methods=["POST"])
    @login_required
    def route_add_drop(order_id):
        require("admin", "dispatcher")
        order = Order.query.get_or_404(order_id)
        pc = (request.form.get("postcode") or "").strip().upper()
        if pc:
            seq = (max([d.seq for d in order.drops]) + 1) if order.drops else 1
            db.session.add(OrderDrop(order_id=order.id, postcode=pc,
                                     name=(request.form.get("name") or "").strip(), seq=seq))
            db.session.commit()
        return redirect(url_for("routes", driver_id=order.driver_id or None,
                                date=(order.delivery_date or date.today()).isoformat(), md=order.id))

    @app.route("/routes/drop/<int:drop_id>/delete", methods=["POST"])
    @login_required
    def route_del_drop(drop_id):
        require("admin", "dispatcher")
        dp = OrderDrop.query.get_or_404(drop_id)
        oid = dp.order_id
        order = dp.order
        db.session.delete(dp)
        db.session.commit()
        return redirect(url_for("routes", driver_id=order.driver_id or None,
                                date=(order.delivery_date or date.today()).isoformat(), md=oid))

    # ---- labels ----
    @app.route("/labels")
    @login_required
    def labels():
        require("admin", "dispatcher")
        mode = request.args.get("mode", "date")
        if mode == "single" and request.args.get("order_id", type=int):
            orders = [Order.query.get_or_404(request.args.get("order_id", type=int))]
        else:
            d = parse_date(request.args.get("date")) or date.today()
            orders = Order.query.filter_by(delivery_date=d).all()
        all_orders = Order.query.order_by(Order.id.desc()).all()
        return render_template("labels.html", orders=orders, all_orders=all_orders,
                               mode=mode, date=(request.args.get("date") or date.today().isoformat()),
                               order_id=request.args.get("order_id", type=int))

    @app.route("/orders/<int:order_id>/labels")
    @login_required
    def order_labels(order_id):
        require("admin", "dispatcher")
        return redirect(url_for("labels", mode="single", order_id=order_id))

    # ---- POD ----
    @app.route("/orders/<int:order_id>/pod")
    @login_required
    def order_pod(order_id):
        require("admin", "dispatcher")
        order = Order.query.get_or_404(order_id)
        copy = request.args.get("copy", "delivery")
        copies = [copy] if copy in ("collection", "delivery") else order.pod_copies()
        return render_template("pod.html", order=order, copies=copies)

    # ---- customers ----
    @app.route("/customers")
    @login_required
    def customers():
        require("admin", "dispatcher")
        return render_template("customers.html", customers=Customer.query.order_by(Customer.name).all())

    @app.route("/customers/save", methods=["POST"])
    @login_required
    def customer_save():
        require("admin", "dispatcher")
        cid = request.form.get("id", type=int)
        c = Customer.query.get(cid) if cid else Customer()
        c.name = request.form["name"].strip()
        c.main_product = (request.form.get("main_product") or "").strip() or None
        c.pricing_type = request.form.get("pricing_type", "pallet")
        c.tier = request.form.get("tier", "Standard")
        c.postcode = (request.form.get("postcode") or "").strip().upper() or None
        c.address = (request.form.get("address") or "").strip() or None
        c.email = (request.form.get("email") or "").strip() or None
        c.phone = (request.form.get("phone") or "").strip() or None
        if not cid:
            db.session.add(c)
        db.session.commit()
        flash("Customer saved.", "success")
        return redirect(url_for("customers"))

    @app.route("/customers/<int:cid>/delete", methods=["POST"])
    @login_required
    def customer_delete(cid):
        require("admin", "dispatcher")
        db.session.delete(Customer.query.get_or_404(cid))
        db.session.commit()
        flash("Customer removed.", "success")
        return redirect(url_for("customers"))

    # ---- fleet ----
    @app.route("/fleet")
    @login_required
    def fleet():
        require("admin", "dispatcher")
        return render_template("fleet.html", drivers=Driver.query.order_by(Driver.name).all(),
                               vehicles=Vehicle.query.order_by(Vehicle.reg).all())

    @app.route("/fleet/driver/save", methods=["POST"])
    @login_required
    def driver_save():
        require("admin", "dispatcher")
        did = request.form.get("id", type=int)
        d = Driver.query.get(did) if did else Driver()
        d.name = request.form["name"].strip()
        d.phone = (request.form.get("phone") or "").strip() or None
        d.pin = (request.form.get("pin") or d.pin or "0000").strip()
        vid = request.form.get("vehicle_id") or None
        d.vehicle_id = int(vid) if vid else None
        d.status = request.form.get("status", "available")
        if not did:
            db.session.add(d)
        db.session.commit()
        flash("Driver saved.", "success")
        return redirect(url_for("fleet"))

    @app.route("/fleet/driver/<int:did>/delete", methods=["POST"])
    @login_required
    def driver_delete(did):
        require("admin", "dispatcher")
        db.session.delete(Driver.query.get_or_404(did))
        db.session.commit()
        flash("Driver removed.", "success")
        return redirect(url_for("fleet"))

    @app.route("/fleet/vehicle/save", methods=["POST"])
    @login_required
    def vehicle_save():
        require("admin", "dispatcher")
        vid = request.form.get("id", type=int)
        v = Vehicle.query.get(vid) if vid else Vehicle()
        v.reg = request.form["reg"].strip().upper()
        v.size = request.form.get("size", "Van").strip()
        v.capacity_pallets = request.form.get("capacity_pallets", type=int) or 4
        v.status = request.form.get("status", "available")
        if not vid:
            db.session.add(v)
        db.session.commit()
        flash("Vehicle saved.", "success")
        return redirect(url_for("fleet"))

    @app.route("/fleet/vehicle/<int:vid>/delete", methods=["POST"])
    @login_required
    def vehicle_delete(vid):
        require("admin", "dispatcher")
        db.session.delete(Vehicle.query.get_or_404(vid))
        db.session.commit()
        flash("Vehicle removed.", "success")
        return redirect(url_for("fleet"))

    # ---- employees ----
    @app.route("/employees")
    @login_required
    def employees():
        require("admin")
        return render_template("employees.html", employees=Employee.query.order_by(Employee.name).all())

    @app.route("/employees/save", methods=["POST"])
    @login_required
    def employee_save():
        require("admin")
        eid = request.form.get("id", type=int)
        e = Employee.query.get(eid) if eid else Employee()
        e.name = request.form["name"].strip()
        e.email = request.form["email"].strip().lower()
        e.access = request.form.get("access", "dispatcher")
        e.emp_type = request.form.get("emp_type", "Employee")
        pw = request.form.get("password")
        if pw or not eid:
            e.set_password(pw or "changeme123")
        if not eid:
            db.session.add(e)
        db.session.commit()
        flash("Employee saved.", "success")
        return redirect(url_for("employees"))

    @app.route("/employees/<int:eid>/toggle", methods=["POST"])
    @login_required
    def employee_toggle(eid):
        require("admin")
        e = Employee.query.get_or_404(eid)
        if e.id != current_user.id:
            e.active = not e.active
            db.session.commit()
        return redirect(url_for("employees"))

    @app.route("/employees/<int:eid>/delete", methods=["POST"])
    @login_required
    def employee_delete(eid):
        require("admin")
        e = Employee.query.get_or_404(eid)
        if e.id != current_user.id:
            db.session.delete(e)
            db.session.commit()
        return redirect(url_for("employees"))


# ---------------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------------
def seed_if_needed():
    admin_email = os.environ.get("ADMIN_EMAIL", "ali@heliolink.co.uk")
    admin_pw = os.environ.get("ADMIN_PASSWORD", "Heliolink26")
    if not Employee.query.filter_by(email=admin_email).first():
        a = Employee(name="Ali Saleh", email=admin_email, access="admin", emp_type="Employee")
        a.set_password(admin_pw)
        db.session.add(a)
        db.session.commit()
        print("[seed] Admin login: %s / %s" % (admin_email, admin_pw))

    if os.environ.get("SEED_DEMO", "true").lower() == "true" and Customer.query.count() == 0:
        v1 = Vehicle(reg="HY17 ABC", size="3.5t Luton", capacity_pallets=6)
        v2 = Vehicle(reg="HY18 DEF", size="3.5t Maxus", capacity_pallets=8)
        db.session.add_all([v1, v2])
        db.session.commit()
        d1 = Driver(name="Jamie Ellis", phone="07700 900123", pin="1111", vehicle_id=v1.id)
        d2 = Driver(name="Priya Anand", phone="07700 900456", pin="2222", vehicle_id=v2.id, status="on_route")
        db.session.add_all([d1, d2])
        cs = [
            Customer(name="Williams Handbaked", main_product="Confectionery", pricing_type="pallet", tier="Standard", postcode="BL1 4AB"),
            Customer(name="Wavelength Records Ltd", main_product="Event equipment", pricing_type="wavelength", postcode="M3 2FW"),
            Customer(name="Northgate Interiors", main_product="Furniture", pricing_type="pallet", tier="Trade", postcode="LS1 5AA"),
            Customer(name="Lakeland Retail Group", main_product="Mixed retail stock", pricing_type="custom", postcode="LA1 1AA"),
        ]
        db.session.add_all(cs)
        db.session.commit()
        print("[seed] Demo customers, drivers and vehicles created.")


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG", "true").lower() == "true")
