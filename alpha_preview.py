"""
Alpha app — streamlined UI mounted at /alpha (LIVE ACTIONS)
===========================================================
Serves the 5-area streamlined UI using REAL data from the SAME database the live
app already reads. Mounted as a Flask Blueprint inside the existing app so it
reuses the app's DB connectivity and Easy Auth — no new web app, no new
credentials, no new database.

Since 2026-07-20 the buttons are WIRED: the front-end calls the EXISTING
production work-order API (/api/wo/*) for report / assign / complete /
auto-plan, so every action goes through the same validation, role checks,
activity log, top-up sync and movement location-cutover logic as the main
dashboard. This module itself remains SELECT-only — all writes happen in
workorders.py endpoints.

  * Fully self-contained: importing this module only defines routes; it never
    runs DB work at import time. Registration in app.py is wrapped in
    try/except as an extra guard.
  * Behind the app's existing Easy Auth (AAD) like every other route.

Routes:  GET /alpha                -> the streamlined UI
          GET /alpha/api/bootstrap  -> {health, machines, work, user}
"""

import base64
import json as _json
from datetime import datetime, timedelta

import pymssql
from flask import Blueprint, jsonify, render_template, request

import config  # reuse the live app's DB creds

alpha_bp = Blueprint("alpha_preview", __name__)

OLE_EPOCH = datetime(1899, 12, 30)
def _to_ole(dt):
    d = dt - OLE_EPOCH
    return d.days + (d.seconds + d.microseconds / 1e6) / 86400.0

HEARTBEAT_THRESHOLD_MINUTES = 225
JOBORDER_STATUS = {0: "assigned", 1: "needs_assistance", 2: "pending_review", 3: "closed"}
MOVEMENT_STATUS = {0: "scheduled", 1: "in_progress", 2: "completed"}
PRIORITY        = {0: "low", 1: "normal", 2: "high"}


def _current_principal():
    b64 = request.headers.get("X-MS-CLIENT-PRINCIPAL", "")
    if not b64:
        return None
    try:
        return _json.loads(base64.b64decode(b64).decode("utf-8"))
    except Exception:
        return None


def _current_user():
    p = _current_principal()
    if p:
        for c in p.get("claims", []):
            if c.get("typ") == "preferred_username":
                return (c.get("val") or "").strip().lower()
    return (getattr(config, "DEV_USER_EMAIL", "") or "").strip().lower()


def _current_roles():
    out = []
    p = _current_principal()
    if p:
        for c in p.get("claims", []):
            if c.get("typ") == "roles":
                v = (c.get("val") or "").strip().lower()
                if v and v not in out:
                    out.append(v)
    if not out and getattr(config, "DEV_ROLE", ""):
        out = [config.DEV_ROLE.strip().lower()]
    return out


def _conn():
    return pymssql.connect(
        server=config.DB_SERVER, database=config.DB_NAME,
        user=config.DB_USER, password=config.DB_PASSWORD,
        tds_version="7.4", login_timeout=8, timeout=20,
    )


def _ago(days):
    if days is None:
        return "—"
    return "today" if days < 1 else f"{int(days)}d ago"


def _fetch_machines(cur):
    cur.execute("""
        SELECT MachineName, MachineCode, Latitude, Longitude,
               ISNULL(IsActive,1) AS Active, LastTopupTimestamp
        FROM MachineLookup ORDER BY MachineName
    """)
    out = {}
    now = _to_ole(datetime.utcnow())
    for name, code, lat, lon, active, last_top in cur.fetchall():
        c = str(code)
        out[c] = {
            "code": c, "name": name,
            "lat": float(lat) if lat is not None else None,
            "lon": float(lon) if lon is not None else None,
            "active": bool(active), "status": "g", "vendsSince": None,
            "lastRefill": "—" if last_top is None else _ago(now - float(last_top)),
        }
    return out


def _apply_heartbeat(cur, machines):
    cur.execute("""
        SELECT ml.MachineCode,
               MAX(md.[Date Time]) AS LastAny,
               MAX(CASE WHEN LEFT(CAST(md.[Event Code] AS VARCHAR(20)),1) IN ('2','3')
                        THEN md.[Date Time] END) AS LastErr
        FROM MachineLookup ml
        LEFT JOIN [MasterData Table] md ON ml.MachineCode = md.[Machine Code]
        WHERE ISNULL(ml.IsActive,1) = 1
        GROUP BY ml.MachineCode
    """)
    now = _to_ole(datetime.utcnow())
    for code, last_any, last_err in cur.fetchall():
        c = str(code)
        if c not in machines:
            continue
        any_min = (now - float(last_any)) * 1440 if last_any is not None else None
        err_min = (now - float(last_err)) * 1440 if last_err is not None else None
        if any_min is None or any_min > HEARTBEAT_THRESHOLD_MINUTES:
            machines[c]["status"] = "r"
        elif err_min is not None and err_min < 60:
            machines[c]["status"] = "y"
        else:
            machines[c]["status"] = "g"


def _apply_vends_since(cur, machines):
    """Per-machine vend count since last refill (deduped to distinct
    machine+instant, same rule as the main dashboard). Single native-typed
    scan (~3-4s) — index-friendly GROUP BY, join via TRY_CAST."""
    cur.execute("""
        SELECT ml.MachineCode, COUNT(v.t) AS VendsSince
        FROM MachineLookup ml
        LEFT JOIN (
            SELECT [Machine Code] AS mc, [Date Time] AS t
            FROM [MasterData Table]
            WHERE LEN(CAST([Event Code] AS NVARCHAR(20))) = 6
              AND CAST([Event Code] AS NVARCHAR(20)) LIKE '1%%'
            GROUP BY [Machine Code], [Date Time]
        ) v ON v.mc = TRY_CAST(ml.MachineCode AS INT)
           AND (ml.LastTopupTimestamp IS NULL OR CAST(v.t AS FLOAT) >= ml.LastTopupTimestamp)
        WHERE ISNULL(ml.IsActive,1) = 1
        GROUP BY ml.MachineCode
    """)
    for code, n in cur.fetchall():
        c = str(code)
        if c in machines:
            machines[c]["vendsSince"] = int(n)


def _fetch_sales(cur):
    # vend [Date Time] floats are SGT wall-clock — shift now() accordingly
    start = _to_ole(datetime.utcnow() + timedelta(hours=8) - timedelta(days=7))
    cur.execute("""
        SELECT COUNT(*) FROM (
            SELECT DISTINCT mdt.[Machine Code], CAST(mdt.[Date Time] AS FLOAT) AS t
            FROM [MasterData Table] mdt
            WHERE CAST(mdt.[Date Time] AS FLOAT) >= %s
              AND LEN(CAST(mdt.[Event Code] AS NVARCHAR(20))) = 6
              AND CAST(mdt.[Event Code] AS NVARCHAR(20)) LIKE '1%%'
        ) _v
    """, (start,))
    r = cur.fetchone()
    return int(r[0]) if r and r[0] is not None else None


def _fetch_work(cur):
    work = []
    try:
        cur.execute("""SELECT TOP 100 ComplaintID, DisplayID, Description, Source,
                       MachineName, MachineCode, JobOrderID
                       FROM WO_Complaints WHERE StatusCode = 0 ORDER BY ComplaintID DESC""")
        for cid, disp, desc, src, mname, mcode, jid in cur.fetchall():
            if jid:
                continue
            work.append({"id": disp or f"CMP-{cid}", "kind": "complaint", "rid": int(cid),
                         "type": "service",
                         "machine": str(mcode) if mcode else "?", "machineName": mname,
                         "desc": (desc or "(no description)")[:140], "priority": "normal",
                         "status": "new", "assignedTo": None, "source": src or "Complaint"})
    except Exception:
        pass
    try:
        cur.execute("""SELECT TOP 200 JobOrderID, DisplayID, MachineName, MachineCode,
                       AssignedTo, PriorityCode, StatusCode, Diagnosis
                       FROM WO_JobOrders ORDER BY CreatedAt DESC, JobOrderID DESC""")
        for jid, disp, mname, mcode, asg, pc, sc, diag in cur.fetchall():
            lbl = JOBORDER_STATUS.get(int(sc) if sc is not None else 0, "assigned")
            # pending_review = operator finished; treat as done in this UI so a
            # completed job doesn't bounce back into My Jobs (manager still
            # closes it via review in the main dashboard).
            status = "done" if lbl in ("closed", "pending_review") else ("assigned" if asg else "new")
            work.append({"id": disp or f"JOB-{jid}", "kind": "joborder", "rid": int(jid),
                         "type": "service",
                         "machine": str(mcode) if mcode else "?", "machineName": mname,
                         "desc": (diag or "Service job order")[:140],
                         "priority": PRIORITY.get(int(pc) if pc is not None else 1, "normal"),
                         "status": status, "assignedTo": asg, "source": "Tech Support"})
    except Exception:
        pass
    try:
        cur.execute("""SELECT TOP 200 DeliveryOrderID, MachineName, MachineCode,
                       AssignedTo, Priority, Status
                       FROM WO_DeliveryOrders ORDER BY CreatedAt DESC, DeliveryOrderID DESC""")
        for did, mname, mcode, asg, pri, st in cur.fetchall():
            done = (st or "").lower() == "completed"
            work.append({"id": f"DEL-{did}", "kind": "delivery", "rid": int(did),
                         "type": "delivery",
                         "machine": str(mcode) if mcode else "?", "machineName": mname,
                         "desc": "Refill / delivery", "priority": (pri or "normal").lower(),
                         "status": "done" if done else ("assigned" if asg else "new"),
                         "assignedTo": asg, "source": "Delivery"})
    except Exception:
        pass
    try:
        cur.execute("""SELECT TOP 200 MovementOrderID, DisplayID, MovementType, MachineCode,
                       FromLocation, ToLocation, StatusCode, AssignedTo
                       FROM WO_MovementOrders ORDER BY CreatedAt DESC, MovementOrderID DESC""")
        for mid, disp, mtype, mcode, frm, to, sc, asg in cur.fetchall():
            lbl = MOVEMENT_STATUS.get(int(sc) if sc is not None else 0, "scheduled")
            desc = f"{(mtype or 'move').title()}"
            if frm or to:
                desc += f": {frm or '?'} → {to or '?'}"
            work.append({"id": disp or f"MOV-{mid}", "kind": "movement", "rid": int(mid),
                         "type": "movement",
                         "machine": str(mcode) if mcode else "?", "machineName": None,
                         "desc": desc[:140], "priority": "normal",
                         "status": "done" if lbl == "completed" else ("assigned" if asg else "new"),
                         "assignedTo": asg, "source": "Movement"})
    except Exception:
        pass
    return work


@alpha_bp.route("/alpha")
def alpha_index():
    return render_template("alpha_preview.html",
                           username=_current_user(), roles=_current_roles())


@alpha_bp.route("/alpha/api/bootstrap")
def alpha_bootstrap():
    conn = None
    try:
        conn = _conn()
        cur = conn.cursor()
        machines = _fetch_machines(cur)
        _apply_heartbeat(cur, machines)
        try:
            _apply_vends_since(cur, machines)
        except Exception as e:
            import sys
            print("[alpha] vends-since skipped:", e, file=sys.stderr)
        try:
            sales = _fetch_sales(cur)
        except Exception:
            sales = None
        work = _fetch_work(cur)
        return jsonify({
            "health": {"live": True, "reason": "Connected (read-only)", "salesVends": sales,
                       "counts": {"machines": len(machines), "work": len(work)}},
            "machines": list(machines.values()), "work": work,
        })
    except Exception as e:
        import sys
        print("[alpha] DB error:", e, file=sys.stderr)
        return jsonify({"health": {"live": False, "reason": "DB unreachable — see server log",
                                    "salesVends": None},
                        "machines": [], "work": []})
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
