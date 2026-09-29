"""
Cost-based rate engine. Builds a recommended sell price bottom-up from
round-trip mileage, fuel, driver time, fixed costs, a zone multiplier, an
optional 24h service uplift, and a margin - then compares it against a
customer's flat rate or standard-mode discount.
"""
from optimise import round_trip_miles, region_of


def zone_multiplier_for(postcode, zones):
    area = region_of(postcode)
    for z in zones:
        prefixes = [p.strip().upper() for p in (z.outward_prefixes or "").split(",") if p.strip()]
        if area in prefixes:
            return z.multiplier
    return 1.0


def vehicle_mpg_for(size, vehicles_by_size=None):
    from models import Vehicle
    return Vehicle.SIZE_MPG_DEFAULT.get(size, 25.0)


def cost_breakdown(order, settings, zones, vehicle_size="panel_van", depot_postcode="M1"):
    """order: object with .delivery_postcode/.collection_postcode, .pallets,
    .timing. settings: RateSettings row. zones: list of Zone rows."""
    postcode = order.delivery_postcode if getattr(order, "job_type", "delivery") != "collection" else order.collection_postcode

    miles = round_trip_miles(depot_postcode, postcode) if settings.round_trip else round_trip_miles(depot_postcode, postcode) / 2
    mpg = vehicle_mpg_for(vehicle_size)
    gallons = miles / mpg
    litres = gallons * 4.54609
    fuel_cost = litres * settings.fuel_price_per_litre

    drive_hours = miles / max(settings.avg_speed_mph, 1)
    handling_hours = (order.pallets or 0) * settings.handling_min_per_pallet / 60.0
    driver_cost = (drive_hours + handling_hours) * settings.driver_rate_per_hour

    base_cost = fuel_cost + driver_cost + settings.fixed_cost_per_job

    zmult = zone_multiplier_for(postcode, zones)
    zoned_cost = base_cost * zmult

    uplift = 0.0
    if getattr(order, "timing", "48h") == "same_day":
        uplift = zoned_cost * (settings.service_uplift_24h_pct / 100.0)

    cost_total = zoned_cost + uplift
    margin = cost_total * (settings.margin_pct / 100.0)
    recommended = round(cost_total + margin, 2)

    return {
        "miles": round(miles, 1),
        "fuel_cost": round(fuel_cost, 2),
        "driver_cost": round(driver_cost, 2),
        "fixed_cost": settings.fixed_cost_per_job,
        "zone_multiplier": zmult,
        "same_day_uplift": round(uplift, 2),
        "cost_total": round(cost_total, 2),
        "margin_pct": settings.margin_pct,
        "margin_amount": round(margin, 2),
        "recommended_sell": recommended,
    }


def quote_order(order, customer, settings, zones, depot_postcode="M1"):
    """Returns the price to charge the customer for this order."""
    if customer.pricing_mode == "flat":
        return round((order.pallets or 0) * (customer.flat_rate_per_pallet or 0), 2)

    breakdown = cost_breakdown(order, settings, zones, depot_postcode=depot_postcode)
    price = breakdown["recommended_sell"]
    discount = price * ((customer.discount_pct or 0) / 100.0)
    return round(price - discount, 2)
