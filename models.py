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

    user = db.relationship("User", back_populates="driver_profile")
    routes = db.relationship("Route", back_populates="driver")


class Vehicle(db.Model):
    __tablename__ = "vehicles"
    id = db.Column(db.Integer, primary_key=True)
    reg = db.Column(db.String(20), unique=True, nullable=False)
    make_model = db.Column(db.String(120))
    # van | 7.5t | 18t | luton | car
    vtype = db.Column(db.String(30), default="van")
    capacity_pallets = db.Column(db.Integer, default=0)
    active = db.Column(db.Boolean, default=True)

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
    portal_code = db.Column(db.String(24), unique=True)   # self-order access code
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    orders = db.relationship("Order", back_populates="customer")


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
    pallets = db.Column(db.Integer, default=1)
    weight_kg = db.Column(db.Float, default=0)
    notes = db.Column(db.Text)
    service = db.Column(db.String(30), default="next-day")  # same-day | next-day | am | pm
    # delivery | collection
    job_type = db.Column(db.String(12), default="delivery")
    # unassigned | assigned | out | delivered | failed | collected
    status = db.Column(db.String(20), default="unassigned")
    price = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    customer = db.relationship("Customer", back_populates="orders")
    stop = db.relationship("Stop", back_populates="order", uselist=False)
    items = db.relationship("Item", back_populates="order",
                            cascade="all, delete-orphan")

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
