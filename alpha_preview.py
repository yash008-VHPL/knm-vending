"""
Alpha preview (ADDITIVE, READ-ONLY) — streamlined UI mounted at /alpha
=====================================================================
Serves the proposed 5-area streamlined UI using REAL data from the SAME database
the live app already reads. It is mounted as a Flask Blueprint inside the existing
app so it reuses the app's DB connectivity and Easy Auth — no new web app, no new
credentials, no new database.

SAFETY:
  * Strictly READ-ONLY: every DB statement here is a SELECT. No INSERT/UPDATE/DELETE/DDL.
  * All UI actions (report/assign/complete) mutate in-memory JS only — never the DB.
  * Fully self-contained: importing this module only defines routes; it never runs
    DB work at import time, so it cannot affect app startup. Registration in app.py
    is wrapped in try/except as an extra guard.
  * Behind the app's existing Easy Auth (AAD) like every other route — not public.

Routes:  GET /alpha              -> the streamlined UI
          GET /alpha/api/bootstrap -> {health, machines, work}
"""

from datetime import datetime, timedelta

import pymssql
from flask import Blueprint, jsonify, render_template

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


def _fetch_sales(cur):
    start = _to_ole(datetime.utcnow() - timedelta(days=7))
    cur.execute("""
        SELECT COUNT(*) FROM [MasterData Table] mdt
        WHERE CAST(mdt.[Date Time] AS FLOAT) >= %s
          AND LEN(CAST(mdt.[Event Code] AS NVARCHAR(20))) = 6
          AND CAST(mdt.[Event Code] AS NVARCHAR(20)) LIKE '1%%'
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
            work.append({"id": disp or f"CMP-{cid}", "type": "service",
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
            status = "done" if lbl == "closed" else ("assigned" if asg else "new")
            work.append({"id": disp or f"JOB-{jid}", "type": "service",
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
            work.append({"id": f"DEL-{did}", "type": "delivery",
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
            work.append({"id": disp or f"MOV-{mid}", "type": "movement",
                         "machine": str(mcode) if mcode else "?", "machineName": None,
                         "desc": desc[:140], "priority": "normal",
                         "status": "done" if lbl == "completed" else ("assigned" if asg else "new"),
                         "assignedTo": asg, "source": "Movement"})
    except Exception:
        pass
    return work


@alpha_bp.route("/alpha")
def alpha_index():
    return render_template("alpha_preview.html")


@alpha_bp.route("/alpha/api/bootstrap")
def alpha_bootstrap():
    conn = None
    try:
        conn = _conn()
        cur = conn.cursor()
        machines = _fetch_machines(cur)
        _apply_heartbeat(cur, machines)
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
