"""
Heliolink pricing engine.

Three pricing modes, matching how the business actually quotes:
  * pallet     - the published pallet rate card: delivery postcode -> distance
                 band (A-G), then a per-pallet rate by account tier and the
                 number of pallets, + 16% fuel surcharge + VAT (+ same-day).
  * wavelength - events/distance pricing (round-trip miles x vehicle mpg).
  * custom     - bespoke / multi-drop: a manually agreed net price + VAT.

All rates exclude VAT and the 16% fuel surcharge unless stated.
"""

FSC = 0.16   # fuel surcharge
VAT = 0.20

TIERS = ["Standard", "Trade", "Key", "Major"]

# ---------------------------------------------------------------------------
# Postcode -> distance band (Heliolink coverage schedule)
# Each row: (area letters, district-from, district-to, band)
# ---------------------------------------------------------------------------
BANDS = [
    ("PR", 1, 9, "A"), ("PR", 25, 26, "A"), ("BB", 1, 6, "A"), ("BB", 7, 12, "A"),
    ("BB", 18, 18, "A"), ("FY", 1, 8, "A"), ("WN", 1, 8, "A"), ("L", 37, 40, "A"),
    ("LA", 1, 6, "A"), ("BL", 1, 9, "A"),
    ("M", 1, 90, "B"), ("WA", 1, 16, "B"), ("L", 1, 36, "B"), ("BD", 23, 24, "B"),
    ("OL", 1, 16, "B"), ("SK", 1, 23, "B"), ("LA", 7, 12, "B"), ("LA", 20, 23, "B"),
    ("BD", 1, 22, "B"), ("HX", 1, 7, "B"), ("HD", 1, 10, "B"),
    ("LS", 1, 29, "C"), ("CH", 1, 4, "C"), ("CH", 41, 66, "C"), ("CH", 5, 8, "C"),
    ("CW", 1, 12, "C"), ("LA", 13, 19, "C"), ("WF", 1, 17, "C"), ("HG", 1, 5, "C"),
    ("LL", 11, 20, "C"), ("ST", 1, 21, "C"), ("S", 1, 81, "C"),
    ("CA", 10, 17, "D"), ("LL", 21, 34, "D"), ("CA", 1, 9, "D"), ("DN", 1, 22, "D"),
    ("YO", 1, 62, "D"), ("CA", 18, 28, "D"), ("DL", 1, 17, "D"), ("TF", 1, 13, "D"),
    ("SY", 1, 25, "D"), ("DE", 13, 15, "D"),
    ("HU", 1, 20, "E"), ("WS", 1, 15, "E"), ("B", 77, 79, "E"), ("NE", 46, 49, "E"),
    ("DG", 1, 16, "E"), ("DH", 1, 9, "E"), ("TS", 1, 29, "E"), ("LL", 35, 49, "E"),
    ("LL", 50, 78, "E"), ("NE", 1, 45, "E"), ("NE", 61, 71, "E"), ("SR", 1, 8, "E"),
    ("TD", 1, 15, "F"), ("ML", 1, 12, "F"), ("KA", 1, 30, "F"),
    ("FK", 1, 21, "G"), ("EH", 1, 55, "G"), ("G", 1, 84, "G"), ("PA", 1, 19, "G"),
]

BAND_MILES = {"A": "0-25 mi", "B": "26-50 mi", "C": "51-75 mi", "D": "76-100 mi",
              "E": "101-150 mi", "F": "151-200 mi", "G": "201+ mi"}

SAME_DAY = {"small": 25.0, "large": 35.0}   # 1-3 pallets / 4-6 pallets, bands A-C

# Per-pallet rates [1..6 pallets] by tier, full & half pallet.
RATECARD = {
    "Standard": {
        "full": {"A": [37.50, 27.50, 22.80, 20.90, 19.95, 19.45], "B": [49.50, 35.00, 28.95, 26.60, 25.15, 24.20], "C": [57.00, 41.00, 33.70, 30.90, 29.45, 28.50], "D": [69.50, 49.50, 40.40, 37.50, 35.60, 34.20], "E": [75.00, 56.50, 47.95, 45.10, 43.20, 41.80], "F": [97.00, 76.00, 65.10, 61.75, 59.85, 58.40], "G": [116.00, 101.00, 91.20, 88.80, 87.40, 86.45]},
        "half": {"A": [26.25, 19.25, 15.95, 14.65, 13.95, 13.60], "B": [34.65, 24.50, 20.25, 18.60, 17.60, 16.95], "C": [39.90, 28.70, 23.60, 21.65, 20.60, 19.95], "D": [48.65, 34.65, 28.30, 26.25, 24.90, 23.95], "E": [52.50, 39.55, 33.55, 31.55, 30.25, 29.25], "F": [67.90, 53.20, 45.55, 43.20, 41.90, 40.90], "G": [81.20, 70.70, 63.85, 62.15, 61.20, 60.50]}},
    "Trade": {
        "full": {"A": [36.00, 26.00, 21.85, 20.40, 19.45, 18.50], "B": [47.50, 34.00, 27.55, 25.65, 24.20, 23.25], "C": [55.00, 39.00, 32.30, 29.90, 28.50, 27.55], "D": [66.50, 47.50, 38.95, 35.60, 34.20, 32.75], "E": [72.00, 54.50, 46.05, 43.20, 41.30, 40.40], "F": [93.00, 73.00, 62.70, 59.40, 57.45, 56.05], "G": [111.50, 97.00, 87.40, 85.00, 83.60, 82.65]},
        "half": {"A": [25.20, 18.20, 15.30, 14.30, 13.60, 12.95], "B": [33.25, 23.80, 19.30, 17.95, 16.95, 16.25], "C": [38.50, 27.30, 22.60, 20.95, 19.95, 19.30], "D": [46.55, 33.25, 27.25, 24.90, 23.95, 22.90], "E": [50.40, 38.15, 32.25, 30.25, 28.90, 28.30], "F": [65.10, 51.10, 43.90, 41.60, 40.20, 39.25], "G": [78.05, 67.90, 61.20, 59.50, 58.50, 57.85]}},
    "Key": {
        "full": {"A": [34.50, 25.00, 20.90, 19.00, 18.50, 17.55], "B": [45.00, 32.00, 26.60, 24.20, 23.25, 22.30], "C": [52.00, 37.00, 30.90, 28.50, 27.10, 26.10], "D": [63.00, 45.00, 37.05, 34.20, 32.30, 31.35], "E": [68.50, 51.50, 43.70, 40.85, 39.40, 38.50], "F": [88.50, 69.00, 59.40, 56.50, 54.60, 53.20], "G": [106.00, 92.00, 83.10, 80.75, 79.80, 78.85]},
        "half": {"A": [24.15, 17.50, 14.65, 13.30, 12.95, 12.30], "B": [31.50, 22.40, 18.60, 16.95, 16.25, 15.60], "C": [36.40, 25.90, 21.65, 19.95, 18.95, 18.25], "D": [44.10, 31.50, 25.95, 23.95, 22.60, 21.95], "E": [47.95, 36.05, 30.60, 28.60, 27.60, 26.95], "F": [61.95, 48.30, 41.60, 39.55, 38.20, 37.25], "G": [74.20, 64.40, 58.15, 56.50, 55.85, 55.20]}},
    "Major": {
        "full": {"A": [33.00, 24.00, 19.95, 18.50, 17.55, 17.10], "B": [43.50, 31.00, 25.65, 23.25, 22.30, 21.40], "C": [50.00, 36.00, 29.45, 27.55, 26.10, 25.15], "D": [61.00, 43.50, 35.60, 32.75, 31.35, 29.90], "E": [66.00, 49.50, 42.25, 39.40, 38.00, 37.05], "F": [85.00, 66.50, 57.45, 54.60, 52.70, 51.30], "G": [102.00, 88.50, 79.80, 77.90, 76.45, 76.00]},
        "half": {"A": [23.10, 16.80, 13.95, 12.95, 12.30, 11.95], "B": [30.45, 21.70, 17.95, 16.25, 15.60, 15.00], "C": [35.00, 25.20, 20.60, 19.30, 18.25, 17.60], "D": [42.70, 30.45, 24.90, 22.90, 21.95, 20.95], "E": [46.20, 34.65, 29.60, 27.60, 26.60, 25.95], "F": [59.50, 46.55, 40.20, 38.20, 36.90, 35.90], "G": [71.40, 61.95, 55.85, 54.55, 53.50, 53.20]}},
}

# Wavelength (events) cost basis - matches the Wavelength Quote Engine.
WL = {"gallon_litres": 4.546, "fuel_price": 1.92, "mpg": {"luton": 23, "maxus": 34},
      "driver_rate": 6.0, "avg_speed": 40.0, "overhead_cpm": 0.10, "margin": 0.20, "after6": 30.0}


def outward_code(pc):
    pc = (pc or "").upper().strip()
    if not pc:
        return ""
    if " " in pc:
        return pc.split()[0]
    if len(pc) > 3:          # inward code is always the last 3 chars
        return pc[:-3]
    return pc


def band_for_postcode(pc):
    ow = outward_code(pc)
    if not ow:
        return None
    i = 0
    while i < len(ow) and ow[i].isalpha():
        i += 1
    area, digits = ow[:i], ow[i:]
    j = 0
    while j < len(digits) and digits[j].isdigit():
        j += 1
    if j == 0:
        return None
    dist = int(digits[:j])
    for a, f, t, b in BANDS:
        if a == area and f <= dist <= t:
            return b
    return None


def _per_pallet(tier, kind, band, spaces):
    arr = RATECARD[tier][kind][band]
    return arr[min(max(spaces, 1), 6) - 1]


def price_pallet(tier, postcode, full_pallets, half_pallets, same_day=False, surcharges=None):
    """Return a pricing breakdown dict. ok=False with a reason if it can't auto-price."""
    tier = tier or "Standard"
    surcharges = surcharges or []
    band = band_for_postcode(postcode)
    spaces = (full_pallets or 0) + (half_pallets or 0)
    if not band:
        return {"ok": False, "reason": (
            "Postcode %s is outside the rate-card coverage - quote on request." % postcode.upper()
            if postcode else "Enter a delivery postcode to auto-price."), "band": None}
    if spaces == 0:
        return {"ok": False, "reason": "No pallets on this booking - add pallets or use custom pricing.", "band": band}

    lines, transport = [], 0.0
    if full_pallets:
        r = _per_pallet(tier, "full", band, spaces)
        transport += r * full_pallets
    if half_pallets:
        r = _per_pallet(tier, "half", band, spaces)
        transport += r * half_pallets
    fsc = transport * FSC
    same_amt = 0.0
    if same_day and band in ("A", "B", "C"):
        same_amt = SAME_DAY["small"] if spaces <= 3 else SAME_DAY["large"]
    sur_total = sum(s.get("amount", 0) for s in surcharges)
    sub = transport + fsc + same_amt + sur_total
    vat = sub * VAT
    total = sub + vat

    lines.append(("Transport charge", transport, "Band %s - %s tier - %s" % (band, tier, BAND_MILES[band])))
    lines.append(("Fuel surcharge 16%", fsc, None))
    if same_amt:
        lines.append(("Same-day service", same_amt, None))
    for s in surcharges:
        lines.append(("Surcharge - " + s.get("label", "Surcharge"), s.get("amount", 0), None))
    return {"ok": True, "band": band, "tier": tier, "spaces": spaces,
            "net": sub, "vat": vat, "total": total, "lines": lines,
            "basis": "Pallet - Band %s - %s" % (band, tier)}


def price_wavelength(vehicle, miles, after6=False, surcharges=None):
    surcharges = surcharges or []
    mpg = WL["mpg"].get(vehicle, WL["mpg"]["luton"])
    cpm = (WL["fuel_price"] * WL["gallon_litres"] / mpg) + (WL["driver_rate"] / WL["avg_speed"]) + WL["overhead_cpm"]
    cost = (miles or 0) * cpm
    transport = cost / (1 - WL["margin"])
    fsc = transport * FSC
    a6 = WL["after6"] if after6 else 0.0
    sur_total = sum(s.get("amount", 0) for s in surcharges)
    sub = transport + fsc + a6 + sur_total
    vat = sub * VAT
    total = sub + vat
    lines = [("Transport charge", transport, "%d mi round trip - %s" % (miles or 0, "3.5t Luton" if vehicle == "luton" else "3.5t Maxus")),
             ("Fuel surcharge 16%", fsc, None)]
    if a6:
        lines.append(("After-6pm surcharge", a6, None))
    for s in surcharges:
        lines.append(("Surcharge - " + s.get("label", "Surcharge"), s.get("amount", 0), None))
    return {"ok": (miles or 0) > 0, "reason": None if (miles or 0) > 0 else "Enter round-trip miles.",
            "net": sub, "vat": vat, "total": total, "lines": lines, "basis": "Wavelength distance"}


def price_custom(net, drops=0, multidrop=False):
    net = float(net or 0)
    vat = net * VAT
    label = "Multi-drop round (ex VAT)" if multidrop else ("Custom - multidrop (%d drops)" % drops if drops > 1 else "Custom charge (ex VAT)")
    basis = "Multi-drop round" if multidrop else ("Custom - %d drops" % drops if drops > 1 else "Custom / bespoke")
    return {"ok": net > 0, "reason": None if net > 0 else "Enter the agreed price.",
            "net": net, "vat": vat, "total": net + vat, "lines": [(label, net, None)], "basis": basis}
