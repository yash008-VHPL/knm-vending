#!/usr/bin/env python3
"""READ-ONLY diagnostic. Puts the Auresys API and the database side by side for
a few terminals over a few days, and shows exactly what the vend counter query
returns and why.

Every statement is a SELECT. Nothing is written, no Teams post, no heartbeat.

    python3 diag_vend_counter.py SGKN_M0005,SGKN_M0028 7

Env: AURESYS_USER, AURESYS_PASSWORD, DB_SERVER, DB_NAME, NETS_DB_USER,
     NETS_DB_PASSWORD
"""
import datetime as dt
import decimal
import os
import sys
from collections import Counter, defaultdict

import requests
import auresys_pull as ap
import nets_mapping

ZERO = decimal.Decimal("0.00")


def hdr(t):
    print("\n" + "=" * 72 + "\n" + t + "\n" + "=" * 72)


def main():
    terms = (sys.argv[1] if len(sys.argv) > 1 else "SGKN_M0005,SGKN_M0028").split(",")
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 7
    today = dt.datetime.now(ap.SGT).date()
    window = [today - dt.timedelta(days=i) for i in range(days - 1, -1, -1)]
    codes = {}
    for t in terms:
        c, name = nets_mapping.resolve(t)
        codes[t] = c
        print("%s -> Machine_Code %r  (%s)" % (t, c, name))
    print("window %s .. %s   (today is %s SGT)" % (window[0], window[-1], today))

    # ------------------------------------------------------------------ API
    hdr("A. WHAT AURESYS SAYS (the portal's own numbers)")
    api = defaultdict(Counter)
    s = requests.Session()
    s.headers["User-Agent"] = "knm-vend-diag/1.0"
    s.headers["Accept-Language"] = "en-US,en;q=0.9"
    s.cookies.set("locale", "en", domain="autwp.auresys.solutions")
    ap.login(s, os.environ["AURESYS_USER"], os.environ["AURESYS_PASSWORD"])
    token, roster = ap.open_report_page(s)
    for t in terms:
        print("  %s on portal roster: %s" % (t, t in roster))
    for day in window:
        raw, expected, _ = ap.fetch_day(s, token, roster, day)
        for r in raw:
            v = str(r.get("vmsID") or "").strip()
            if v in terms:
                api[(v, day)][str(r.get("dispenseStatus")).strip()] += 1
    for t in terms:
        for day in window:
            c = api[(t, day)]
            print("  API  %s %s  total=%-5d %s"
                  % (t, day, sum(c.values()), dict(c) or "-"))

    # ------------------------------------------------------------------- DB
    hdr("B. WHAT THE DATABASE HAS")
    conn = ap.connect()
    cur = conn.cursor()
    marks = ",".join(["%s"] * len(terms))

    cur.execute(
        "SELECT NETS_Terminal_No, Txn_Date, Txn_Status_Code, COUNT(*), "
        "       MIN(Txn_DateTime), MAX(Txn_DateTime), "
        "       SUM(CASE WHEN Machine_Code IS NULL THEN 1 ELSE 0 END) "
        "FROM dbo.NETS_Transaction "
        "WHERE NETS_Terminal_No IN (" + marks + ") AND Txn_Date >= %s "
        "GROUP BY NETS_Terminal_No, Txn_Date, Txn_Status_Code "
        "ORDER BY NETS_Terminal_No, Txn_Date, Txn_Status_Code",
        tuple(terms) + (window[0],))
    rows = cur.fetchall()
    if not rows:
        print("  NO ROWS AT ALL for these terminals in this window.")
    for term, d, st, n, lo, hi, nullcode in rows:
        print("  DB   %s %s  status=%s count=%-5d  %s .. %s  null_machine_code=%d"
              % (term, d, st, n, lo, hi, nullcode))

    hdr("C. LOAD AUDIT - what the loader decided, per terminal-day")
    cur.execute(
        "SELECT TOP 200 NETS_Terminal_No, Txn_Date, Rows_Before, Rows_Staged, "
        "       Rows_Inserted, Load_Action, Note "
        "FROM dbo.NETS_Load_Audit "
        "WHERE NETS_Terminal_No IN (" + marks + ") AND Txn_Date >= %s "
        "ORDER BY Txn_Date DESC, NETS_Terminal_No",
        tuple(terms) + (window[0],))
    for r in cur.fetchall():
        print("  AUDIT %s %s before=%-5s staged=%-5s inserted=%-5s %-16s %s"
              % (r[0], r[1], r[2], r[3], r[4], r[5], r[6] or ""))

    hdr("D. PULL RUNS - did the scheduled pulls actually run?")
    cur.execute("SELECT TOP 12 Run_Seq, Window_From, Window_To, Status, "
                "       Finished_At_UTC, Rows_Parsed "
                "FROM dbo.NETS_Pull_Run ORDER BY Run_Seq DESC")
    for r in cur.fetchall():
        print("  RUN %-5s %s..%s %-8s finished=%s rows=%s"
              % (r[0], r[1], r[2], r[3], r[4], r[5]))

    hdr("E. THE VEND COUNTER, STEP BY STEP")
    live = [c for c in codes.values() if c]
    if not live:
        print("  none of these terminals resolve to a Machine_Code - stop here.")
        return
    cmarks = ",".join(["%s"] * len(live))

    cur.execute("SELECT COUNT(*) FROM dbo.NETS_FlagCard WHERE IsActive = 1")
    print("  active flag cards: %d" % cur.fetchone()[0])

    cur.execute(
        "SELECT t.Machine_Code, MAX(t.Txn_DateTime), COUNT(*) "
        "FROM dbo.NETS_Transaction t "
        "WHERE t.Card_Hash IN (SELECT Card_Hash FROM dbo.NETS_FlagCard WHERE IsActive=1) "
        "  AND t.Machine_Code IN (" + cmarks + ") "
        "GROUP BY t.Machine_Code", tuple(live))
    flags = {str(r[0]): (r[1], r[2]) for r in cur.fetchall()}
    for c in live:
        f = flags.get(str(c))
        print("  last flag tap  machine %s: %s (%d taps on record)"
              % (c, f[0] if f else "NONE - machine would not appear in the counter",
                 f[1] if f else 0))

    for c in live:
        f = flags.get(str(c))
        if not f:
            continue
        cur.execute(
            "SELECT COUNT(*) FROM dbo.NETS_Transaction "
            "WHERE Machine_Code = %s AND Txn_Status_Code = 0 AND Txn_DateTime > %s",
            (c, f[0]))
        since = int(cur.fetchone()[0])
        cur.execute(
            "SELECT COUNT(*) FROM dbo.NETS_Transaction "
            "WHERE Machine_Code = %s AND Txn_Status_Code = 0 AND Txn_Date >= %s",
            (c, window[0]))
        total = int(cur.fetchone()[0])
        print("  machine %s: counter shows %d  (dispenses in window: %d, "
              "flag tap at %s)" % (c, since, total, f[0]))

    hdr("F. NEWEST ROW IN THE WHOLE TABLE")
    cur.execute("SELECT MAX(Txn_Date), MAX(Txn_DateTime) FROM dbo.NETS_Transaction")
    print("  newest Txn_Date=%s  newest Txn_DateTime=%s" % cur.fetchone())
    conn.close()


if __name__ == "__main__":
    try:
        main()
    except ap.Abort as e:
        sys.exit("ABORT [%s] %s" % (e.status, e.msg))
