"""
Hoya (Voya) - Last-mile courier dispatch & delivery ops platform.
SQLAlchemy models. Same architecture pattern as Collecta: Flask + SQLAlchemy,
SQLite locally, Postgres (Neon) in production via DATABASE_URL.
"""
from datetime import datetime, date
from werkzeug.security import generate_password_hash, check_password_hash
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin

db = SQLAlchemy()


def now():
    return datetime.utcnow()


# ---------------------------------------------------------------------------
# Users / auth
# ---------------------------------------------------------------------------

class User(UserMixin, db.Model):
    """Staff-side login: admin or dispatcher. Drivers and customers have their
    own lightweight login records (Driver.pin / Customer.password_hash) but
    also get a User row so flask-login can treat everyone uniformly."""
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    name = db.Column(db.String(255), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="dispatcher")  # admin | dispatcher | driver | customer
    driver_id = db.Column(db.Integer, db.ForeignKey("drivers.id"), nullable=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=True)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=now)

    def set_password(self, raw):
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw):
        return check_password_hash(self.password_hash, raw)

    @property
    def is_admin(self):
        return self.role == "admin"

    @property
    def is_dispatcher(self):
        return self.role in ("admin", "dispatcher")


# ---------------------------------------------------------------------------
# Fleet: drivers & vehicles
# ---------------------------------------------------------------------------

class Vehicle(db.Model):
    __tablename__ = "vehicles"
    id = db.Column(db.Integer, primary_key=True)
    registration = db.Column(db.String(20), nullable=False)
    size = db.Column(db.String(20), nullable=False, default="panel_van")  # panel_van | 2.5t | 7.5t
    capacity_pallets = db.Column(db.Integer, nullable=False, default=4)
    mpg = db.Column(db.Float, default=30.0)
    status = db.Column(db.String(20), nullable=False, default="available")  # available | on_route | off_road
    created_at = db.Column(db.DateTime, default=now)

    SIZE_CAPACITY = {"panel_van": 4, "2.5t": 8, "7.5t": 16}
    SIZE_MPG_DEFAULT = {"panel_van": 34.0, "2.5t": 22.0, "7.5t": 14.0}


class Driver(db.Model):
    __tablename__ = "drivers"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    phone = db.Column(db.String(50))
    pin = db.Column(db.String(10), default="1234")  # simple PIN login for the driver PWA
    status = db.Column(db.String(20), nullable=False, default="available")  # available | on_route | off
    current_vehicle_id = db.Column(db.Integer, db.ForeignKey("vehicles.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=now)

    vehicle = db.relationship("Vehicle", foreign_keys=[current_vehicle_id])


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------

class Customer(db.Model):
    __tablename__ = "customers"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(255))
    phone = db.Column(db.String(50))
    billing_address = db.Column(db.String(500))
    main_product = db.Column(db.String(120))
    # pricing_mode: pallet (published rate card by postcode band + tier) |
    # wavelength (events/distance pricing) | custom (manually agreed net) |
    # flat (legacy fixed £ per pallet, unchanged from earlier Hoya versions)
    pricing_mode = db.Column(db.String(20), default="pallet")
    tier = db.Column(db.String(20), default="Standard")  # Standard | Trade | Key | Major (pallet mode)
    discount_pct = db.Column(db.Float, default=0.0)
    flat_rate_per_pallet = db.Column(db.Float, default=25.0)
    zone_id = db.Column(db.Integer, db.ForeignKey("zones.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=now)

    zone = db.relationship("Zone")


# ---------------------------------------------------------------------------
# Orders / shipments
# ---------------------------------------------------------------------------

class Order(db.Model):
    __tablename__ = "orders"
    id = db.Column(db.Integer, primary_key=True)
    reference = db.Column(db.String(30), unique=True, nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False)

    job_type = db.Column(db.String(20), nullable=False, default="delivery")  # delivery | collection | both | multidrop

    collection_address = db.Column(db.String(500))
    collection_postcode = db.Column(db.String(12))
    delivery_address = db.Column(db.String(500), nullable=False)
    delivery_postcode = db.Column(db.String(12), nullable=False)

    contact_name = db.Column(db.String(255))
    contact_phone = db.Column(db.String(50))
    notes = db.Column(db.Text)

    # pallets/parcels remain the totals used everywhere else (labels, box
    # scanning, weight, dashboards) - full_pallets/half_pallets/parcels break
    # that total down for accurate rate-card pricing and a richer booking form.
    pallets = db.Column(db.Integer, default=0)
    parcels = db.Column(db.Integer, default=0)
    full_pallets = db.Column(db.Integer, default=0)
    half_pallets = db.Column(db.Integer, default=0)
    goods_category = db.Column(db.String(120))
    goods_description = db.Column(db.Text)
    quantity_summary = db.Column(db.String(255))
    weight_per_pallet_kg = db.Column(db.Float, default=0.0)
    pallet_length_cm = db.Column(db.Float, default=120.0)
    pallet_width_cm = db.Column(db.Float, default=100.0)
    pallet_height_cm = db.Column(db.Float, default=150.0)

    # Wavelength (events/distance) pricing inputs
    wl_vehicle = db.Column(db.String(20))      # luton | maxus
    wl_miles = db.Column(db.Float)
    wl_after6 = db.Column(db.Boolean, default=False)
    # Custom / multidrop manually-agreed net price
    manual_net = db.Column(db.Float)

    collection_date = db.Column(db.Date)
    collection_time = db.Column(db.String(10))
    delivery_date = db.Column(db.Date)
    timing = db.Column(db.String(20), default="48h")  # same_day | 48h | booked

    status = db.Column(db.String(20), nullable=False, default="pending")
    # pending | assigned | picked_up | in_transit | delivered | failed | returned | collected

    driver_id = db.Column(db.Integer, db.ForeignKey("drivers.id"), nullable=True)
    vehicle_id = db.Column(db.Integer, db.ForeignKey("vehicles.id"), nullable=True)

    priority = db.Column(db.String(20), default="standard")  # standard | express

    quoted_price = db.Column(db.Float)          # gross total (net + VAT), kept for backward compatibility
    price_net = db.Column(db.Float)             # net of VAT (fuel surcharge baked in for pallet/wavelength modes)
    price_vat = db.Column(db.Float)
    price_basis = db.Column(db.String(120))     # human-readable quote basis, e.g. "Pallet - Band C - Trade"
    invoiced = db.Column(db.Boolean, default=False)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=True)

    # Cash on delivery
    cod_amount = db.Column(db.Float, default=0.0)
    cod_collected = db.Column(db.Boolean, default=False)

    # Lightweight "last known location" ping from the driver's phone (browser
    # geolocation, one-shot per stop open) - not continuous live GPS tracking,
    # but gives dispatch/customers a last-seen position without a paid API.
    last_known_lat = db.Column(db.Float)
    last_known_lng = db.Column(db.Float)
    last_location_at = db.Column(db.DateTime)

    created_at = db.Column(db.DateTime, default=now)
    updated_at = db.Column(db.DateTime, default=now, onupdate=now)

    customer = db.relationship("Customer")
    driver = db.relationship("Driver")
    vehicle = db.relationship("Vehicle")
    boxes = db.relationship("OrderBox", backref="order", cascade="all, delete-orphan")
    events = db.relationship("OrderEvent", backref="order", cascade="all, delete-orphan",
                              order_by="OrderEvent.created_at")
    surcharges = db.relationship("OrderSurcharge", backref="order", cascade="all, delete-orphan")
    drops = db.relationship("OrderDrop", backref="order", cascade="all, delete-orphan",
                             order_by="OrderDrop.seq")

    @property
    def is_multidrop(self):
        return self.job_type == "multidrop"

    @property
    def total_weight_kg(self):
        return round((self.weight_per_pallet_kg or 0) * (self.pallets or 0), 1)

    @property
    def all_boxes_scanned(self):
        if not self.boxes:
            return True
        return all(b.scanned for b in self.boxes)

    @property
    def sla_deadline(self):
        return self.delivery_date


class OrderBox(db.Model):
    """One row per pallet/parcel that gets a barcode label and must be
    scanned at load, delivery and collection."""
    __tablename__ = "order_boxes"
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), nullable=False)
    barcode = db.Column(db.String(64), unique=True, nullable=False)
    kind = db.Column(db.String(10), default="pallet")  # pallet | parcel
    label_index = db.Column(db.Integer, default=1)
    scanned = db.Column(db.Boolean, default=False)
    scanned_at = db.Column(db.DateTime)
    missing = db.Column(db.Boolean, default=False)


class OrderDrop(db.Model):
    """A single drop on a multi-drop round, managed from the route planner."""
    __tablename__ = "order_drops"
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), nullable=False)
    postcode = db.Column(db.String(20), nullable=False)
    name = db.Column(db.String(255))
    seq = db.Column(db.Integer, default=0)


class OrderEvent(db.Model):
    __tablename__ = "order_events"
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), nullable=False)
    status = db.Column(db.String(30), nullable=False)
    note = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=now)


class ProofOfDelivery(db.Model):
    __tablename__ = "proof_of_delivery"
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), unique=True, nullable=False)
    recipient_name = db.Column(db.String(255))
    signature_data_url = db.Column(db.Text)
    photo_data_url = db.Column(db.Text)
    notes = db.Column(db.Text)
    exception_reason = db.Column(db.String(255))
    boxes_confirmed = db.Column(db.Integer, default=0)
    boxes_missing = db.Column(db.Integer, default=0)
    delivered_at = db.Column(db.DateTime, default=now)

    order = db.relationship("Order", backref=db.backref("pod", uselist=False))


# ---------------------------------------------------------------------------
# Rates, zones, surcharges, invoicing
# ---------------------------------------------------------------------------

class Zone(db.Model):
    __tablename__ = "zones"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    outward_prefixes = db.Column(db.String(500))
    multiplier = db.Column(db.Float, default=1.0)


class RateSettings(db.Model):
    __tablename__ = "rate_settings"
    id = db.Column(db.Integer, primary_key=True)
    fuel_price_per_litre = db.Column(db.Float, default=1.55)
    driver_rate_per_hour = db.Column(db.Float, default=15.0)
    avg_speed_mph = db.Column(db.Float, default=35.0)
    handling_min_per_pallet = db.Column(db.Float, default=8.0)
    fixed_cost_per_job = db.Column(db.Float, default=6.0)
    margin_pct = db.Column(db.Float, default=25.0)
    service_uplift_24h_pct = db.Column(db.Float, default=30.0)
    round_trip = db.Column(db.Boolean, default=True)
    fuel_surcharge_pct = db.Column(db.Float, default=8.0)
    vat_pct = db.Column(db.Float, default=20.0)


class Surcharge(db.Model):
    __tablename__ = "surcharges"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    default_amount = db.Column(db.Float, default=0.0)
    is_percent = db.Column(db.Boolean, default=False)


class OrderSurcharge(db.Model):
    __tablename__ = "order_surcharges"
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), nullable=False)
    surcharge_id = db.Column(db.Integer, db.ForeignKey("surcharges.id"), nullable=False)
    amount = db.Column(db.Float, default=0.0)

    surcharge = db.relationship("Surcharge")


class Invoice(db.Model):
    __tablename__ = "invoices"
    id = db.Column(db.Integer, primary_key=True)
    invoice_number = db.Column(db.String(30), unique=True, nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False)
    period_start = db.Column(db.Date)
    period_end = db.Column(db.Date)
    subtotal = db.Column(db.Float, default=0.0)
    fuel_surcharge = db.Column(db.Float, default=0.0)
    vat = db.Column(db.Float, default=0.0)
    total = db.Column(db.Float, default=0.0)
    status = db.Column(db.String(20), default="draft")  # draft | sent | paid
    created_at = db.Column(db.DateTime, default=now)

    customer = db.relationship("Customer")
    orders = db.relationship("Order", backref="invoice", foreign_keys=[Order.invoice_id])
