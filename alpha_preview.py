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
from flask import Blueprint, jsonify, redirect, render_template, request

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


# dispatch and field_manager are one role (see app.ROLE_ALIASES); the beta app
# must agree or the picker offers two entries for the same person.
_ROLE_ALIASES = {"dispatch": "field_manager"}


def _current_roles():
    out = []
    p = _current_principal()
    if p:
        for c in p.get("claims", []):
            if c.get("typ") == "roles":
                v = (c.get("val") or "").strip().lower()
                v = _ROLE_ALIASES.get(v, v)
                if v and v not in out:
                    out.append(v)
    if not out and getattr(config, "DEV_ROLE", ""):
        _d = config.DEV_ROLE.strip().lower()
        out = [_ROLE_ALIASES.get(_d, _d)]
    return out


def _active_role():
    """The ONE role the server will actually enforce.

    app.get_role() resolves it from the knm_active_role cookie, falling back to
    the first claim. /alpha must render this and nothing else — deriving its own
    answer is what put an operator on the Plan board while every /api/wo/* call
    returned 403.

    Imported lazily on purpose: app.py imports this module, so a module-level
    import would be circular. By request time app is fully loaded.
    """
    try:
        from app import get_role
        return get_role(_current_user())
    except Exception as e:
        # Never fall back silently: get_role honours knm_active_role and this
        # path did not, so a user who switched role would be handed their FIRST
        # claim — reinstating the exact bug, with the UI confidently mislabelled.
        import sys
        print("[alpha] get_role unavailable, using cookie fallback:", e, file=sys.stderr)
        r = _current_roles()
        try:
            w = (request.cookies.get("knm_active_role") or "").strip().lower()
        except Exception:
            w = ""
        w = _ROLE_ALIASES.get(w, w)
        if w and w in r:
            return w
        return r[0] if r else None


def _gate():
    """None when the caller may proceed, otherwise the response to return.

    /alpha and /alpha/api/bootstrap were the only undecorated routes in the app:
    bootstrap hands back the whole machine registry and every open order, so any
    authenticated tenant user with zero app roles could read it.
    """
    if not _current_user():
        return redirect("/.auth/login/aad?post_login_redirect_uri=/alpha")
    if not _current_roles():
        # Match app.login_required: a page gets the branded page, the API gets
        # JSON. A browser tab full of {"error": ...} is not an answer.
        if request.path.startswith("/alpha/api/"):
            return jsonify({"error": "You are not authorised to use this app."}), 403
        return ("<h2>Access Denied</h2><p>You are not authorised to use this app."
                "<br>Please contact your administrator.</p>"
                '<p><a href="/.auth/logout">Sign out</a></p>'), 403
    return None


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


def _has_do_col(cur, col):
    try:
        cur.execute("""SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                       WHERE TABLE_NAME='WO_DeliveryOrders' AND COLUMN_NAME=%s""", (col,))
        return cur.fetchone() is not None
    except Exception:
        return False


def _wo_has_scheduled(cur, table):
    """The ScheduledDate columns are added by workorders.init_workorders_db,
    which swallows each ALTER independently — so one table can have them and
    another not. Probe PER TABLE: getting this wrong makes an entire order type
    vanish from the UI behind a swallowed exception."""
    try:
        cur.execute("""SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                       WHERE TABLE_NAME=%s AND COLUMN_NAME='ScheduledDate'""", (table,))
        return cur.fetchone() is not None
    except Exception:
        return False


def _sd_cols(cur, table):
    return ("ScheduledDate, RouteSeq" if _wo_has_scheduled(cur, table)
            else "NULL AS ScheduledDate, NULL AS RouteSeq")


# Open delivery rows the board will load in one go. Sized for a full quarter of
# sales schedule across the fleet; the response reports truncation rather than
# quietly dropping stops when it is exceeded.
_DELIVERY_CAP = 900


def _fetch_work(cur, meta=None):
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
                         "note": desc,
                         "status": "new", "assignedTo": None, "source": src or "Complaint"})
    except Exception:
        pass
    try:
        cur.execute(f"""SELECT TOP 200 JobOrderID, DisplayID, MachineName, MachineCode,
                       AssignedTo, PriorityCode, StatusCode, Diagnosis, {_sd_cols(cur, "WO_JobOrders")}
                       FROM WO_JobOrders ORDER BY CreatedAt DESC, JobOrderID DESC""")
        for jid, disp, mname, mcode, asg, pc, sc, diag, sdate, rseq in cur.fetchall():
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
                         "status": status, "assignedTo": asg, "source": "Tech Support",
                         # `desc` is truncated for display. `note` is the real
                         # stored text — the board edits this, so truncating it
                         # would destroy a manager's diagnosis on every save.
                         "note": diag,
                         "scheduledDate": str(sdate)[:10] if sdate else None,
                         "routeSeq": int(rseq) if rseq is not None else None})
    except Exception:
        pass
    try:
        _svc = ("ISNULL(NeedsService,0) AS NeedsService, ServiceNote"
                if (_has_do_col(cur, "NeedsService") and _has_do_col(cur, "ServiceNote"))
                else "CAST(0 AS BIT) AS NeedsService, NULL AS ServiceNote")
        # 2026-08-11 — the sales calendar materialises repeats, so the number of
        # OPEN delivery rows is now measured in weeks of schedule rather than in
        # today's round. TOP 200 ordered by CreatedAt would have silently pushed
        # this morning's stops off the board the moment sales keyed a quarter
        # ahead. Two guards: drop completed rows older than 30 days (only the
        # Triage "done" counter reads them), and raise the cap — with
        # _work_truncated reported so the UI can say so instead of quietly
        # showing an incomplete board.
        _sched = _wo_has_scheduled(cur, "WO_DeliveryOrders")
        _where = ("WHERE Status <> 'completed' "
                  "OR CreatedAt >= DATEADD(day, -30, GETUTCDATE())")
        _order = ("ORDER BY CASE WHEN Status <> 'completed' THEN 0 ELSE 1 END, "
                  + ("ScheduledDate ASC, " if _sched else "")
                  + "CreatedAt DESC, DeliveryOrderID DESC")
        cur.execute(f"""SELECT TOP {_DELIVERY_CAP} DeliveryOrderID, MachineName, MachineCode,
                       AssignedTo, Priority, Status, Notes, {_svc},
                       {_sd_cols(cur, "WO_DeliveryOrders")}
                       FROM WO_DeliveryOrders {_where} {_order}""")
        _rows = cur.fetchall()
        if meta is not None and len(_rows) >= _DELIVERY_CAP:
            meta["truncated"] = True
        for did, mname, mcode, asg, pri, st, notes, needsvc, svcnote, sdate, rseq in _rows:
            done = (st or "").lower() == "completed"
            _ns = bool(needsvc)
            _n  = (svcnote if _ns else notes)
            work.append({"id": f"DEL-{did}", "kind": "delivery", "rid": int(did),
                         # A stop that needs service reads as SERVICE on the
                         # board even though it is one delivery-order row.
                         "type": "service" if _ns else "delivery",
                         "needsService": _ns,
                         "machine": str(mcode) if mcode else "?", "machineName": mname,
                         "desc": (_n or "Refill / delivery")[:140],
                         "priority": (pri or "normal").lower(),
                         "status": "done" if done else ("assigned" if asg else "new"),
                         "assignedTo": asg, "source": "Delivery",
                         "note": _n,
                         "scheduledDate": str(sdate)[:10] if sdate else None,
                         "routeSeq": int(rseq) if rseq is not None else None})
    except Exception:
        pass
    try:
        cur.execute(f"""SELECT TOP 200 MovementOrderID, DisplayID, MovementType, MachineCode,
                       FromLocation, ToLocation, StatusCode, AssignedTo,
                       {_sd_cols(cur, "WO_MovementOrders")}
                       FROM WO_MovementOrders ORDER BY CreatedAt DESC, MovementOrderID DESC""")
        for mid, disp, mtype, mcode, frm, to, sc, asg, sdate, rseq in cur.fetchall():
            lbl = MOVEMENT_STATUS.get(int(sc) if sc is not None else 0, "scheduled")
            desc = f"{(mtype or 'move').title()}"
            if frm or to:
                desc += f": {frm or '?'} → {to or '?'}"
            work.append({"id": disp or f"MOV-{mid}", "kind": "movement", "rid": int(mid),
                         "type": "movement",
                         "machine": str(mcode) if mcode else "?", "machineName": None,
                         "desc": desc[:140], "priority": "normal",
                         "status": "done" if lbl == "completed" else ("assigned" if asg else "new"),
                         "assignedTo": asg, "source": "Movement",
                         "scheduledDate": str(sdate)[:10] if sdate else None,
                         "routeSeq": int(rseq) if rseq is not None else None})
    except Exception:
        pass
    return work


@alpha_bp.route("/alpha")
def alpha_index():
    blocked = _gate()
    if blocked is not None:
        return blocked
    # `role` is what the embedded main-dashboard screens expect (index.html's
    # own context name) — the Locations pane gates its Edit/Delete/Add controls
    # on it. Same value as active_role; both are passed so neither template half
    # has to know about the other's naming.
    _r = _active_role()
    return render_template("alpha_preview.html",
                           username=_current_user(), roles=_current_roles(),
                           active_role=_r, role=_r)


@alpha_bp.route("/alpha/api/bootstrap")
def alpha_bootstrap():
    blocked = _gate()
    if blocked is not None:
        return blocked
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
        wmeta = {}
        work = _fetch_work(cur, wmeta)
        return jsonify({
            "health": {"live": True, "reason": "Connected (read-only)", "salesVends": sales,
                       "workTruncated": bool(wmeta.get("truncated")),
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
