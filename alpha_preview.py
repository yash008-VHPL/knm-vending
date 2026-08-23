"""
KNM ops app — streamlined UI, PRODUCTION at "/" (LIVE ACTIONS)
==============================================================
2026-08-23 cutover: this blueprint serves "/". It stays mounted at /alpha too
(same view, not a redirect) so next-phase beta work keeps that path and any
bookmark or Easy Auth return URL still lands somewhere real. The previous
dashboard is archived, admin-only, at /archive2608.

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

Routes:  GET /  and  GET /alpha    -> the streamlined UI
          GET /alpha/api/bootstrap  -> {health, machines, work, user}
          GET /alpha/api/board/completed?date=YYYY-MM-DD
"""

import base64
import json as _json
from datetime import datetime, timedelta

import pymssql
from urllib.parse import quote

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


def _gate(is_api=False):
    """None when the caller may proceed, otherwise the response to return.

    /alpha and /alpha/api/bootstrap were the only undecorated routes in the app:
    bootstrap hands back the whole machine registry and every open order, so any
    authenticated tenant user with zero app roles could read it.

    2026-08-23 — the API/page split is tested FIRST, before the sign-in check.
    A fetch() answered with a 302 to AAD makes res.json() throw, and boot() in
    alpha_preview.html catches that and falls back to its DEMO seed: on an
    expired session this app would have shown synthetic machines as if they
    were the fleet. Now APIs get JSON 401/403 and only page loads redirect.
    """
    # Passed explicitly by each API route. Deriving it from request.path was
    # right for the four rules this blueprint serves today, but it now owns "/"
    # and the failure mode is silent: a fetch answered with a 302 makes
    # res.json() throw and boot() paints its demo seed.
    _is_api = bool(is_api) or request.path.startswith("/alpha/api/")
    if not _current_user():
        if _is_api:
            return jsonify({"error": "Your sign-in expired. Reload the page."}), 401
        _back = request.full_path if request.query_string else request.path
        # See app.login_required: a leading "//" is emitted as a
        # protocol-relative Location.
        if not _back.startswith("/") or _back[:2] in ("//", "/\\"):
            _back = "/"
        return redirect("/.auth/login/aad?post_login_redirect_uri="
                        + quote(_back, safe=""))
    if not _current_roles():
        # Match app.login_required: a page gets the branded page, the API gets
        # JSON. A browser tab full of {"error": ...} is not an answer.
        if _is_api:
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


# ── Completed stops for ONE board day ────────────────────────────────────────
# Deliberately NOT part of /alpha/api/bootstrap. That payload is capped at
# _DELIVERY_CAP rows and its ORDER BY sorts completed rows LAST, so on a fleet
# carrying a quarter of materialised sales repeats the completed rows are the
# first thing truncation throws away — the dispatcher would see a silent gap
# where a finished stop should be. This endpoint is keyed on a single date, so
# it is bounded by one day's work no matter how far ahead sales has keyed.
#
# It also keeps `work` (and therefore openWork(), BOARD_SITES, autoPlan and
# assignSite on the client) completely unchanged: a completed stop can never
# leak into an edit, a delete or a reassignment loop, because it never enters
# the structures those functions read.

def _has_table(cur, name):
    try:
        cur.execute("""SELECT 1 FROM INFORMATION_SCHEMA.TABLES
                       WHERE TABLE_NAME=%s""", (name,))
        return cur.fetchone() is not None
    except Exception:
        return False


def _iso_day(s):
    try:
        return datetime.strptime((s or "").strip(), "%Y-%m-%d").date()
    except Exception:
        return None


def _accepted_joborders(cur, jids):
    """Job orders whose manager review DECISION was 'accept'.

    Returns a set, or None if the decision could not be determined.

    StatusCode 3 alone is not enough: api_joborder_review closes the WO with
    StatusCode = 3 "regardless of decision", so a REJECTED job carries the same
    code as an accepted one. The decision exists only in WO_Activity.Detail,
    which _log_activity writes as f"{decision}" (+ optional " - notes") from a
    value already whitelisted to 'accept' / 'reject'.

    On failure this returns None, NOT an empty set. An empty set would send
    every accepted job down the else-branch and label it "REJECTED - may need
    re-dispatch", i.e. a transient DB error would tell dispatch to re-send
    drivers to sites the manager had signed off. Unknown must not read as bad.
    """
    if not jids:
        return set()
    try:
        marks = ", ".join(["%s"] * len(jids))
        cur.execute(f"""
            SELECT DISTINCT ParentID FROM WO_Activity
            WHERE ParentType = 'joborder' AND Action = 'manager_review'
              AND ParentID IN ({marks})
              AND LOWER(LEFT(CAST(Detail AS NVARCHAR(20)), 6)) = 'accept'
        """, tuple(jids))
        return {int(r[0]) for r in cur.fetchall()}
    except Exception as e:
        import sys
        print("[alpha] review-decision lookup failed:", e, file=sys.stderr)
        return None


def _fetch_completed_day(cur, day):
    """Stops finished on `day`, split into finalised (green) and submitted (amber).

    finalised = nothing further is expected of anyone.
    submitted = the work is done but a human still owes a decision, so dispatch
                must keep the ability to re-send a driver.

    Date rule: `ScheduledDate = day` OR (undated AND completed on `day` in SGT).
    The board deliberately carries undated work (showCarry), and the ad-hoc
    delivery order that api_visit_update mints when a driver records quantities
    against an unplanned visit has no ScheduledDate at all. Matching only on
    ScheduledDate would make exactly those stops vanish the moment they were
    completed - reintroducing, for the commonest unplanned case, the hole this
    endpoint exists to close. CompletedAt is UTC; +8h puts it on the SGT day.

    Returns (rows, failed) - failed is True when any block errored, so the
    caller can tell the client "partial" rather than let a swallowed exception
    read as a quiet day.
    """
    out = []
    failed = False
    iso = day.isoformat()
    has_visits = _has_table(cur, "WO_VisitSessions")
    # Both placeholders take the same day: dated stops match ScheduledDate,
    # undated ones match the SGT date they were completed on.
    _when = ("(%s.ScheduledDate = %%s OR (%s.ScheduledDate IS NULL "
             "AND CAST(DATEADD(hour, 8, %s.CompletedAt) AS DATE) = %%s))")

    # -- Deliveries -----------------------------------------------------------
    if _wo_has_scheduled(cur, "WO_DeliveryOrders"):
        # MAX(CASE WHEN signed THEN VisitID END), not MAX(VisitID): a DO can
        # carry a signed visit AND a later draft, and taking the plain MAX would
        # hand the dispatcher a link to the draft (404 "PDF not yet generated").
        # SignedVisit is therefore both the green test and the PDF target.
        _vis = ("""LEFT JOIN (
                     SELECT LinkedDeliveryOrderID AS do_id,
                            COUNT(*) AS Visits,
                            MAX(CASE WHEN Status = 'signed' THEN VisitID END) AS SignedVisit
                     FROM WO_VisitSessions
                     WHERE LinkedDeliveryOrderID IS NOT NULL
                     GROUP BY LinkedDeliveryOrderID
                   ) v ON v.do_id = d.DeliveryOrderID"""
                if has_visits else "")
        _cols = ("ISNULL(v.Visits,0) AS Visits, v.SignedVisit" if has_visits
                 else "CAST(0 AS INT) AS Visits, CAST(NULL AS INT) AS SignedVisit")
        try:
            cur.execute(f"""
                SELECT d.DeliveryOrderID, d.MachineName, d.MachineCode, d.AssignedTo,
                       d.Notes, d.RecipientName, d.RouteSeq, {_cols}
                FROM WO_DeliveryOrders d
                {_vis}
                WHERE d.Status = 'completed' AND {_when % ('d', 'd', 'd')}
            """, (iso, iso))
            for did, mname, mcode, asg, notes, recip, rseq, nvis, svid in cur.fetchall():
                recip = (recip or "").strip()
                if svid is not None:
                    # Signed on site through the Work Order sheet.
                    fin, why = True, (("Signed for by " + recip) if recip else "Signed on site")
                elif int(nvis or 0) == 0 and recip:
                    # No visit at all: completed from the main dashboard's
                    # /deliveryorders/<id>/complete, which REFUSES to run
                    # without a recipient name. That is a signature - painting
                    # it amber would put most of the board in warning colour on
                    # any fleet still using the legacy button.
                    fin, why = True, f"Signed for by {recip} (completed in the main dashboard)"
                elif int(nvis or 0) == 0:
                    fin, why = False, "Completed with no signature on file"
                else:
                    fin, why = False, "Customer unavailable - signature still outstanding"
                out.append({
                    "id": f"DEL-{did}", "kind": "delivery", "rid": int(did),
                    "type": "delivery",
                    "machine": str(mcode) if mcode else "?", "machineName": mname,
                    "desc": (notes or "")[:140],
                    "assignedTo": asg, "scheduledDate": iso,
                    "routeSeq": int(rseq) if rseq is not None else None,
                    "state": "finalised" if fin else "submitted",
                    "why": why,
                    # Only ever the SIGNED visit. Labelling a draft or an
                    # unsigned sheet "Open signed Work Order" is worse than
                    # offering no link.
                    "visitId": int(svid) if svid is not None else None,
                })
        except Exception as e:
            import sys
            print("[alpha] completed deliveries failed:", e, file=sys.stderr)
            failed = True

    # -- Job orders -----------------------------------------------------------
    if _wo_has_scheduled(cur, "WO_JobOrders"):
        try:
            cur.execute(f"""
                SELECT j.JobOrderID, j.DisplayID, j.MachineName, j.MachineCode, j.AssignedTo,
                       j.StatusCode, j.Diagnosis, j.RouteSeq
                FROM WO_JobOrders j
                WHERE j.StatusCode IN (2, 3) AND {_when % ('j', 'j', 'j')}
            """, (iso, iso))
            rows = cur.fetchall()
            closed = [int(r[0]) for r in rows if int(r[5] or 0) == 3]
            ok = _accepted_joborders(cur, closed)
            if ok is None:
                failed = True
            for jid, disp, mname, mcode, asg, sc, diag, rseq in rows:
                jid = int(jid)
                if int(sc or 0) == 2:
                    fin, why = False, "Submitted - awaiting manager review"
                elif ok is None:
                    # Decision unknown. Fail GREEN: the work IS closed, and the
                    # only thing in doubt is whether the manager accepted it.
                    fin, why = True, "Closed by the manager"
                elif jid in ok:
                    fin, why = True, "Reviewed and accepted"
                else:
                    fin, why = False, "Closed as REJECTED - may need re-dispatch"
                out.append({
                    "id": disp or f"JOB-{jid}", "kind": "joborder", "rid": jid,
                    "type": "service",
                    "machine": str(mcode) if mcode else "?", "machineName": mname,
                    "desc": (diag or "")[:140],
                    "assignedTo": asg, "scheduledDate": iso,
                    "routeSeq": int(rseq) if rseq is not None else None,
                    "state": "finalised" if fin else "submitted",
                    "why": why, "visitId": None,
                })
        except Exception as e:
            import sys
            print("[alpha] completed job orders failed:", e, file=sys.stderr)
            failed = True

    # -- Movements ------------------------------------------------------------
    if _wo_has_scheduled(cur, "WO_MovementOrders"):
        try:
            cur.execute(f"""
                SELECT m.MovementOrderID, m.DisplayID, m.MovementType, m.MachineCode,
                       m.FromLocation, m.ToLocation, m.AssignedTo, m.RouteSeq
                FROM WO_MovementOrders m
                WHERE m.StatusCode = 2 AND {_when % ('m', 'm', 'm')}
            """, (iso, iso))
            for mid, disp, mtype, mcode, frm, to, asg, rseq in cur.fetchall():
                desc = (mtype or "move").title()
                if frm or to:
                    desc += f": {frm or '?'} -> {to or '?'}"
                out.append({
                    "id": disp or f"MOV-{mid}", "kind": "movement", "rid": int(mid),
                    "type": "movement",
                    "machine": str(mcode) if mcode else "?", "machineName": None,
                    "desc": desc[:140],
                    "assignedTo": asg, "scheduledDate": iso,
                    "routeSeq": int(rseq) if rseq is not None else None,
                    "state": "finalised", "why": "Move completed", "visitId": None,
                })
        except Exception as e:
            import sys
            print("[alpha] completed movements failed:", e, file=sys.stderr)
            failed = True

    return out, failed


@alpha_bp.route("/alpha/api/board/completed")
def alpha_board_completed():
    blocked = _gate(is_api=True)
    if blocked is not None:
        return blocked
    day = _iso_day(request.args.get("date"))
    if day is None:
        return jsonify({"error": "date must be YYYY-MM-DD."}), 400
    conn = None
    try:
        conn = _conn()
        cur = conn.cursor()
        stops, failed = _fetch_completed_day(cur, day)
        # NB: "partial", not "error". The client's api() helper treats ANY
        # payload carrying `.error` as a hard failure — it returns null and
        # toasts the value — so an `error` key here would both discard the rows
        # that DID load and pop a bare word at the dispatcher.
        return jsonify({"date": day.isoformat(), "stops": stops, "partial": bool(failed)})
    except Exception as e:
        import sys
        print("[alpha] completed-day DB error:", e, file=sys.stderr)
        # A soft failure must not blank the board: the client keeps rendering
        # open work and says plainly that the green block is missing.
        return jsonify({"date": day.isoformat(), "stops": [], "partial": True})
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


@alpha_bp.route("/")
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
    blocked = _gate(is_api=True)
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
