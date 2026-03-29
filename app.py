import base64
import json
from flask import Flask, render_template, request, jsonify, redirect
from functools import wraps
import pymssql
from datetime import datetime
import config

app = Flask(__name__)


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _decode_principal():
    """
    Azure Easy Auth injects X-MS-CLIENT-PRINCIPAL — a base64-encoded JSON
    containing the user's claims (email, assigned app roles, etc.).
    Returns the decoded dict, or None if the header is absent (local dev).
    """
    b64 = request.headers.get("X-MS-CLIENT-PRINCIPAL", "")
    if not b64:
        return None
    try:
        return json.loads(base64.b64decode(b64).decode("utf-8"))
    except Exception:
        return None


def get_current_user():
    """Return the logged-in user's email (lowercase)."""
    principal = _decode_principal()
    if principal:
        for claim in principal.get("claims", []):
            if claim.get("typ") == "preferred_username":
                return claim.get("val", "").strip().lower()
    # Local dev fallback
    return config.DEV_USER_EMAIL.strip().lower() if config.DEV_USER_EMAIL else ""


def get_role(email=None):
    """
    Return the user's role ('admin' or 'sales') as assigned in
    Azure Entra ID → Enterprise Applications → Users and groups.
    Falls back to DEV_ROLE in config.py when running locally.
    """
    principal = _decode_principal()
    if principal:
        for claim in principal.get("claims", []):
            if claim.get("typ") == "roles":
                return claim.get("val", "").strip().lower()
    # Local dev fallback
    return config.DEV_ROLE.strip().lower() if config.DEV_ROLE else None


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        email = get_current_user()
        if not email:
            # Not authenticated — send to Azure AD login
            return redirect("/.auth/login/aad?post_login_redirect_uri=/")
        if not get_role(email):
            # Authenticated but not in ROLE_MAP — deny access
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


# ── DB helpers ────────────────────────────────────────────────────────────────

def to_ole_date(dt_obj):
    ole_epoch = datetime(1899, 12, 30)
    delta = dt_obj - ole_epoch
    return delta.days + (delta.seconds + delta.microseconds / 1e6) / 86400.0


def get_connection():
    return pymssql.connect(
        server=config.DB_SERVER,
        database=config.DB_NAME,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        tds_version="7.4",
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/logout")
def logout():
    return redirect("/.auth/logout?post_logout_redirect_uri=/")


@app.route("/")
@login_required
def index():
    email = get_current_user()
    return render_template("index.html", role=get_role(email), username=email)


@app.route("/api/locations")
@login_required
def get_locations():
    query = "SELECT MachineName, MachineCode FROM MachineLookup ORDER BY MachineName"
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(query)
        rows = cursor.fetchall()
        conn.close()
        return jsonify([{"name": row[0], "code": row[1]} for row in rows])
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500


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

    # OLE dates are embedded as numeric literals rather than passed as parameters
    # because pymssql silently fails when binding Python floats to a decimal column.
    # This is safe: start_ole and end_ole are computed from datetime.strptime output.
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


@app.route("/api/admin/locations", methods=["POST"])
@admin_required
def add_location():
    data = request.get_json()
    name = (data.get("name") or "").strip()
    code = (data.get("code") or "").strip()
    if not name or not code:
        return jsonify({"error": "Location name and machine code are both required."}), 400
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO MachineLookup (MachineName, MachineCode) VALUES (%s, %s)",
            (name, code),
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
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE MachineLookup SET MachineName = %s WHERE MachineCode = %s",
            (name, code),
        )
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5000)
