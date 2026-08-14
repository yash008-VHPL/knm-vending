"""
gcal_sync.py — one-way sync: Google Calendar -> WO_DeliveryOrders.

The sales coordinator's calendar IS the schedule. Anything she keys there that
resolves to an exact machine set becomes a real, dispatchable stop with no
second confirmation on this end. Runs on gcal_feed's background thread after
each successful poll.

RULES (agreed 2026-08-14):
  * Horizon 28 days rolling. Events further out stay visible in the pane but do
    not become rows — a bad calendar edit must not propagate months forward.
  * An event removed or moved in Google cancels its stop ONLY while that stop is
    unassigned. Once dispatch has named a driver the row is left alone and
    reported as an exception, so a round never changes under a driver mid-day.
  * Only status == "ok" events sync (qty matches the mapped machine count).
    "unmapped"/"partial"/"over" are reported as exceptions and never guessed —
    silently creating the wrong machine is worse than creating nothing.
  * Never books over a stop that already exists for that machine+date from any
    source, so hand-keyed dispatch work is never doubled.

The diff is a PURE function (plan) so it can be tested without a database;
apply() does nothing but execute what plan() decided.
"""

from datetime import datetime, timedelta

SYNC_HORIZON_DAYS = 28
SYSTEM_USER = "google-calendar@feed"


def sgt_today():
    return (datetime.utcnow() + timedelta(hours=8)).date()


def _iso(d):
    return d.isoformat() if hasattr(d, "isoformat") else str(d)[:10]


def plan(events, existing, today=None, horizon_days=SYNC_HORIZON_DAYS):
    """Pure diff.

    events   -- gcal_feed snapshot stops
    existing -- rows already created by this sync, as dicts:
                {id, gcal_id, code, date, assigned_to, status}
    returns  {"create":[...], "cancel":[...], "flag":[...], "exceptions":[...],
              "window":(from,to)}
    """
    today = today or sgt_today()
    frm, to = _iso(today), _iso(today + timedelta(days=horizon_days))

    want = {}          # (gcal_id, code, date) -> event
    live = {}          # (gcal id, date) -> status, for events INSIDE the window
    exceptions = []
    for e in events:
        d = e.get("date") or ""
        if not (frm <= d <= to):
            continue
        # Keyed on (event, date), NOT event alone: an event moved from the 3rd
        # to the 9th is still "live" by id, but the 3rd's stop is genuinely
        # stale and must be cancelled, not held.
        k = (e.get("gcalId"), d)
        if live.get(k) != "ok":
            live[k] = e.get("status")
        if e.get("status") != "ok":
            exceptions.append({
                "gcalId": e.get("gcalId"), "title": e.get("title"),
                "date": d, "status": e.get("status"),
                "qty": e.get("qty"), "mapped": len(e.get("codes") or []),
                "why": {
                    "unmapped": "no machine mapped — add it to GCalSiteAlias",
                    "unknown":  "title never seen — add it to GCalSiteAlias",
                    "partial":  "calendar asks for fewer machines than are mapped; "
                                "the title does not say which",
                    "over":     "calendar asks for more machines than are mapped",
                }.get(e.get("status"), "not syncable"),
            })
            continue
        for c in e.get("codes") or []:
            want[(e.get("gcalId"), str(c), d)] = e

    have = {}
    for r in existing:
        have[(r.get("gcal_id"), str(r.get("code")), _iso(r.get("date")))] = r

    create = []
    for key, e in want.items():
        if key in have:
            continue
        gid, code, d = key
        create.append({"gcalId": gid, "code": code, "date": d,
                       "title": e.get("title") or e.get("site"),
                       "site": e.get("site")})

    cancel, flag, hold = [], [], []
    for key, r in have.items():
        if key in want:
            continue
        if (r.get("status") or "open").lower() != "open":
            continue                      # completed work is history, never touched
        gid, code, d = key
        item = {"id": r.get("id"), "gcalId": gid, "code": code, "date": d,
                "assigned_to": r.get("assigned_to")}

        st = live.get((gid, d))
        # st is None   -> the event is gone from this date (deleted, or moved
        #                 elsewhere). The stop is stale: cancel it.
        # st == "ok"   -> the event is still here and DOES resolve, it just no
        #                 longer wants this machine ("Meta x3" -> "Meta x2").
        #                 A deliberate reduction: cancel it.
        # anything else-> the event is still here but no longer parses (a title
        #                 edit the alias table cannot follow, e.g. "CGH x3" ->
        #                 "CGH x4"). We cannot tell intent. Cancelling would
        #                 delete the stop while sales still sees the event in
        #                 Google — the worst failure, because nobody is looking.
        #                 Hold the row and put it in front of a human.
        if st is not None and st != "ok":
            hold.append({**item, "why": "the calendar entry changed and no longer "
                                        "resolves to this machine"})
            continue

        (flag if r.get("assigned_to") else cancel).append(item)

    return {"create": create, "cancel": cancel, "flag": flag, "hold": hold,
            "exceptions": exceptions, "window": (frm, to)}


def _load_existing(cur, frm, to):
    cur.execute(
        "SELECT DeliveryOrderID, GCalEventID, MachineCode, "
        "CONVERT(VARCHAR(10), ScheduledDate, 23), AssignedTo, Status "
        "FROM WO_DeliveryOrders "
        "WHERE GCalEventID IS NOT NULL "
        "AND ScheduledDate BETWEEN %s AND %s", (frm, to))
    return [{"id": int(r[0]), "gcal_id": r[1], "code": str(r[2]),
             "date": r[3], "assigned_to": r[4], "status": r[5]}
            for r in cur.fetchall()]


def apply(p, cur):
    """Execute a plan. Caller owns the transaction."""
    from workorders import _sched_cols, _cancel_stop_rows, _log_activity

    have = _sched_cols(cur)
    if not have.get("ScheduledDate"):
        return {"error": "scheduling columns not migrated", "created": 0,
                "cancelled": 0, "skipped": []}

    created, skipped = 0, []
    for c in p["create"]:
        cur.execute("SELECT TOP 1 MachineName, ISNULL(IsActive,1) "
                    "FROM MachineLookup WHERE MachineCode = %s", (c["code"],))
        m = cur.fetchone()
        if not m:
            skipped.append({**c, "why": "not in machine registry"}); continue
        name = m[0] or c["code"]
        if not int(m[1] or 0):
            skipped.append({**c, "why": f"{name} is decommissioned"}); continue

        # Same guard as api_schedule_create: an UNDATED open row collides with
        # every date, so ISNULL() is deliberate, not sloppy.
        cur.execute(
            "SELECT TOP 1 DeliveryOrderID, AssignedTo FROM WO_DeliveryOrders "
            "WHERE MachineCode = %s AND Status <> 'completed' "
            "AND ISNULL(CONVERT(VARCHAR(10), ScheduledDate, 23), %s) = %s",
            (c["code"], c["date"], c["date"]))
        dup = cur.fetchone()
        if dup:
            skipped.append({**c, "why": "already has an open stop",
                            "existing_id": int(dup[0])}); continue

        cols = ["MachineName", "MachineCode", "AssignedTo", "Priority",
                "CreatedBy", "ScheduledDate", "GCalEventID"]
        vals = [name, c["code"], None, "normal", SYSTEM_USER, c["date"], c["gcalId"]]
        cols += ["Notes"]; vals += ["From sales calendar: %s" % (c["title"] or c["site"])]
        if have.get("NeedsService"):
            cols += ["NeedsService"]; vals += [0]
        if have.get("RequestedBy"):
            cols += ["RequestedBy"]; vals += [SYSTEM_USER]
        cur.execute(
            "INSERT INTO WO_DeliveryOrders (%s) OUTPUT INSERTED.DeliveryOrderID "
            "VALUES (%s)" % (", ".join(cols), ", ".join(["%s"] * len(cols))),
            tuple(vals))
        did = int(cur.fetchone()[0])
        _log_activity(cur, "deliveryorder", did, "created",
                      "Google Calendar sync: %s on %s" % (name, c["date"]),
                      SYSTEM_USER)
        created += 1

    cancelled = []
    if p["cancel"]:
        cancelled = _cancel_stop_rows(
            cur, [x["id"] for x in p["cancel"]], SYSTEM_USER,
            "Removed from the sales calendar")

    return {"created": created, "cancelled": len(cancelled),
            "flagged": len(p["flag"]), "held": len(p.get("hold") or []),
            "skipped": skipped, "exceptions": len(p["exceptions"])}


def run(events, get_cursor):
    """Poll-time entry point. Returns a report dict; never raises."""
    conn = cur = None
    try:
        conn, cur = get_cursor()
        today = sgt_today()
        frm = _iso(today)
        to = _iso(today + timedelta(days=SYNC_HORIZON_DAYS))
        p = plan(events, _load_existing(cur, frm, to), today)
        rep = apply(p, cur)
        conn.commit()
        rep["window"] = p["window"]
        rep["exceptionList"] = p["exceptions"]
        rep["flagList"] = p["flag"]
        rep["holdList"] = p.get("hold") or []
        return rep
    except Exception as e:
        if conn is not None:
            try: conn.rollback()
            except Exception: pass
        return {"error": "%s: %s" % (type(e).__name__, e)}
    finally:
        if conn is not None:
            try: conn.close()
            except Exception: pass
