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
import pricing

TIERS = pricing.TIERS


def quote_for_order(order, customer, surcharges=None):
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
        postcode = order.delivery_postcode if order.job_type != "collection" else order.collection_postcode
        tier = customer.tier if customer else "Standard"
        q = pricing.price_pallet(tier, postcode or "", order.full_pallets or 0, order.half_pallets or 0,
                                  same_day=(order.timing == "same_day"), surcharges=surcharges)

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
