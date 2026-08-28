#!/usr/bin/env python3
"""Read-only status probe. For one day, reports every row whose dispenseStatus
auresys_pull.status_code() does not recognise, and whether the API's
totalAmount includes those rows.

No database connection, no writes, no Teams post, no heartbeat. Runs in GitHub
Actions, where AURESYS_USER / AURESYS_PASSWORD already exist as secrets.

    python3 probe_manual_stlm.py 2026-08-26

Output goes to a CI log readable by anyone with repo access, so rows are
printed as a field allowlist, never as the raw dict. Unexpected field NAMES are
listed without their values.
"""
import datetime as dt
import decimal
import os
import sys
from collections import Counter, defaultdict

import requests
import auresys_pull as ap

ZERO = decimal.Decimal("0.00")

# Printed verbatim. Everything else is reported by name only - the server
# returns whatever it returns, not just what COLUMNS asked for, and a
# settlement row is exactly the kind that carries an extra name or reference.
SHOW = ("vmsID", "outletNo", "time", "dispenseStatus", "paymentType",
        "amount", "isSettled", "skuNo", "skuName", "errorCode", "errorMsg")


def amt(r):
    return decimal.Decimal(str(r["amount"])).quantize(ZERO)


def main():
    day = dt.date.fromisoformat(sys.argv[1] if len(sys.argv) > 1 else "2026-08-26")
    user, pw = os.environ.get("AURESYS_USER"), os.environ.get("AURESYS_PASSWORD")
    if not user or not pw:
        sys.exit("AURESYS_USER / AURESYS_PASSWORD not set.")

    s = requests.Session()
    s.headers["User-Agent"] = "knm-auresys-probe/1.0"
    s.headers["Accept-Language"] = "en-US,en;q=0.9"
    s.cookies.set("locale", "en", domain="autwp.auresys.solutions")
    ap.login(s, user, pw)
    token, terminals = ap.open_report_page(s)
    print("logged in; roster %d terminals" % len(terminals))

    raw, expected, expected_amt = ap.fetch_day(s, token, terminals, day)
    print("%s: rows=%d recordsFiltered=%s totalAmount=%s"
          % (day, len(raw), expected, expected_amt))
    if len(raw) != expected:
        sys.exit("collected %d rows but recordsFiltered=%s - partial fetch, "
                 "every figure below would be wrong. Stopping."
                 % (len(raw), expected))
    if not raw:
        sys.exit("no rows for %s - nothing to probe." % day)

    print("status histogram: %s"
          % sorted(Counter(str(r.get("dispenseStatus")).strip() for r in raw).items(),
                   key=lambda kv: -kv[1]))

    # Amount per status string, so it is visible which subset reconciles.
    by_status = defaultdict(lambda: ZERO)
    for r in raw:
        by_status[str(r.get("dispenseStatus")).strip()] += amt(r)
    for k, v in sorted(by_status.items(), key=lambda kv: -abs(kv[1])):
        print("  amount[%-20s] = %s" % (k, v))

    odd = [r for r in raw if ap.status_code(r.get("dispenseStatus")) is None]
    known = [r for r in raw if ap.status_code(r.get("dispenseStatus")) is not None]
    print("unrecognised rows: %d" % len(odd))
    for r in odd:
        print("  --- unrecognised row ---")
        for k in SHOW:
            if k in r:
                print("   %-14s = %r" % (k, r[k]))
        rest = sorted(k for k in r if k not in SHOW)
        if rest:
            print("   other field NAMES only (values withheld - CI log): %s" % rest)

    known_amt = sum((amt(r) for r in known), ZERO)
    odd_amt = sum((amt(r) for r in odd), ZERO)
    print("recognised rows only  = %s" % known_amt)
    print("unrecognised rows     = %s" % odd_amt)
    print("both together         = %s" % (known_amt + odd_amt))
    print("API totalAmount       = %s" % expected_amt)

    if expected_amt is None:
        print("VERDICT: the API returned no totalAmount - INCONCLUSIVE.")
    elif odd_amt == ZERO:
        print("VERDICT: the unrecognised rows sum to 0.00, so both hypotheses fit "
              "the arithmetic - INCONCLUSIVE. Decide from the row fields above.")
    elif known_amt == expected_amt:
        print("VERDICT: totalAmount EXCLUDES the unrecognised rows -> mapping them "
              "into STATUS_MAP would break the amount check at auresys_pull.py:590.")
    elif known_amt + odd_amt == expected_amt:
        print("VERDICT: totalAmount INCLUDES the unrecognised rows -> they must be "
              "mapped, not skipped.")
    else:
        print("VERDICT: neither total matches - INCONCLUSIVE, do not patch anything.")


if __name__ == "__main__":
    try:
        main()
    except ap.Abort as e:
        sys.exit("ABORT [%s] %s" % (e.status, e.msg))
