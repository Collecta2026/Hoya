"""
Route sequencing and postcode-zone helpers.
No external mapping API key required: stops are sequenced by nearest-neighbour
over approximate UK postcode *outward code* centroids (e.g. "SW1", "M1", "EH3").
This mirrors the approach used on the previous build of this platform.
"""
import re
import math

# Approximate lat/lng centroids for common UK outward-code areas (first letters
# of the postcode, e.g. "SW", "EC", "M", "B", "LS"...). Good enough for
# clustering/sequencing multi-drop routes without a paid geocoding API.
AREA_CENTROIDS = {
    "E": (51.5285, -0.0212), "EC": (51.5178, -0.0913), "N": (51.5619, -0.1099),
    "NW": (51.5432, -0.1850), "SE": (51.4783, -0.0508), "SW": (51.4820, -0.1610),
    "W": (51.5140, -0.1900), "WC": (51.5170, -0.1230),
    "M": (53.4808, -2.2426), "B": (52.4862, -1.8904), "L": (53.4084, -2.9916),
    "LS": (53.8008, -1.5491), "S": (53.3811, -1.4701), "SW1": (51.4975, -0.1357),
    "BS": (51.4545, -2.5879), "NE": (54.9783, -1.6178), "CF": (51.4816, -3.1791),
    "EH": (55.9533, -3.1883), "G": (55.8642, -4.2518), "NG": (52.9548, -1.1581),
    "LE": (52.6369, -1.1398), "CV": (52.4068, -1.5197), "OX": (51.7520, -1.2577),
    "CB": (52.2053, 0.1218), "NR": (52.6309, 1.2974), "PE": (52.5695, -0.2405),
    "PL": (50.3755, -4.1427), "EX": (50.7184, -3.5339), "TR": (50.2632, -5.0510),
    "BN": (50.8225, -0.1372), "PO": (50.8198, -1.0880), "RG": (51.4543, -0.9781),
    "SN": (51.5558, -1.7797), "GL": (51.8642, -2.2380), "HR": (52.0567, -2.7160),
    "WR": (52.1920, -2.2210), "ST": (52.9821, -2.1450), "DE": (52.9225, -1.4746),
    "SK": (53.4083, -2.1494), "WA": (53.3900, -2.5970), "PR": (53.7632, -2.7031),
    "BB": (53.7477, -2.4867), "BD": (53.7960, -1.7594), "HD": (53.6458, -1.7850),
    "HX": (53.7220, -1.8600), "WF": (53.6833, -1.4977), "YO": (53.9600, -1.0873),
    "HU": (53.7457, -0.3367), "DN": (53.5228, -1.1285), "DL": (54.5238, -1.5540),
    "SR": (54.9069, -1.3838), "DH": (54.7761, -1.5760), "TS": (54.5742, -1.2350),
    "CA": (54.8951, -2.9382), "LA": (54.0466, -2.8007), "KA": (55.6110, -4.4960),
    "PA": (55.8600, -4.4300), "ML": (55.7770, -3.9840), "FK": (56.0019, -3.7839),
    "DD": (56.4620, -2.9707), "AB": (57.1497, -2.0943), "IV": (57.4778, -4.2247),
    "KY": (56.1165, -3.1590), "TD": (55.5960, -2.7800), "DG": (55.0709, -3.6050),
    "SA": (51.6214, -3.9436), "NP": (51.5842, -2.9977), "LD": (52.2440, -3.3860),
    "SY": (52.7100, -2.7530), "LL": (53.2270, -3.8360), "CH": (53.1900, -2.8900),
    "WN": (53.5450, -2.6318), "OL": (53.5409, -2.1114), "BL": (53.5769, -2.4280),
    "PR1": (53.7632, -2.7031), "IP": (52.0567, 1.1482), "CO": (51.8959, 0.8919),
    "SS": (51.5459, 0.7077), "CM": (51.7356, 0.4685), "AL": (51.7520, -0.3360),
    "HP": (51.6290, -0.7480), "LU": (51.8787, -0.4200), "MK": (52.0406, -0.7594),
    "NN": (52.2405, -0.9027), "DA": (51.4460, 0.2150), "ME": (51.3730, 0.5060),
    "CT": (51.2802, 1.0789), "TN": (51.1330, 0.2610), "GU": (51.2362, -0.5704),
    "KT": (51.4085, -0.3064), "SL": (51.5105, -0.5950), "TW": (51.4479, -0.3260),
    "UB": (51.5380, -0.4780), "HA": (51.5793, -0.3370), "EN": (51.6520, -0.0810),
    "IG": (51.5590, 0.0810), "RM": (51.5770, 0.1830), "CR": (51.3762, -0.0982),
    "SM": (51.3600, -0.1960), "BR": (51.4040, 0.0140), "WD": (51.6560, -0.4200),
}

DEFAULT_CENTROID = (52.5, -1.5)  # roughly the centre of England, used as a fallback


def area_of(postcode):
    """Extract the outward-code area letters, e.g. 'SW1A 1AA' -> 'SW'."""
    if not postcode:
        return None
    pc = postcode.strip().upper().replace(" ", "")
    m = re.match(r"^([A-Z]{1,2})\d", pc)
    return m.group(1) if m else None


def centroid_of(postcode):
    area = area_of(postcode)
    return AREA_CENTROIDS.get(area, DEFAULT_CENTROID)


def region_of(postcode):
    """Human-readable-ish region label, used by the rate engine for zone lookup."""
    return area_of(postcode) or "UNKNOWN"


def haversine_km(a, b):
    R = 6371.0
    lat1, lng1 = a
    lat2, lng2 = b
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    h = (math.sin(d_lat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lng / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(h))


def optimise_stops(orders, depot_postcode="M1"):
    """orders: list of Order-like objects with .delivery_postcode (or
    .collection_postcode for collection jobs). Returns the same list
    reordered by greedy nearest-neighbour starting from the depot."""
    def stop_point(o):
        pc = o.collection_postcode if o.job_type == "collection" else o.delivery_postcode
        return centroid_of(pc)

    remaining = list(orders)
    ordered = []
    current = centroid_of(depot_postcode)
    while remaining:
        best_idx, best_dist = 0, float("inf")
        for i, o in enumerate(remaining):
            d = haversine_km(current, stop_point(o))
            if d < best_dist:
                best_dist, best_idx = d, i
        nxt = remaining.pop(best_idx)
        ordered.append(nxt)
        current = stop_point(nxt)
    return ordered


def round_trip_miles(depot_postcode, postcode):
    km = haversine_km(centroid_of(depot_postcode), centroid_of(postcode)) * 2  # there and back
    # straight-line distance underestimates real road distance; apply a
    # standard routing-inefficiency factor
    km *= 1.3
    return km * 0.621371
