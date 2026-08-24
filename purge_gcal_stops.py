"""
purge_gcal_stops.py — retire the stops gcal_sync created but nobody confirmed.
                                                                     2026-08-24

WHY THIS EXISTS
---------------
Until 2026-08-24 gcal_feed defaulted GCAL_SYNC to ON, and gcal_sync.apply()
INSERTed a real WO_DeliveryOrders row for every resolvable sales-calendar event
inside a 28-day rolling horizon. A sales calendar entry is a REQUEST for a
visit; only dispatch confirms a stop. Those rows made every request look like
confirmed, dispatched work.

The flag is off now, but the horizon it already materialised is still in the
table. This retires the part of it that nobody has touched, and leaves
everything else alone.

RUN IT STANDALONE. Do NOT rewrite this to `from workorders import
_cancel_stop_rows`: workorders imports app, and importing app registers the
blueprints, runs init_workorders_db()'s ALTERs and calls gcal_feed.start() — so
the "purge script" would start a second live calendar poller in your shell and,
if GCAL_SYNC were set in that shell, put back everything it had just deleted.
The delete below is _cancel_stop_rows (workorders.py:3816) inlined, statement
for statement.

ORDER OF OPERATIONS — the flip must be LIVE before this runs
------------------------------------------------------------
gcal_feed reads the setting per poll (default 300s), and gcal_sync.plan() puts
back anything it finds missing, with NEW DeliveryOrderIDs. Purging against a
process that still has the sync on leaves a deletion log full of ids that no
longer mean anything and looks like the script silently failed.

  1. Deploy the code change and set GCAL_SYNC=0 in App Service. Restart.
  2. Verify against the RUNNING app, not the portal:
        GET /api/topups/calendar?from=<today>&to=<today>   ->  "gcalSyncOn": false
  3. Then run this, and pass --sync-verified-off to say you did step 2.

WHAT IT WILL NOT DELETE
-----------------------
`AssignedTo IS NULL` is NOT a proxy for "unconfirmed". Four routes mutate a
gcal row in place, are real dispatcher decisions, and leave AssignedTo NULL:

  PATCH /api/wo/stops/topup/<id>   NeedsService, ServiceNote, Notes, Priority
  POST  /api/topups/move  (move)   ScheduledDate, ShiftCode, RouteSeq
  POST  /api/topups/assign (null)  ScheduledDate, RouteSeq
  POST  /api/topups/batch  (move)  ScheduledDate, ShiftCode

So the predicate also requires every column gcal_sync never writes to be
untouched, the Notes string it wrote to be intact, no visit session attached,
and no WO_Activity row other than the 'created' one it logged itself. That last
clause is the real backstop: all four routes call _log_activity.

Dry run prints the set AND the rows the naive predicate would have taken that
this one spares. Read that list before passing --apply.

USAGE
  export DB_SERVER=... DB_NAME=... DB_USER=... DB_PASSWORD=...
  python3 purge_gcal_stops.py                       # dry run, from tomorrow SGT
  python3 purge_gcal_stops.py --from 2026-08-25
  python3 purge_gcal_stops.py --apply --sync-verified-off

Default cut is TOMORROW (SGT): today's board is a round somebody may already be
driving. --include-today moves it to today.
"""

import argparse
import json
import sys
from datetime import datetime, timedelta

import pymssql

import config

SYSTEM_USER = "google-calendar@feed"
CHUNK = 50


def sgt_today():
    return (datetime.utcnow() + timedelta(hours=8)).date()


def connect():
    if not config.DB_USER or not config.DB_PASSWORD:
        sys.exit("DB_USER / DB_PASSWORD are not set in the environment.")
    return pymssql.connect(server=config.DB_SERVER, database=config.DB_NAME,
                           user=config.DB_USER, password=config.DB_PASSWORD,
                           tds_version="7.4")


def have_cols(cur, table, names):
    """Which of these columns exist. The topups migration is applied by hand and
    a half-applied schema is a real state, so the predicate is built from what
    is actually there rather than assumed."""
    cur.execute("SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_NAME = %s", (table,))
    got = {r[0] for r in cur.fetchall()}
    return {n: (n in got) for n in names}


def build_where(cur, narrow):
    """Returns (sql_fragment, column_probe, clause_list)."""
    c = have_cols(cur, "WO_DeliveryOrders",
                  ["GCalEventID", "NeedsService", "ShiftCode", "RouteSeq",
                   "OutcomeCode", "Priority", "Notes", "CreatedBy"])
    if not c["GCalEventID"]:
        sys.exit("WO_DeliveryOrders has no GCalEventID column — nothing this "
                 "script targets can exist. Nothing to do.")

    w = ["d.GCalEventID IS NOT NULL",
         "d.AssignedTo IS NULL",
         "d.Status = 'open'",
         "d.ScheduledDate >= %s"]
    if not narrow:
        return " AND ".join(w), c, list(w)

    # Columns gcal_sync.apply() never writes. Anything set here is a human.
    if c["NeedsService"]: w.append("ISNULL(d.NeedsService, 0) = 0")
    if c["ShiftCode"]:    w.append("d.ShiftCode IS NULL")
    if c["RouteSeq"]:     w.append("d.RouteSeq IS NULL")
    if c["OutcomeCode"]:  w.append("d.OutcomeCode IS NULL")
    if c["Priority"]:     w.append("ISNULL(d.Priority, 'normal') = 'normal'")
    if c["CreatedBy"]:    w.append("d.CreatedBy = '%s'" % SYSTEM_USER)
    # %% survives to a literal % only because every caller of this fragment
    # passes params (fetch() always passes (cut,)); pymssql leaves the string
    # alone when params is None. Do not execute this fragment without params.
    if c["Notes"]:        w.append("d.Notes LIKE 'From sales calendar:%%'")
    w.append("NOT EXISTS (SELECT 1 FROM WO_VisitSessions v "
             "WHERE v.LinkedDeliveryOrderID = d.DeliveryOrderID)")
    # The backstop: every dispatcher route logs an activity row.
    w.append("NOT EXISTS (SELECT 1 FROM WO_Activity a "
             "WHERE a.ParentType = 'deliveryorder' "
             "AND a.ParentID = d.DeliveryOrderID AND a.Action <> 'created')")
    return " AND ".join(w), c, list(w)


SEL = ("SELECT d.DeliveryOrderID, d.MachineCode, d.MachineName, "
       "CONVERT(VARCHAR(10), d.ScheduledDate, 23), d.GCalEventID "
       "FROM WO_DeliveryOrders d WHERE ")


def fetch(cur, cut, narrow):
    where, _cols, _clauses = build_where(cur, narrow)
    cur.execute(SEL + where + " ORDER BY d.ScheduledDate, d.DeliveryOrderID", (cut,))
    return [{"id": int(r[0]), "code": r[1], "name": r[2], "date": r[3],
             "gcal": r[4]} for r in cur.fetchall()]


def delete_one(cur, did, user, reason):
    """_cancel_stop_rows (workorders.py:3816) for a single id, inlined.

    Statement for statement, including the Status re-check inside the
    transaction and the 'signed'/'pending_email_signature' exemption — a signed
    visit keeps its link, so its evidence is never orphaned.

    TWO DELIBERATE DIVERGENCES FROM THE ORIGINAL:

    1. It also re-checks AssignedTo. Upstream every caller checks that
       immediately beforehand in the same transaction; here fetch() ran minutes
       ago and the loop commits per chunk against a LIVE app. POST
       /api/topups/assign names a driver WITHOUT changing Status, so the Status
       re-check alone would delete a stop out from under somebody's round —
       which is the one thing api_topups_move's withdraw refuses to do
       (topups_api.py:1159).
    2. log_deletion's try/except (app.py:182-194) is NOT reproduced. Upstream a
       failed WO_DeletedLog insert must never block a user's delete; here an
       audit row that will not write is a reason to stop the whole run, and
       earlier chunks are already committed and already logged.
    """
    cur.execute("SELECT Status, AssignedTo, MachineName FROM WO_DeliveryOrders "
                "WHERE DeliveryOrderID = %s", (did,))
    r = cur.fetchone()
    if not r or (r[0] or "").lower() != "open":
        return False
    if r[1]:
        print("  skipped DO-%d: assigned to %s since the dry run" % (did, r[1]))
        return False
    cur.execute("UPDATE WO_VisitSessions SET LinkedDeliveryOrderID = NULL "
                "WHERE LinkedDeliveryOrderID = %s "
                "AND Status NOT IN ('signed', 'pending_email_signature')", (did,))
    cur.execute("SELECT * FROM WO_DeliveryOrders WHERE DeliveryOrderID = %s", (did,))
    row = cur.fetchone()
    snap = {d[0]: v for d, v in zip(cur.description, row)} if row else None
    cur.execute("DELETE FROM WO_DeliveryOrderLines WHERE DeliveryOrderID = %s", (did,))
    cur.execute("DELETE FROM WO_Activity "
                "WHERE ParentType = 'deliveryorder' AND ParentID = %s", (did,))
    cur.execute("DELETE FROM WO_DeliveryOrders WHERE DeliveryOrderID = %s", (did,))
    cur.execute("INSERT INTO WO_DeletedLog "
                "(EntityType, EntityKey, Summary, Snapshot, Reversible, DeletedBy) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                ("deliveryorder", str(did),
                 ("%s: %s" % (reason, (snap or {}).get("MachineName") or did))[:500],
                 json.dumps(snap, default=str) if snap is not None else None,
                 0, user))
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="cut",
                    help="earliest ScheduledDate to purge (YYYY-MM-DD). "
                         "Default: tomorrow SGT.")
    ap.add_argument("--include-today", action="store_true",
                    help="move the cut to today. Today's board may be a round "
                         "somebody is already driving.")
    ap.add_argument("--apply", action="store_true",
                    help="actually delete. Without this it is a dry run.")
    ap.add_argument("--sync-verified-off", action="store_true",
                    help="you have confirmed gcalSyncOn:false on the RUNNING "
                         "app since its restart. Required with --apply.")
    ap.add_argument("--user", default="purge_gcal_stops.py",
                    help="name recorded in WO_DeletedLog.")
    a = ap.parse_args()

    cut = a.cut or (sgt_today() if a.include_today
                    else sgt_today() + timedelta(days=1)).isoformat()
    if a.apply and not a.sync_verified_off:
        sys.exit("Refusing to --apply without --sync-verified-off. gcal_sync "
                 "re-creates anything it finds missing, with new ids, within "
                 "one poll interval. Check GET /api/topups/calendar first.")

    conn = connect()
    cur = conn.cursor()
    # The probe drops a protective clause for every column the topups migration
    # has not landed, which degrades TOWARDS deleting more. Print what is
    # actually in force so "untouched by any dispatcher" is never read as a
    # stronger promise than this schema can make.
    _sql, _cols, active = build_where(cur, True)
    narrow = fetch(cur, cut, True)
    naive = fetch(cur, cut, False)
    narrow_ids = {r["id"] for r in narrow}
    spared = [r for r in naive if r["id"] not in narrow_ids]

    print("cut date        : %s (ScheduledDate >= this)" % cut)
    print("naive predicate : %d rows  (GCalEventID + unassigned + open)" % len(naive))
    print("THIS SCRIPT     : %d rows  (also untouched by any dispatcher)" % len(narrow))
    print("SPARED          : %d rows  <- a human has touched these" % len(spared))
    print("\nclauses in force on this database:")
    for cl in active:
        print("    %s" % cl)
    print()

    def show(title, rows):
        print("── %s ─────────────────────────────" % title)
        if not rows:
            print("  (none)\n"); return
        for r in rows:
            print("  DO-%-6d %-10s %-34s %s" % (r["id"], r["date"],
                                                (r["name"] or "")[:34], r["code"]))
        print()

    show("WOULD DELETE", narrow)
    show("SPARED — read every one of these", spared)

    if not a.apply:
        print("Dry run. Nothing was changed. Re-run with "
              "--apply --sync-verified-off to delete the first list.")
        conn.close(); return

    if not narrow:
        print("Nothing to delete."); conn.close(); return

    stamp = datetime.utcnow().strftime("%Y%m%d%H%M")
    bak_do = "WO_DeliveryOrders_gcalpurge_bak_%s" % stamp
    bak_ac = "WO_Activity_gcalpurge_bak_%s" % stamp
    ids = [r["id"] for r in narrow]
    inlist = ", ".join(str(i) for i in ids)

    # WO_DeletedLog is NOT a restore path: the deliveryorder restore route is
    # unimplemented (workorders.py:2721 returns 400) and WO_Activity rows are
    # hard deleted and are not in the snapshot. These two tables are the rollback.
    cur.execute("SELECT * INTO %s FROM WO_DeliveryOrders "
                "WHERE DeliveryOrderID IN (%s)" % (bak_do, inlist))
    cur.execute("SELECT * INTO %s FROM WO_Activity "
                "WHERE ParentType = 'deliveryorder' AND ParentID IN (%s)"
                % (bak_ac, inlist))
    conn.commit()
    print("backed up to %s and %s" % (bak_do, bak_ac))

    # Chunked, committed per chunk: this is ~7 statements per row against a
    # table with no index on GCalEventID, and the web app runs one gunicorn sync
    # worker — a single long transaction blocks every driver finalising a sheet.
    done = 0
    reason = "gcal_sync backfill retired 2026-08-24 (unconfirmed sales request)"
    for i in range(0, len(ids), CHUNK):
        for did in ids[i:i + CHUNK]:
            if delete_one(cur, did, a.user, reason):
                done += 1
        conn.commit()
        print("  committed %d/%d" % (min(i + CHUNK, len(ids)), len(ids)))

    print("\ndeleted %d of %d (any difference changed status mid-run)" % (done, len(ids)))
    print("rollback: the two backup tables above.")
    conn.close()


if __name__ == "__main__":
    main()
