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
    python3 auresys_pull.py --days 1 --probe   # dump one raw row, load nothing
    python3 auresys_pull.py --days 10 --dry-run
Env:
    AURESYS_USER, AURESYS_PASSWORD, NETS_CARD_PEPPER
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


def parse_rows(raw_rows, pepper, day):
    out, unknown = [], {}
    for r in raw_rows:
        for k in ("vmsID", "time", "dispenseStatus", "amount"):
            if k not in r:
                raise Abort("ABORTED_PARSE",
                            "API row is missing %r - the response shape changed. "
                            "Run with --probe. Row keys: %s" % (k, sorted(r)))
        code = status_code(r["dispenseStatus"])
        if code is None:
            unknown.setdefault(str(r["dispenseStatus"]).strip(), 0)
            unknown[str(r["dispenseStatus"]).strip()] += 1
            continue
        ts = dt.datetime.strptime(str(r["time"]).strip(), "%Y-%m-%d %H:%M:%S")
        amt = decimal.Decimal(str(r["amount"])).quantize(ZERO)
        if not amt.is_finite():
            raise Abort("ABORTED_PARSE", "non-finite amount in %r" % r)
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
        })
    if unknown:
        raise Abort("ABORTED_PARSE",
                    "unrecognised Status values - refusing to guess. Send Claude this "
                    "whole line: %s" % sorted(unknown.items(), key=lambda kv: -kv[1]))
    return out


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
# parameters; at 10 columns that is 210 rows, so 180 leaves headroom.
INSERT_CHUNK = 180
AUDIT_CHUNK = 150
SQL_INSERT = ("INSERT INTO dbo.NETS_Transaction "
              "(NETS_Terminal_No, Machine_Code, Location_Name, Txn_DateTime, "
              " Txn_Status_Code, Scheme, Amount, Card_Hash, Card_Hash_Ver, Load_Batch_Ref) "
              "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)")
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


def load(conn, rows, window, args, last_full=None):
    """last_full = the newest day this run considers COMPLETE (D-1 on the
    reconciliation run, None when --include-today). That day is always
    rewritten rather than short-circuited by NO_CHANGE - see the comment on
    that branch."""
    cur = conn.cursor()
    cur.execute("INSERT INTO dbo.NETS_Pull_Run "
                "(Run_Id, Window_From, Window_To, Source_File, Csv_Line_Count, "
                " Rows_Parsed, Header_Signature, Status) "
                "OUTPUT INSERTED.Run_Id, INSERTED.Run_Seq "
                "VALUES (NEWID(), %s, %s, 'api:getTransaction', %s, %s, %s, 'RUNNING')",
                (window[0], window[-1], len(rows), len(rows), "|".join(COLUMNS)))
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
        for t in nets_mapping.known_terminals():
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

            if date > today or (date == today and not args.include_today):
                # Today is incomplete by definition, and the whole D-1
                # reconciliation rests on that. --include-today is the deliberate
                # exception: the 17:00 SGT run loads the day so far so the
                # dashboard's vend counter is hours old rather than a day old.
                # The 05:00 run reloads the same day complete and the LOADED
                # branch replaces it, because staged > before. Tomorrow is never
                # loaded under any flag.
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
                note = ("terminal absent from the whole pull - feed may be broken"
                        if term not in alive else
                        "partial reload of today staged %d < stored %d"
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
                    and not (date == last_full and not args.include_today)):
                # identical content already stored: touch nothing. This is what
                # stops a later mapping fix re-stamping historical rows.
                #
                # ONE EXCEPTION, added with --include-today: the newest day of a
                # D-1 run is always rewritten, never short-circuited. That day
                # may have been written PARTIALLY by yesterday's 17:00 run, and
                # a partial day that is never rewritten is worse than a missing
                # one - vw_NETS_Daily_Count stops excluding it the next day and
                # serves undercounted figures as the daily metric, which reads
                # as telemetry > Auresys, the direction that does NOT alert. So
                # every genuine paid-but-no-drink event on that day would be
                # silently suppressed.
                #
                # It also un-freezes the mapping: rows stamped Machine_Code NULL
                # at 17:00 get re-resolved against nets_mapping the next morning
                # instead of staying unattributable forever.
                counts["NO_CHANGE"] += 1
                continue
            if staged == 0 and before > 0:
                if term in alive:
                    action, note = "PURGED_VANISHED", "terminal reported other dates, none for this one"
                else:
                    action, note = "SKIPPED_SHRINK", "terminal absent from the whole pull - feed may be broken"
            elif staged < before and not args.force:
                action, note = "SKIPPED_SHRINK", "staged %d < stored %d - possible partial pull" % (staged, before)
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
                     PEPPER_VER if r["card_hash"] else None, run_seq) for r in prows)
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
                        " Load_Batch_Ref) VALUES "
                        + ",".join(["(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"] * len(chunk)),
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

        seen = {}
        for r in rows:
            k = (r["terminal"], r["outlet"])
            v = seen.setdefault(k, [r["ts"], r["ts"]])
            v[0] = min(v[0], r["ts"])
            v[1] = max(v[1], r["ts"])
        for (term, outlet), (lo, hi) in seen.items():
            cur.execute(SQL_SEEN, (term, outlet, lo, hi))
        cur.execute(SQL_SEEN_RECOUNT)

        cur.execute("UPDATE dbo.NETS_Pull_Run SET Status='SUCCESS', "
                    "Finished_At_UTC=SYSUTCDATETIME() WHERE Run_Seq=%s", (run_seq,))
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
                    help="also load TODAY, partial. For the 17:00 SGT run, which "
                         "exists to keep the dashboard's vend counter fresh. The "
                         "05:00 run must NOT use this: the D-1 reconciliation is "
                         "built on today always being complete.")
    a = ap.parse_args()

    user = os.environ.get("AURESYS_USER")
    pw = os.environ.get("AURESYS_PASSWORD")
    pepper = os.environ.get("NETS_CARD_PEPPER")
    if not (user and pw):
        sys.exit("AURESYS_USER / AURESYS_PASSWORD not set.")
    if not pepper:
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

    session = requests.Session()
    session.headers["User-Agent"] = "knm-auresys-pull/1.0"
    # The API localises status text by session locale and falls back to Chinese
    # for a non-browser client - the portal only looks English because a browser
    # sends a language header and sets a locale cookie. Do both.
    session.headers["Accept-Language"] = "en-US,en;q=0.9"
    session.cookies.set("locale", "en", domain="autwp.auresys.solutions")
    try:
        login(session, user, pw)
        token, terminals = open_report_page(session)
        log("logged in; roster %d terminals; window %s .. %s" % (len(terminals), d0, d1))

        known = nets_mapping.known_terminals()
        new_terms = sorted(set(terminals) - known)
        if new_terms:
            log("NEW TERMINALS on the portal, absent from nets_mapping: %s" % ", ".join(new_terms))

        all_rows = []
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
                return
            parsed = parse_rows(raw, pepper, day)
            got_amt = sum((r["amount"] for r in parsed), ZERO)
            if expected_amt is not None and got_amt != expected_amt:
                raise Abort("ABORTED_PARSE",
                            "%s: parsed amount %s does not match the API total %s"
                            % (day, got_amt, expected_amt))
            st = Counter(r["status"] for r in parsed)
            log("  %s  rows=%-5d dispenses=%-5d stlm=%-3d fail=%-3d amount=%s"
                % (day, len(parsed), st[STATUS_SUCCESS], st[STATUS_STLM],
                   st[STATUS_FAIL], got_amt))
            all_rows.extend(parsed)

        # terminals reporting more than one outlet in the window = moved
        by_term = {}
        for r in all_rows:
            by_term.setdefault(r["terminal"], set()).add(r["outlet"])
        moved = {t: o for t, o in by_term.items() if len(o) > 1}

        if a.dry_run:
            log("\ndry run - nothing written")
            if moved:
                for t, o in moved.items():
                    log("REASSIGNED %s -> %s" % (t, " | ".join(sorted(o))))
            return

        conn = connect()
        try:
            run_seq, counts, unmapped = load(
                conn, all_rows, window, a,
                last_full=None if a.include_today else today - dt.timedelta(days=1))
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
        if counts.get("SKIPPED_SHRINK"):
            alerts.append("%d machine-days skipped by the shrink guard - see NETS_Load_Audit"
                          % counts["SKIPPED_SHRINK"])
        if alerts:
            for x in alerts:
                log("ALERT: " + x)
            notify("**Auresys daily pull needs attention**\n\n- " + "\n- ".join(alerts))

        hb = os.environ.get("HEARTBEAT_URL")
        if hb:
            # Deliberately not in the database: a monitor that lives in the thing
            # it monitors cannot report that the thing is down.
            try:
                requests.get(hb, timeout=30)
            except Exception as e:
                log("heartbeat ping failed: %s" % e)

    except Abort as e:
        log("ABORT [%s] %s" % (e.status, e.msg))
        notify("**Auresys daily pull FAILED** [%s]\n\n%s" % (e.status, e.msg))
        sys.exit(2)


if __name__ == "__main__":
    main()
