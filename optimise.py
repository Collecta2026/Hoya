"""Route sequencing and light geocoding.

No external API required: a table of UK outward-code centroids gives every
postcode an approximate lat/lng, then a nearest-neighbour pass orders the
stops from the depot. Good enough to remove backtracking on a multidrop
round. Swap in a real routing API later by replacing `optimise_sequence`.
"""
import math
import re

# Approximate lat/lng for common UK postcode areas / outward codes.
# Keyed by the leading letters of the outward code (the "area").
AREA_CENTROIDS = {
    "PR": (53.76, -2.70), "BB": (53.75, -2.48), "BL": (53.58, -2.43),
    "FY": (53.82, -3.02), "L": (53.41, -2.99), "WA": (53.39, -2.60),
    "WN": (53.55, -2.63), "M": (53.48, -2.24), "OL": (53.55, -2.11),
    "SK": (53.36, -2.16), "CW": (53.16, -2.44), "CH": (53.20, -2.89),
    "ST": (52.99, -2.18), "TF": (52.68, -2.44), "SY": (52.71, -2.75),
    "WV": (52.59, -2.13), "DE": (52.92, -1.48), "NG": (52.95, -1.15),
    "LE": (52.63, -1.13), "B": (52.48, -1.90), "CV": (52.41, -1.51),
    "NN": (52.24, -0.90), "MK": (52.04, -0.76), "OX": (51.75, -1.26),
    "RG": (51.46, -1.00), "SP": (51.07, -1.79), "HP": (51.71, -0.75),
    "LU": (51.88, -0.42), "WR": (52.19, -2.22), "GL": (51.86, -2.24),
    "HR": (52.06, -2.72), "LD": (52.24, -3.38), "CF": (51.48, -3.18),
    "SA": (51.62, -3.94), "NP": (51.58, -2.99), "LS": (53.80, -1.55),
    "BD": (53.79, -1.75), "HX": (53.72, -1.86), "HD": (53.65, -1.78),
    "WF": (53.68, -1.50), "S": (53.38, -1.47), "DN": (53.52, -1.13),
    "YO": (53.96, -1.08), "HU": (53.74, -0.33), "CA": (54.65, -2.93),
    "LA": (54.05, -2.80), "DL": (54.53, -1.55), "TS": (54.57, -1.23),
    "NE": (54.98, -1.61), "SR": (54.90, -1.38), "DH": (54.78, -1.58),
    "CB": (52.20, 0.12), "PE": (52.57, -0.24), "NR": (52.63, 1.30),
    "IP": (52.06, 1.16), "CO": (51.89, 0.90), "CM": (51.73, 0.47),
    "SG": (51.90, -0.20), "AL": (51.75, -0.34), "WD": (51.66, -0.40),
    "EN": (51.65, -0.08), "HA": (51.58, -0.34), "UB": (51.53, -0.42),
    "TW": (51.45, -0.37), "KT": (51.35, -0.29), "SM": (51.36, -0.19),
    "CR": (51.35, -0.09), "BR": (51.40, 0.02), "DA": (51.44, 0.22),
    "SE": (51.47, -0.05), "SW": (51.46, -0.17), "N": (51.56, -0.11),
    "E": (51.53, -0.03), "W": (51.51, -0.22), "NW": (51.55, -0.20),
    "EC": (51.52, -0.09), "WC": (51.52, -0.12), "GU": (51.24, -0.59),
    "RH": (51.10, -0.19), "TN": (51.13, 0.27), "ME": (51.35, 0.52),
    "CT": (51.28, 1.08), "BN": (50.83, -0.14), "PO": (50.82, -1.09),
    "SO": (50.92, -1.40), "BH": (50.74, -1.88), "DT": (50.71, -2.44),
    "TA": (51.02, -3.10), "BS": (51.45, -2.59), "BA": (51.28, -2.36),
    "EX": (50.72, -3.53), "PL": (50.37, -4.14), "TQ": (50.46, -3.53),
    "TR": (50.26, -5.05),
}
DEPOT_DEFAULT = (53.76, -2.70)  # Preston PR


def _area(postcode):
    if not postcode:
        return None
    pc = postcode.strip().upper()
    m = re.match(r"^([A-Z]{1,2})", pc)
    return m.group(1) if m else None


def geocode(postcode):
    """Return (lat, lng) for a postcode, approximated from its area."""
    area = _area(postcode)
    if area and area in AREA_CENTROIDS:
        return AREA_CENTROIDS[area]
    # try single-letter fallback (e.g. 'M4' area 'M')
    if area and len(area) == 2 and area[0] in AREA_CENTROIDS:
        return AREA_CENTROIDS[area[0]]
    return None


def haversine(a, b):
    lat1, lon1 = a
    lat2, lon2 = b
    r = 3958.8  # miles
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def optimise_sequence(stops, start_postcode="PR2 2TE"):
    """Order a list of stop-like objects (need .order.postcode) by nearest
    neighbour from the depot. Returns the reordered list plus total miles.
    """
    depot = geocode(start_postcode) or DEPOT_DEFAULT
    pts = []
    for s in stops:
        pc = s.order.postcode if s.order else None
        pts.append((s, geocode(pc) or depot))

    remaining = list(pts)
    ordered = []
    cur = depot
    total = 0.0
    while remaining:
        remaining.sort(key=lambda x: haversine(cur, x[1]))
        nxt = remaining.pop(0)
        total += haversine(cur, nxt[1])
        ordered.append(nxt[0])
        cur = nxt[1]
    return ordered, round(total, 1)


# UK postcode area -> region (for zone-based rate multipliers)
REGION_OF = {
    "PR": "North West", "BB": "North West", "BL": "North West", "FY": "North West",
    "L": "North West", "WA": "North West", "WN": "North West", "M": "North West",
    "OL": "North West", "SK": "North West", "CW": "North West", "CH": "North West",
    "LA": "North West", "CA": "North West",
    "NE": "North East", "SR": "North East", "DH": "North East", "DL": "North East",
    "TS": "North East",
    "LS": "Yorkshire", "BD": "Yorkshire", "HX": "Yorkshire", "HD": "Yorkshire",
    "WF": "Yorkshire", "S": "Yorkshire", "DN": "Yorkshire", "YO": "Yorkshire",
    "HU": "Yorkshire",
    "DE": "East Midlands", "NG": "East Midlands", "LE": "East Midlands",
    "NN": "East Midlands",
    "B": "West Midlands", "CV": "West Midlands", "WV": "West Midlands",
    "ST": "West Midlands", "TF": "West Midlands", "SY": "West Midlands",
    "WR": "West Midlands", "HR": "West Midlands", "GL": "West Midlands",
    "CF": "Wales", "SA": "Wales", "NP": "Wales", "LD": "Wales",
    "BS": "South West", "BA": "South West", "TA": "South West", "EX": "South West",
    "PL": "South West", "TQ": "South West", "TR": "South West", "DT": "South West",
    "SP": "South West", "BH": "South West",
    "OX": "South East", "RG": "South East", "GU": "South East", "RH": "South East",
    "TN": "South East", "ME": "South East", "CT": "South East", "BN": "South East",
    "PO": "South East", "SO": "South East", "SL": "South East", "HP": "South East",
    "LU": "South East", "MK": "South East", "AL": "South East", "SG": "South East",
    "CB": "East", "PE": "East", "NR": "East", "IP": "East", "CO": "East", "CM": "East",
    "E": "London", "EC": "London", "N": "London", "NW": "London", "SE": "London",
    "SW": "London", "W": "London", "WC": "London", "BR": "London", "CR": "London",
    "DA": "London", "EN": "London", "HA": "London", "KT": "London", "SM": "London",
    "TW": "London", "UB": "London", "WD": "London",
}
REGIONS = ["North West", "North East", "Yorkshire", "East Midlands",
           "West Midlands", "Wales", "South West", "South East", "East",
           "London", "Other"]


def region_of(postcode):
    a = _area(postcode)
    return REGION_OF.get(a, "Other")
