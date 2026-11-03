"""
Adapter between Hoya's Order/Customer models and pricing.py, Heliolink's
published rate card (postcode band x account tier for pallets/half-pallets,
a "Wavelength" events/distance mode, and a manual/custom mode for bespoke or
multi-drop work). This replaced Hoya's earlier generic cost-based engine.

Every quote returns a dict: {ok, net, vat, total, lines, basis, band, reason}.
"net" already has the 16% fuel surcharge baked in for pallet/wavelength modes
(pricing.py's own convention) and excludes VAT; "total" is net+VAT (gross).
A customer discount_pct, if set, is applied on top of pricing.py's net.
"""
from datetime import datetime, date

import pricing

TIERS = pricing.TIERS

try:
    from zoneinfo import ZoneInfo
    _UK = ZoneInfo("Europe/London")
except Exception:  # pragma: no cover - tz database missing; fall back to server time
    _UK = None


def uk_now():
    return datetime.now(_UK) if _UK else datetime.utcnow()


def is_late_same_day(order, now=None):
    """Rate card: a same-day booking confirmed after 12:00 carries a late-notice fee.
    True when the job is same-day, is for today (or has no date yet), and it's
    now 12:00 or later UK time."""
    if getattr(order, "timing", None) != "same_day":
        return False
    now = now or uk_now()
    d = getattr(order, "delivery_date", None)
    if isinstance(d, str):
        try:
            d = datetime.strptime(d, "%Y-%m-%d").date()
        except ValueError:
            d = None
    if d is not None and d != now.date():
        return False
    return now.hour >= 12


BAND_ORDER = "ABCDEFG"


def pricing_postcode(order):
    """Pick the postcode a pallet job is priced on: whichever of the collection
    and delivery postcodes sits in the HIGHER (further) distance band, so a
    collection from Glossop into Preston is priced as Band B, not Band A.
    Returns (postcode, note, problem). problem is set if a postcode given on
    the job is outside the rate-card coverage."""
    legs = []
    for label, pc in (("collection", getattr(order, "collection_postcode", None)),
                      ("delivery", getattr(order, "delivery_postcode", None))):
        pc = (pc or "").strip().upper()
        if pc and pc != "MULTIPLE":
            legs.append((label, pc, pricing.band_for_postcode(pc)))
    if not legs:
        return "", None, None          # price_pallet asks for a postcode
    for label, pc, band in legs:
        if not band:
            return pc, None, (
                "The %s postcode %s is outside the rate-card coverage (network-partner area). "
                "Change the customer's pricing to Custom and enter the agreed price." % (label, pc))
    label, pc, band = max(legs, key=lambda leg: BAND_ORDER.index(leg[2]))
    note = None
    if len(legs) == 2 and legs[0][2] != legs[1][2]:
        other = [leg for leg in legs if leg[0] != label][0]
        note = ("Priced on the %s postcode %s (Band %s) - further than the %s postcode %s (Band %s)."
                % (label, pc, band, other[0], other[1], other[2]))
    return pc, note, None


def estimate_job_miles(order, mode=None):
    """Estimated miles driven, for our cost view, plus a plain-English reason.
    Wavelength jobs: the round-trip miles entered on the booking.
    Otherwise: depot -> furthest stop and back (2 x the rate card's one-way road
    miles to the further of the collection / delivery postcodes).
    Returns (miles or None, explanation, stops)."""
    drops = len(getattr(order, "drops", None) or [])
    stops = max(2, drops + 1) if getattr(order, "job_type", None) == "multidrop" else 2
    if mode == "wavelength" and getattr(order, "wl_miles", None):
        return float(order.wl_miles), "round-trip miles entered on the booking", stops
    best = None
    for label, pc in (("collection", getattr(order, "collection_postcode", None)),
                      ("delivery", getattr(order, "delivery_postcode", None))):
        pc = (pc or "").strip().upper()
        if pc and pc != "MULTIPLE":
            m = pricing.road_miles_for_postcode(pc)
            if m is not None and (best is None or m > best[2]):
                best = (label, pc, m)
    if best is None:
        return None, "no rate-card mileage for these postcodes - enter the miles", stops
    return float(best[2] * 2), "2 x %d mi to %s (%s, furthest stop)" % (best[2], best[1], best[0]), stops


def quote_for_order(order, customer, surcharges=None, now=None):
    """order: Order instance (unsaved or saved) with pallets/wavelength/manual
    fields already populated. customer: Customer instance. surcharges: list
    of {"label":.., "amount":..} ad-hoc extras (optional, on top of the
    rate-card calc). Returns the pricing.py-style breakdown dict."""
    surcharges = surcharges or []
    mode = (customer.pricing_mode if customer else "pallet") or "pallet"

    if order.job_type == "multidrop":
        q = pricing.price_custom(order.manual_net, drops=len(order.drops or []), multidrop=True)
    elif mode == "wavelength":
        q = pricing.price_wavelength(order.wl_vehicle or "luton", order.wl_miles or 0,
                                      after6=bool(order.wl_after6), surcharges=surcharges)
    elif mode == "custom":
        q = pricing.price_custom(order.manual_net)
    elif mode == "flat":
        net = round((order.pallets or 0) * (customer.flat_rate_per_pallet or 0), 2)
        vat = round(net * pricing.VAT, 2)
        q = {"ok": net > 0, "reason": None if net > 0 else "No pallets to price.",
             "net": net, "vat": vat, "total": net + vat,
             "lines": [("Flat rate - %d pallet(s)" % (order.pallets or 0), net, None)],
             "basis": "Flat rate per pallet", "band": None}
    else:  # "pallet" - published rate card by postcode band + account tier
        postcode, note, problem = pricing_postcode(order)
        if problem:
            return {"ok": False, "reason": problem, "band": None}
        tier = customer.tier if customer else "Standard"
        q = pricing.price_pallet(tier, postcode or "", order.full_pallets or 0, order.half_pallets or 0,
                                  same_day=(order.timing == "same_day"), surcharges=surcharges,
                                  late_notice=is_late_same_day(order, now))
        if q.get("ok") and note:
            q.setdefault("notes", []).insert(0, note)
            q["basis"] += " (%s)" % postcode.upper()

    if q.get("ok") and customer and customer.discount_pct:
        factor = 1 - (customer.discount_pct / 100.0)
        q["net"] = round(q["net"] * factor, 2)
        q["vat"] = round(q["net"] * pricing.VAT, 2)
        q["total"] = round(q["net"] + q["vat"], 2)
        q["basis"] = q["basis"] + (" (-%.0f%%)" % customer.discount_pct)
    elif q.get("ok"):
        q["net"] = round(q["net"], 2)
        q["vat"] = round(q["vat"], 2)
        q["total"] = round(q["total"], 2)
    return q
