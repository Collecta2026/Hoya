"""
Heliolink Delivery Ops - SQLAlchemy models.
SQLite locally, Postgres (Neon) in production via DATABASE_URL.
"""
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


def now():
    return datetime.utcnow()


class Employee(UserMixin, db.Model):
    """Staff / contractor sign-in and access control."""
    __tablename__ = "employees"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    access = db.Column(db.String(20), default="dispatcher")   # admin | dispatcher | driver
    emp_type = db.Column(db.String(20), default="Employee")   # Employee | Contractor
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=now)

    def set_password(self, raw):
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw):
        return check_password_hash(self.password_hash, raw)

    @property
    def is_admin(self):
        return self.access == "admin"


class Vehicle(db.Model):
    __tablename__ = "vehicles"
    id = db.Column(db.Integer, primary_key=True)
    reg = db.Column(db.String(20), nullable=False)
    size = db.Column(db.String(40), default="3.5t Luton")
    capacity_pallets = db.Column(db.Integer, default=6)
    status = db.Column(db.String(20), default="available")   # available | on_route | off_road
    created_at = db.Column(db.DateTime, default=now)


class Driver(db.Model):
    __tablename__ = "drivers"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    phone = db.Column(db.String(50))
    pin = db.Column(db.String(10), default="0000")
    vehicle_id = db.Column(db.Integer, db.ForeignKey("vehicles.id"))
    status = db.Column(db.String(20), default="available")   # available | on_route | off
    created_at = db.Column(db.DateTime, default=now)
    vehicle = db.relationship("Vehicle")


class Customer(db.Model):
    __tablename__ = "customers"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    main_product = db.Column(db.String(120))
    pricing_type = db.Column(db.String(20), default="pallet")   # pallet | wavelength | custom
    tier = db.Column(db.String(20), default="Standard")         # pallet tier
    postcode = db.Column(db.String(12))
    address = db.Column(db.String(500))
    email = db.Column(db.String(255))
    phone = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=now)


class Order(db.Model):
    __tablename__ = "orders"
    id = db.Column(db.Integer, primary_key=True)
    reference = db.Column(db.String(30), unique=True, nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False)

    job_type = db.Column(db.String(20), default="delivery")    # delivery | collection | both | multidrop
    goods_category = db.Column(db.String(120))
    goods_description = db.Column(db.Text)
    quantity_summary = db.Column(db.String(255))
    full_pallets = db.Column(db.Integer, default=0)
    half_pallets = db.Column(db.Integer, default=0)
    parcels = db.Column(db.Integer, default=0)

    delivery_address = db.Column(db.String(500))   # also holds "region" for multidrop
    delivery_postcode = db.Column(db.String(20))   # "MULTIPLE" for multidrop

    delivery_date = db.Column(db.Date)
    delivery_time = db.Column(db.String(10))
    priority = db.Column(db.String(30), default="Next Day")
    same_day = db.Column(db.Boolean, default=False)

    driver_id = db.Column(db.Integer, db.ForeignKey("drivers.id"))
    vehicle_id = db.Column(db.Integer, db.ForeignKey("vehicles.id"))
    status = db.Column(db.String(20), default="pending")       # pending|assigned|in_transit|delivered|collected|failed
    notes = db.Column(db.Text)

    price_net = db.Column(db.Float, default=0.0)
    price_vat = db.Column(db.Float, default=0.0)
    price_total = db.Column(db.Float, default=0.0)
    price_basis = db.Column(db.String(120))
    bill_status = db.Column(db.String(20), default="quoted")    # quoted | invoiced | paid

    created_at = db.Column(db.DateTime, default=now)
    updated_at = db.Column(db.DateTime, default=now, onupdate=now)

    customer = db.relationship("Customer")
    driver = db.relationship("Driver")
    vehicle = db.relationship("Vehicle")
    drops = db.relationship("OrderDrop", backref="order", cascade="all, delete-orphan",
                            order_by="OrderDrop.seq")

    @property
    def pallet_spaces(self):
        return (self.full_pallets or 0) + (self.half_pallets or 0)

    @property
    def is_multidrop(self):
        return self.job_type == "multidrop"

    def boxes(self):
        """Label boxes - one barcode per ORDER (the reference) on every box."""
        out = []
        for i in range(1, (self.full_pallets or 0) + (self.half_pallets or 0) + 1):
            out.append(("Pallet", i, self.reference))
        for i in range(1, (self.parcels or 0) + 1):
            out.append(("Parcel", i, self.reference))
        if not out:
            out.append(("Item", 1, self.reference))
        return out

    def pod_copies(self):
        if self.job_type == "both":
            return ["collection", "delivery"]
        return ["collection"] if self.job_type == "collection" else ["delivery"]


class OrderDrop(db.Model):
    """A single drop on a multi-drop round (planned in the route planner)."""
    __tablename__ = "order_drops"
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), nullable=False)
    postcode = db.Column(db.String(20), nullable=False)
    name = db.Column(db.String(255))
    seq = db.Column(db.Integer, default=0)
