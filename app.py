import base64
import json
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
          AND CAST(mdt.[Event Code] AS NVARCHAR(20)) LIKE '1%'
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
                      AND CAST(mdt.[Event Code] AS NVARCHAR(20)) LIKE '1%'
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
                  AND CAST([Event Code] AS NVARCHAR(20)) LIKE '1%'
                  AND CAST([Date Time] AS FLOAT) >= {float(current_ole)}
            """, (code,))
        else:
            cursor.execute("""
                SELECT COUNT(*) FROM [MasterData Table]
                WHERE CAST([Machine Code] AS NVARCHAR(50)) = %s
                  AND CAST([Event Code] AS NVARCHAR(20)) LIKE '1%'
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
@admin_required
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


if __name__ == "__main__":
    app.run(debug=True, port=5000)
