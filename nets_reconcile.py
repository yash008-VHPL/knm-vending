#!/usr/bin/env python3
"""
nets_reconcile.py — Monthly NETS vs DB vend reconciliation
Downloads the NETS CSV, fetches DB vend counts via the Flask API,
compares them, and posts a report to Microsoft Teams.

Runs automatically via GitHub Actions on the 2nd of each month.
Can also be triggered manually for any month.

Usage:
    python nets_reconcile.py                       # auto: previous month
    python nets_reconcile.py --year 2026 --month 3
"""

import argparse, calendar, csv, io, json, os, sys, textwrap
from datetime import date, datetime
from collections import defaultdict

import requests

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("Run: pip install playwright && playwright install chromium")

# ── Credentials — env vars in GitHub Actions, config files locally ─────────────
def _env(key, config_attr=None, default=""):
    val = os.environ.get(key, "")
    if val:
        return val
    if config_attr:
        try:
            import config_nets as cn
            return getattr(cn, config_attr, default)
        except ImportError:
            pass
    return default

NETS_USERNAME    = _env("NETS_USERNAME",    "USERNAME")
NETS_PASSWORD    = _env("NETS_PASSWORD",    "PASSWORD")
INTERNAL_API_KEY = _env("INTERNAL_API_KEY")
TEAMS_WEBHOOK    = _env("TEAMS_WEBHOOK_URL")
APP_BASE_URL     = _env("APP_BASE_URL", default=
    "https://knmdispenseviewer-eqdjbscahtfufxfj.southeastasia-01.azurewebsites.net")

LOGIN_URL  = "https://autwp.auresys.solutions"
REPORT_URL = "https://autwp.auresys.solutions/vms/report/transactions"

# ── NETS outlet name → DB MachineName mapping ──────────────────────────────────
# Keys are NETS outlet names in UPPER CASE.
# Values are the exact MachineName strings stored in MachineLookup.
# None = location exists in NETS but not in our DB (expected).
NETS_TO_DB = {
    "336 RIVER VALLEY RD":                  None,
    "ADAM ROAD THE JAPANESE ASSOCIATION":   "Japanese Association Singapore",
    "ALICE AT MEDIAPOLIS":                  "Alice@medipolis",
    "ALJUNIED PARKWAY LAB":                 None,
    "AMK MAYBANK CENTRE":                   "Maybank",
    "AMK SKYWORKS":                         "Skyworks @ AMK",
    "AMK TECHPOINT":                        "AMK Techpoint",
    "ANGUILLA MOSQUE":                      "Anguillia Mosque",
    "BEDOK POLICE HQ":                      "Home Team Academy",
    "BEDOK SKYWORKS TABLETOP":              "Skyworks @ Bedok",
    "BRADDELL 351":                         "351 Braddell",
    "BUROH LN JTC PPH":                     None,
    "CERTIS PAYA LEBAR":                    "Paya Lebar Certis",
    "CHANGI AIRPORT POLICE HQ":             "Changi Airport Police",
    "CHANGI NAVAL BASE BLK 119":            "Changi Naval Base Blk119",
    "CHANGI NAVAL BASE BLK 225":            None,
    "CHANGI NAVAL BASE COOKHOUSE":          "Changi Naval Base Cookhouse",
    "CHASEN LOGISTICS":                     None,
    "CHINATOWN SA TOURS":                   "SA Tours",
    "COLLINS AEROSPACE CHANGI":             "Collins Aerospace",
    "FEI SIONG HQ":                         None,
    "GEYLANG NPC":                          "Geylang NPC",
    "GUL RD TAKADA INDUSTRIES":             None,
    "HDB HUB NATIONAL YOUTH COUNCIL":       None,
    "HOLLAND V CSCOLLEGE":                  "CSC Holland",
    "IMH":                                  "IMH Main Lobby",
    "IMH THE ANNEX":                        None,
    "JOO KOON SINWA GLOBAL":                "Sinwa Global",
    "KAKI BUKIT CAMP":                      "Kaki Bukit Camp",
    "KEPPEL RAEBURN PARK":                  "MinDef Lunch Club",
    "KRANJI CAMP 3 COOKHOUSE":              "Kranji Camp 3",
    "KRANJI CAMP BLK 808":                  None,
    "KRANJI CAMP II":                       "Kranji Camp II",
    "LINK@AMK":                             "Link @ AMK",
    "MT CARMEL BP CHURCH":                  "Mount Carmel BP West Coast",
    "ONE NORTH MEDIACORP":                  "Mediacorp",
    "ORCHARD AMERICAN CLUB":                "Welcia Orchard",
    "OXLEY BIZHUB 2":                       "Oxley Bizhub 2",
    "POLICE CANTONTMENT COMPLEX":           "Police Cantonment Complex",
    "RIFLE RANGE ATREC":                    None,
    "SCIENCE PARK ASCENT":                  "Ascent",
    "SERANGOON AKRIBIS":                    None,
    "SERANGOON AUPE CLUB":                  "AUPE",
    "SGH BLK 6 L9":                         None,
    "SINGAPORE SAILING FEDERATION":         None,
    "SINGPOST CENTRE":                      "Singpost Center",
    "SP CHOA CHU KANG":                     "SP Choa Chu Kang",
    "SP KALLANG":                           "SP Kallang",
    "SP PASIR PANJANG":                     None,
    "SUNSHINE PLAZA":                       None,
    "T3 SIA CONTROL CENTRE":               "SIA Terminal 3 Control Center",
    "TANAH MERAH FERRY TERMINAL":           None,
    "TANGLIN GLENEAGLES L1 UCC":            None,
    "TANGLINGLENEAGLESL4ICU":              "Gleneagles L4",
    "TUAS NAVAL BASE":                      None,
    "UBI TECHPARK LOBBY A":                 "Ubi Tech Park A",
    "UBI TECHPARK LOBBY C":                 "Ubi Tech Park C",
}

# ── Step 1: Download NETS CSV ──────────────────────────────────────────────────
def download_nets_csv(from_date: str, to_date: str) -> str:
    """Returns the CSV content as a string."""
    from_str = f"{from_date} 00:00"
    to_str   = f"{to_date} 23:59"
    print(f"Downloading NETS CSV  {from_str} → {to_str}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx  = browser.new_context(ignore_https_errors=True, accept_downloads=True)
        page = ctx.new_page()

        page.goto(LOGIN_URL, wait_until="networkidle")
        page.locator('input[placeholder="Account"]').fill(NETS_USERNAME)
        page.locator('input[placeholder="Password"]').fill(NETS_PASSWORD)
        page.locator('button:has-text("Sign In")').click()
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(2000)

        page.goto(REPORT_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        page.wait_for_selector("text=Search Result", timeout=20_000)

        # Set date range
        all_inputs = page.locator('input[type="text"]:visible').all()
        dt_inputs  = [i for i in all_inputs
                      if i.input_value() and "-" in i.input_value()]
        for inp, val in zip(dt_inputs[:2], [from_str, to_str]):
            inp.click(click_count=3)
            inp.fill(val)
            inp.press("Tab")

        # Click export and capture download
        green_btns = page.locator('button.btn-success:visible').all()
        export_btn = green_btns[1] if len(green_btns) >= 2 else green_btns[-1]

        with page.expect_download(timeout=60_000) as dl:
            export_btn.click()
        download = dl.value
        content  = download.path()

        with open(content, encoding="utf-8-sig") as f:
            csv_text = f.read()

        browser.close()
    print(f"  Downloaded {len(csv_text):,} bytes")
    return csv_text


# ── Step 2: Parse NETS CSV ─────────────────────────────────────────────────────
def parse_nets(csv_text: str) -> dict:
    """Returns {outlet_name_upper: txn_count}."""
    counts = defaultdict(int)
    for row in csv.DictReader(io.StringIO(csv_text)):
        if row["Status"].strip() == "Success":
            counts[row["Outlet Name"].strip().upper()] += 1
    return dict(counts)


# ── Step 3: Fetch DB vend counts via Flask API ─────────────────────────────────
def fetch_db_counts(year: int, month: int) -> dict:
    """Returns {machine_name: vend_count}."""
    url  = f"{APP_BASE_URL}/api/internal/vend-counts"
    resp = requests.get(url, params={"year": year, "month": month},
                        headers={"X-Internal-Key": INTERNAL_API_KEY},
                        timeout=30)
    resp.raise_for_status()
    return {r["name"]: r["vends"] for r in resp.json()}


# ── Step 4: Compare ────────────────────────────────────────────────────────────
MISSING_THRESHOLD  = 0.10   # flag if DB is missing >10% of NETS transactions
MISSING_MIN        = 10     # and at least this many absolute transactions

def compare(nets: dict, db: dict):
    missing, overcount, ok, unmapped_nets = [], [], [], []

    for nets_name, nets_count in sorted(nets.items()):
        if nets_name not in NETS_TO_DB:
            unmapped_nets.append((nets_name, nets_count))
            continue

        db_name = NETS_TO_DB[nets_name]
        if db_name is None:
            continue                            # expected: no DB entry

        db_count = db.get(db_name, 0)
        diff     = nets_count - db_count

        if diff > MISSING_MIN and diff / nets_count > MISSING_THRESHOLD:
            pct = diff / nets_count * 100
            missing.append((nets_name, db_name, nets_count, db_count, diff, pct))
        elif db_count - nets_count > MISSING_MIN:
            overcount.append((nets_name, db_name, nets_count, db_count, db_count - nets_count))
        else:
            ok.append((nets_name, nets_count, db_count))

    # DB machines with no NETS counterpart
    mapped_db_names = {v for v in NETS_TO_DB.values() if v}
    no_nets = [(name, vends) for name, vends in db.items() if name not in mapped_db_names]

    return missing, overcount, ok, unmapped_nets, no_nets


# ── Step 5: Format & send Teams report ────────────────────────────────────────
def post_teams(title: str, missing, overcount, ok, unmapped_nets, no_nets):
    lines = []

    if missing:
        lines.append("**🔴 Possible disconnection — DB significantly under NETS:**")
        for nets_name, _, nets_c, db_c, diff, pct in sorted(missing, key=lambda x: -x[4]):
            lines.append(f"- {nets_name}: NETS={nets_c}, DB={db_c}, missing={diff} ({pct:.0f}%)")
    else:
        lines.append("**✅ No significant disconnection events detected.**")

    if overcount:
        lines.append("\n**🟡 DB higher than NETS (minor overcounting):**")
        for nets_name, _, nets_c, db_c, extra in sorted(overcount, key=lambda x: -x[4]):
            lines.append(f"- {nets_name}: NETS={nets_c}, DB={db_c}, extra={extra}")

    lines.append(f"\n**✅ Within tolerance:** {len(ok)} locations")

    if unmapped_nets:
        lines.append(f"\n**⚠️ New NETS outlet names not in mapping ({len(unmapped_nets)}) — update NETS_TO_DB in nets_reconcile.py:**")
        for name, count in unmapped_nets:
            lines.append(f"- {name} ({count} txns)")

    payload = {
        "@type":    "MessageCard",
        "@context": "https://schema.org/extensions",
        "summary":  title,
        "themeColor": "d9534f" if missing else "5cb85c",
        "title":    title,
        "text":     "\n".join(lines),
    }

    resp = requests.post(TEAMS_WEBHOOK, json=payload, timeout=10)
    if resp.status_code == 200:
        print("Teams notification sent.")
    else:
        print(f"Teams notification failed: {resp.status_code} {resp.text}")


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--year",  type=int)
    parser.add_argument("--month", type=int)
    args = parser.parse_args()

    today = date.today()
    if args.year and args.month:
        year, month = args.year, args.month
    else:
        # Default: previous month
        first_of_this = today.replace(day=1)
        prev = date(first_of_this.year, first_of_this.month, 1)
        if first_of_this.month == 1:
            prev = date(first_of_this.year - 1, 12, 1)
        else:
            prev = date(first_of_this.year, first_of_this.month - 1, 1)
        year, month = prev.year, prev.month

    last_day = calendar.monthrange(year, month)[1]
    from_date = f"{year}-{month:02d}-01"
    to_date   = f"{year}-{month:02d}-{last_day:02d}"
    month_label = datetime(year, month, 1).strftime("%B %Y")
    print(f"\n=== NETS Reconciliation — {month_label} ===\n")

    # Validate credentials
    if not NETS_USERNAME or not NETS_PASSWORD:
        sys.exit("NETS credentials missing. Set NETS_USERNAME and NETS_PASSWORD.")
    if not INTERNAL_API_KEY or INTERNAL_API_KEY == "change-me-to-a-strong-random-string":
        sys.exit("INTERNAL_API_KEY not set.")

    csv_text = download_nets_csv(from_date, to_date)
    nets     = parse_nets(csv_text)
    print(f"NETS: {sum(nets.values()):,} transactions across {len(nets)} outlets")

    print("Fetching DB vend counts…")
    db = fetch_db_counts(year, month)
    print(f"DB:   {sum(db.values()):,} vends across {len(db)} machines")

    missing, overcount, ok, unmapped_nets, no_nets = compare(nets, db)

    # Print to stdout (visible in GitHub Actions logs)
    print(f"\n{'─'*60}")
    if missing:
        print(f"🔴 DISCONNECTION RISK ({len(missing)} locations):")
        for nets_name, db_name, nets_c, db_c, diff, pct in sorted(missing, key=lambda x: -x[4]):
            print(f"   {nets_name:<40} NETS={nets_c:>5} DB={db_c:>5} missing={diff:>5} ({pct:.0f}%)")
    else:
        print("✅ No significant disconnection events.")

    if overcount:
        print(f"\n🟡 OVERCOUNT ({len(overcount)} locations):")
        for nets_name, db_name, nets_c, db_c, extra in sorted(overcount, key=lambda x: -x[4]):
            print(f"   {nets_name:<40} NETS={nets_c:>5} DB={db_c:>5} extra={extra:>5}")

    print(f"\n✅ Within tolerance: {len(ok)} locations")

    if unmapped_nets:
        print(f"\n⚠️  New unmapped NETS outlets ({len(unmapped_nets)}) — add to NETS_TO_DB:")
        for name, count in unmapped_nets:
            print(f"   {name}  ({count} txns)")

    # Post to Teams
    if TEAMS_WEBHOOK:
        title = f"📊 NETS Reconciliation — {month_label}"
        post_teams(title, missing, overcount, ok, unmapped_nets, no_nets)
    else:
        print("\n(TEAMS_WEBHOOK_URL not set — skipping Teams notification)")

    # Exit non-zero if there are disconnection risks (makes GitHub Actions flag the run)
    if missing:
        sys.exit(1)


if __name__ == "__main__":
    main()
