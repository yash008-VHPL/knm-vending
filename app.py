import base64
import json
import math
from flask import Flask, render_template, request, jsonify, redirect
from functools import wraps
import pymssql
from datetime import datetime, timedelta
import config

app = Flask(__name__)


# ── Auth helpers ───────────────────────────────────────────────────────────────

def _decode_principal():
    b64 = request.headers.get("X-MS-CLIENT-PRINCIPAL", "")
    if not b64:
        return None
    try:
        return json.loads(base64.b64decode(b64).decode("utf-8"))
    except Exception:
        return None


def get_current_user():
    principal = _decode_principal()
    if principal:
        for claim in principal.get("claims", []):
            if claim.get("typ") == "preferred_username":
                return claim.get("val", "").strip().lower()
    return config.DEV_USER_EMAIL.strip().lower() if config.DEV_USER_EMAIL else ""


def get_role(email=None):
    principal = _decode_principal()
    if principal:
        for claim in principal.get("claims", []):
            if claim.get("typ") == "roles":
                return claim.get("val", "").strip().lower()
    return config.DEV_ROLE.strip().lower() if config.DEV_ROLE else None


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        email = get_current_user()
        if not email:
            return redirect("/.auth/login/aad?post_login_redirect_uri=/")
        if not get_role(email):
            return (
                f"<h2>Access Denied</h2><p>{email} is not authorised to use this app."
                f"<br>Please contact your administrator.</p>"
                f'<p><a href="/.auth/logout">Sign out</a></p>'
            ), 403
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        email = get_current_user()
        if not email or get_role(email) != "admin":
            return jsonify({"error": "Unauthorized"}), 403
        return f(*args, **kwargs)
    return decorated


def dispatch_or_admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        email = get_current_user()
        if not email or get_role(email) not in ("admin", "dispatch"):
            return jsonify({"error": "Unauthorized"}), 403
        return f(*args, **kwargs)
    return decorated


# ── DB helpers ─────────────────────────────────────────────────────────────────

OLE_EPOCH = datetime(1899, 12, 30)


def to_ole_date(dt_obj):
    delta = dt_obj - OLE_EPOCH
    return delta.days + (delta.seconds + delta.microseconds / 1e6) / 86400.0


def from_ole_date(ole_value):
    if ole_value is None:
        return None
    try:
        return OLE_EPOCH + timedelta(days=float(ole_value))
    except Exception:
        return None


def get_connection():
    return pymssql.connect(
        server=config.DB_SERVER,
        database=config.DB_NAME,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        tds_version="7.4",
    )


# ── Schema migration (idempotent) ─────────────────────────────────────────────

def init_db():
    """Add new MachineLookup columns if they don't already exist."""
    new_cols = [
        ("Latitude",               "FLOAT"),
        ("Longitude",              "FLOAT"),
        ("LastTopupTimestamp",     "FLOAT"),
        ("PreviousTopupTimestamp", "FLOAT"),
        ("CountBeforeLastTopup",   "INT"),
    ]
    try:
        conn = get_connection()
        cursor = conn.cursor()
        for col, dtype in new_cols:
            cursor.execute(f"""
                IF NOT EXISTS (
                    SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_NAME = 'MachineLookup' AND COLUMN_NAME = '{col}'
                )
                ALTER TABLE MachineLookup ADD [{col}] {dtype} NULL
            """)
            conn.commit()
        conn.close()
    except Exception as e:
        print(f"[init_db] Warning: {e}")


try:
    init_db()
except Exception as e:
    print(f"[startup] init_db failed: {e}")


# ── Location seed data (idempotent) ───────────────────────────────────────────

LOCATION_SEED = [
    # (machine_code_or_None, location_name, longitude, latitude)
    ("32720359", "Ubi Tech Park A",                103.89633,    1.326781),
    ("32720370", "Singpost Center",                103.893995,   1.319107),
    ("32720372", "Prive Condo",                    103.904052,   1.401143),
    ("42920754", "Skyworks @ Bedok",               103.929152,   1.32086),
    ("42920755", "IMH Main Lobby",                 103.884719,   1.381952),
    ("42920756", "Sunshine Plaza",                 103.851104,   1.300522),
    ("42920757", "351 Braddell",                   103.845449,   1.343634),
    ("42920759", "IMH Annex",                      103.885335,   1.382854),
    ("42920761", "Ubi Tech Park C",                103.89633,    1.326781),
    ("42920762", "Paya Lebar Certis",              103.722805,   1.315294),
    ("43020632", "Ang Mo Kio Tech Point",          103.849955,   1.389623),
    ("43020633", "Oxley Bizhub 2",                 103.892079,   1.331771),
    ("44220851", "Home Team Academy",              103.722951,   1.374502),
    ("44520325", "Singapore Sailing Center",       103.962,      1.315934),
    ("44520629", "Kranji Camp II",                 103.742855,   1.399051),
    ("44520630", "GnC Plaza 8",                    103.9659,     1.33334),
    ("44520632", "AUPE",                           103.882271,   1.344073),
    ("44520633", "Welcia Orchard",                 103.83139,    1.30306),
    ("44520635", "GnC MBC",                        103.7995973,  1.2756422),
    ("45021768", "Bedok Police HQ",                103.937042,   1.3328),
    ("45021769", "CSC Holland",                    103.791865,   1.311371),
    ("45021770", "Gleneagles L1",                  103.819694,   1.308647),
    ("45021773", "Tanah Merah Ferry Terminal",     103.988507,   1.314537),
    ("45021774", "SP Kallang",                     103.872182,   1.326527),
    ("45021776", "Science Park Ascent",            103.785626,   1.290623),
    ("50220245", "PSA ITC",                        103.79042,    1.275732),
    ("50220246", "Delta House",                    103.825389,   1.2912),
    ("50220248", "PSA Alongside",                  103.79042,    1.275732),
    ("50220249", "TCF @ Jurong",                   103.737667,   1.332495),
    ("50420522", "Gleneagles L4",                  103.819694,   1.308647),
    ("50420523", "Maybank",                        103.849223,   1.387287),
    ("50420532", "RWS",                            103.821832,   1.255479),
    ("50420533", "Changi Airport Police",          103.981071,   1.343252),
    ("51321279", "10X Genomics",                   103.875756,   1.324564),
    ("51321280", "Lim Kim Hai Electric",           103.86844,    1.314087),
    ("51321286", "Parkway Lab",                    103.889214,   1.320228),
    ("51421679", "Skyworks",                       103.929152,   1.32086),
    ("51421681", "Collins Aerospace",              103.968736,   1.346705),
    ("51421682", "GnC IOI",                        103.852017,   1.280978),
    ("51421683", "CGH L9",                         103.94957,    1.340385),
    ("51421685", "ST Marine L2 Pantry",            None,         None),
    ("51421686", "Chinese Swimming Club",          103.900542,   1.30032),
    ("51421694", "ST Rifle Range",                 103.779406,   1.34369),
    ("51421696", "NYC HDB Hub",                    103.848552,   1.332773),
    ("51421698", "Mount Carmel BP West Coast",     103.765747,   1.303174),
    ("51421699", "SA Tours",                       103.842531,   1.284095),
    ("51421700", "GnC Marina One",                 103.852887,   1.278725),
    ("51421701", "Hundred Grains VivoCity",        103.823059,   1.265282),
    ("51421702", "GnC Geneos",                     103.785170,   1.292711),
    ("51421703", "GnC 1 Raffles Place",            103.85096,    1.284574),
    ("51421704", "Meta L27",                       103.8524,     1.277534),
    ("51421706", "Welcia Bedok Mall",              103.9302,     1.32482),
    ("52920213", "Welcia Raffles City",            103.8533,     1.29424),
    ("52920225", "Chasen Logistics",               103.727234,   1.316345),
    ("52920226", "CGH L5",                         103.94957,    1.340385),
    ("52920229", "Kaki Bukit Camp",                103.907542,   1.339464),
    ("53920763", "Kranji Camp 3",                  103.742764,   1.403701),
    ("53920765", "NV Residence",                   103.943952,   1.372053),
    ("53920767", "Medtronics",                     103.972083,   1.336573),
    ("53920769", "Affinity Serangoon",             103.873068,   1.366414),
    ("55120035", "Changi Naval Base Cookhouse",    104.0139996,  1.3180665),
    ("45021777", "Mediacorp",                      103.7895933,  1.2955542),
    ("52920224", "Skyworks @ AMK",                 103.8465424,  1.3894852),
    ("52821401", "Tuas Naval Base",                103.6627741,  1.2956892),
    ("52821398", "Changi Lv 1",                    103.9662336,  1.3516001),
    ("43020632", "AMK Techpoint",                  103.8468548,  1.3894349),
    ("52920228", "Amran's Kitchen",                103.8795815,  1.3454914),
    ("52821394", "Fei Siong Group",                103.7044427,  1.3326885),
    ("53920761", "SIA Terminal 3 Control Center",  103.9839315,  1.356275),
    ("50420524", "Alice@medipolis",                103.7908503,  1.2938511),
    ("51421685", "St. Marine Benoi",               103.6770818,  1.3015955),
    ("51621681", "Collins Aerospace Changi",       103.9659583,  1.3457854),
    ("53920766", "Police Cantonment Complex",      103.8370915,  1.2785558),
    ("44520630", "MinDef Lunch Club",              103.6872948,  1.3716234),
    ("52920227", "SP Choa Chu Kang",               103.8735121,  1.3769613),
    ("52920230", "ST Marine Benoi (new)",          103.6770818,  1.3015955),
    ("44520635", "St Joseph (canteen)",            103.7050664,  1.3505482),
    ("51421682", "St Joseph (cafe)",               103.7050664,  1.3505482),
    ("44520326", "St Joseph (conference room)",    103.7050664,  1.3505482),
    ("45021776", "Ascent",                         103.7830407,  1.2904462),
    ("51421702", "Grains & Co Geneos",             103.8484069,  1.2844078),
    ("51421679", "Little Splashes Paya Lebar",     103.8918856,  1.3189472),
    ("55120032", "Geylang NPC",                    103.8835589,  1.3109648),
    ("51421703", "Anguillia Mosque",               103.851667,   1.3104972),
    ("52821396", "ST Digital",                     103.8767108,  1.328872),
    ("52821395", "Dawn Shipping",                  103.6942719,  1.3313423),
    ("54120170", "SFATC Toa Payoh",                103.8461766,  1.3370622),
    # No machine code
    (None, "10 Raeburn Park",                      103.830957,   1.2748861),
    (None, "54 Pandan Road",                       103.744892,   1.2998825),
    (None, "American Club",                        103.8296255,  1.3084663),
    (None, "Best Bakes",                           103.7957429,  1.3018705),
    (None, "Big Elephant Cafe Havelock 2",         103.8426068,  1.2871803),
    (None, "CDPL Tuas Dormitory",                  103.6346703,  1.2716146),
    (None, "Cheers CMPB Gombak",                   103.7613159,  1.3670568),
    (None, "Commonwealth Towers",                  103.8029461,  1.29587),
    (None, "First Culinary Restaurant",            103.8378796,  1.3778131),
    (None, "Foresque Residences",                  103.7737331,  1.369308),
    (None, "German Centre",                        103.7437705,  1.3249777),
    (None, "Goldbell Tower",                       103.8344614,  1.3123062),
    (None, "iNz Residences",                       103.737463,   1.3748321),
    (None, "Japanese Association Singapore",       103.8132587,  1.3306242),
    (None, "Queens Peak Condominium",              103.804259,   1.2947572),
    (None, "Rochester Commons",                    103.7853008,  1.3047695),
    (None, "SP Pasir Panjang",                     103.7959422,  1.2704721),
    (None, "Thye Hong Centre",                     103.8122588,  1.2910836),
    (None, "ST Tuas",                              None,         None),
]


def seed_locations():
    """
    Upsert location seed data into MachineLookup.
    - Rows with a machine code: UPDATE name+coords if code exists, INSERT otherwise.
    - Rows without a machine code: INSERT only if no row with that exact name exists.
    Each row is committed individually so one failure cannot roll back the rest.
    """
    ok = fail = 0
    for code, name, lon, lat in LOCATION_SEED:
        try:
            conn = get_connection()
            cursor = conn.cursor()
            if code:
                cursor.execute(
                    "SELECT COUNT(*) FROM MachineLookup WHERE MachineCode = %s", (code,)
                )
                if cursor.fetchone()[0]:
                    cursor.execute(
                        "UPDATE MachineLookup SET MachineName=%s, Longitude=%s, Latitude=%s WHERE MachineCode=%s",
                        (name, lon, lat, code),
                    )
                else:
                    cursor.execute(
                        "INSERT INTO MachineLookup (MachineCode, MachineName, Longitude, Latitude) VALUES (%s, %s, %s, %s)",
                        (code, name, lon, lat),
                    )
            else:
                cursor.execute(
                    "SELECT COUNT(*) FROM MachineLookup WHERE MachineName = %s", (name,)
                )
                if not cursor.fetchone()[0]:
                    cursor.execute(
                        "INSERT INTO MachineLookup (MachineName, Longitude, Latitude) VALUES (%s, %s, %s)",
                        (name, lon, lat),
                    )
            conn.commit()
            conn.close()
            ok += 1
        except Exception as e:
            print(f"[seed_locations] FAILED row ({code}, {name}): {e}")
            fail += 1
    print(f"[seed_locations] Done — {ok} ok, {fail} failed.")


try:
    seed_locations()
except Exception as e:
    print(f"[startup] seed_locations failed: {e}")


# ── Heartbeat config ──────────────────────────────────────────────────────────
# Default threshold: a machine is RED if silent longer than this.
# Run /api/admin/heartbeat-analysis (admin only) to measure the actual
# average off-hours gap for your fleet and tune this value.
HEARTBEAT_THRESHOLD_MINUTES = 225   # p95 off-hours gap (180 min) + 25 % headroom

# ── Routing helpers (no external dependencies) ────────────────────────────────

DEPOT_LAT = 1.3407711524195856
DEPOT_LON = 103.8896748329062

def haversine(lat1, lon1, lat2, lon2):
    """Straight-line distance in km between two lat/lon points."""
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def route_distance(route):
    """Total haversine distance for an ordered list of stops."""
    return sum(
        haversine(route[i]['lat'], route[i]['lon'], route[i+1]['lat'], route[i+1]['lon'])
        for i in range(len(route) - 1)
    )


def nearest_neighbor_tsp(stops, start_lat=None, start_lon=None):
    """
    Build an initial tour using the nearest-neighbor heuristic.
    If start_lat/start_lon are given (e.g. depot), the tour begins from
    that external point — it is not added to the returned route.
    Otherwise starts from the southernmost stop.
    """
    if not stops:
        return []
    remaining = stops[:]

    if start_lat is not None and start_lon is not None:
        cur_lat, cur_lon = start_lat, start_lon
        route = []
    else:
        start = min(remaining, key=lambda s: s['lat'])
        remaining.remove(start)
        cur_lat, cur_lon = start['lat'], start['lon']
        route = [start]

    while remaining:
        nearest = min(remaining, key=lambda s: haversine(cur_lat, cur_lon, s['lat'], s['lon']))
        route.append(nearest)
        remaining.remove(nearest)
        cur_lat, cur_lon = nearest['lat'], nearest['lon']
    return route


def two_opt_improve(route):
    """
    Improve a route by repeatedly reversing segments that reduce total distance.
    Eliminates crossing edges (loops). O(n^2) per pass, runs until no improvement.
    """
    best = route[:]
    improved = True
    while improved:
        improved = False
        for i in range(1, len(best) - 1):
            for j in range(i + 1, len(best)):
                candidate = best[:i] + best[i:j+1][::-1] + best[j+1:]
                if route_distance(candidate) < route_distance(best) - 1e-9:
                    best = candidate
                    improved = True
    return best


def split_tour_equally(tour, k):
    """
    Split an ordered tour into k consecutive segments of as-equal size as possible.
    Returns a list of k non-empty lists.
    """
    n = len(tour)
    segments, idx = [], 0
    for i in range(k):
        size = (n - idx) // (k - i)   # distribute remainder evenly
        if size > 0:
            segments.append(tour[idx:idx + size])
            idx += size
    return segments


def build_maps_url(stops, origin_lat=DEPOT_LAT, origin_lon=DEPOT_LON):
    """
    Build Google Maps driving directions URL(s) starting AND ending at the depot.
    All stops are waypoints; destination = depot so the driver returns to base.
    Google Maps allows up to 9 waypoints between origin and destination.
    Returns (url1, url2) where url2 is only set when there are >9 stops.
    """
    BASE = "https://www.google.com/maps/dir/?api=1&travelmode=driving"

    def fmtc(lat, lon): return f"{lat},{lon}"
    def fmt(s):         return fmtc(s['lat'], s['lon'])

    def make_url(orig_str, waypoints, dest_str):
        url = f"{BASE}&origin={orig_str}&destination={dest_str}"
        if waypoints:
            url += "&waypoints=" + "|".join(fmt(s) for s in waypoints)
        return url

    if not stops:
        return None, None

    origin_str = fmtc(origin_lat, origin_lon)

    if len(stops) <= 9:
        # All stops fit as waypoints; full round trip back to depot in one URL
        url1 = make_url(origin_str, stops, origin_str)
        url2 = None
    else:
        # First leg: depot → stops[0..7] → stops[8]  (8 waypoints + destination)
        url1 = make_url(origin_str, stops[:8], fmt(stops[8]))
        # Second leg: stops[8] → stops[9..] → depot
        url2 = make_url(fmt(stops[8]), stops[9:], origin_str)

    return url1, url2


# ── Message type prefix map ────────────────────────────────────────────────────

MSG_TYPE_PREFIX = {
    "error":     "2",
    "exception": "3",
    "event":     "4",
    "message":   "5",
}


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/logout")
def logout():
    return redirect("/.auth/logout?post_logout_redirect_uri=/")


@app.route("/")
@login_required
def index():
    email = get_current_user()
    return render_template("index.html", role=get_role(email), username=email)


# ── Locations ──────────────────────────────────────────────────────────────────

@app.route("/api/locations")
@login_required
def get_locations():
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT MachineName, MachineCode, Latitude, Longitude
            FROM MachineLookup
            ORDER BY MachineName
        """)
        rows = cursor.fetchall()
        conn.close()
        return jsonify([{
            "name": row[0],
            "code": row[1],
            "lat":  float(row[2]) if row[2] is not None else None,
            "lon":  float(row[3]) if row[3] is not None else None,
        } for row in rows])
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500


# ── Sales ──────────────────────────────────────────────────────────────────────

@app.route("/api/dispenses")
@login_required
def get_dispenses():
    start_str = request.args.get("start",   "").strip()
    end_str   = request.args.get("end",     "").strip()
    machine   = request.args.get("machine", "").strip()

    if not start_str or not end_str:
        return jsonify({"error": "Please provide both a start and end datetime."}), 400

    try:
        start_dt = datetime.strptime(start_str, "%Y-%m-%d %H:%M")
        end_dt   = datetime.strptime(end_str,   "%Y-%m-%d %H:%M")
    except ValueError:
        return jsonify({"error": "Invalid date format. Expected YYYY-MM-DD HH:MM."}), 400

    if start_dt >= end_dt:
        return jsonify({"error": "Start time must be before end time."}), 400

    start_ole = to_ole_date(start_dt)
    end_ole   = to_ole_date(end_dt)

    machine_filter = "AND CAST(mdt.[Machine Code] AS NVARCHAR(50)) = %s" if machine else ""

    query = f"""
        SELECT
            mdt.[Event Code]    AS EventCode,
            MIN(mc.EventName)   AS SKUName,
            COUNT(*)            AS DispenseCount
        FROM [MasterData Table] mdt
        INNER JOIN MasterCode mc
            ON mdt.[Event Code] = mc.ItemCode
        WHERE CAST(mdt.[Date Time] AS float) >= {start_ole}
          AND CAST(mdt.[Date Time] AS float) <= {end_ole}
          AND LEN(CAST(mdt.[Event Code] AS NVARCHAR(20))) = 6 AND CAST(mdt.[Event Code] AS NVARCHAR(20)) LIKE '1%'
          {machine_filter}
        GROUP BY mdt.[Event Code]
        ORDER BY DispenseCount DESC
    """

    params = (machine,) if machine else ()

    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()
        results = [{"code": int(row[0]), "sku": row[1], "count": int(row[2])} for row in rows]
        return jsonify({"results": results, "total": sum(r["count"] for r in results)})
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500


# ── Messages ───────────────────────────────────────────────────────────────────

@app.route("/api/messages")
@login_required
def get_messages():
    start_str = request.args.get("start",   "").strip()
    end_str   = request.args.get("end",     "").strip()
    machine   = request.args.get("machine", "").strip()
    msg_type  = request.args.get("type",    "").strip().lower()

    if not start_str or not end_str:
        return jsonify({"error": "Please provide both a start and end datetime."}), 400

    prefix = MSG_TYPE_PREFIX.get(msg_type)
    if not prefix:
        return jsonify({"error": "Please select a valid message type."}), 400

    try:
        start_dt = datetime.strptime(start_str, "%Y-%m-%d %H:%M")
        end_dt   = datetime.strptime(end_str,   "%Y-%m-%d %H:%M")
    except ValueError:
        return jsonify({"error": "Invalid date format. Expected YYYY-MM-DD HH:MM."}), 400

    if start_dt >= end_dt:
        return jsonify({"error": "Start time must be before end time."}), 400

    start_ole = to_ole_date(start_dt)
    end_ole   = to_ole_date(end_dt)

    machine_filter = "AND CAST(mdt.[Machine Code] AS NVARCHAR(50)) = %s" if machine else ""

    query = f"""
        SELECT
            CAST(mdt.[Date Time] AS FLOAT) AS EventTime,
            mc.EventName                   AS MessageName,
            ISNULL(ml.MachineName, CAST(mdt.[Machine Code] AS NVARCHAR(50))) AS MachineName
        FROM [MasterData Table] mdt
        INNER JOIN MasterCode mc
            ON mdt.[Event Code] = mc.ItemCode
        LEFT JOIN MachineLookup ml
            ON CAST(mdt.[Machine Code] AS NVARCHAR(50)) = CAST(ml.MachineCode AS NVARCHAR(50))
        WHERE CAST(mdt.[Date Time] AS FLOAT) >= {start_ole}
          AND CAST(mdt.[Date Time] AS FLOAT) <= {end_ole}
          AND CAST(mdt.[Event Code] AS NVARCHAR(20)) LIKE '{prefix}%'
          {machine_filter}
        ORDER BY mdt.[Date Time] DESC
    """

    params = (machine,) if machine else ()

    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()
        results = []
        for row in rows:
            dt = from_ole_date(row[0])
            results.append({
                "timestamp": dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "Unknown",
                "name":    row[1],
                "machine": row[2],
            })
        return jsonify({"results": results})
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500


# ── Topups ─────────────────────────────────────────────────────────────────────

@app.route("/api/topups")
@login_required
def get_topups():
    """All machines with topup state; vends_since computed live from MasterData."""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT
                ml.MachineName,
                ml.MachineCode,
                ml.LastTopupTimestamp,
                ml.CountBeforeLastTopup,
                (
                    SELECT COUNT(*)
                    FROM [MasterData Table] mdt
                    WHERE CAST(mdt.[Machine Code] AS NVARCHAR(50)) = CAST(ml.MachineCode AS NVARCHAR(50))
                      AND LEN(CAST(mdt.[Event Code] AS NVARCHAR(20))) = 6 AND CAST(mdt.[Event Code] AS NVARCHAR(20)) LIKE '1%'
                      AND (
                          ml.LastTopupTimestamp IS NULL
                          OR CAST(mdt.[Date Time] AS FLOAT) >= ml.LastTopupTimestamp
                      )
                ) AS VendsSince
            FROM MachineLookup ml
            ORDER BY ml.MachineName
        """)
        rows = cursor.fetchall()
        conn.close()

        machines = []
        for row in rows:
            last_dt = from_ole_date(row[2])
            machines.append({
                "name":         row[0],
                "code":         row[1],
                "last_topup":   last_dt.strftime("%Y-%m-%d %H:%M") if last_dt else None,
                "vends_before": int(row[3]) if row[3] is not None else None,
                "vends_since":  int(row[4]),
            })
        return jsonify({"machines": machines})
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500


@app.route("/api/topups/<path:code>", methods=["POST"])
@login_required
def log_topup(code):
    """Log a topup for one machine."""
    data = request.get_json()
    ts_str = (data.get("timestamp") or "").strip()
    if not ts_str:
        return jsonify({"error": "Please provide a topup datetime."}), 400

    try:
        topup_dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M")
    except ValueError:
        return jsonify({"error": "Invalid datetime format. Expected YYYY-MM-DD HH:MM."}), 400

    new_ole = to_ole_date(topup_dt)

    try:
        conn = get_connection()
        cursor = conn.cursor()

        # Read current LastTopupTimestamp
        cursor.execute(
            "SELECT LastTopupTimestamp FROM MachineLookup WHERE MachineCode = %s",
            (code,)
        )
        row = cursor.fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "Machine not found."}), 404

        current_ole = row[0]

        # Count vends since the current last topup (to snapshot as CountBeforeLastTopup)
        if current_ole is not None:
            cursor.execute(f"""
                SELECT COUNT(*) FROM [MasterData Table]
                WHERE CAST([Machine Code] AS NVARCHAR(50)) = %s
                  AND LEN(CAST([Event Code] AS NVARCHAR(20))) = 6 AND CAST([Event Code] AS NVARCHAR(20)) LIKE '1%'
                  AND CAST([Date Time] AS FLOAT) >= {float(current_ole)}
            """, (code,))
        else:
            cursor.execute("""
                SELECT COUNT(*) FROM [MasterData Table]
                WHERE CAST([Machine Code] AS NVARCHAR(50)) = %s
                  AND LEN(CAST([Event Code] AS NVARCHAR(20))) = 6 AND CAST([Event Code] AS NVARCHAR(20)) LIKE '1%'
            """, (code,))
        vends_since = int(cursor.fetchone()[0])

        # Shift timestamps and save snapshot
        cursor.execute(f"""
            UPDATE MachineLookup
            SET PreviousTopupTimestamp = LastTopupTimestamp,
                LastTopupTimestamp     = {new_ole},
                CountBeforeLastTopup   = %s
            WHERE MachineCode = %s
        """, (vends_since, code))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500


@app.route("/api/topups/<path:code>", methods=["DELETE"])
@login_required
def delete_topup(code):
    """Undo the last topup for one machine."""
    try:
        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT LastTopupTimestamp FROM MachineLookup WHERE MachineCode = %s",
            (code,)
        )
        row = cursor.fetchone()
        if not row or row[0] is None:
            conn.close()
            return jsonify({"error": "No topup recorded for this machine."}), 400

        # Revert: LastTopupTimestamp ← PreviousTopupTimestamp, clear the rest
        cursor.execute("""
            UPDATE MachineLookup
            SET LastTopupTimestamp     = PreviousTopupTimestamp,
                PreviousTopupTimestamp = NULL,
                CountBeforeLastTopup   = 0
            WHERE MachineCode = %s
        """, (code,))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500


# ── Dispatch planning ──────────────────────────────────────────────────────────

SHIFT_HOURS   = 10.0   # 08:00–18:00
HOURS_PER_STOP = 1.0
AVG_SPEED_KMH  = 35.0  # Singapore urban average
ROAD_FACTOR    = 1.3   # haversine-to-road distance multiplier

DRIVER_COLOURS = [
    "#1a56db", "#0891b2", "#7c3aed", "#059669",
    "#d97706", "#dc2626", "#db2777", "#65a30d",
]


@app.route("/api/dispatch/plan", methods=["POST"])
@dispatch_or_admin_required
def plan_dispatch():
    data      = request.get_json() or {}
    codes     = [str(c).strip() for c in data.get("machine_codes", []) if c]
    try:
        num_drivers = max(1, min(20, int(data.get("num_drivers", 1))))
    except (ValueError, TypeError):
        return jsonify({"error": "num_drivers must be an integer between 1 and 20."}), 400

    if not codes:
        return jsonify({"error": "No locations selected."}), 400

    try:
        conn   = get_connection()
        cursor = conn.cursor()

        # Fetch selected locations that have coordinates
        placeholders = ", ".join(["%s"] * len(codes))
        cursor.execute(f"""
            SELECT
                ml.MachineCode,
                ml.MachineName,
                ml.Latitude,
                ml.Longitude,
                ml.LastTopupTimestamp,
                (
                    SELECT COUNT(*)
                    FROM [MasterData Table] mdt
                    WHERE CAST(mdt.[Machine Code] AS NVARCHAR(50)) = CAST(ml.MachineCode AS NVARCHAR(50))
                      AND LEN(CAST(mdt.[Event Code] AS NVARCHAR(20))) = 6 AND CAST(mdt.[Event Code] AS NVARCHAR(20)) LIKE '1%'
                      AND (
                          ml.LastTopupTimestamp IS NULL
                          OR CAST(mdt.[Date Time] AS FLOAT) >= ml.LastTopupTimestamp
                      )
                ) AS VendsSince
            FROM MachineLookup ml
            WHERE ml.MachineCode IN ({placeholders})
              AND ml.Latitude  IS NOT NULL
              AND ml.Longitude IS NOT NULL
        """, tuple(codes))
        rows = cursor.fetchall()
        conn.close()
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500

    found_codes = {str(row[0]) for row in rows}
    skipped     = [c for c in codes if c not in found_codes]

    if not rows:
        return jsonify({"error": "None of the selected locations have coordinates on file."}), 400

    stops = [{
        "code":        str(row[0]),
        "name":        row[1],
        "lat":         float(row[2]),
        "lon":         float(row[3]),
        "last_topup":  from_ole_date(row[4]).strftime("%Y-%m-%d") if row[4] else None,
        "vends_since": int(row[5]),
    } for row in rows]

    # Cap drivers to number of stops
    effective_drivers = min(num_drivers, len(stops))
    warnings = []

    # 1. Build one globally optimised tour starting from the factory depot
    global_tour = nearest_neighbor_tsp(stops, start_lat=DEPOT_LAT, start_lon=DEPOT_LON)
    global_tour = two_opt_improve(global_tour)

    # 2. Split into equal consecutive segments — adjacent stops in the
    #    optimised tour are already geographically compact, so each
    #    segment forms a natural area and all drivers get equal workload.
    segments = split_tour_equally(global_tour, effective_drivers)

    routes = []
    for i, segment in enumerate(segments):
        # Estimate total shift time for this segment
        travel_h = 0.0
        for j in range(1, len(segment)):
            d = haversine(segment[j-1]['lat'], segment[j-1]['lon'],
                          segment[j]['lat'],   segment[j]['lon'])
            travel_h += (d * ROAD_FACTOR) / AVG_SPEED_KMH
        total_h = travel_h + len(segment) * HOURS_PER_STOP
        within  = total_h <= SHIFT_HOURS

        if not within:
            warnings.append(
                f"Driver {i+1}: estimated {total_h:.1f}h exceeds the 10-hour shift window."
            )

        url1, url2 = build_maps_url(segment)
        routes.append({
            "driver":          i + 1,
            "colour":          DRIVER_COLOURS[i % len(DRIVER_COLOURS)],
            "stops":           segment,
            "stop_count":      len(segment),
            "estimated_hours": round(total_h, 1),
            "within_shift":    within,
            "maps_url":        url1,
            "maps_url_2":      url2,
        })

    if skipped:
        warnings.append(
            f"{len(skipped)} location(s) skipped — no coordinates on file: "
            + ", ".join(skipped)
        )
    if effective_drivers < num_drivers:
        warnings.append(
            f"Only {effective_drivers} driver(s) needed for {len(stops)} stop(s)."
        )

    return jsonify({"routes": routes, "warnings": warnings})


# ── Admin: location management ─────────────────────────────────────────────────

@app.route("/api/admin/locations", methods=["POST"])
@admin_required
def add_location():
    data = request.get_json()
    name = (data.get("name") or "").strip()
    code = (data.get("code") or "").strip()
    if not name or not code:
        return jsonify({"error": "Location name and machine code are both required."}), 400
    try:
        lat = float(data["lat"]) if data.get("lat") not in (None, "") else None
        lon = float(data["lon"]) if data.get("lon") not in (None, "") else None
    except (ValueError, TypeError):
        return jsonify({"error": "Latitude and Longitude must be numeric."}), 400
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO MachineLookup (MachineName, MachineCode, Latitude, Longitude) VALUES (%s, %s, %s, %s)",
            (name, code, lat, lon),
        )
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500


@app.route("/api/admin/locations/<path:code>", methods=["PUT"])
@dispatch_or_admin_required
def update_location(code):
    data = request.get_json()
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Location name is required."}), 400
    try:
        lat = float(data["lat"]) if data.get("lat") not in (None, "") else None
        lon = float(data["lon"]) if data.get("lon") not in (None, "") else None
    except (ValueError, TypeError):
        return jsonify({"error": "Latitude and Longitude must be numeric."}), 400
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE MachineLookup SET MachineName=%s, Latitude=%s, Longitude=%s WHERE MachineCode=%s",
            (name, lat, lon, code),
        )
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500


# ── Heartbeat ─────────────────────────────────────────────────────────────────

@app.route("/api/heartbeat")
@login_required
def get_heartbeat():
    """
    For every machine in MachineLookup return:
      - last_any_event  : OLE float of most recent event of any kind
      - last_error_event: OLE float of most recent error/exception (codes 2x or 3x)
    Status logic applied in the browser using the threshold sent with the response.
    """
    threshold_min = HEARTBEAT_THRESHOLD_MINUTES
    try:
        conn   = get_connection()
        cursor = conn.cursor()
        # Single-pass GROUP BY for efficiency.
        # LEFT JOIN so machines with zero history still appear.
        cursor.execute("""
            SELECT
                ml.MachineName,
                ml.MachineCode,
                MAX(md.[Date Time]) AS LastAnyEvent,
                MAX(CASE
                    WHEN LEFT(CAST(md.[Event Code] AS VARCHAR(20)), 1) IN ('2','3')
                    THEN md.[Date Time]
                END) AS LastErrorEvent
            FROM MachineLookup ml
            LEFT JOIN [MasterData Table] md ON ml.MachineCode = md.[Machine Code]
            GROUP BY ml.MachineName, ml.MachineCode
            ORDER BY ml.MachineName
        """)
        rows = cursor.fetchall()
        conn.close()
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500

    now_ole = to_ole_date(datetime.utcnow())
    machines = []
    for name, code, last_any_ole, last_err_ole in rows:
        last_any_min  = (now_ole - float(last_any_ole))  * 1440 if last_any_ole  is not None else None
        last_err_min  = (now_ole - float(last_err_ole))  * 1440 if last_err_ole  is not None else None

        if last_any_min is None or last_any_min > threshold_min:
            status = "red"
        elif last_err_min is not None and last_err_min < 60:
            status = "yellow"
        else:
            status = "green"

        machines.append({
            "name":          name,
            "code":          str(code),
            "status":        status,
            "last_any_min":  round(last_any_min,  1) if last_any_min  is not None else None,
            "last_err_min":  round(last_err_min,  1) if last_err_min  is not None else None,
        })

    return jsonify({
        "machines":           machines,
        "threshold_minutes":  threshold_min,
        "counts": {
            "green":  sum(1 for m in machines if m["status"] == "green"),
            "yellow": sum(1 for m in machines if m["status"] == "yellow"),
            "red":    sum(1 for m in machines if m["status"] == "red"),
        },
    })


@app.route("/api/admin/heartbeat-analysis")
@admin_required
def heartbeat_analysis():
    """
    Analyse the average gap between consecutive messages during off-hours
    (23:00-06:00 local, proxied via OLE fractional part) over the last 90 days.
    Use the result to calibrate HEARTBEAT_THRESHOLD_MINUTES.
    """
    frac_23 = 23.0 / 24.0   # 0.9583…
    frac_06 =  6.0 / 24.0   # 0.25
    ole_90  = to_ole_date(datetime.utcnow() - timedelta(days=90))

    try:
        conn   = get_connection()
        cursor = conn.cursor()
        # [Date Time] - FLOOR([Date Time]) extracts the fractional (time-of-day) part
        cursor.execute(f"""
            SELECT [Machine Code], [Date Time]
            FROM [MasterData Table]
            WHERE [Date Time] >= {ole_90}
              AND (
                ([Date Time] - FLOOR([Date Time])) >= {frac_23}
                OR ([Date Time] - FLOOR([Date Time])) <  {frac_06}
              )
            ORDER BY [Machine Code], [Date Time]
        """)
        rows = cursor.fetchall()
        conn.close()
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500

    if not rows:
        return jsonify({"error": "No off-hours data found in the last 90 days."}), 404

    # Group events by machine and compute consecutive gaps (in minutes)
    from collections import defaultdict
    machine_events = defaultdict(list)
    for code, ole_dt in rows:
        machine_events[str(code)].append(float(ole_dt))

    gaps = []
    for events in machine_events.values():
        events.sort()
        for j in range(1, len(events)):
            gap_min = (events[j] - events[j - 1]) * 1440  # days → minutes
            # Exclude gaps that span across the off-hours window boundary
            # (i.e., > 7 hours means a new night, not a within-night gap)
            if 0 < gap_min <= 420:
                gaps.append(gap_min)

    if not gaps:
        return jsonify({"error": "Could not compute gaps (too few consecutive off-hours events)."}), 404

    gaps.sort()
    n   = len(gaps)
    avg = sum(gaps) / n
    p50 = gaps[int(0.50 * n)]
    p90 = gaps[int(0.90 * n)]
    p95 = gaps[int(0.95 * n)]
    mx  = gaps[-1]

    recommendation = round(p95 * 1.25)  # 25 % headroom above 95th percentile

    print(f"[heartbeat-analysis] gaps={n} machines={len(machine_events)} "
          f"avg={avg:.1f}m p50={p50:.1f}m p90={p90:.1f}m p95={p95:.1f}m max={mx:.1f}m "
          f"recommended_threshold={recommendation}m")

    return jsonify({
        "sample_gaps":             n,
        "machines_with_data":      len(machine_events),
        "avg_gap_minutes":         round(avg, 1),
        "p50_gap_minutes":         round(p50, 1),
        "p90_gap_minutes":         round(p90, 1),
        "p95_gap_minutes":         round(p95, 1),
        "max_gap_minutes":         round(mx,  1),
        "recommended_threshold":   recommendation,
        "current_threshold":       HEARTBEAT_THRESHOLD_MINUTES,
        "note": (
            "Set HEARTBEAT_THRESHOLD_MINUTES in app.py to 'recommended_threshold' "
            "(or a value you're comfortable with), then redeploy."
        ),
    })


if __name__ == "__main__":
    app.run(debug=True, port=5000)
