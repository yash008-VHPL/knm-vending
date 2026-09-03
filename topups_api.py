"""
Topups tab — server side.                                          2026-08-23

A NEW blueprint mounted at /api/topups. Nothing in workorders.py, app.py,
alpha_preview.py, gcal_feed.py or gcal_sync.py is modified; helpers are imported
from workorders so authorisation, connection handling and the duplicate rules
stay in exactly one place.

Two screens are served from here:

  Calendar  — what actually happened on past days and what is firmed up for
              future ones, grouped by driver. Read-only except for the
              dispatcher's outcome flag, the assignment page and the move.
  Plan      — the flag-card vend counter, the next 14 days of SALES REQUESTS,
              and one batch submit that places them on a chosen date and shift.

A SALES CALENDAR ENTRY IS A REQUEST, NOT A STOP
-----------------------------------------------
This distinction is the whole design and an earlier version got it wrong.
Sales keys "CGH, Wednesday" into Google Calendar. That is a REQUEST: the site
wants a visit that week. Dispatch decides which day it actually happens on and
which shift runs it. So picking a request in the Plan tab MOVES its stop onto
the chosen date — it never creates a second one. Two open stops for one machine
is the corruption the duplicate guard exists to prevent: the driver's sheet
reads TOP 1, so the other can never be closed and then blocks that machine on
every future date through the ISNULL rule.

The first cut treated an entry that already had a row as "booked" and disabled
it, which made the entire list inert — gcal_sync had already created a stop for
every event in its 28-day horizon, so nothing was ever pickable.

THREE THINGS THAT LOOK LIKE MISTAKES AND ARE NOT
------------------------------------------------
1.  This module writes SourceGCalEventID, never GCalEventID.
    gcal_sync._load_existing() selects WHERE GCalEventID IS NOT NULL, and any
    row it finds on a date the live calendar does not agree with is HARD
    DELETED while AssignedTo is NULL (gcal_sync.py:121 -> _cancel_stop_rows ->
    DELETE FROM WO_DeliveryOrders). Every row this planner creates is unassigned
    by design. Writing GCalEventID here would point that delete at our own rows
    and the dispatcher's morning would vanish inside one poll interval.

2.  The duplicate guard is ISNULL(CONVERT(VARCHAR(10), ScheduledDate, 23), %s)
    = %s, copied byte-for-byte from workorders.py:3763. The ISNULL is
    deliberate: WO_VisitSessions carries a SINGLE LinkedDeliveryOrderID, so an
    UNDATED open row for a machine collides with every date, not just its own.
    Loosening it to a plain machine+date match produces a second open row that
    the driver's TOP 1 sheet can never reach and nothing can ever close.

3.  /batch refuses with 409 while the Google Calendar auto-sync is running.
    With GCAL_SYNC on, gcal_sync already creates a stop for every "ok" event
    inside 28 days, so this screen would either no-op or double-book, and any
    stop pulled forward from a later calendar date is deleted on the next poll.
    The two designs are mutually exclusive. Refusing loudly beats silently
    losing a dispatcher's work at 05:00.
"""

import re
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request

from app import get_connection, get_current_user
from workorders import (
    require_roles,
    DISPATCH_ROLES, SALES_ROLES,
    _log_activity, _next_route_seq, _has_col, _sgt_today,
)

# Franchisee labels and colours. nets_mapping.py sits at the repo root and is
# the single source of truth for accounts (the pull refuses a key it cannot
# find there), so the dashboard reads the same dict rather than keeping a
# second copy. Guarded: a missing module must not take the blueprint down.
try:
    from nets_mapping import ACCOUNTS as _ACCOUNTS, MAIN_ACCOUNT as _MAIN_ACCOUNT
except Exception as _e:                      # pragma: no cover
    print("[topups_api] nets_mapping unavailable, franchisee stripes off: %s" % _e)
    _ACCOUNTS, _MAIN_ACCOUNT = {}, "MAIN"

topups_bp = Blueprint("topups_api", __name__)

# Colour codes the Calendar paints with. Kept as ints so the wire format never
# depends on a display string.
OUTCOME_SERVICED = 0   # green
OUTCOME_FAULTY   = 1   # yellow
OUTCOME_UNABLE   = 2   # red
OUTCOME_LABEL = {0: "serviced", 1: "faulty", 2: "unable"}

SHIFT_LABEL = {0: "day", 1: "night"}

# The Plan tab's calendar column looks this far ahead. Sales keys further out
# than that; pulling one of those forward is the whole point of the screen, so
# the picker is allowed to reach past it via the "show more" range.
GCAL_WINDOW_DAYS = 14

# Widest span the Calendar will render in one request. Same cap as
# workorders.api_schedule_list, for the same reason: an unbounded range is a
# table scan a user can trigger from a URL.
MAX_RANGE_DAYS = 400

# The vend counter is two joins over ~800k rows. Recomputing it on every render
# would block the single gunicorn sync worker for every other user. One cache,
# refreshed on demand, is enough — the underlying feed only lands once a day.
_VEND_CACHE = {"at": None, "payload": None}
VEND_CACHE_SECONDS = 300

# TWO different questions, deliberately two thresholds.
#
#   DEAD  - has the feed stopped? 26h, unchanged: that is the dead-man's switch
#           the Auresys handoff defines, measured against the 06:00
#           RECONCILIATION run, which is the run that must never be missed.
#   STALE - are the numbers current? 15h, i.e. the 18:00 freshness run did not
#           land. Worth saying, but it is not a broken feed, and painting it red
#           would light the alarm every time the optional run was skipped while
#           the pipeline was perfectly healthy.
FEED_DEAD_SECONDS  = 26 * 3600
FEED_STALE_SECONDS = 15 * 3600

# Schema probe result. Nine INFORMATION_SCHEMA round trips on every /calendar
# render is nine too many on a single sync worker. Cached ONLY once every
# column is present — see _topup_cols.
_COLS_CACHE = None


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────

def _db_error(e):
    """Log the detail, return a fixed string.

    The house style elsewhere echoes str(e) straight back, which on these routes
    reaches a sales user and, through /health, any signed-in operator — column
    names, table names and the DB principal, from a public-facing app.
    """
    print("[topups_api] %s: %s" % (type(e).__name__, e))
    return ("Something went wrong reading the schedule. It has been logged — "
            "check the App Service log stream.")


def _iso(s):
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _gcal_sync_on():
    """Is the Google Calendar auto-sync actually able to create and delete stops?

    Delegates to gcal_feed's OWN helpers rather than re-reading the setting.
    A private re-implementation drifted three ways in review and every drift
    failed in the dangerous direction:

      * it defaulted to ON when the setting was absent, so /batch returned 409
        on a fresh deploy that had no calendar feed at all and could not sync;
      * config.GCAL_SYNC = 0 (an int) made gcal_feed sync while the copy read
        it as off — the exact state the 409 exists to prevent, inverted;
      * GCAL_SYNC = "" was ON here and fell through to config there.

    2026-08-24: the comparison itself now lives in gcal_feed.sync_enabled(), so
    there is exactly one copy of it, and its default is OFF. An AttributeError
    here (an older gcal_feed.py redeployed from one of the .bak copies in the
    repo) falls through to the except below and returns True, which refuses
    loudly rather than planning against a live sync.

    gcal_feed.enabled() is the other half: the sync only ever runs inside
    refresh_once(), which only runs if start() succeeded, which needs
    GCAL_FEED_URL and GCAL_FEED_SECRET. Without them nothing can be created or
    deleted no matter what GCAL_SYNC says, and the planner is safe.
    """
    try:
        import gcal_feed
    except Exception:
        return False              # no feed module, nothing can sync
    try:
        if not gcal_feed.enabled():
            return False
        fn = getattr(gcal_feed, "sync_enabled", None)
        if fn is None:
            # A pre-2026-08-24 gcal_feed.py is deployed under a newer
            # topups_api.py — there are nine .bak copies of these modules in the
            # repo, so a stray redeploy is a real state. Refuse (that old module
            # also defaults GCAL_SYNC to ON), but say WHICH thing is wrong: the
            # 409 below tells the operator to set the app setting to 0, which
            # they will already have done, and nothing else distinguishes the
            # two causes.
            print("[topups_api] gcal_feed has no sync_enabled(): a pre-2026-08-24 "
                  "gcal_feed.py is deployed. Redeploy the matching version — the "
                  "GCAL_SYNC app setting is not the problem.")
            return True
        return fn()
    except Exception:
        # Unreadable state is not proof of safety. Assume it is live and let
        # /batch refuse loudly rather than let it double-book silently.
        return True


def _topup_cols(cursor):
    """Which of this migration's columns actually landed.

    Every ALTER in init_workorders_db is swallowed independently and this
    migration is run by hand, so a half-applied schema is a real state. Probing
    keeps it degraded-but-usable instead of a 500 that loses the day's plan.
    """
    global _COLS_CACHE
    # Only a fully-migrated answer is cached. Running the migration in the
    # Azure Query Editor does NOT restart the App Service, and the first
    # request after a deploy normally lands BEFORE the migration — caching that
    # "no" for the process lifetime meant /calendar returned dated:false and
    # /outcome 503d forever, with /health cheerfully repeating the stale dict.
    # A transient failure of the WO_VisitSessions probe was equally permanent,
    # and repainted every signed stop amber until someone restarted the app.
    if _COLS_CACHE is not None and all(_COLS_CACHE.values()):
        return _COLS_CACHE
    out = {c: _has_col(cursor, "WO_DeliveryOrders", c) for c in
           ("ScheduledDate", "RouteSeq", "ShiftCode", "OutcomeCode", "OutcomeNote",
            "OutcomeBy", "OutcomeAt", "SourceGCalEventID", "NeedsService")}
    # RouteSeq is a separate swallowed ALTER from ScheduledDate
    # (workorders.py:358-359), so "dated but no RouteSeq" is a real state and
    # selecting it unguarded is the 500 this probing exists to prevent.
    try:
        cursor.execute("SELECT 1 FROM INFORMATION_SCHEMA.TABLES "
                       "WHERE TABLE_NAME = 'WO_VisitSessions'")
        out["VisitSessions"] = cursor.fetchone() is not None
    except Exception:
        out["VisitSessions"] = False
    _COLS_CACHE = out
    return out


def _derive_state(row, today_iso):
    """Colour for one delivery stop, when the dispatcher has not judged it.

    Ported from alpha_preview._fetch_completed_day (alpha_preview.py:494-509),
    including the branch that exists only because of the legacy button:
    /deliveryorders/<id>/complete REFUSES to run without a recipient name, so a
    recipient name with no visit row IS the signature. Painting those amber put
    most of the board in warning colour on any fleet still using it.

    There is an explicit final else. A completed row must never render blank.
    """
    status     = (row["status"] or "").lower()
    sched      = row["scheduled_date"]
    signed_vis = row["signed_visit"]
    n_visits   = int(row["visits"] or 0)
    recipient  = row["recipient_name"]

    if status != "completed":
        if sched and sched < today_iso:
            return OUTCOME_UNABLE, "Planned for this day and never completed"
        return None, ""                                   # still ahead of us

    if signed_vis:
        return OUTCOME_SERVICED, "Signed on site"
    if n_visits == 0 and recipient:
        return OUTCOME_SERVICED, "Signed for by %s" % recipient
    if n_visits == 0:
        return OUTCOME_FAULTY, "Completed with no signature on file"
    return OUTCOME_FAULTY, "Customer unavailable - signature still outstanding"


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/topups/calendar
# ─────────────────────────────────────────────────────────────────────────────

@topups_bp.route("/calendar")
@require_roles(*SALES_ROLES)
def api_topups_calendar():
    """Every top-up stop in a date range, grouped client-side by driver.

    Past and future in ONE query on purpose: a day either has outcomes or it has
    a plan, and the client decides which face to show. Two endpoints would have
    let the boundary drift between them at midnight SGT.
    """
    frm, to = _iso(request.args.get("from")), _iso(request.args.get("to"))
    if not frm or not to:
        return jsonify({"error": "from and to are required (YYYY-MM-DD)."}), 400
    if to < frm:
        frm, to = to, frm
    if (to - frm).days > MAX_RANGE_DAYS:
        return jsonify({"error": "Range too wide (max %d days)." % MAX_RANGE_DAYS}), 400

    today = _sgt_today()
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        have = _topup_cols(cur)
        if not have["ScheduledDate"]:
            return jsonify({"stops": [], "dated": False, "today": today,
                            "gcalSyncOn": _gcal_sync_on(),
                            "warning": "Scheduling columns are not migrated yet. "
                                       "Run migration_topups_2026-08-23.sql."})

        sel = ["d.DeliveryOrderID", "d.MachineName", "d.MachineCode", "d.AssignedTo",
               "d.Status", "d.RecipientName",
               "CONVERT(VARCHAR(10), d.ScheduledDate, 23)",
               "CONVERT(VARCHAR(10), DATEADD(hour, 8, d.CompletedAt), 23)"]
        sel.append("d.RouteSeq"      if have["RouteSeq"]    else "CAST(NULL AS INT)")
        sel.append("d.ShiftCode"     if have["ShiftCode"]   else "CAST(NULL AS TINYINT)")
        sel.append("d.OutcomeCode"   if have["OutcomeCode"] else "CAST(NULL AS TINYINT)")
        sel.append("d.OutcomeNote"   if have["OutcomeNote"] else "CAST(NULL AS NVARCHAR(500))")
        sel.append("d.NeedsService"  if have["NeedsService"] else "CAST(NULL AS BIT)")
        cols = ", ".join(sel)

        # Notes is NVARCHAR(MAX) and nothing on this screen renders it. Shipping
        # it for every stop in a 400-day range is pure wire cost.
        #
        # UNION ALL of two seekable branches, NOT one OR. The OR form made
        # IX_WODO_ScheduledDate unusable and forced a scan of the whole table on
        # every render — on a single gunicorn sync worker, which means every
        # other user, drivers finalising work orders included, waits for it.
        #
        # The visit aggregate is bounded by the same range for the same reason:
        # unbounded it re-aggregated every visit session ever recorded, per
        # request. The second branch exists because api_visit_update mints an
        # ad-hoc delivery order with NO ScheduledDate (workorders.py:5899);
        # matching on ScheduledDate alone makes exactly those stops — real,
        # performed, signed work — vanish the moment they are completed.
        a, b = frm.isoformat(), to.isoformat()
        if have["VisitSessions"]:
            vjoin = """
            LEFT JOIN (
                SELECT v.LinkedDeliveryOrderID AS do_id,
                       COUNT(*) AS Visits,
                       MAX(CASE WHEN v.Status = 'signed' THEN v.VisitID END) AS SignedVisit
                FROM WO_VisitSessions v
                JOIN WO_DeliveryOrders dd ON dd.DeliveryOrderID = v.LinkedDeliveryOrderID
                WHERE v.LinkedDeliveryOrderID IS NOT NULL
                  AND (dd.ScheduledDate BETWEEN %s AND %s
                       OR (dd.ScheduledDate IS NULL
                           AND CAST(DATEADD(hour, 8, dd.CompletedAt) AS DATE)
                               BETWEEN %s AND %s))
                GROUP BY v.LinkedDeliveryOrderID
            ) v ON v.do_id = d.DeliveryOrderID"""
            vsel, vparams = "ISNULL(v.Visits,0), v.SignedVisit", (a, b, a, b)
        else:
            vjoin, vsel, vparams = "", "CAST(0 AS INT), CAST(NULL AS INT)", ()

        sql = ("SELECT " + cols + ", " + vsel +
               " FROM WO_DeliveryOrders d" + vjoin +
               " WHERE d.ScheduledDate BETWEEN %s AND %s"
               " UNION ALL "
               "SELECT " + cols + ", " + vsel +
               " FROM WO_DeliveryOrders d" + vjoin +
               " WHERE d.ScheduledDate IS NULL"
               "   AND d.CompletedAt IS NOT NULL"
               "   AND CAST(DATEADD(hour, 8, d.CompletedAt) AS DATE) BETWEEN %s AND %s"
               " UNION ALL "
               # Undated AND still open. These block every date for their
               # machine through the ISNULL rule the batch endpoint enforces,
               # and until now they appeared on no screen the error message
               # pointed at. Surfaced against today so a dispatcher can see
               # what is jamming the machine.
               "SELECT " + cols + ", " + vsel +
               " FROM WO_DeliveryOrders d" + vjoin +
               " WHERE d.ScheduledDate IS NULL AND d.CompletedAt IS NULL"
               "   AND d.Status <> 'completed'"
               "   AND %s BETWEEN %s AND %s")
        cur.execute(sql, vparams + (a, b) + vparams + (a, b) + vparams + (today, a, b))

        stops = []
        seen_ids = set()
        for r in cur.fetchall():
            if int(r[0]) in seen_ids:
                continue          # UNION ALL branches are disjoint, but cheap insurance
            seen_ids.add(int(r[0]))
            row = {
                "id": int(r[0]), "machineName": r[1], "machineCode": r[2],
                "assignedTo": (r[3] or "").lower() or None,
                "status": r[4], "recipient_name": r[5],
                "scheduled_date": r[6], "completed_day": r[7],
                "routeSeq": r[8],
                "shift": int(r[9]) if r[9] is not None else None,
                "outcome": int(r[10]) if r[10] is not None else None,
                "outcomeNote": r[11],
                "needsService": bool(r[12]) if r[12] is not None else False,
                "visits": r[13], "signed_visit": r[14],
            }
            derived, why = _derive_state(row, today)
            # The dispatcher's judgement always wins over the derivation. That
            # is the entire point of the column: this screen is for the
            # dispatcher's eyes and the dispatcher's call, and nothing on the
            # driver's Work Order sheet was changed to feed it.
            row["colour"] = row["outcome"] if row["outcome"] is not None else derived
            row["colourSource"] = "dispatch" if row["outcome"] is not None else "derived"
            row["why"] = row["outcomeNote"] or why
            row["undated"] = row["scheduled_date"] is None
            # An undated OPEN row is filed against today so it is visible at all;
            # it is flagged so the day cell can say what it is rather than
            # pretending it was planned for today.
            row["blocking"] = (row["undated"] and row["completed_day"] is None
                               and (row["status"] or "").lower() != "completed")
            row["date"] = (row["scheduled_date"] or row["completed_day"]
                           or (today if row["blocking"] else None))
            for k in ("visits", "signed_visit", "recipient_name"):
                row.pop(k, None)
            stops.append(row)

        # Job orders and movements are NOT top-ups and do not belong on this
        # screen — but they still consume a driver's day, and letting them
        # disappear from every day-keyed view would be a silent regression. A
        # per-day count, linking back to Service > Day board, keeps them visible.
        other = {}
        for tbl, idc, cond in (
            ("WO_JobOrders", "JobOrderID", "StatusCode IN (0,1,2,3)"),
            ("WO_MovementOrders", "MovementOrderID", "StatusCode IN (0,1,2)"),
        ):
            try:
                cur.execute(
                    "SELECT CONVERT(VARCHAR(10), ScheduledDate, 23), COUNT(*) "
                    "FROM %s WHERE ScheduledDate BETWEEN %%s AND %%s AND %s "
                    "GROUP BY ScheduledDate" % (tbl, cond),
                    (frm.isoformat(), to.isoformat()))
                for d, n in cur.fetchall():
                    other.setdefault(d, {"joborders": 0, "movements": 0})
                    other[d]["joborders" if tbl == "WO_JobOrders" else "movements"] = int(n)
            except Exception:
                pass          # columns not migrated on that table; not fatal here

        return jsonify({"stops": stops, "dated": True, "today": today,
                        "otherWork": other,
                        "gcalSyncOn": _gcal_sync_on()})
    except Exception as e:
        return jsonify({"error": _db_error(e)}), 500
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/topups/vendcounter
# ─────────────────────────────────────────────────────────────────────────────

@topups_bp.route("/vendcounter")
@require_roles(*SALES_ROLES)
def api_topups_vendcounter():
    """Dispenses at each machine since its top-up flag card was last tapped.

    The flag card IS the top-up marker. A driver taps one of the physical cards
    at the machine's payment terminal when the refill is done, and that tap
    arrives through the Auresys feed like any other transaction — identified by
    its Card_Hash, which dbo.NETS_FlagCard holds. No card number and no pepper
    is ever in this process: the seed script does the hashing once, offline.

    FOUR HONESTIES THIS ENDPOINT OWES THE SCREEN, all returned as fields rather
    than swallowed:

      staleAsOf   The feed is a BATCH, pulled at 06:00 and 18:00 SGT - the
                  first catching the night shift, the second the day shift. It
                  is not live. Without this the machine just topped up an hour
                  ago still sorts to the top and invites a second visit.
      unflagged   Machines with an Auresys terminal that have never shown a flag
                  tap. Counted from their first transaction instead, and marked.
      noTerminal  Machines with no payment terminal at all (~61 of 134). They
                  can never appear here. The screen must say so rather than
                  imply the fleet is 73 machines.
      nullCoded   Dispenses in the window with Machine_Code IS NULL (~16%).
                  Unattributable. The handoff requires this figure on screen or
                  the totals do not reconcile.

    FRANCHISEES (2026-09-03). The feed now covers the KNM Main account plus
    each franchisee's own Auresys account, and every row carries Account_Key.
    A machine's franchisee is the Account_Key of its LATEST transaction - not
    MAX(Account_Key), which is alphabetical and wrong for a machine that has
    changed hands. NULL / 'MAIN' = KNM's own machine, no stripe. Two more
    fields carry the honesty this adds:
      franchisees   [{key,label,color}] for the legend - only accounts that
                    actually have a machine on screen.
      skippedAccounts  accounts the last pull could NOT log in to / load. Their
                    machines' numbers are frozen at the previous run, so the
                    screen must say so per franchisee rather than let the
                    global "last pulled" stamp vouch for them.
    """
    force = request.args.get("refresh") == "1"
    now = datetime.utcnow()
    if (not force and _VEND_CACHE["payload"] is not None
            and _VEND_CACHE["at"] is not None
            and (now - _VEND_CACHE["at"]).total_seconds() < VEND_CACHE_SECONDS):
        return jsonify({**_VEND_CACHE["payload"], "cached": True})

    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()

        try:
            cur.execute("SELECT COUNT(*) FROM dbo.NETS_FlagCard WHERE IsActive = 1")
            n_cards = int(cur.fetchone()[0])
        except Exception as e:
            # notReady, NOT error. The client's api() helper toasts and returns
            # null for any body carrying a truthy `error` key, so remediation
            # text sent that way is destroyed and replaced by a generic message
            # — and a 1.9-second toast is not where you put "run the migration".
            # No raw exception text either: this route is SALES_ROLES.
            print("[topups_api] NETS_FlagCard unreachable: %s" % e)
            return jsonify({
                "rows": [], "ready": False,
                "notReady": "The flag-card table is not there yet. Run "
                            "migration_topups_2026-08-23.sql BLOCK 2, then "
                            "seed_flag_cards.py. (Details are in the App "
                            "Service log stream.)"}), 200
        if n_cards == 0:
            return jsonify({
                "rows": [], "ready": False,
                "notReady": "No top-up flag cards are seeded yet. Run "
                            "seed_flag_cards.py with NETS_CARD_PEPPER set."}), 200

        # Feed freshness. NETS_Pull_Run is the dead-man's switch: newest SUCCESS
        # older than ~26h means the feed has stopped and every number below is
        # quietly frozen.
        stale_as_of, feed_ok, feed_fresh = None, True, True
        skipped_accounts = []
        try:
            cur.execute("SELECT MAX(Finished_At_UTC) FROM dbo.NETS_Pull_Run "
                        "WHERE Status = 'SUCCESS'")
            r = cur.fetchone()
            if r and r[0]:
                stale_as_of = r[0].isoformat() + "Z"   # UTC. Without the Z the
                #  browser parses it as local time and the "Nh ago" line goes negative.
                age = (now - r[0]).total_seconds()
                feed_ok = age < FEED_DEAD_SECONDS
                feed_fresh = age < FEED_STALE_SECONDS
        except Exception:
            pass
        # A SUCCESS run can still have skipped a whole account (login failed,
        # MFA, network). auresys_pull prefixes Error_Text with a fixed token
        # "[SKIPPED_ACCOUNTS=KEY,KEY]" for exactly this read - a token, not
        # free text, so an Abort message containing ';' cannot confuse it and
        # the 4000-char truncation cannot chop it. Only the newest SUCCESS run
        # counts: an account that was back on the next run is no longer behind.
        # MAIN is included: if KNM's own account was the one skipped while a
        # franchisee loaded, the global stamp would otherwise vouch for frozen
        # numbers on every KNM machine - so it also demotes feedFresh.
        try:
            cur.execute("SELECT TOP 1 Error_Text FROM dbo.NETS_Pull_Run "
                        "WHERE Status = 'SUCCESS' ORDER BY Finished_At_UTC DESC")
            r = cur.fetchone()
            txt = (r[0] if r else None) or ""
            mm = re.match(r"\s*\[SKIPPED_ACCOUNTS=([A-Z0-9_,]+)\]", txt)
            for key in (mm.group(1).split(",") if mm else []):
                key = key.strip()
                if not key:
                    continue
                skipped_accounts.append({
                    "key": key,
                    "label": (_ACCOUNTS.get(key) or {}).get("label") or key})
                if key == _MAIN_ACCOUNT:
                    feed_fresh = False
        except Exception:
            pass

        # Last flag tap per machine. UNBOUNDED on purpose for the flag lookup:
        # a 180-day floor made "last flagged 200 days ago" indistinguishable
        # from "never flagged", and a never-flagged machine then counted its
        # whole window and sorted to the TOP of the list — sending a driver to
        # the machine that least needed one. The filtered index on Card_Hash
        # (migration BLOCK 4) makes this a small seek, not a scan.
        cur.execute("""
            SELECT t.Machine_Code, MAX(t.Txn_DateTime)
            FROM dbo.NETS_Transaction t
            WHERE t.Card_Hash IN (SELECT Card_Hash FROM dbo.NETS_FlagCard WHERE IsActive = 1)
              AND t.Machine_Code IS NOT NULL
            GROUP BY t.Machine_Code
        """)
        last_flag = {str(r[0]): r[1] for r in cur.fetchall()}

        # Dispenses since. Status 0 only — counting every row over-states by
        # ~3.2%, because settlement heartbeats and declines are in there too.
        # Only machines that HAVE a flag are counted: a machine with no flag
        # has no meaningful "since", and inventing one is what produced the
        # false top-of-list above.
        counts = {}
        if last_flag:
            marks = ", ".join(["%s"] * len(last_flag))
            codes = list(last_flag.keys())
            cur.execute("""
                SELECT t.Machine_Code, COUNT(*), MAX(t.Txn_DateTime)
                FROM dbo.NETS_Transaction t
                JOIN (SELECT Machine_Code, MAX(Txn_DateTime) AS LastFlag
                      FROM dbo.NETS_Transaction
                      WHERE Card_Hash IN (SELECT Card_Hash FROM dbo.NETS_FlagCard
                                          WHERE IsActive = 1)
                        AND Machine_Code IS NOT NULL
                      GROUP BY Machine_Code) f ON f.Machine_Code = t.Machine_Code
                WHERE t.Txn_Status_Code = 0
                  AND t.Machine_Code IN (%s)
                  AND t.Txn_DateTime > f.LastFlag
                GROUP BY t.Machine_Code
            """ % marks, tuple(codes))
            counts = {str(r[0]): (int(r[1]), r[2]) for r in cur.fetchall()}

        # Unattributable dispenses. Required on screen by the Auresys handoff:
        # "Any UI must show this figure rather than silently excluding it, or
        # totals will not reconcile."
        cur.execute("""
            SELECT COUNT(*) FROM dbo.NETS_Transaction
            WHERE Machine_Code IS NULL AND Txn_Status_Code = 0
              AND Txn_Date >= DATEADD(day, -30, CAST(GETDATE() AS DATE))
        """)
        null_coded = int(cur.fetchone()[0])

        # Three different things used to be collapsed into "no payment
        # terminal", and the screen stated it as fact:
        #   never seen at all        -> genuinely has no terminal
        #   seen historically, quiet -> DEAD READER or broken feed
        #   seen recently            -> fine
        # The middle one is the alert condition the handoff names — "check
        # before dispatching anyone" — and calling it a benign property made
        # the one screen that could have surfaced it hide it instead.
        cur.execute("""
            SELECT Machine_Code, MAX(Txn_Date) FROM dbo.NETS_Transaction
            WHERE Machine_Code IS NOT NULL GROUP BY Machine_Code
        """)
        last_seen = {str(r[0]): r[1] for r in cur.fetchall()}
        cutoff = (now + timedelta(hours=8)).date() - timedelta(days=30)

        # Franchisee = Account_Key of the machine's newest row. Guarded so a
        # database that has not had migration_2026-09-03_franchisee.sql yet
        # still serves the counter, just without stripes. 180-day floor keeps
        # the window function off the whole history; a machine with nothing
        # in 180 days is on the dead-reader list anyway. Filtered on
        # Txn_DateTime, the index key - Txn_Date is not on
        # IX_NETS_Txn_MachineTime and would force a lookup per row.
        acct_of = {}
        try:
            cur.execute("""
                SELECT Machine_Code, Account_Key FROM (
                    SELECT Machine_Code, Account_Key,
                           ROW_NUMBER() OVER (PARTITION BY Machine_Code
                                              ORDER BY Txn_DateTime DESC) AS rn
                    FROM dbo.NETS_Transaction
                    WHERE Machine_Code IS NOT NULL
                      AND Txn_DateTime >= DATEADD(day, -180, CAST(GETDATE() AS DATE))
                ) x WHERE rn = 1
            """)
            for code, key in cur.fetchall():
                key = (key or "").strip().upper() or _MAIN_ACCOUNT
                if key != _MAIN_ACCOUNT:
                    acct_of[str(code)] = key
        except Exception as e:
            print("[topups_api] Account_Key unavailable (migration not run?): %s" % e)

        cur.execute("SELECT MachineCode, MachineName FROM MachineLookup "
                    "WHERE ISNULL(IsActive,1) = 1 ORDER BY MachineName")
        machines = [(str(a), b) for a, b in cur.fetchall()]

        rows, no_terminal, dead_reader = [], [], []
        for code, name in machines:
            ls = last_seen.get(code)
            if ls is None:
                no_terminal.append({"code": code, "name": name})
                continue
            if ls < cutoff:
                dead_reader.append({"code": code, "name": name,
                                    "lastSeen": ls.isoformat()})
                continue
            lf = last_flag.get(code)
            n, _last_vend = counts.get(code, (0, None))
            rows.append({
                "code": code, "name": name,
                # null, not 0, when the machine has never been flagged. The UI
                # must not be able to render an invented number.
                "dispenses": (n if lf is not None else None),
                "lastFlag": lf.isoformat() if lf else None,
                "everFlagged": lf is not None,
                # account key or null. Label/colour come from `franchisees`
                # below, once per payload, not repeated on every row.
                "franchisee": acct_of.get(code),
            })
        # Flagged machines first, busiest at the top; never-flagged machines
        # after them, alphabetically. One list, two clearly separated blocks —
        # not one ranking over two incomparable scales.
        rows.sort(key=lambda x: (x["dispenses"] is None,
                                 -(x["dispenses"] or 0), x["name"] or ""))

        # Legend: only the franchisees actually on screen, in nets_mapping
        # order. An account with no machine listed gets no legend entry - a
        # legend that names a colour nothing uses is noise. A key the mapping
        # does not know (someone renamed ACCOUNTS) still gets a stripe, with
        # the key as its label and a neutral colour, rather than vanishing.
        present = {r["franchisee"] for r in rows if r["franchisee"]}
        franchisees = []
        for key, meta in _ACCOUNTS.items():
            if key in present and key != _MAIN_ACCOUNT:
                franchisees.append({"key": key, "label": meta.get("label") or key,
                                    "color": meta.get("color") or "#6b7280"})
        for key in sorted(present - set(_ACCOUNTS)):
            franchisees.append({"key": key, "label": key, "color": "#6b7280"})

        payload = {
            "rows": rows, "ready": True,
            "franchisees": franchisees,
            "skippedAccounts": skipped_accounts,
            "cards": n_cards,
            "staleAsOf": stale_as_of, "feedOk": feed_ok, "feedFresh": feed_fresh,
            "pullTimes": "06:00 and 18:00 SGT",
            "nullCoded": null_coded,
            "noTerminal": no_terminal,
            "deadReader": dead_reader,
            "unflagged": sum(1 for r in rows if not r["everFlagged"]),
            "generatedAt": now.isoformat() + "Z",
        }
        _VEND_CACHE["at"], _VEND_CACHE["payload"] = now, payload
        return jsonify({**payload, "cached": False})
    except Exception as e:
        return jsonify({"error": _db_error(e)}), 500
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/topups/gcal
# ─────────────────────────────────────────────────────────────────────────────

@topups_bp.route("/gcal")
@require_roles(*SALES_ROLES)
def api_topups_gcal():
    """The next N days of the sales calendar, with anything already booked marked.

    Reads gcal_feed's in-memory cache. Never fetches: app.py runs one gunicorn
    sync worker, so an outbound call inside a request blocks every other user
    for its full timeout.
    """
    days = request.args.get("days", type=int) or GCAL_WINDOW_DAYS
    days = max(1, min(days, 56))            # gcal_feed only holds FORWARD_DAYS=56
    today = _sgt_today()
    _t = datetime.strptime(today, "%Y-%m-%d").date()
    to = (_t + timedelta(days=days - 1)).isoformat()
    far = (_t + timedelta(days=90)).isoformat()      # move-target lookahead

    try:
        import gcal_feed
        snap = gcal_feed.snapshot(today, to)
        # gcal_feed's own error string can contain the Apps Script URL, and that
        # URL carries GCAL_FEED_SECRET in ?k=. It never goes to a browser.
        if snap.get("error"):
            print("[topups_api] gcal feed error: %s" % snap["error"])
            snap["feedError"] = ("The sales calendar could not be reached on the "
                                 "last poll. The list below may be out of date.")
    except Exception as e:
        print("[topups_api] gcal snapshot failed: %s" % e)
        # gcalSyncOn MUST be present even here. Omitting it made the client read
        # `!!undefined` -> false, which hid the red banner and RE-ENABLED the
        # Submit button, so a dispatcher could stage thirty machines and get the
        # 409 back as a 1.9-second toast with nothing created.
        return jsonify({"events": [], "enabled": False,
                        "gcalSyncOn": _gcal_sync_on(),
                        "feedError": "The sales calendar feed is unavailable."}), 200

    codes = {c for e in snap.get("stops", []) for c in (e.get("codes") or [])}
    booked = {}
    if codes:
        conn = None
        try:
            conn = get_connection()
            cur = conn.cursor()
            marks = ", ".join(["%s"] * len(codes))
            # Bounded, but MUCH wider than the 14-day request window. Two
            # failures to avoid at once:
            #   too narrow - a machine whose open stop sits beyond the window
            #     reports no move target, so submitting it INSERTS a second open
            #     stop instead of moving the first. Silent double-booking.
            #   unbounded  - one stale open row from last November follows the
            #     machine around forever with no way to find it from here.
            # today..+90d covers every stop a planner can realistically be
            # moving, and anything older than today is deliberately excluded:
            # a stop in the past is history, not something to drag forward.
            cur.execute(
                "SELECT MachineCode, CONVERT(VARCHAR(10), ScheduledDate, 23), "
                "       DeliveryOrderID "
                "FROM WO_DeliveryOrders "
                "WHERE Status <> 'completed' AND MachineCode IN (%s) "
                "  AND ScheduledDate BETWEEN %%s AND %%s" % marks,
                tuple(codes) + (today, far))
            for c, d, i in cur.fetchall():
                booked.setdefault(str(c), []).append({"date": d, "id": int(i)})
        except Exception:
            booked = {}
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    out = []
    for e in snap.get("stops", []):
        ev = dict(e)
        # PER CODE, not a union across the event. A "CGH x3" entry maps to three
        # machines; when one already had a stop, the union disabled all three
        # and the planner could not reach the other two at all.
        ev["bookedByCode"] = {str(c): booked.get(str(c), []) for c in (e.get("codes") or [])}
        # Pickable = we know which machine it means. Whether a stop already
        # exists is NOT a reason to block: the request is the thing sales keyed,
        # and placing it on a date is exactly what this screen is for. An
        # existing row is carried as bookedByCode so the client can send its id
        # and have the server move it rather than duplicate it.
        #
        # partial / over / unmapped / unknown stay unpickable — those are cases
        # where the alias table cannot say which machine is meant, and guessing
        # is the one thing gcal_sync was careful never to do.
        ev["pickable"] = (e.get("status") == "ok") and bool(e.get("codes"))
        out.append(ev)

    # feedError, NOT error. gcal_feed keeps _cache["error"] set from the moment
    # one poll fails until the next success, while the cached events stay
    # perfectly good — and the client's api() discards any body with a truthy
    # `error`, which blanked the list AND left gcalSyncOn undefined, which
    # re-enabled Submit while the auto-sync was genuinely live. One transient
    # 500 from Apps Script was enough.
    return jsonify({"events": out, "enabled": snap.get("enabled"),
                    "ok": snap.get("ok"), "feedError": snap.get("feedError"),
                    "fetchedAt": snap.get("fetchedAt"),
                    "gcalSyncOn": _gcal_sync_on(),
                    "from": today, "to": to})


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/topups/batch
# ─────────────────────────────────────────────────────────────────────────────

@topups_bp.route("/batch", methods=["POST"])
@require_roles(*SALES_ROLES)
def api_topups_batch():
    """Turn a screenful of picks into unassigned stops for one date.

    Creating stops is sales' job and dispatch's job; NAMING A DRIVER is neither.
    Like /api/wo/schedule/stops, this route never accepts an assigned_to, so
    there stays exactly one place in the app where assignment happens.
    """
    if _gcal_sync_on():
        return jsonify({
            "error": "Google Calendar auto-sync is still switched on. It creates "
                     "its own stops for the next 28 days and deletes any stop it "
                     "does not recognise, so anything submitted here would be "
                     "double-booked or removed within five minutes. Set the "
                     "GCAL_SYNC app setting to 0 and restart the app first.",
            "code": "gcal_sync_on"}), 409

    data = request.get_json(silent=True) or {}
    day = _iso(data.get("scheduled_date"))
    items = data.get("items") or []
    # An item carrying move_id RELOCATES that stop instead of creating one.
    # See the module docstring: a sales calendar entry is a request, and placing
    # it must never leave two open stops for one machine.
    if not day:
        return jsonify({"error": "scheduled_date is required (YYYY-MM-DD)."}), 400
    if day.isoformat() < _sgt_today():
        return jsonify({"error": "Pick today or a later date."}), 400
    if not isinstance(items, list) or not items:
        return jsonify({"error": "Pick at least one machine."}), 400
    if len(items) > 200:
        return jsonify({"error": "Too many machines in one submit (max 200)."}), 400

    user = get_current_user()
    iso = day.isoformat()
    created, skipped = [], []
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        have = _topup_cols(cur)
        if not have["ScheduledDate"]:
            return jsonify({"error": "Scheduling columns are not migrated yet."}), 503

        # Two set-based probes before the loop instead of two round trips per
        # item. 200 items used to be ~800 round trips inside ONE uncommitted
        # transaction holding locks on WO_DeliveryOrders, on the single sync
        # worker — every other user, drivers finalising work orders included,
        # waited for all of it.
        want = []
        for it in items:
            c = str((it or {}).get("machine_code") or "").strip()
            if c and c not in want:
                want.append(c)
        reg, blocked, future_open = {}, {}, {}
        if want:
            marks = ", ".join(["%s"] * len(want))
            cur.execute("SELECT MachineCode, MachineName, ISNULL(IsActive,1) "
                        "FROM MachineLookup WHERE MachineCode IN (%s)" % marks,
                        tuple(want))
            # Keyed case-INSENSITIVELY. The per-item version compared inside
            # SQL, under the database's case-insensitive collation. Moving the
            # comparison into Python quietly made it case-sensitive, so a
            # MachineCode stored in a different case — the ad-hoc rows minted
            # from WO_VisitSessions at workorders.py:5894 are the live example —
            # would slip past the duplicate guard and create the second open row
            # the whole ISNULL rule exists to prevent.
            reg = {str(a).upper(): (b, int(c)) for a, b, c in cur.fetchall()}
            cur.execute(
                "SELECT MachineCode, DeliveryOrderID, "
                "       CONVERT(VARCHAR(10), ScheduledDate, 23) "
                "FROM WO_DeliveryOrders "
                "WHERE MachineCode IN (%s) AND Status <> 'completed' "
                "  AND ISNULL(CONVERT(VARCHAR(10), ScheduledDate, 23), %%s) = %%s "
                "ORDER BY CreatedAt, DeliveryOrderID" % marks,
                tuple(want) + (iso, iso))
            for c, i, d in cur.fetchall():
                blocked.setdefault(str(c).upper(), (int(i), d))   # first by CreatedAt wins
            # Any open, dated, future stop for these machines - the set the
            # "you already have one on another day" check below reads.
            cur.execute(
                "SELECT MachineCode, DeliveryOrderID, "
                "       CONVERT(VARCHAR(10), ScheduledDate, 23) "
                "FROM WO_DeliveryOrders "
                "WHERE MachineCode IN (%s) AND Status <> 'completed' "
                "  AND ScheduledDate IS NOT NULL AND ScheduledDate >= %%s "
                "ORDER BY ScheduledDate, DeliveryOrderID" % marks,
                tuple(want) + (_sgt_today(),))
            for c, i, d in cur.fetchall():
                future_open.setdefault(str(c).upper(), (int(i), d))

        # Rows the client asked to move, fetched up front so the loop does not
        # round-trip per item.
        move_ids = []
        for it in items:
            try:
                mid = int((it or {}).get("move_id"))
            except (TypeError, ValueError):
                continue
            if mid not in move_ids:
                move_ids.append(mid)
        movable = {}
        if move_ids:
            mm = ", ".join(["%s"] * len(move_ids))
            cur.execute(
                "SELECT DeliveryOrderID, MachineCode, MachineName, Status, AssignedTo, "
                "       CONVERT(VARCHAR(10), ScheduledDate, 23) "
                "FROM WO_DeliveryOrders WHERE DeliveryOrderID IN (%s)" % mm,
                tuple(move_ids))
            for did, mc, mn, st, who, sd in cur.fetchall():
                movable[int(did)] = {"code": (str(mc).upper() if mc else None),
                                     "name": mn, "status": (st or "").lower(),
                                     "assigned": who, "date": sd}

        seen = set()
        for it in items:
            code = str((it or {}).get("machine_code") or "").strip()
            if not code:
                continue
            if code in seen:
                skipped.append({"code": code, "why": "listed twice in this submit"})
                continue
            seen.add(code)

            shift = (it or {}).get("shift")
            shift = {"day": 0, "night": 1, 0: 0, 1: 1, "0": 0, "1": 1}.get(shift)
            if shift is None:
                skipped.append({"code": code, "why": "no day/night shift chosen"})
                continue

            m = reg.get(code.upper())
            if not m:
                skipped.append({"code": code, "why": "not in the machine registry"})
                continue
            if not m[1]:
                skipped.append({"code": code, "name": m[0],
                                "why": "%s is decommissioned" % m[0]})
                continue

            # ── MOVE an existing stop rather than create a second one ────────
            try:
                mid = int((it or {}).get("move_id"))
            except (TypeError, ValueError):
                mid = None
            if mid is not None:
                row = movable.get(mid)
                if not row:
                    skipped.append({"code": code, "name": m[0],
                                    "why": "that request's stop no longer exists"})
                    continue
                if row["status"] == "completed":
                    skipped.append({"code": code, "name": m[0],
                                    "why": "already completed on %s" % (row["date"] or "an earlier day")})
                    continue
                if row["code"] != code.upper():
                    skipped.append({"code": code, "name": m[0],
                                    "why": "that stop is for a different machine"})
                    continue
                # Anything else open for this machine on the target day, other
                # than the row being moved, is still a collision.
                other = blocked.get(code.upper())
                if other and other[0] != mid:
                    skipped.append({"code": code, "name": m[0], "existing_id": other[0],
                                    "why": ("already has another open stop on this day"
                                            if other[1] else
                                            "has an open stop with no date (DO-%d), which "
                                            "blocks every day until it is closed or dated"
                                            % other[0])})
                    continue
                sets, params = ["ScheduledDate = %s"], [iso]
                if have["ShiftCode"]:
                    sets.append("ShiftCode = %s"); params.append(shift)
                # Moving a stop invalidates its position in the old day's round.
                if have["RouteSeq"]:
                    sets.append("RouteSeq = NULL")
                params.append(mid)
                cur.execute("UPDATE WO_DeliveryOrders SET %s WHERE DeliveryOrderID = %%s"
                            % ", ".join(sets), tuple(params))
                _log_activity(cur, "deliveryorder", mid, "moved",
                              "Top-up planner: %s -> %s, %s shift"
                              % (row["date"] or "undated", iso, SHIFT_LABEL[shift]), user)
                created.append({"id": mid, "code": code, "name": m[0], "shift": shift,
                                "moved_from": row["date"], "assigned_to": row["assigned"]})
                continue

            # No move_id, but this machine already has an open stop on some
            # OTHER future day. Creating one here would leave two, and the
            # driver's TOP 1 sheet can only ever reach one of them. Refuse and
            # say where the existing one is, so the planner picks it from the
            # request list and moves it instead.
            elsewhere = future_open.get(code.upper())
            if elsewhere and elsewhere[1] != iso:
                skipped.append({
                    "code": code, "name": m[0], "existing_id": elsewhere[0],
                    "why": "already has an open stop on %s (DO-%d) — pick that "
                           "request from the list to MOVE it here rather than "
                           "adding a second" % (elsewhere[1], elsewhere[0])})
                continue

            # See the module docstring, point 2. The ISNULL is load-bearing.
            dup = blocked.get(code.upper())
            if dup:
                skipped.append({
                    "code": code, "name": m[0], "existing_id": dup[0],
                    "why": ("already has an open stop on this day" if dup[1]
                            else "has an open stop with no date (DO-%d), which blocks "
                                 "every day until it is closed or dated — it is shown "
                                 "on today's cell in the Calendar" % dup[0])})
                continue

            cols = ["MachineName", "MachineCode", "AssignedTo", "Priority",
                    "CreatedBy", "ScheduledDate"]
            vals = [m[0], code, None, "normal", user, iso]
            if have["ShiftCode"]:
                cols.append("ShiftCode"); vals.append(shift)
            # NeedsService is deliberately NOT settable from this screen. It
            # was accepted and never sent, and when it WAS sent the note went to
            # Notes rather than ServiceNote (unlike workorders.py:3776-3781), so
            # the driver's amber service box would have been empty. Ticking
            # "needs service" stays on the Day board, where it already works.
            if have["NeedsService"]:
                cols.append("NeedsService"); vals.append(0)
            note = (it or {}).get("note") or ""
            src = (it or {}).get("source") or "planner"
            gid = (it or {}).get("gcal_event_id")
            if gid and have["SourceGCalEventID"]:
                # NEVER GCalEventID. See the module docstring, point 1.
                cols.append("SourceGCalEventID"); vals.append(str(gid)[:256])
            cols.append("Notes")
            vals.append((note or ("From sales calendar: %s" % (it or {}).get("title", "")
                                  if src == "gcal" else
                                  "Top-up planner (%s shift)" % SHIFT_LABEL[shift]))[:3900])

            cur.execute(
                "INSERT INTO WO_DeliveryOrders (%s) OUTPUT INSERTED.DeliveryOrderID "
                "VALUES (%s)" % (", ".join(cols), ", ".join(["%s"] * len(cols))),
                tuple(vals))
            did = int(cur.fetchone()[0])
            _log_activity(cur, "deliveryorder", did, "created",
                          "Top-up planner: %s shift on %s (from %s)"
                          % (SHIFT_LABEL[shift], iso, src), user)
            created.append({"id": did, "code": code, "name": m[0], "shift": shift})

        conn.commit()
        return jsonify({"ok": True, "scheduled_date": iso,
                        "created": created, "skipped": skipped})
    except Exception as e:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        return jsonify({"error": _db_error(e)}), 500
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/topups/assign
# ─────────────────────────────────────────────────────────────────────────────

@topups_bp.route("/assign", methods=["POST"])
@require_roles(*DISPATCH_ROLES)
def api_topups_assign():
    """Put a driver's name on a day's unassigned stops, in one call.

    Deliberately NOT a loop over /api/wo/deliveryorders/<id>/assign: that route
    carries no machine+date duplicate guard and never allocates a RouteSeq, so
    assigning a stop onto a day the machine already has one produces a second
    open row the driver's TOP 1 sheet can never reach.

    Sales cannot reach this route. DISPATCH_ROLES excludes sales by design
    (workorders.py:60) and the client hides the screen to match, rather than
    showing a button that 403s.
    """
    data = request.get_json(silent=True) or {}
    day = _iso(data.get("scheduled_date"))
    pairs = data.get("assignments") or []
    if not day:
        return jsonify({"error": "scheduled_date is required (YYYY-MM-DD)."}), 400
    if not isinstance(pairs, list) or not pairs:
        return jsonify({"error": "Nothing to assign."}), 400

    user, iso = get_current_user(), day.isoformat()
    done, skipped = [], []
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        # The one write path dispatch depends on must not be the only one that
        # 500s on a half-applied schema.
        if not _topup_cols(cur)["ScheduledDate"]:
            return jsonify({"error": "Scheduling columns are not migrated yet."}), 503
        for p in pairs:
            did = (p or {}).get("id")
            who = ((p or {}).get("assigned_to") or "").strip().lower() or None
            try:
                did = int(did)
            except Exception:
                continue

            cur.execute("SELECT Status, MachineCode, MachineName, AssignedTo, "
                        "       CONVERT(VARCHAR(10), ScheduledDate, 23) "
                        "FROM WO_DeliveryOrders WHERE DeliveryOrderID = %s", (did,))
            r = cur.fetchone()
            if not r:
                skipped.append({"id": did, "why": "no longer exists"}); continue
            if (r[0] or "").lower() == "completed":
                skipped.append({"id": did, "name": r[2],
                                "why": "already completed"}); continue

            # The guard /api/wo/deliveryorders/<id>/assign is missing. It runs
            # whenever this call would MOVE the stop — not only when a driver is
            # named. An earlier version gated it on `if who:` while still
            # writing ScheduledDate unconditionally, so unassigning a stop onto
            # a day the machine already had one produced two open rows: the
            # driver's sheet is TOP 1 (workorders.py:5554), so the other could
            # never be closed and then blocked EVERY future date for that
            # machine through the ISNULL rule.
            moving = (r[4] != iso)
            if moving and r[1]:        # NULL MachineCode = NULL is UNKNOWN, so
                                       # the guard would pass vacuously on the
                                       # ad-hoc orders api_visit_update mints.
                cur.execute(
                    "SELECT TOP 1 DeliveryOrderID FROM WO_DeliveryOrders "
                    "WHERE MachineCode = %s AND DeliveryOrderID <> %s "
                    "  AND Status <> 'completed' "
                    "  AND ISNULL(CONVERT(VARCHAR(10), ScheduledDate, 23), %s) = %s "
                    "ORDER BY CreatedAt, DeliveryOrderID",
                    (r[1], did, iso, iso))
                dup = cur.fetchone()
                if dup:
                    skipped.append({"id": did, "name": r[2],
                                    "existing_id": int(dup[0]),
                                    "why": "that machine already has another open "
                                           "stop on this day"})
                    continue

            seq = _next_route_seq(cur, who, iso) if who else None
            sets, params = ["AssignedTo = %s"], [who]
            if moving:
                sets.append("ScheduledDate = %s"); params.append(iso)
            if who:
                if seq is not None:
                    sets.append("RouteSeq = %s"); params.append(seq)
            else:
                # A stop with no driver has no position in anybody's round.
                # Leaving the old RouteSeq behind made it sort into a stranger's
                # sequence the moment it was reassigned.
                sets.append("RouteSeq = NULL")
            params.append(did)
            cur.execute("UPDATE WO_DeliveryOrders SET %s WHERE DeliveryOrderID = %%s"
                        % ", ".join(sets), tuple(params))
            _log_activity(cur, "deliveryorder", did,
                          "assigned" if who else "unassigned",
                          "%s for %s" % (who or "unassigned", iso), user)
            done.append({"id": did, "assigned_to": who, "route_seq": seq})

        conn.commit()
        return jsonify({"ok": True, "assigned": done, "skipped": skipped})
    except Exception as e:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        return jsonify({"error": _db_error(e)}), 500
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/topups/move
# ─────────────────────────────────────────────────────────────────────────────

@topups_bp.route("/move", methods=["POST"])
@require_roles(*DISPATCH_ROLES)
def api_topups_move():
    """Dispatch relocates one stop to another day, or withdraws it entirely.

    Sales says which sites want a visit; dispatch says which day. This is that
    authority, exercised from the Calendar rather than the planner — the same
    stop, moved back and forth, never copied.

    action = "move"     -> scheduled_date (+ optional shift)
    action = "withdraw" -> deletes the stop, returning the site to the request
                           pool it came from.

    WHY WITHDRAW DELETES RATHER THAN CLEARING ScheduledDate
    A stop with a NULL ScheduledDate is not "unscheduled" — because
    WO_VisitSessions carries a single LinkedDeliveryOrderID, the duplicate guard
    has to treat an undated open row as colliding with EVERY date
    (workorders.py:3752-3765). Nulling the date would therefore lock that
    machine out of the planner completely, which is the opposite of withdrawing
    it. Deleting is what the sales cancel path already does
    (workorders._cancel_stop_rows), and it snapshots to WO_DeletedLog first, so
    it is recoverable.

    Withdraw refuses once a driver holds the stop: at that point it is in
    somebody's round, and pulling it out from under them silently is how a
    driver ends up at a site nobody expects. Unassign it first.
    """
    data = request.get_json(silent=True) or {}
    action = (data.get("action") or "move").lower()
    try:
        did = int(data.get("id"))
    except (TypeError, ValueError):
        return jsonify({"error": "id is required."}), 400

    user = get_current_user()
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        have = _topup_cols(cur)
        if not have["ScheduledDate"]:
            return jsonify({"error": "Scheduling columns are not migrated yet."}), 503

        cur.execute("SELECT Status, MachineCode, MachineName, AssignedTo, "
                    "       CONVERT(VARCHAR(10), ScheduledDate, 23) "
                    "FROM WO_DeliveryOrders WHERE DeliveryOrderID = %s", (did,))
        r = cur.fetchone()
        if not r:
            return jsonify({"error": "That stop no longer exists."}), 404
        status, code, name, who, cur_date = (r[0] or "").lower(), r[1], r[2], r[3], r[4]
        if status == "completed":
            return jsonify({"error": "%s was completed on %s — it cannot be moved."
                                     % (name, cur_date or "an earlier day")}), 409

        if action == "withdraw":
            if who:
                return jsonify({"error": "%s is assigned to %s. Unassign it first, then "
                                         "withdraw it." % (name, who)}), 409
            # No _log_activity first: _cancel_stop_rows DELETEs this order's
            # WO_Activity rows on its way out (workorders.py:3838), so anything
            # written here would be wiped in the same breath. What survives is
            # the reason string below, in WO_DeletedLog, alongside a full row
            # snapshot — which is what makes this recoverable rather than gone.
            from workorders import _cancel_stop_rows
            gone = _cancel_stop_rows(
                cur, [did], user,
                "Withdrawn from the plan (was %s)" % (cur_date or "undated"))
            if not gone:
                # The helper only deletes rows whose Status is exactly 'open'.
                # Anything else means someone changed it while this was in flight.
                conn.rollback()
                return jsonify({"error": "%s is no longer an open stop — reload the "
                                         "calendar." % name}), 409
            conn.commit()
            return jsonify({"ok": True, "id": did, "action": "withdraw", "name": name})

        day = _iso(data.get("scheduled_date"))
        if not day:
            return jsonify({"error": "scheduled_date is required (YYYY-MM-DD)."}), 400
        iso = day.isoformat()
        if iso < _sgt_today():
            return jsonify({"error": "Pick today or a later date."}), 400

        shift = data.get("shift")
        shift = {"day": 0, "night": 1, 0: 0, 1: 1, "0": 0, "1": 1}.get(shift)

        if code:
            cur.execute(
                "SELECT TOP 1 DeliveryOrderID FROM WO_DeliveryOrders "
                "WHERE MachineCode = %s AND DeliveryOrderID <> %s "
                "  AND Status <> 'completed' "
                "  AND ISNULL(CONVERT(VARCHAR(10), ScheduledDate, 23), %s) = %s "
                "ORDER BY CreatedAt, DeliveryOrderID", (code, did, iso, iso))
            dup = cur.fetchone()
            if dup:
                return jsonify({"error": "%s already has another open stop on %s "
                                         "(DO-%d)." % (name, iso, int(dup[0]))}), 409

        sets, params = ["ScheduledDate = %s"], [iso]
        if shift is not None and have["ShiftCode"]:
            sets.append("ShiftCode = %s"); params.append(shift)
        if have["RouteSeq"]:
            # Its position belonged to the old day's round.
            seq = _next_route_seq(cur, (who or "").lower() or None, iso) if who else None
            if seq is not None:
                sets.append("RouteSeq = %s"); params.append(seq)
            else:
                sets.append("RouteSeq = NULL")
        params.append(did)
        cur.execute("UPDATE WO_DeliveryOrders SET %s WHERE DeliveryOrderID = %%s"
                    % ", ".join(sets), tuple(params))
        _log_activity(cur, "deliveryorder", did, "moved",
                      "%s -> %s%s" % (cur_date or "undated", iso,
                                      (", %s shift" % SHIFT_LABEL[shift])
                                      if shift is not None else ""), user)
        conn.commit()
        return jsonify({"ok": True, "id": did, "action": "move",
                        "name": name, "from": cur_date, "to": iso, "shift": shift})
    except Exception as e:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        return jsonify({"error": _db_error(e)}), 500
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/topups/outcome
# ─────────────────────────────────────────────────────────────────────────────

@topups_bp.route("/outcome", methods=["POST"])
@require_roles(*DISPATCH_ROLES)
def api_topups_outcome():
    """The dispatcher's own colour on a past stop.

    "This is for the dispatcher to see" — so the judgement is recorded here, by
    dispatch, on the Calendar screen. The driver's Work Order sheet is untouched
    by this release: it still offers only 'customer unavailable', and that still
    completes the delivery, exactly as it does in production today.

    Passing outcome = null clears the override and hands the cell back to the
    derived colour.
    """
    data = request.get_json(silent=True) or {}
    try:
        did = int(data.get("id"))
    except Exception:
        return jsonify({"error": "id is required."}), 400
    oc = data.get("outcome")
    if oc is not None:
        try:
            oc = int(oc)
        except Exception:
            oc = None
        if oc not in OUTCOME_LABEL:
            return jsonify({"error": "outcome must be 0 (serviced), 1 (faulty), "
                                     "2 (unable) or null."}), 400
    note = (data.get("note") or "").strip()[:500] or None
    user = get_current_user()

    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        have = _topup_cols(cur)
        if not have["OutcomeCode"]:
            return jsonify({"error": "Outcome columns are not migrated yet. Run "
                                     "migration_topups_2026-08-23.sql BLOCK 1."}), 503

        cur.execute("SELECT MachineName FROM WO_DeliveryOrders WHERE DeliveryOrderID = %s",
                    (did,))
        r = cur.fetchone()
        if not r:
            return jsonify({"error": "That stop no longer exists."}), 404

        sets, params = ["OutcomeCode = %s"], [oc]
        if have["OutcomeNote"]:
            sets.append("OutcomeNote = %s"); params.append(note)
        if have["OutcomeBy"]:
            sets.append("OutcomeBy = %s"); params.append(user if oc is not None else None)
        if have["OutcomeAt"]:
            sets.append("OutcomeAt = %s")
            params.append(datetime.utcnow() if oc is not None else None)
        params.append(did)
        cur.execute("UPDATE WO_DeliveryOrders SET %s WHERE DeliveryOrderID = %%s"
                    % ", ".join(sets), tuple(params))
        _log_activity(cur, "deliveryorder", did, "outcome_set",
                      "%s%s" % (OUTCOME_LABEL.get(oc, "cleared"),
                                (" - " + note) if note else ""), user)
        conn.commit()
        return jsonify({"ok": True, "id": did, "outcome": oc})
    except Exception as e:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        return jsonify({"error": _db_error(e)}), 500
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/topups/health
# ─────────────────────────────────────────────────────────────────────────────

@topups_bp.route("/health")
@require_roles(*DISPATCH_ROLES)
def api_topups_health():
    """One call that says whether this feature is actually wired up.

    Every failure in this module degrades quietly by house style, which is right
    for a screen but wrong for a cutover. Hit this after deploying.
    """
    out = {"gcalSyncOn": _gcal_sync_on(), "checks": {}}
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        out["checks"]["columns"] = _topup_cols(cur)
        for name, sql in (
            ("nets_transaction_readable",
             "SELECT TOP 1 1 FROM dbo.NETS_Transaction"),
            ("flagcard_table",
             "SELECT COUNT(*) FROM dbo.NETS_FlagCard WHERE IsActive = 1"),
            ("nets_pull_run",
             "SELECT MAX(Finished_At_UTC) FROM dbo.NETS_Pull_Run WHERE Status='SUCCESS'"),
        ):
            try:
                cur.execute(sql)
                r = cur.fetchone()
                out["checks"][name] = {"ok": True,
                                       "value": str(r[0]) if r and r[0] is not None else None}
            except Exception as e:
                # Managers only reach this route, and a cutover check that hides
                # the reason is useless — so the detail stays here and nowhere else.
                out["checks"][name] = {"ok": False, "error": str(e)}
        # out["checks"]["columns"] is a dict of column -> bool with no "ok" key,
        # so the generic filter below skipped it entirely: the route whose whole
        # job is "is this feature wired up" reported ok:true on a completely
        # unmigrated database.
        out["ok"] = (
            all(v.get("ok") for v in out["checks"].values()
                if isinstance(v, dict) and "ok" in v)
            and all(out["checks"]["columns"].values())
            and not out["gcalSyncOn"])
        return jsonify(out)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), **out}), 500  # managers only
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
