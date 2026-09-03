#!/usr/bin/env python3
"""
auresys_pull.py - daily Auresys VMS -> Azure SQL transaction pull.

Redundant sales source for KNM vending. The machines' own telemetry (the email
system) drops events when their connection is poor; Auresys receives the same
transactions from the payment terminal side, so the two disagree only when
something is actually wrong.

No browser. The portal's Table button drives a JSON API and this calls it
directly:
    POST /api/login                 {account, password}
    GET  /vms/report/transactions   -> csrfToken + the machine roster
    POST /api/report/getTransaction DataTables server-side, paginated

Integrity, all in-band (nothing is scraped off a rendered page):
  * recordsFiltered from the API must equal the rows actually collected
  * totalAmount from the API must equal their sum, to the cent
  * the endpoint is serverSide, so pagination is mandatory - fetching one page
    and stopping would silently truncate and still look plausible

Load: DELETE-then-INSERT per (terminal, date), with a NO_CHANGE short-circuit
so a later mapping correction cannot retroactively re-stamp history, and a
shrink guard so a partial pull cannot delete good data.

Usage:
    python3 auresys_pull.py --days 10          # rolling window, the daily job
    python3 auresys_pull.py --from 2026-07-01 --to 2026-07-31
    python3 auresys_pull.py --days 1 --probe   # one raw row per account, load nothing
    python3 auresys_pull.py --days 10 --dry-run
    python3 auresys_pull.py --roster            # log in to every account, print
                                                #   each roster, load nothing
Env:
    AURESYS_USER, AURESYS_PASSWORD, NETS_CARD_PEPPER      (the MAIN account)
    AURESYS_FRANCHISEES   comma list of account keys, e.g. AUVION,COFFEERUSH.
                          Each key K needs AURESYS_USER_K / AURESYS_PASSWORD_K
                          and an entry in nets_mapping.ACCOUNTS. One account
                          failing (login, MFA, reconciliation) is reported and
                          skipped; the other accounts still load. Rows carry
                          NETS_Transaction.Account_Key (NULL = MAIN, pre-2026-09).
    DB_SERVER, DB_NAME, NETS_DB_USER, NETS_DB_PASSWORD
    HEARTBEAT_URL   (optional) pinged on success
    TEAMS_WEBHOOK_URL (optional) posted to on anything needing a human
"""
import argparse
import datetime as dt
import decimal
import hashlib
import hmac
import json
import os
import re
import sys
import time
from collections import Counter

import requests

try:
    import nets_mapping
except ImportError:
    sys.exit("nets_mapping.py must sit next to this script.")

BASE = "https://autwp.auresys.solutions"
REPORT_PAGE = BASE + "/vms/report/transactions"
PAGE_SIZE = 1000
PEPPER_VER = 1
ZERO = decimal.Decimal("0.00")
SGT = dt.timezone(dt.timedelta(hours=8))

# Auresys status text -> stored code. The API localises this string by session
# locale and defaults to Chinese for a non-browser client, so both are mapped.
# Anything unrecognised aborts rather than being silently bucketed, because a
# mis-bucketed status corrupts the dispense count.
STATUS_SUCCESS, STATUS_STLM, STATUS_FAIL = 0, 1, 2
STATUS_MAP = {
    "success": STATUS_SUCCESS, "\u6210\u529f": STATUS_SUCCESS,
    "stlm": STATUS_STLM, "\u7d50\u7b97": STATUS_STLM, "\u7ed3\u7b97": STATUS_STLM,
    # 2026-08-26 10:33:31, SGKN_M0019. Probed before mapping (see
    # probe_manual_stlm.py): amount 0, isSettled 0, skuNo null, paymentType
    # "Unknown" - the same shape as the 68 plain "STLM" rows that day, which
    # also summed to 0.00. Not a dispense, whatever else it is: cardNo was not
    # inspected, so "settlement marker" vs "manual card tap" is not settled -
    # both belong in this bucket, and the dispense count excludes it either way.
    # Exact key, not a substring test, so "STLM Reversal" still aborts. Note
    # "Fail STLM" does NOT reach this map - FAIL_PREFIXES catches it first.
    "manual stlm": STATUS_STLM,
}
FAIL_PREFIXES = ("fail", "\u5931\u6557", "\u5931\u8d25")


class Abort(Exception):
    def __init__(self, status, msg):
        self.status, self.msg = status, msg
        super().__init__(msg)


def log(m):
    print(m, flush=True)


def status_code(raw):
    """Returns the stored code, or None if the value is not recognised. The
    caller collects every unknown value and reports them together - failing one
    at a time turns a five-minute fix into five round trips."""
    s = (raw or "").strip()
    key = s.lower()
    if key in STATUS_MAP:
        return STATUS_MAP[key]
    if key.startswith(FAIL_PREFIXES):
        return STATUS_FAIL
    return None


def card_hash(raw, pepper):
    raw = (raw or "").strip()
    if not raw or set(raw) <= {"*"}:
        return None
    # digest(), not hexdigest() - the column is BINARY(32)
    return hmac.new(pepper.encode(), raw.encode(), hashlib.sha256).digest()


# --------------------------------------------------------------------------- #
# portal
# --------------------------------------------------------------------------- #
def login(session, user, password):
    r = session.post(BASE + "/api/login", data={"account": user, "password": password},
                     timeout=60)
    if r.status_code != 200:
        raise Abort("ABORTED_LOGIN", "login HTTP %d: %s" % (r.status_code, r.text[:200]))
    try:
        body = r.json()
    except ValueError:
        raise Abort("ABORTED_LOGIN", "login did not return JSON: %s" % r.text[:200])
    if body.get("mfa_setup_required") or body.get("mfa_required"):
        raise Abort("ABORTED_LOGIN",
                    "Auresys is now demanding MFA for this account. Unattended login "
                    "cannot proceed until the TOTP secret is provisioned to this job.")
    return body


def open_report_page(session):
    """Returns (csrf_token, [terminal_ids]). Also the session check: an expired
    or rejected session serves the login page, which has neither."""
    r = session.get(REPORT_PAGE, timeout=60)
    if r.status_code != 200:
        raise Abort("ABORTED_LOGIN", "report page HTTP %d" % r.status_code)
    html = r.text
    m = re.search(r'id="csrfToken"\s+value="([^"]+)"', html)
    if not m:
        raise Abort("ABORTED_LOGIN",
                    "no csrfToken on the report page - almost certainly not logged in")
    token = m.group(1)

    mm = re.search(r"let machines\s*=\s*JSON\.parse\(`(.*?)`\)", html, re.S)
    if not mm:
        raise Abort("ABORTED_PARSE", "could not find the machine roster on the page")
    roster = json.loads(mm.group(1))
    ids = [x["vmsID"] for x in roster if x.get("vmsID")]
    if not ids:
        raise Abort("ABORTED_PARSE", "machine roster parsed but empty")
    return token, ids


COLUMNS = ["vmsID", "outletNo", "outletName", "time", "dispenseStatus", "skuNo",
           "productName", "colNo", "paymentType", "amount", "cardNo", "tid", "isSettled"]


def fetch_day(session, token, terminals, day):
    """One calendar day, all terminals, every page. Returns (rows, expected_count,
    expected_amount)."""
    sdate = "%s 00:00" % day.isoformat()
    edate = "%s 23:59" % day.isoformat()
    rows, start, expected, expected_amt, draw = [], 0, None, None, 1

    while True:
        form = {
            "sdate": sdate, "edate": edate,
            "paymentType": "ALL", "status": "ALL",
            "second": "false",
            "draw": draw, "start": start, "length": PAGE_SIZE,
            "order[0][column]": 3, "order[0][dir]": "asc",
        }
        for t in terminals:
            form.setdefault("machineID[]", []).append(t)
            form.setdefault("mids[]", []).append(t)
        for i, c in enumerate(COLUMNS):
            form["columns[%d][data]" % i] = c
            form["columns[%d][searchable]" % i] = "false"
            form["columns[%d][orderable]" % i] = "false"

        r = session.post(BASE + "/api/report/getTransaction", data=form,
                         headers={"X-CSRF-Token": token,
                                  "X-Requested-With": "XMLHttpRequest"}, timeout=180)
        if r.status_code != 200:
            raise Abort("FAILED", "getTransaction HTTP %d: %s" % (r.status_code, r.text[:200]))
        try:
            body = r.json()
        except ValueError:
            raise Abort("ABORTED_LOGIN",
                        "getTransaction returned non-JSON (session probably expired)")

        if expected is None:
            expected = int(body.get("recordsFiltered", 0))
            if body.get("totalAmount") not in (None, ""):
                expected_amt = decimal.Decimal(str(body["totalAmount"])).quantize(ZERO)
        page = body.get("data") or []
        rows.extend(page)
        start += len(page)
        draw += 1
        if not page or start >= expected:
            break
        if draw > 200:
            raise Abort("FAILED", "pagination did not terminate")

    return rows, expected, expected_amt


def parse_rows(raw_rows, pepper, day, account=nets_mapping.MAIN_ACCOUNT):
    """Returns (good_rows, quarantined).

    A row this function cannot classify is set aside, not raised on. Aborting
    the whole run over one row is what took the feed down on 2026-08-26:
    parse_rows runs before load(), so a single unmapped status string stopped
    all ten days of the rolling window from loading, on every run, until a
    human patched STATUS_MAP.

    Quarantined rows never enter NETS_Transaction, so no dispense count, amount
    or flag-card query can see them. They are listed in NETS_Unmapped_Row and on
    the dashboard instead, and the day reloads clean once the cause is fixed.

    Still fatal, because these mean the pull itself is wrong rather than one row
    being odd: a missing field (the response shape changed) and a row dated
    outside the day queried (the date filter is not doing what we think).
    """
    out, bad = [], []

    def quarantine(r, reason, ts=None):
        # Best-effort amount even on a quarantined row: the day's cent-exact
        # reconciliation adds these back in, so a row set aside for an unknown
        # STATUS must still contribute its amount. Only a genuinely unparseable
        # amount stays None, and that is what makes the caller refuse to load
        # the day rather than load it with its only integrity check skipped.
        try:
            amt = decimal.Decimal(str(r.get("amount"))).quantize(ZERO)
            # DECIMAL(12,2) on NETS_Unmapped_Row. A garbage-but-parseable value
            # like 1e20 quantizes fine and then overflows on INSERT - after the
            # transaction data has committed, with no Abort and so no alert.
            # This table receives exactly the population most likely to contain
            # such a value.
            if not amt.is_finite() or abs(amt) >= decimal.Decimal("10000000000"):
                amt = None
        except (decimal.InvalidOperation, ValueError, TypeError):
            amt = None
        bad.append({
            # Truncated to the NETS_Unmapped_Row column widths. Without this a
            # long outlet name throws on INSERT *after* the transaction data has
            # already been committed for the day.
            "terminal": str(r.get("vmsID") or "").strip()[:50],
            "outlet": ((r.get("outletName") or "").strip() or None),
            "date": day,
            "ts": ts,
            "raw_time": str(r.get("time"))[:64] if r.get("time") is not None else None,
            "raw_status": str(r.get("dispenseStatus"))[:128],
            "raw_scheme": (str(r.get("paymentType"))[:64]
                           if r.get("paymentType") is not None else None),
            "raw_amount": (str(r.get("amount"))[:64]
                           if r.get("amount") is not None else None),
            "amount": amt,
            "reason": reason,
            "account": account,
        })
        if bad[-1]["outlet"]:
            bad[-1]["outlet"] = bad[-1]["outlet"][:200]

    for r in raw_rows:
        for k in ("vmsID", "time", "dispenseStatus", "amount"):
            if k not in r:
                raise Abort("ABORTED_PARSE",
                            "API row is missing %r - the response shape changed. "
                            "Run with --probe. Row keys: %s" % (k, sorted(r)))
        code = status_code(r["dispenseStatus"])
        if code is None:
            quarantine(r, "UNMAPPED_STATUS")
            continue
        try:
            ts = dt.datetime.strptime(str(r["time"]).strip(), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            quarantine(r, "BAD_TIME")
            continue
        try:
            amt = decimal.Decimal(str(r["amount"])).quantize(ZERO)
        except (decimal.InvalidOperation, ValueError):
            quarantine(r, "BAD_AMOUNT", ts=ts)
            continue
        if not amt.is_finite():
            quarantine(r, "BAD_AMOUNT", ts=ts)
            continue
        if ts.date() != day:
            raise Abort("ABORTED_PARSE",
                        "row dated %s came back for the %s query - the date filter is "
                        "not doing what we think" % (ts.date(), day))
        out.append({
            "terminal": str(r["vmsID"]).strip(),
            "outlet": (r.get("outletName") or "").strip(),
            "ts": ts,
            "date": ts.date(),
            "status": code,
            "scheme": (r.get("paymentType") or "").strip()[:20],
            "amount": amt,
            "card_hash": card_hash(r.get("cardNo"), pepper),
            "account": account,
        })
    return out, bad


# --------------------------------------------------------------------------- #
# database
# --------------------------------------------------------------------------- #
# One scan for the whole window instead of a COUNT per machine-day. At 73
# terminals x 10 days that is 1 round trip instead of 730.
SQL_SCAN = ("SELECT NETS_Terminal_No, Txn_Date, COUNT(*), ISNULL(SUM(Amount),0) "
            "FROM dbo.NETS_Transaction WHERE Txn_Date BETWEEN %s AND %s "
            "GROUP BY NETS_Terminal_No, Txn_Date")
# pymssql has no bulk insert and its executemany loops singleton INSERTs, so
# rows are batched into multi-row VALUES. SQL Server caps a statement at 2100
# parameters; at 11 columns that is 190 rows, so 170 leaves headroom.
INSERT_CHUNK = 170
AUDIT_CHUNK = 150
SQL_INSERT = ("INSERT INTO dbo.NETS_Transaction "
              "(NETS_Terminal_No, Machine_Code, Location_Name, Txn_DateTime, "
              " Txn_Status_Code, Scheme, Amount, Card_Hash, Card_Hash_Ver, Load_Batch_Ref, "
              " Account_Key) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)")
SQL_AUDIT = ("INSERT INTO dbo.NETS_Load_Audit "
             "(Load_Batch_Id, NETS_Terminal_No, Txn_Date, Rows_Before, Rows_Staged, "
             " Rows_Deleted, Rows_Inserted, Sum_Amount_Before, Sum_Amount_After, "
             " Load_Action, Note) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)")
SQL_SEEN = """MERGE dbo.NETS_Terminal_Outlet_Seen WITH (HOLDLOCK) AS t
              USING (SELECT %s AS Term, %s AS Loc, %s AS Lo, %s AS Hi) AS s
                ON t.NETS_Terminal_No = s.Term AND t.Location_Name = s.Loc
              WHEN MATCHED THEN UPDATE SET
                   t.First_Seen = CASE WHEN s.Lo < t.First_Seen THEN s.Lo ELSE t.First_Seen END,
                   t.Last_Seen  = CASE WHEN s.Hi > t.Last_Seen  THEN s.Hi ELSE t.Last_Seen  END
              WHEN NOT MATCHED THEN
                   INSERT (NETS_Terminal_No, Location_Name, First_Seen, Last_Seen, Txn_Count)
                   VALUES (s.Term, s.Loc, s.Lo, s.Hi, 0);"""
# Quarantine list. Each run clears and rewrites only the dates it queried, so
# the rows for the current window are always a live picture. Rows for a day that
# has aged OUT of the rolling window are never revisited and therefore never
# removed - deliberately: that day is no longer fetched, so its record here is
# the only remaining evidence that something was set aside. Clear an old range
# by re-running over it (--from/--to). See migration_2026-08-28_unmapped_rows.sql.
SQL_UNMAPPED_INSERT = (
    "INSERT INTO dbo.NETS_Unmapped_Row "
    "(Run_Seq, NETS_Terminal_No, Machine_Code, Location_Name, Txn_Date, "
    " Txn_DateTime, Raw_Time, Raw_Status, Raw_Payment_Type, Raw_Amount, "
    " Amount, Reason, Account_Key) VALUES ")
# 13 columns x 150 = 1950 parameters, under the 2100 cap.
UNMAPPED_CHUNK = 150
SQL_SEEN_RECOUNT = """UPDATE s SET Txn_Count = x.c
                      FROM dbo.NETS_Terminal_Outlet_Seen AS s
                      CROSS APPLY (SELECT COUNT(*) AS c FROM dbo.NETS_Transaction AS t
                                   WHERE t.NETS_Terminal_No = s.NETS_Terminal_No
                                     AND t.Location_Name    = s.Location_Name) AS x;"""


def connect():
    import pymssql
    for k in ("DB_SERVER", "DB_NAME", "NETS_DB_USER", "NETS_DB_PASSWORD"):
        if not os.environ.get(k):
            sys.exit("%s is not set." % k)
    conn = pymssql.connect(server=os.environ["DB_SERVER"], database=os.environ["DB_NAME"],
                           user=os.environ["NETS_DB_USER"],
                           password=os.environ["NETS_DB_PASSWORD"],
                           autocommit=True, timeout=180, login_timeout=60)
    cur = conn.cursor()
    # The PERSISTED computed column and the filtered index both require these.
    # NOCOUNT must be OFF or cursor.rowcount comes back as -1 and the audit lies.
    cur.execute("SET ARITHABORT ON; SET NUMERIC_ROUNDABORT OFF; "
                "SET XACT_ABORT ON; SET NOCOUNT OFF;")
    return conn


def load(conn, rows, window, args, last_full=None, bad=None,
         full_window=None, degraded=False, roster=None, excluded=None,
         degraded_text=None, failed_keys=None):
    """last_full = the newest day that is certainly complete, i.e. D-1.

    That day is always rewritten rather than short-circuited by NO_CHANGE, so a
    day first written partially is guaranteed to be replaced by a complete one.
    Independent of --include-today - see the comment on the NO_CHANGE branch."""
    bad = bad or []
    # (terminal, date) pairs this run must not touch: the account that owns the
    # terminal failed, or that account's day failed its reconciliation. They
    # are audited and skipped BEFORE the shrink / vanished logic can see them,
    # because with no staged rows they would otherwise look like a terminal
    # that stopped trading and be purged.
    excluded = excluded or set()
    # What was queried, vs what is loadable. They differ when a day failed its
    # reconciliation and was skipped.
    full_window = full_window or window

    cur = conn.cursor()

    # Account_Key column check, same reasoning as the NETS_Unmapped_Row check
    # below: a missing column raises a bare pymssql error inside the per-date
    # transaction, after the run row is written and with no Abort.
    for tbl in ("NETS_Transaction", "NETS_Unmapped_Row"):
        cur.execute("SELECT COL_LENGTH('dbo.%s', 'Account_Key')" % tbl)
        if cur.fetchone()[0] is None:
            raise Abort("FAILED",
                        "dbo.%s.Account_Key does not exist - run "
                        "migration_2026-09-03_franchisee.sql BLOCK 1 before this "
                        "version of auresys_pull.py. Nothing has been written." % tbl)

    # Checked BEFORE anything is written. The quarantine write is unconditional,
    # so a missing table would otherwise raise a bare pymssql error partway
    # through - which is not an Abort, so main()'s handler never fires: no Teams
    # alert, no heartbeat, and the transaction data already committed. Apply
    # migration_2026-08-28_unmapped_rows.sql before deploying this loader.
    cur.execute("SELECT OBJECT_ID('dbo.NETS_Unmapped_Row', 'U')")
    if cur.fetchone()[0] is None:
        raise Abort("FAILED",
                    "dbo.NETS_Unmapped_Row does not exist - run "
                    "migration_2026-08-28_unmapped_rows.sql before this version "
                    "of auresys_pull.py. Nothing has been written.")

    # Machine-days holding a quarantined row. Used only to keep them off the
    # PURGED_VANISHED path below; NO_CHANGE needs no exception, because once a
    # status is mapped the staged count rises and NO_CHANGE cannot fire anyway.
    quarantined_days = {(b["terminal"], b["date"]) for b in bad}

    cur.execute("INSERT INTO dbo.NETS_Pull_Run "
                "(Run_Id, Window_From, Window_To, Source_File, Csv_Line_Count, "
                " Rows_Parsed, Header_Signature, Status) "
                "OUTPUT INSERTED.Run_Id, INSERTED.Run_Seq "
                "VALUES (NEWID(), %s, %s, 'api:getTransaction', %s, %s, %s, 'RUNNING')",
                (full_window[0], full_window[-1], len(rows), len(rows),
                 "|".join(COLUMNS)))
    # NETS_Load_Audit still keys on the GUID; NETS_Transaction uses the compact
    # Run_Seq. Both come from the same row, so carry both.
    run_id, run_seq = cur.fetchone()

    try:
        # ---- one scan of the window, not one query per partition ----
        cur.execute(SQL_SCAN, (window[0], window[-1]))
        stored = {}
        for term, d, cnt, amt in cur.fetchall():
            if hasattr(d, "date"):
                d = d.date()
            stored[(term, d)] = (int(cnt), decimal.Decimal(str(amt)).quantize(ZERO))

        parts = {}
        for r in rows:
            parts.setdefault((r["terminal"], r["date"]), []).append(r)
        # delete scope = the full roster x every date in the window, not just
        # what came back. A terminal that reports nothing must still be cleared.
        # roster = every terminal an account that COMPLETED this run can see,
        # plus the mapping. Terminals belonging to a failed account are left
        # out by the caller, so they are never seeded and never purged.
        for t in (roster if roster is not None else nets_mapping.known_terminals()):
            for d in window:
                parts.setdefault((t, d), [])

        today = dt.datetime.now(SGT).date()
        alive = {r["terminal"] for r in rows}
        counts, unmapped, audits = Counter(), set(), []
        to_delete, to_insert = {}, {}     # keyed by date

        for (term, date), prows in sorted(parts.items()):
            before, sum_before = stored.get((term, date), (0, ZERO))
            staged = len(prows)
            sum_staged = sum((r["amount"] for r in prows), ZERO)

            if (term, date) in excluded:
                # Load_Action reuses SKIPPED_SHRINK: NETS_Load_Audit's DDL is
                # not in this repo and may carry a CHECK on that column, so a
                # new value could be rejected after the data committed. The
                # note is what distinguishes it. No audit row when nothing was
                # stored - one per roster terminal per date is pure noise.
                if before:
                    audits.append((run_id, term, date, before, staged, 0, 0,
                                   sum_before, sum_before, "SKIPPED_SHRINK",
                                   "EXCLUDED: owning account failed or its day "
                                   "failed reconciliation - not touched"))
                counts["SKIPPED_EXCLUDED"] += 1
                continue
            if date > today or (date == today and not args.include_today):
                # Today is incomplete by definition and the D-1 reconciliation
                # rests on that, so it is excluded unless asked for.
                #
                # --include-today is the deliberate exception, and BOTH
                # scheduled runs carry it:
                #   06:00 SGT - "today" is 00:00-06:00, which is precisely where
                #               a night shift's work lands. Without this, a top-up
                #               finished at 01:00 stayed invisible until 17:00.
                #   18:00 SGT - "today" is the day so far, for the day shift.
                # Each pass replaces the last (staged > before -> LOADED), and
                # D-1 is force-rewritten complete by the last_full rule below.
                # Tomorrow is never loaded under any flag.
                counts["SKIPPED_TODAY"] += 1
                continue
            if date == today and staged < before:
                # A later partial pull of today must NEVER shrink an earlier one,
                # and this guard is deliberately NOT overridable by --force.
                #
                # --force exists to override the shrink guard on a HISTORICAL
                # day, where the operator can see the full day and judge it. On
                # today it would fall through to the PURGED_VANISHED branch
                # below, which appends to to_delete and NOT to to_insert: the
                # partial rows already loaded at 17:00 would be deleted and
                # nothing put back. Before --include-today existed, a forced run
                # against today was inert (SKIPPED_TODAY caught it first), so
                # this flag is what would have turned a harmless command into a
                # destructive one.
                if (term, date) in quarantined_days:
                    note = ("staged %d < stored %d because row(s) were quarantined "
                            "- see dbo.NETS_Unmapped_Row, not a partial feed"
                            % (staged, before))
                elif term not in alive:
                    note = "terminal absent from the whole pull - feed may be broken"
                else:
                    note = ("partial reload of today staged %d < stored %d"
                            % (staged, before))
                audits.append((run_id, term, date, before, staged, 0, 0,
                               sum_before, sum_before, "SKIPPED_SHRINK", note))
                log("  SHRINK  %s %s stored=%d staged=%d SKIPPED (today)"
                    % (term, date, before, staged))
                counts["SKIPPED_SHRINK"] += 1
                continue
            if staged == before == 0:
                continue
            if (staged == before and sum_staged == sum_before and staged > 0
                    and date != last_full):
                # identical content already stored: touch nothing. This is what
                # stops a later mapping fix re-stamping historical rows.
                #
                # ONE EXCEPTION, added with --include-today: D-1 - the newest
                # day that is certainly complete - is ALWAYS rewritten, never
                # short-circuited. It will have been written partially by the
                # runs that saw it as "today", and a partial day that is never
                # rewritten is worse than a missing one: vw_NETS_Daily_Count
                # stops excluding it the next day and serves undercounted
                # figures as the daily metric, which reads as telemetry >
                # Auresys - the direction that does NOT alert. Every genuine
                # paid-but-no-drink event on that day would be silently
                # suppressed.
                #
                # last_full is deliberately INDEPENDENT of --include-today. An
                # earlier cut tied the two together, so the moment both
                # scheduled runs carried the flag the rewrite stopped happening
                # at all - the exact failure it was added to prevent.
                #
                # It also un-freezes the mapping: rows stamped Machine_Code NULL
                # at 17:00 get re-resolved against nets_mapping the next morning
                # instead of staying unattributable forever.
                counts["NO_CHANGE"] += 1
                continue
            if staged == 0 and before > 0:
                if (term, date) in quarantined_days:
                    # Every row for this machine-day was unclassifiable. That is
                    # NOT the API dropping the data, so it must never reach
                    # PURGED_VANISHED, which deletes the stored rows and puts
                    # nothing back. A status-string rename would otherwise
                    # destroy good history one machine-day at a time.
                    action, note = ("SKIPPED_SHRINK",
                                    "all rows for this machine-day quarantined - "
                                    "see dbo.NETS_Unmapped_Row")
                elif term in alive:
                    action, note = "PURGED_VANISHED", "terminal reported other dates, none for this one"
                else:
                    action, note = "SKIPPED_SHRINK", "terminal absent from the whole pull - feed may be broken"
            elif staged < before and not args.force:
                if (term, date) in quarantined_days:
                    # NOT a partial pull, and --force here would delete the
                    # stored rows and reinsert only the good ones, permanently
                    # losing the quarantined transactions for this machine-day.
                    note = ("staged %d < stored %d because row(s) were "
                            "quarantined - fix the status mapping, do NOT --force"
                            % (staged, before))
                else:
                    note = ("staged %d < stored %d - possible partial pull"
                            % (staged, before))
                action = "SKIPPED_SHRINK"
            else:
                action = "FORCED" if staged < before else "LOADED"
                note = None

            if action == "SKIPPED_SHRINK":
                audits.append((run_id, term, date, before, staged, 0, 0,
                               sum_before, sum_before, action, note))
                log("  SHRINK  %s %s stored=%d staged=%d SKIPPED" % (term, date, before, staged))
                counts[action] += 1
                continue

            code, _ = nets_mapping.resolve(term)
            if code is None and prows:
                unmapped.add(term)
                note = ((note + "; ") if note else "") + "terminal not in nets_mapping"

            to_delete.setdefault(date, []).append(term)
            if prows:
                to_insert.setdefault(date, []).extend(
                    (term, code, r["outlet"], r["ts"], r["status"], r["scheme"],
                     r["amount"], r["card_hash"],
                     PEPPER_VER if r["card_hash"] else None, run_seq,
                     r.get("account") or nets_mapping.MAIN_ACCOUNT) for r in prows)
            # Rows_Deleted is the pre-scan count, which is exactly what the
            # delete removes - a batched delete cannot report it per partition.
            audits.append((run_id, term, date, before, staged, before,
                           len(prows), sum_before, sum_staged, action, note))
            counts[action] += 1

        # ---- write, one transaction per date ----
        for date in sorted(set(to_delete) | set(to_insert)):
            terms = to_delete.get(date, [])
            vals = to_insert.get(date, [])
            cur.execute("BEGIN TRANSACTION")
            try:
                for i in range(0, len(terms), 500):
                    chunk = terms[i:i + 500]
                    cur.execute(
                        "DELETE FROM dbo.NETS_Transaction WHERE Txn_Date = %s "
                        "AND NETS_Terminal_No IN (" + ",".join(["%s"] * len(chunk)) + ")",
                        tuple([date] + chunk))
                for i in range(0, len(vals), INSERT_CHUNK):
                    chunk = vals[i:i + INSERT_CHUNK]
                    cur.execute(
                        "INSERT INTO dbo.NETS_Transaction "
                        "(NETS_Terminal_No, Machine_Code, Location_Name, Txn_DateTime, "
                        " Txn_Status_Code, Scheme, Amount, Card_Hash, Card_Hash_Ver, "
                        " Load_Batch_Ref, Account_Key) VALUES "
                        + ",".join(["(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"] * len(chunk)),
                        tuple(v for row in chunk for v in row))
                cur.execute("COMMIT TRANSACTION")
            except Exception:
                cur.execute("IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION")
                raise
            log("  %s  %d terminals, %d rows" % (date, len(terms), len(vals)))

        for i in range(0, len(audits), AUDIT_CHUNK):
            chunk = audits[i:i + AUDIT_CHUNK]
            cur.execute(
                "INSERT INTO dbo.NETS_Load_Audit "
                "(Load_Batch_Id, NETS_Terminal_No, Txn_Date, Rows_Before, Rows_Staged, "
                " Rows_Deleted, Rows_Inserted, Sum_Amount_Before, Sum_Amount_After, "
                " Load_Action, Note) VALUES "
                + ",".join(["(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"] * len(chunk)),
                tuple(v for row in chunk for v in row))

        # ---- quarantine list ----
        # Own transaction, like the per-date writes above. Without it a failure
        # between the DELETE and the INSERTs leaves the list empty while the
        # transaction data is already committed, and the dashboard under-reports.
        cur.execute("BEGIN TRANSACTION")
        try:
            # Bounded to the window this run actually queried. An unbounded
            # delete would let `--days 1` wipe the list for the other nine days
            # it never looked at; an upper bound of the LOADABLE window would
            # leave a skipped last day's rows behind to duplicate every run.
            # An account this run could not pull keeps its quarantine rows:
            # nothing is about to rewrite them, and they are the only record
            # of what was set aside there.
            fk = sorted(failed_keys or [])
            keep = ((" AND ISNULL(Account_Key, %r) NOT IN (%s)"
                     % (nets_mapping.MAIN_ACCOUNT, ",".join(["%s"] * len(fk))))
                    if fk else "")
            cur.execute("DELETE FROM dbo.NETS_Unmapped_Row "
                        "WHERE Txn_Date BETWEEN %s AND %s" + keep,
                        tuple([full_window[0], full_window[-1]] + fk))
            vals = []
            for b in bad:
                code, _ = nets_mapping.resolve(b["terminal"])
                vals.append((run_seq, b["terminal"], code, b["outlet"], b["date"],
                             b["ts"], b["raw_time"], b["raw_status"], b["raw_scheme"],
                             b["raw_amount"], b["amount"], b["reason"],
                             b.get("account") or nets_mapping.MAIN_ACCOUNT))
            for i in range(0, len(vals), UNMAPPED_CHUNK):
                chunk = vals[i:i + UNMAPPED_CHUNK]
                cur.execute(
                    SQL_UNMAPPED_INSERT
                    + ",".join(["(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"] * len(chunk)),
                    tuple(v for row in chunk for v in row))
            cur.execute("COMMIT TRANSACTION")
        except Exception:
            cur.execute("IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION")
            raise
        if bad:
            counts["QUARANTINED"] = len(bad)

        seen = {}
        for r in rows:
            k = (r["terminal"], r["outlet"])
            v = seen.setdefault(k, [r["ts"], r["ts"]])
            v[0] = min(v[0], r["ts"])
            v[1] = max(v[1], r["ts"])
        for (term, outlet), (lo, hi) in seen.items():
            cur.execute(SQL_SEEN, (term, outlet, lo, hi))
        cur.execute(SQL_SEEN_RECOUNT)

        # DEGRADED, not SUCCESS: rows were set aside or a day was refused, so
        # the run did not load everything it fetched. Without this the database
        # is the one observer that says nothing went wrong.
        # Status stays SUCCESS. dbo.NETS_Pull_Run carries a CHECK constraint
        # (CK_NETS_Pull_Run_Status) enumerating RUNNING / SUCCESS / FAILED /
        # ABORTED_*, so writing 'DEGRADED' would be rejected - and the rejection
        # would land AFTER every transaction had committed, as a bare pymssql
        # error rather than an Abort: no Teams alert, no heartbeat, a traceback
        # on a run whose data actually loaded. Degradation is recorded in
        # Error_Text (the column the FAILED path already writes), and carried by
        # the Teams alert, the non-zero exit and dbo.NETS_Unmapped_Row.
        # Adding 'DEGRADED' to the constraint is a separate change.
        cur.execute("UPDATE dbo.NETS_Pull_Run SET Status='SUCCESS', "
                    "Error_Text=%s, Finished_At_UTC=SYSUTCDATETIME() "
                    "WHERE Run_Seq=%s",
                    ((degraded_text or
                      "DEGRADED: %d row(s) quarantined; see dbo.NETS_Unmapped_Row"
                      % len(bad))[:4000] if degraded else None, run_seq))
        return run_seq, counts, unmapped
    except Exception as e:
        try:
            cur.execute("IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION")
            cur.execute("UPDATE dbo.NETS_Pull_Run SET Status='FAILED', Error_Text=%s, "
                        "Finished_At_UTC=SYSUTCDATETIME() WHERE Run_Seq=%s",
                        (str(e)[:4000], run_seq))
        except Exception:
            pass
        raise


def notify(text):
    url = os.environ.get("TEAMS_WEBHOOK_URL")
    if not url:
        return
    try:
        requests.post(url, json={"text": text}, timeout=30)
    except Exception as e:
        log("teams notify failed: %s" % e)


ACCOUNT_KEY_RE = re.compile(r"^[A-Z0-9_]{1,16}$")


def read_accounts():
    """[(key, user, password)] - MAIN first, then AURESYS_FRANCHISEES in the
    order given. Missing credentials come back as empty strings and are
    reported per account by main(), not raised here: one franchisee's secret
    being absent must not stop the MAIN pull."""
    out = [(nets_mapping.MAIN_ACCOUNT,
            os.environ.get("AURESYS_USER", ""), os.environ.get("AURESYS_PASSWORD", ""))]
    raw = os.environ.get("AURESYS_FRANCHISEES", "")
    for k in [x.strip().upper() for x in raw.split(",") if x.strip()]:
        if not ACCOUNT_KEY_RE.match(k):
            raise Abort("FAILED", "AURESYS_FRANCHISEES key %r is not ^[A-Z0-9_]{1,16}$ "
                        "(Account_Key is NVARCHAR(16))." % k)
        if k not in nets_mapping.ACCOUNTS:
            raise Abort("FAILED", "AURESYS_FRANCHISEES key %r has no entry in "
                        "nets_mapping.ACCOUNTS - add its label there first." % k)
        if k == nets_mapping.MAIN_ACCOUNT:
            continue
        out.append((k, os.environ.get("AURESYS_USER_" + k, ""),
                    os.environ.get("AURESYS_PASSWORD_" + k, "")))
    return out


def synthetic_machine_code(terminal_id):
    """MachineLookup.MachineCode for a machine that has no KNM telemetry id.
    Two live queries join MachineLookup to [MasterData Table] WITHOUT a CAST
    (app.py ~1698, alpha_preview.py ~206), so the code MUST be numeric or the
    whole machine list query fails. 9 + the terminal number zero-padded to 8
    digits: 9 digits fits INT and cannot collide with the 8-digit telemetry
    codes. SGKN_M0080 -> 900000080."""
    m = re.search(r"(\d+)$", terminal_id or "")
    if not m:
        return None
    return "9" + m.group(1).zfill(8)


def print_roster(acct, terminals, session):
    """--roster output. Outlet names come from the same page the roster does,
    so nothing is invented: a terminal whose outlet is blank prints blank."""
    r = session.get(REPORT_PAGE, timeout=60)
    mm = re.search(r"let machines\s*=\s*JSON\.parse\(`(.*?)`\)", r.text, re.S)
    roster = json.loads(mm.group(1)) if mm else []
    # The roster embedded in the page is URL-encoded ("RWS%20Office") and an
    # unset outlet arrives as the string "null" (2026-09-03 run). Decode, and
    # treat "null" / blank as NO NAME rather than inventing one: the stub
    # falls back to the terminal id and is flagged for a human to replace.
    from urllib.parse import unquote
    names = {}
    for x in roster:
        raw = x.get("outletName") or x.get("name") or ""
        nm = unquote(str(raw)).strip()
        names[x.get("vmsID")] = "" if nm.lower() in ("", "null", "--") else nm
    log("  %-14s %-10s %s" % ("terminal", "mapped?", "outlet name (from portal)"))
    stubs_ml, stubs_map, unnamed = [], [], []
    for t in terminals:
        known = t in nets_mapping.TERMINAL_TO_MACHINE
        log("  %-14s %-10s %s" % (t, "yes" if known else "NO",
                                 names.get(t) or "(no name on portal)"))
        if not known and acct != nets_mapping.MAIN_ACCOUNT:
            code = synthetic_machine_code(t)
            if not names.get(t):
                unnamed.append(t)
            # MachineLookup.MachineName is NVARCHAR(100) (checked 2026-09-03).
            nm = (names.get(t) or t)[:100].replace("'", "''")
            stubs_ml.append("    ('%s', N'%s'),   -- %s%s"
                            % (code, nm, t, "  NAME MISSING - ask the franchisee"
                               if not names.get(t) else ""))
            stubs_map.append("    '%-14s: ('%s', %r, %r),   # %s%s"
                             % ("%s'" % t, code, names.get(t) or t,
                                names.get(t, ""), acct,
                                "  NAME MISSING" if not names.get(t) else ""))
    if unnamed:
        log("  %d terminal(s) have no outlet name on the portal: %s"
            % (len(unnamed), ", ".join(unnamed)))
    if stubs_ml:
        log("\n  -- MachineLookup rows for account %s (paste into "
            "migration_2026-09-03_franchisee.sql BLOCK 3, then REVIEW the names):" % acct)
        for x in stubs_ml:
            log(x)
        log("\n  # nets_mapping.TERMINAL_TO_MACHINE entries for %s:" % acct)
        for x in stubs_map:
            log(x)
        log("  # nets_mapping.TERMINAL_ACCOUNT entries:")
        for t in terminals:
            if t not in nets_mapping.TERMINAL_TO_MACHINE:
                log("    '%s': '%s'," % (t, acct))


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10,
                    help="rolling window ending yesterday SGT (default 10)")
    ap.add_argument("--from", dest="win_from")
    ap.add_argument("--to", dest="win_to")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--probe", action="store_true",
                    help="fetch one day, print one raw row and the field names, load nothing")
    ap.add_argument("--force", action="store_true", help="override the shrink guard")
    ap.add_argument("--include-today", action="store_true", dest="include_today",
                    help="also load TODAY, partial. Both scheduled runs use it: "
                         "at 06:00 SGT 'today' is the small hours, which is where "
                         "a night shift's work lands; at 18:00 it is the day so "
                         "far. D-1 is always rewritten complete afterwards, so "
                         "the reconciliation still only ever reads whole days.")
    ap.add_argument("--roster", action="store_true",
                    help="log in to every configured account, print its terminal "
                         "roster and a MachineLookup / nets_mapping stub for each "
                         "terminal not yet mapped, load nothing")
    a = ap.parse_args()

    pepper = os.environ.get("NETS_CARD_PEPPER")
    if not a.roster and not pepper:
        sys.exit("NETS_CARD_PEPPER not set. Refusing to run: unpeppered hashes could "
                 "never be reconciled with existing rows.")

    today = dt.datetime.now(SGT).date()
    # The window normally ends at D-1. --include-today extends it by one day so
    # the partial current day is fetched at all; without this the per-day guard
    # in load() would never see today because it was never in `window`.
    last = today if a.include_today else today - dt.timedelta(days=1)
    if a.win_from:
        d0 = dt.date.fromisoformat(a.win_from)
        d1 = dt.date.fromisoformat(a.win_to) if a.win_to else last
    else:
        d1 = last
        # --to without --from used to be silently ignored here.
        if a.win_to:
            d1 = dt.date.fromisoformat(a.win_to)
        d0 = d1 - dt.timedelta(days=a.days - 1)
    if d0 > d1:
        sys.exit("window start is after window end")
    window = [d0 + dt.timedelta(days=i) for i in range((d1 - d0).days + 1)]

    try:
        # Inside the try: a bad key is an Abort (Teams alert, exit 2), not a
        # bare sys.exit that takes the MAIN pull down silently.
        accounts = read_accounts()
        # ------------------------------------------------------------------ #
        # one pass per account. Everything an account produces stays LOCAL to
        # its pass until the pass completes every day; only then is it merged.
        # An account that fails part-way therefore contributes nothing - not
        # even the days it did fetch - so its terminals are never "alive" for
        # some dates and absent for others, which is the shape that would send
        # them down the PURGED_VANISHED path.
        # ------------------------------------------------------------------ #
        all_rows, all_bad = [], []
        excluded = set()            # (terminal, date) pairs load() must not touch
        skipped_days = []           # (account, day, why)
        failed_accounts = []        # (account, why)
        rosters = {}                # account -> [terminal ids]  (completed accounts)
        new_terms, moved = [], {}
        misfiled = []               # terminal seen on account X, mapping says Y

        for acct, user, pw in accounts:
            log("\n== account %s (%s) ==" % (acct, nets_mapping.ACCOUNTS[acct]["label"]))
            if not (user and pw):
                why = ("credentials not configured (AURESYS_USER_%s / "
                       "AURESYS_PASSWORD_%s)" % (acct, acct)) if acct != nets_mapping.MAIN_ACCOUNT \
                      else "AURESYS_USER / AURESYS_PASSWORD not set"
                log("  SKIPPED - " + why)
                failed_accounts.append((acct, why))
                continue
            session = requests.Session()
            session.headers["User-Agent"] = "knm-auresys-pull/1.1"
            # The API localises status text by session locale and falls back to
            # Chinese for a non-browser client - the portal only looks English
            # because a browser sends a language header and sets a locale
            # cookie. Do both.
            session.headers["Accept-Language"] = "en-US,en;q=0.9"
            session.cookies.set("locale", "en", domain="autwp.auresys.solutions")
            acct_rows, acct_bad, acct_skipped = [], [], []
            try:
                login(session, user, pw)
                token, terminals = open_report_page(session)
                log("logged in; roster %d terminals; window %s .. %s"
                    % (len(terminals), d0, d1))
                if a.roster:
                    print_roster(acct, terminals, session)
                    rosters[acct] = terminals
                    continue

                for day in window:
                    raw, expected, expected_amt = fetch_day(session, token, terminals, day)
                    if len(raw) != expected:
                        raise Abort("ABORTED_PARSE",
                                    "%s: collected %d rows but the API reported recordsFiltered=%d"
                                    % (day, len(raw), expected))
                    if a.probe:
                        log("PROBE %s: %d rows, recordsFiltered=%d, totalAmount=%s"
                            % (day, len(raw), expected, expected_amt))
                        if raw:
                            log("PROBE field names: %s" % sorted(raw[0]))
                            log("PROBE first row: %s" % json.dumps(raw[0], indent=1)[:1500])
                        break               # one day per account is the point
                    parsed, bad = parse_rows(raw, pepper, day, account=acct)
                    got_amt = sum((r["amount"] for r in parsed), ZERO)
                    # Quarantined rows are excluded from the load but NOT from
                    # the reconciliation: the API totals every row it returned,
                    # so dropping their amounts here would turn the cent-exact
                    # check into a guaranteed mismatch and cost the guard entirely.
                    bad_amt = sum((b["amount"] for b in bad if b["amount"] is not None), ZERO)
                    unpriced = [b for b in bad if b["amount"] is None]

                    # A day that cannot be reconciled is NOT loaded - for THIS
                    # account only. Its terminal-days go to `excluded`; the other
                    # accounts' rows for the same date still load.
                    skip = None
                    if expected_amt is None:
                        pass                      # API gave no total; nothing to check
                    elif unpriced:
                        skip = ("%d row(s) have an unparseable amount, so the day cannot "
                                "be reconciled against the API total %s"
                                % (len(unpriced), expected_amt))
                    elif got_amt + bad_amt != expected_amt and got_amt != expected_amt:
                        # Either interpretation of totalAmount is accepted,
                        # because which one Auresys uses has never been
                        # established: the 2026-08-26 probe was INCONCLUSIVE.
                        skip = ("parsed %s (quarantined %s) matches neither the API total "
                                "%s with nor without the quarantined rows"
                                % (got_amt, bad_amt, expected_amt))

                    st = Counter(r["status"] for r in parsed)
                    log("  %s  rows=%-5d dispenses=%-5d stlm=%-3d fail=%-3d amount=%s%s"
                        % (day, len(parsed), st[STATUS_SUCCESS], st[STATUS_STLM],
                           st[STATUS_FAIL], got_amt,
                           ("  QUARANTINED=%d" % len(bad)) if bad else ""))
                    for b in bad:
                        log("    QUARANTINE %s %s status=%r reason=%s"
                            % (b["terminal"], b["raw_time"], b["raw_status"], b["reason"]))

                    # Quarantined rows are recorded for EVERY day, including a
                    # skipped one - that list is how anyone finds out why.
                    acct_bad.extend(bad)
                    if skip:
                        log("  %s  NOT LOADED - %s" % (day, skip))
                        acct_skipped.append((acct, day, skip))
                        continue
                    acct_rows.extend(parsed)
            except (Abort, requests.RequestException) as e:
                if len(accounts) == 1:
                    raise
                # A timeout or connection reset on one portal login is as much
                # "this account failed" as an Abort is; it must not take the
                # other accounts down with it.
                status = getattr(e, "status", "FAILED")
                msg = getattr(e, "msg", None) or ("%s: %s" % (type(e).__name__, e))
                log("  ACCOUNT FAILED [%s] %s - skipped, nothing from it will load"
                    % (status, msg))
                failed_accounts.append((acct, "[%s] %s" % (status, msg)))
                continue

            # ---- the account completed: merge ----
            rosters[acct] = terminals
            all_rows.extend(acct_rows)
            all_bad.extend(acct_bad)
            skipped_days.extend(acct_skipped)
            for _, day, _ in acct_skipped:
                for t in terminals:
                    excluded.add((t, day))
            for t in terminals:
                if t not in nets_mapping.known_terminals():
                    new_terms.append("%s (%s)" % (t, acct))
                elif nets_mapping.account_of(t) != acct:
                    misfiled.append("%s on %s, nets_mapping says %s"
                                    % (t, acct, nets_mapping.account_of(t)))

        if not rosters:
            raise Abort("FAILED", "every account failed: %s"
                        % "; ".join("%s (%s)" % f for f in failed_accounts))
        if a.probe or a.roster:
            if failed_accounts:
                for acct, why in failed_accounts:
                    log("account %s FAILED - %s" % (acct, why))
                sys.exit(1)
            return

        # ---- cross-account integrity, before anything touches the DB ----
        # The delete/scan/NO_CHANGE logic keys on (terminal, date) with no
        # account, so one terminal on two rosters would double-stage and then
        # fail the shrink arithmetic on every run. Refuse outright.
        seen_on = {}
        for acct, terms in rosters.items():
            for t in terms:
                seen_on.setdefault(t, []).append(acct)
        dup = {t: accts for t, accts in seen_on.items() if len(accts) > 1}
        if dup:
            raise Abort("ABORTED_PARSE",
                        "terminal(s) present on more than one account's roster - "
                        "cannot attribute rows: %s"
                        % "; ".join("%s: %s" % (t, "/".join(x)) for t, x in sorted(dup.items())))
        # Delete scope for load(): the mapping plus every completed roster,
        # MINUS any terminal the mapping files under a failed account. Those
        # were not fetched, so they must not be seeded as "reported nothing".
        # Ownership comes from the roster a terminal was actually seen on this
        # run, falling back to the mapping only for terminals nobody listed
        # (a failed account's, typically). Without this, a franchisee terminal
        # not yet in TERMINAL_ACCOUNT would default to MAIN and be dropped
        # from the scope whenever MAIN failed.
        owner = {t: acct for acct, terms in rosters.items() for t in terms}
        failed_keys = {acct for acct, _ in failed_accounts}
        roster = set(nets_mapping.known_terminals())
        # Franchisee rosters are added so their (unmapped, for now) terminals
        # are cleared per date like mapped ones. The MAIN roster is NOT: for a
        # MAIN-only installation the scope stays exactly known_terminals(),
        # i.e. the pre-2026-09-03 behaviour, byte for byte.
        for acct, terms in rosters.items():
            if acct != nets_mapping.MAIN_ACCOUNT:
                roster.update(terms)
        roster = {t for t in roster
                  if owner.get(t, nets_mapping.account_of(t)) not in failed_keys}

        # A date on which EVERY completed account skipped leaves the window
        # entirely (same reasoning as before); a date skipped by some accounts
        # stays, with those accounts' terminal-days in `excluded`.
        full_window = list(window)
        by_date = Counter(day for _, day, _ in skipped_days)
        window = [d for d in window if by_date.get(d, 0) < len(rosters)]

        # terminals reporting more than one outlet in the window = moved
        by_term = {}
        for r in all_rows:
            by_term.setdefault(r["terminal"], set()).add(r["outlet"])
        moved = {t: o for t, o in by_term.items() if len(o) > 1}

        degraded = bool(all_bad or skipped_days or failed_accounts)
        degraded_bits = []
        if all_bad:
            degraded_bits.append("%d row(s) quarantined; see dbo.NETS_Unmapped_Row" % len(all_bad))
        if skipped_days:
            degraded_bits.append("%d account-day(s) failed reconciliation" % len(skipped_days))
        for acct, why in failed_accounts:
            degraded_bits.append("account %s skipped: %s" % (acct, why))
        # The dashboard reads the [SKIPPED_ACCOUNTS=...] token to warn per
        # franchisee. It goes FIRST so the 4000-char truncation of Error_Text
        # can never chop it, and it is a fixed-format token so a ';' or ':'
        # inside an Abort message cannot confuse the parse.
        degraded_text = None
        if degraded:
            tok = ("[SKIPPED_ACCOUNTS=%s] " % ",".join(a_ for a_, _ in failed_accounts)
                   if failed_accounts else "")
            degraded_text = tok + "DEGRADED: " + "; ".join(degraded_bits)

        if a.dry_run:
            # Deliberately ahead of the empty-window Abort below: --dry-run is
            # the command an operator runs to find out WHY everything failed,
            # so it must print the per-day reasons rather than one line.
            log("\ndry run - nothing written")
            log("accounts completed: %s" % ", ".join(sorted(rosters)) if rosters else "none")
            for acct, why in failed_accounts:
                log("account %s FAILED - %s" % (acct, why))
            if all_bad:
                log("would quarantine %d row(s)" % len(all_bad))
            for acct, d, why in skipped_days:
                log("would NOT load %s for %s - %s" % (d, acct, why))
            if not window:
                log("NOTHING would be loaded - every day failed its reconciliation.")
            if moved:
                for t, o in moved.items():
                    log("REASSIGNED %s -> %s" % (t, " | ".join(sorted(o))))
            if new_terms:
                log("NEW TERMINALS not in nets_mapping: %s" % ", ".join(new_terms))
            if misfiled:
                log("MISFILED: %s" % "; ".join(misfiled))
            log("delete scope: %d terminals; excluded terminal-days: %d"
                % (len(roster), len(excluded)))
            return

        if not window:
            raise Abort("FAILED",
                        "every day in the window failed its reconciliation - "
                        "nothing can be loaded. Re-run with --dry-run for the "
                        "per-day reasons.")

        conn = connect()
        try:
            run_seq, counts, unmapped = load(
                conn, all_rows, window, a, last_full=today - dt.timedelta(days=1),
                bad=all_bad, full_window=full_window,
                degraded=degraded, roster=roster, excluded=excluded,
                degraded_text=degraded_text, failed_keys=failed_keys)
        finally:
            conn.close()

        log("\nrun %d  partitions: %s" % (run_seq, dict(counts)))

        alerts = []
        if unmapped:
            alerts.append("Unmapped terminals trading (rows loaded with no machine): %s"
                          % ", ".join(sorted(unmapped)))
        if moved:
            alerts.append("Terminals reporting a new outlet - update nets_mapping.py and "
                          "MachineLookup: " + "; ".join("%s -> %s" % (t, " | ".join(sorted(o)))
                                                        for t, o in moved.items()))
        if new_terms:
            alerts.append("New terminals on the portal: %s" % ", ".join(new_terms))
        if failed_accounts:
            alerts.append(
                "%d account(s) SKIPPED - their machines did not update: %s"
                % (len(failed_accounts),
                   "; ".join("%s (%s)" % (acct, why) for acct, why in failed_accounts)))
        if misfiled:
            alerts.append("Terminal on a different account than nets_mapping.TERMINAL_ACCOUNT "
                          "records - fix the mapping: " + "; ".join(misfiled))
        if skipped_days:
            alerts.append(
                "%d account-day(s) NOT loaded - failed reconciliation: %s"
                % (len(skipped_days),
                   "; ".join("%s %s (%s)" % (acct, d, why) for acct, d, why in skipped_days)))
        if all_bad:
            by_reason = Counter((b["reason"], b["raw_status"]) for b in all_bad)
            alerts.append(
                "%d row(s) quarantined and NOT loaded - see dbo.NETS_Unmapped_Row: %s"
                % (len(all_bad),
                   "; ".join("%s %r x%d" % (rsn, st, n)
                             for (rsn, st), n in by_reason.most_common())))
        if counts.get("SKIPPED_SHRINK"):
            alerts.append("%d machine-days skipped by the shrink guard - see NETS_Load_Audit"
                          % counts["SKIPPED_SHRINK"])
        if alerts:
            for x in alerts:
                log("ALERT: " + x)
            notify("**Auresys daily pull needs attention**\n\n- " + "\n- ".join(alerts))

        # The heartbeat answers ONE question: did the pull run to completion?
        # It is pinged whenever it did. Degradation is reported through Teams,
        # the DEGRADED row in NETS_Pull_Run and the exit code - using the
        # liveness monitor as a data-quality channel would mean one unmapped
        # row makes the feed look dead for days, and a muted alert after that.
        hb = os.environ.get("HEARTBEAT_URL")
        if hb:
            # Deliberately not in the database: a monitor that lives in the thing
            # it monitors cannot report that the thing is down.
            try:
                requests.get(hb, timeout=30)
            except Exception as e:
                log("heartbeat ping failed: %s" % e)

        if degraded:
            # Data landed, but not all of it. Exit non-zero so the run is red
            # in the Actions history: a green tick on a run that refused a day
            # is how a problem goes unnoticed for a week.
            log("DEGRADED: %d row(s) quarantined, %d account-day(s) not loaded, "
                "%d account(s) skipped. The rest loaded normally."
                % (len(all_bad), len(skipped_days), len(failed_accounts)))
            sys.exit(1)

    except Abort as e:
        log("ABORT [%s] %s" % (e.status, e.msg))
        notify("**Auresys daily pull FAILED** [%s]\n\n%s" % (e.status, e.msg))
        sys.exit(2)


if __name__ == "__main__":
    main()
