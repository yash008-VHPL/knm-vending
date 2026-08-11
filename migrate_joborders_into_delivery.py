#!/usr/bin/env python3
"""
One-off — fold open, manually-raised job orders into delivery orders
====================================================================

Since 2026-08-11 a dispatch stop is ONE row: a WO_DeliveryOrders record with
`NeedsService` set and the request in `ServiceNote`. Job orders remain only for
the Tech Support path — a customer complaint triaged into a WO — which is
exactly the "job order with no delivery order" case.

This migrates the job orders created under the old two-row model.

SELF-CONTAINED BY DESIGN
    It does NOT import app.py or workorders.py. Importing workorders pulls in
    app, which re-runs init_db() and seed_locations() against production on
    every import — and the two import each other, so it fails anyway. Every
    statement this script needs is written out below.

IN SCOPE — a job order is migrated only when ALL of these hold:
  * StatusCode = 0        assigned. StatusCode 1 (needs_assistance) is SKIPPED:
                          a merged stop has no status codes, so migrating it
                          would drop LastBlockReason and the driver's ability
                          to re-escalate.
  * ComplaintID IS NULL   customer-raised work stays a job order.
  * MachineCode IS NOT NULL
  * no rows in WO_VisitSession_JobOrders   nobody has it open on a sheet
  * AttachedKBID IS NULL and no WO_JobOrderTasks rows
  * no rows in WO_Images                   a photo has no home on a delivery
                                           order and would be destroyed

TARGET SELECTION mirrors what the driver's sheet actually opens
(api_operator_location_detail): same machine, same assignee, not completed,
preferring the same ScheduledDate. If there is no such order — including when
the only open one belongs to another driver — a NEW delivery order is created
rather than filing the request somewhere nobody looks.

SAFETY
  * Preview is the default and writes nothing.
  * --apply commits one job order per transaction; a failure rolls back that
    one and the rest continue.
  * The job order's full row is copied into WO_DeletedLog before deletion.
    That is a snapshot for manual reconstruction, NOT a one-click restore —
    restoring after its note was absorbed would duplicate the request.
  * SharePoint files are never deleted. Job orders carrying images are skipped
    outright, so nothing is orphaned.

Run:
    python migrate_joborders_into_delivery.py            # preview
    python migrate_joborders_into_delivery.py --apply
"""
import argparse
import json
import sys
from datetime import datetime

import pymssql

import config


def connect():
    return pymssql.connect(
        server=config.DB_SERVER, database=config.DB_NAME,
        user=config.DB_USER, password=config.DB_PASSWORD,
        tds_version="7.4", login_timeout=10, timeout=60,
    )


def has_col(cur, table, col):
    cur.execute(
        "SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME=%s AND COLUMN_NAME=%s",
        (table, col),
    )
    return cur.fetchone() is not None


def candidates(cur, dated):
    sd = "jo.ScheduledDate" if dated else "NULL"
    rs = "jo.RouteSeq" if dated else "NULL"
    cur.execute(f"""
        SELECT jo.JobOrderID, jo.DisplayID, jo.MachineCode, jo.MachineName,
               jo.AssignedTo, jo.Priority, jo.StatusCode,
               jo.Diagnosis, jo.ProposedFix, jo.Notes, jo.AttachedKBID,
               jo.CreatedBy, {sd}, {rs},
               (SELECT COUNT(*) FROM WO_JobOrderTasks t       WHERE t.JobOrderID = jo.JobOrderID),
               (SELECT COUNT(*) FROM WO_VisitSession_JobOrders v WHERE v.JobOrderID = jo.JobOrderID),
               (SELECT COUNT(*) FROM WO_Images i WHERE i.ParentType='joborder' AND i.ParentID = jo.JobOrderID)
        FROM WO_JobOrders jo
        WHERE jo.StatusCode IN (0, 1) AND jo.ComplaintID IS NULL
        ORDER BY jo.JobOrderID
    """)
    out = []
    for r in cur.fetchall():
        out.append(dict(
            jid=int(r[0]), display=r[1], code=r[2], name=r[3], assigned=r[4],
            priority=r[5], status=int(r[6] or 0), diagnosis=r[7], fix=r[8],
            notes=r[9], kb=r[10], created_by=r[11],
            date=str(r[12])[:10] if r[12] else None, seq=r[13],
            tasks=int(r[14] or 0), visits=int(r[15] or 0), images=int(r[16] or 0),
        ))
    return out


def skip_reason(jo):
    if jo["status"] == 1:
        return "needs_assistance — keep the escalation, migrate by hand"
    if not jo["code"]:
        return "no MachineCode — a stop needs a machine"
    if jo["visits"]:
        return f"open on {jo['visits']} Work Order sheet(s)"
    if jo["kb"]:
        return f"KB reference {jo['kb']} — no home on a delivery order"
    if jo["tasks"]:
        return f"{jo['tasks']} task(s) — no home on a delivery order"
    if jo["images"]:
        return f"{jo['images']} photo(s) — would be orphaned"
    return None


def find_target(cur, jo, dated, claimed):
    """The order the driver's sheet would actually open: same machine, same
    assignee, not completed, today's date preferred. Anything else is invisible
    to them, so we would rather create a new order than file into it."""
    q = ("SELECT TOP 1 DeliveryOrderID, ServiceNote FROM WO_DeliveryOrders d "
         "WHERE d.MachineCode = %s AND d.Status <> 'completed' "
         "AND ISNULL(d.AssignedTo,'') = ISNULL(%s,'') "
         "AND NOT EXISTS (SELECT 1 FROM WO_VisitSessions v "
         "                WHERE v.LinkedDeliveryOrderID = d.DeliveryOrderID "
         "                  AND v.Status NOT IN ('signed','pending_email_signature'))")
    p = [jo["code"], jo["assigned"]]
    order = " ORDER BY "
    if dated and jo["date"]:
        order += "CASE WHEN CONVERT(VARCHAR(10), d.ScheduledDate, 23) = %s THEN 0 ELSE 1 END, "
        p.append(jo["date"])
    order += "d.CreatedAt"
    cur.execute(q + order, tuple(p))
    row = cur.fetchone()
    if row and int(row[0]) in claimed:
        return None                       # already absorbed a note this run
    return row


def service_text(jo):
    bits = [b for b in (jo["diagnosis"], jo["fix"], jo["notes"]) if b and b.strip()]
    txt = " — ".join(b.strip() for b in bits) or "Service requested"
    return f"[{jo['display'] or ('JOB-' + str(jo['jid']))}] {txt}"


def snapshot(cur, jid):
    cur.execute("SELECT * FROM WO_JobOrders WHERE JobOrderID=%s", (jid,))
    row = cur.fetchone()
    if not row:
        return None
    names = [d[0] for d in cur.description]
    return {n: (v.isoformat() if hasattr(v, "isoformat") else v) for n, v in zip(names, row)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: preview only)")
    ap.add_argument("--limit", type=int, default=0, help="process at most N MIGRATABLE job orders")
    args = ap.parse_args()

    c = connect()
    cur = c.cursor()
    try:
        if not (has_col(cur, "WO_DeliveryOrders", "NeedsService")
                and has_col(cur, "WO_DeliveryOrders", "ServiceNote")):
            print("ABORT: WO_DeliveryOrders.NeedsService / ServiceNote do not exist.")
            print("       Deploy the app first so init_workorders_db adds them, then re-run.")
            return 2
        dated = has_col(cur, "WO_JobOrders", "ScheduledDate")

        rows = candidates(cur, dated)
        print("=" * 78)
        print(f"{'APPLY' if args.apply else 'PREVIEW'} — fold open manual job orders into delivery orders")
        print(f"scheduling columns present : {dated}")
        print(f"open non-complaint JOs     : {len(rows)}")
        print("=" * 78)

        merged = created = skipped = failed = 0
        claimed = set()          # delivery orders that already took a note this run
        done = 0

        for jo in rows:
            why = skip_reason(jo)
            if why:
                print(f"  SKIP    {str(jo['display'] or jo['jid']):<20} "
                      f"{str(jo['name'] or jo['code']):<28} {why}")
                skipped += 1
                continue
            if args.limit and done >= args.limit:
                print(f"  (limit {args.limit} reached — {len(rows) - rows.index(jo)} not examined)")
                break
            done += 1

            tgt = find_target(cur, jo, dated, claimed)
            print(f"  {'MERGE ' if tgt else 'CREATE'}  {str(jo['display'] or jo['jid']):<20} "
                  f"{str(jo['name'] or jo['code']):<28} "
                  + (f"into DO {tgt[0]}" if tgt else f"new DO for {jo['assigned'] or '(unassigned)'}"))
            print(f"          note -> {service_text(jo)[:96]}")

            if not args.apply:
                if tgt:
                    merged += 1; claimed.add(int(tgt[0]))
                else:
                    created += 1
                continue

            did = None
            try:
                if tgt:
                    did, existing = int(tgt[0]), tgt[1]
                    note = (service_text(jo) if not (existing or "").strip()
                            else existing.rstrip() + "\n" + service_text(jo))
                    cur.execute("UPDATE WO_DeliveryOrders SET NeedsService = 1, ServiceNote = %s "
                                "WHERE DeliveryOrderID = %s", (note, did))
                    merged += 1
                else:
                    cols = ["MachineName", "MachineCode", "AssignedTo", "Priority",
                            "NeedsService", "ServiceNote", "CreatedBy"]
                    vals = [jo["name"] or jo["code"], jo["code"], jo["assigned"],
                            (jo["priority"] or "normal"), 1, service_text(jo),
                            jo["created_by"] or "migration"]
                    if dated and has_col(cur, "WO_DeliveryOrders", "ScheduledDate"):
                        cols += ["ScheduledDate", "RouteSeq"]; vals += [jo["date"], jo["seq"]]
                    cur.execute(
                        f"INSERT INTO WO_DeliveryOrders ({', '.join(cols)}) "
                        f"OUTPUT INSERTED.DeliveryOrderID VALUES ({', '.join(['%s'] * len(cols))})",
                        tuple(vals))
                    did = int(cur.fetchone()[0])
                    created += 1
                claimed.add(did)

                snap = snapshot(cur, jo["jid"])
                cur.execute(
                    "INSERT INTO WO_DeletedLog (EntityType, EntityKey, Summary, Snapshot, Reversible, DeletedBy) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    ("joborder", str(jo["jid"]),
                     f"Migrated into delivery order {did} on {datetime.utcnow().date().isoformat()}",
                     json.dumps(snap, default=str), 0, "migration"))
                # Screened above: no images, no tasks, no visit links, so the row
                # itself and its activity trail are all that need removing.
                cur.execute("DELETE FROM WO_Activity WHERE ParentType='joborder' AND ParentID=%s", (jo["jid"],))
                cur.execute("DELETE FROM WO_JobOrders WHERE JobOrderID=%s", (jo["jid"],))
                cur.execute(
                    "INSERT INTO WO_Activity (ParentType, ParentID, Action, Detail, ByUser) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    ("deliveryorder", did, "service_merged",
                     f"Absorbed {jo['display'] or jo['jid']}", "migration"))
                c.commit()
            except Exception as e:
                c.rollback()
                failed += 1
                if did is not None: claimed.discard(did)
                print(f"          FAILED: {e}")

        print("-" * 78)
        verb = "would be" if not args.apply else ""
        print(f"  merged into an existing stop : {merged} {verb}".rstrip())
        print(f"  new delivery orders created  : {created} {verb}".rstrip())
        print(f"  skipped, left as job orders  : {skipped}")
        if args.apply:
            print(f"  failed                       : {failed}")
        else:
            print("\nNothing was written. Re-run with --apply to perform the migration.")
        print("Each migrated job order's full row is copied into WO_DeletedLog first —")
        print("a snapshot for manual reconstruction, not a one-click restore.")
        return 1 if failed else 0
    finally:
        try:
            c.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
