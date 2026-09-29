"""Database models for HelioOps — Heliolink delivery operations platform."""
from datetime import datetime, date
from werkzeug.security import generate_password_hash, check_password_hash
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(180), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    # role: admin | dispatcher | driver
    role = db.Column(db.String(20), nullable=False, default="dispatcher")
    phone = db.Column(db.String(40))
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    driver_profile = db.relationship("Driver", back_populates="user", uselist=False)

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)


class Driver(db.Model):
    __tablename__ = "drivers"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True)
    licence_no = db.Column(db.String(60))
    phone = db.Column(db.String(40))
    active = db.Column(db.Boolean, default=True)
    # available | on_route | off
    status = db.Column(db.String(12), default="available")

    user = db.relationship("User", back_populates="driver_profile")
    routes = db.relationship("Route", back_populates="driver")


class Vehicle(db.Model):
    __tablename__ = "vehicles"
    id = db.Column(db.Integer, primary_key=True)
    reg = db.Column(db.String(20), unique=True, nullable=False)
    make_model = db.Column(db.String(120))
    # panel van | 2.5t | 7.5t
    vtype = db.Column(db.String(30), default="panel van")
    capacity_pallets = db.Column(db.Integer, default=0)
    mpg = db.Column(db.Float, default=0.0)   # average fuel consumption
    active = db.Column(db.Boolean, default=True)
    # available | on_route | off_road
    status = db.Column(db.String(12), default="available")

    routes = db.relationship("Route", back_populates="vehicle")


class Customer(db.Model):
    __tablename__ = "customers"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    contact_name = db.Column(db.String(120))
    email = db.Column(db.String(180))
    phone = db.Column(db.String(40))
    address = db.Column(db.String(300))
    postcode = db.Column(db.String(12))
    # per-customer rate settings
    rate_base = db.Column(db.Float, default=0.0)          # £ per drop
    rate_per_mile = db.Column(db.Float, default=0.0)      # £ per mile
    rate_per_pallet = db.Column(db.Float, default=0.0)    # £ per pallet
    rate_per_kg = db.Column(db.Float, default=0.0)        # £ per kg
    min_charge = db.Column(db.Float, default=0.0)         # minimum charge per shipment
    fuel_surcharge_pct = db.Column(db.Float, default=15.0)  # % on carriage
    # pricing_mode: flat (use the £ rates above) | standard (cost engine + discount)
    pricing_mode = db.Column(db.String(10), default="flat")
    discount_pct = db.Column(db.Float, default=0.0)       # discount off standard rate
    portal_code = db.Column(db.String(24), unique=True)   # self-order access code
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    orders = db.relationship("Order", back_populates="customer")
    invoices = db.relationship("Invoice", back_populates="customer")


class Route(db.Model):
    __tablename__ = "routes"
    id = db.Column(db.Integer, primary_key=True)
    ref = db.Column(db.String(30), unique=True)
    run_date = db.Column(db.Date, default=date.today)
    name = db.Column(db.String(120))
    driver_id = db.Column(db.Integer, db.ForeignKey("drivers.id"))
    vehicle_id = db.Column(db.Integer, db.ForeignKey("vehicles.id"))
    # planned | dispatched | completed
    status = db.Column(db.String(20), default="planned")
    start_postcode = db.Column(db.String(12), default="PR2 2TE")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    driver = db.relationship("Driver", back_populates="routes")
    vehicle = db.relationship("Vehicle", back_populates="routes")
    stops = db.relationship("Stop", back_populates="route",
                            order_by="Stop.sequence",
                            cascade="all, delete-orphan")


class Order(db.Model):
    __tablename__ = "orders"
    id = db.Column(db.Integer, primary_key=True)
    ref = db.Column(db.String(30), unique=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"))
    # delivery details
    recipient = db.Column(db.String(160))
    phone = db.Column(db.String(40))
    address = db.Column(db.String(300))
    postcode = db.Column(db.String(12))
    # explicit collection + delivery legs (for billing / labels)
    collection_address = db.Column(db.String(300))
    collection_postcode = db.Column(db.String(12))
    delivery_address = db.Column(db.String(300))
    delivery_postcode = db.Column(db.String(12))
    pallets = db.Column(db.Integer, default=1)
    weight_kg = db.Column(db.Float, default=0)
    # per-pallet dimensions (cm) and weight (kg) — booking sheet detail
    pallet_length = db.Column(db.Float, default=0)
    pallet_width = db.Column(db.Float, default=0)
    pallet_height = db.Column(db.Float, default=0)
    weight_per_pallet = db.Column(db.Float, default=0)
    notes = db.Column(db.Text)
    service = db.Column(db.String(30), default="next-day")  # same-day | next-day | am | pm
    # scheduling
    collection_date = db.Column(db.Date)
    collection_time = db.Column(db.String(5))   # "HH:MM"
    delivery_date = db.Column(db.Date)
    timing = db.Column(db.String(12), default="booked")  # same-day | 48h | booked
    # delivery | collection
    job_type = db.Column(db.String(12), default="delivery")
    # unassigned | assigned | out | delivered | failed | collected
    status = db.Column(db.String(20), default="unassigned")
    price = db.Column(db.Float, default=0.0)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"))
    # resource allocation (pre-route, shown on the control board)
    assigned_driver_id = db.Column(db.Integer, db.ForeignKey("drivers.id"))
    assigned_vehicle_id = db.Column(db.Integer, db.ForeignKey("vehicles.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    customer = db.relationship("Customer", back_populates="orders")
    stop = db.relationship("Stop", back_populates="order", uselist=False)
    items = db.relationship("Item", back_populates="order",
                            cascade="all, delete-orphan")
    surcharges = db.relationship("Surcharge", secondary="order_surcharges")
    invoice = db.relationship("Invoice", back_populates="orders")
    assigned_driver = db.relationship("Driver", foreign_keys=[assigned_driver_id])
    assigned_vehicle = db.relationship("Vehicle", foreign_keys=[assigned_vehicle_id])

    @property
    def pod(self):
        return self.stop.pod if self.stop else None

    @property
    def board_status(self):
        if self.status in ("delivered", "collected", "failed"):
            return "done"
        if self.status == "out":
            return "on route"
        if self.assigned_driver_id or self.assigned_vehicle_id:
            return "allocated"
        return "unallocated"

    @property
    def parcels(self):
        return sum(1 for i in self.items if i.kind == "parcel")

    @property
    def pallet_count(self):
        return sum(1 for i in self.items if i.kind == "pallet")

    @property
    def scanned_count(self):
        return sum(1 for i in self.items
                   if i.status in ("delivered", "collected", "loaded"))


class Stop(db.Model):
    __tablename__ = "stops"
    id = db.Column(db.Integer, primary_key=True)
    route_id = db.Column(db.Integer, db.ForeignKey("routes.id"))
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"))
    sequence = db.Column(db.Integer, default=0)
    # pending | arrived | delivered | failed
    status = db.Column(db.String(20), default="pending")
    eta = db.Column(db.String(10))
    completed_at = db.Column(db.DateTime)

    route = db.relationship("Route", back_populates="stops")
    order = db.relationship("Order", back_populates="stop")
    pod = db.relationship("POD", back_populates="stop", uselist=False,
                          cascade="all, delete-orphan")


class POD(db.Model):
    __tablename__ = "pods"
    id = db.Column(db.Integer, primary_key=True)
    stop_id = db.Column(db.Integer, db.ForeignKey("stops.id"), unique=True)
    signed_by = db.Column(db.String(120))
    notes = db.Column(db.Text)
    gps = db.Column(db.String(60))
    # images stored in DB so they survive Render redeploys (ephemeral disk).
    photo = db.Column(db.LargeBinary)
    photo_mime = db.Column(db.String(40))
    signature = db.Column(db.LargeBinary)   # PNG data-url decoded
    outcome = db.Column(db.String(20), default="delivered")  # delivered | failed
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    stop = db.relationship("Stop", back_populates="pod")


class Item(db.Model):
    """A scannable unit on an order — a parcel or a pallet."""
    __tablename__ = "items"
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"))
    barcode = db.Column(db.String(60), index=True)
    kind = db.Column(db.String(10), default="parcel")   # parcel | pallet
    description = db.Column(db.String(160))
    # expected | loaded | delivered | collected | missing | returned
    status = db.Column(db.String(20), default="expected")
    scanned_at = db.Column(db.DateTime)

    order = db.relationship("Order", back_populates="items")


class Scan(db.Model):
    """Audit trail of every barcode scan event."""
    __tablename__ = "scans"
    id = db.Column(db.Integer, primary_key=True)
    barcode = db.Column(db.String(60), index=True)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"))
    stop_id = db.Column(db.Integer, db.ForeignKey("stops.id"))
    route_id = db.Column(db.Integer, db.ForeignKey("routes.id"))
    driver_id = db.Column(db.Integer, db.ForeignKey("drivers.id"))
    scan_type = db.Column(db.String(10))   # load | deliver | collect
    result = db.Column(db.String(12))      # matched | unknown | duplicate
    gps = db.Column(db.String(60))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class SmsLog(db.Model):
    __tablename__ = "sms_log"
    id = db.Column(db.Integer, primary_key=True)
    to = db.Column(db.String(40))
    body = db.Column(db.Text)
    status = db.Column(db.String(20))   # sent | logged | failed
    order_ref = db.Column(db.String(30))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class RateSettings(db.Model):
    """Global standard rate-card parameters (single row, id=1)."""
    __tablename__ = "rate_settings"
    id = db.Column(db.Integer, primary_key=True)
    fuel_price_per_litre = db.Column(db.Float, default=1.45)
    driver_rate_per_hour = db.Column(db.Float, default=18.0)
    avg_speed_mph = db.Column(db.Float, default=40.0)
    handling_min_per_pallet = db.Column(db.Float, default=6.0)
    fixed_cost_per_job = db.Column(db.Float, default=8.0)
    margin_pct = db.Column(db.Float, default=30.0)
    service_uplift_24h_pct = db.Column(db.Float, default=20.0)
    round_trip = db.Column(db.Boolean, default=True)
    # fallback fuel consumption (mpg) by van type when a vehicle has none set
    mpg_panel = db.Column(db.Float, default=30.0)
    mpg_25t = db.Column(db.Float, default=22.0)
    mpg_75t = db.Column(db.Float, default=14.0)


class Zone(db.Model):
    """UK region → cost multiplier (remote regions can cost more)."""
    __tablename__ = "zones"
    id = db.Column(db.Integer, primary_key=True)
    region = db.Column(db.String(40), unique=True)
    multiplier = db.Column(db.Float, default=1.0)


class Surcharge(db.Model):
    """Catalogue of accessorial surcharges (industry standard)."""
    __tablename__ = "surcharges"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), unique=True)
    name = db.Column(db.String(120))
    # kind: flat (£/shipment) | per_pallet (£ x pallets) | percent (% of carriage)
    kind = db.Column(db.String(12), default="flat")
    amount = db.Column(db.Float, default=0.0)
    active = db.Column(db.Boolean, default=True)

    def compute(self, base_carriage, pallets):
        if self.kind == "flat":
            return round(self.amount, 2)
        if self.kind == "per_pallet":
            return round(self.amount * (pallets or 1), 2)
        if self.kind == "percent":
            return round(base_carriage * self.amount / 100.0, 2)
        return 0.0


order_surcharges = db.Table(
    "order_surcharges",
    db.Column("order_id", db.Integer, db.ForeignKey("orders.id"), primary_key=True),
    db.Column("surcharge_id", db.Integer, db.ForeignKey("surcharges.id"), primary_key=True),
)


class Invoice(db.Model):
    __tablename__ = "invoices"
    id = db.Column(db.Integer, primary_key=True)
    ref = db.Column(db.String(30), unique=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"))
    period_start = db.Column(db.Date)
    period_end = db.Column(db.Date)
    issue_date = db.Column(db.Date, default=date.today)
    status = db.Column(db.String(12), default="draft")   # draft | sent | paid
    carriage = db.Column(db.Float, default=0.0)          # sum of base carriage
    surcharge_total = db.Column(db.Float, default=0.0)   # accessorials (ex fuel)
    fuel_surcharge_pct = db.Column(db.Float, default=15.0)
    fuel_surcharge = db.Column(db.Float, default=0.0)
    subtotal = db.Column(db.Float, default=0.0)          # ex VAT
    vat = db.Column(db.Float, default=0.0)
    total = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    customer = db.relationship("Customer", back_populates="invoices")
    orders = db.relationship("Order", back_populates="invoice")
    lines = db.relationship("InvoiceLine", back_populates="invoice",
                            cascade="all, delete-orphan")


class InvoiceLine(db.Model):
    __tablename__ = "invoice_lines"
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"))
    order_ref = db.Column(db.String(30))
    ship_date = db.Column(db.Date)
    collection = db.Column(db.String(320))
    collection_pc = db.Column(db.String(12))
    delivery = db.Column(db.String(320))
    delivery_pc = db.Column(db.String(12))
    pallets = db.Column(db.Integer, default=0)
    weight_kg = db.Column(db.Float, default=0)
    carriage = db.Column(db.Float, default=0.0)
    surcharge_detail = db.Column(db.String(300))
    surcharge_amount = db.Column(db.Float, default=0.0)
    line_total = db.Column(db.Float, default=0.0)
    pod_ref = db.Column(db.String(30))   # order ref, used to build POD hyperlink

    invoice = db.relationship("Invoice", back_populates="lines")
