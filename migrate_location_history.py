#!/usr/bin/env python3
"""
KNM Vending — Location-History migration runner (one-off, CI-driven).

Runs INSIDE GitHub Actions (Azure-hosted runner) so it can reach Azure SQL via the
"Allow Azure services" firewall rule — the same path nets_reconcile.py uses.

WHAT IT FIXES
  - Relocations had no sharp cutoff and transactions doubled under the old location
    name. Cause: MachineLookup has >1 row per MachineCode (admin used "Add location"
    to move a machine), so the LEFT JOIN fans out.
  - Solution: de-dupe + UNIQUE(MachineCode); effective-dated MachineLocationHistory;
    per-vend location resolution; and for admin-edited moves (no movement record),
    derive the cutoff from the machine's vend gap (it goes dark during
    retrieve/service/redeploy).

MODES
  --mode preview   READ-ONLY. Scans the WHOLE table for every duplicated MachineCode,
                   shows both names, the largest vend gap, and the proposed cutoff.
                   Writes MIGRATION_PREVIEW.md. Changes nothing.
  --mode apply     Transactional. De-dupes all duplicates, creates + back-fills
                   MachineLocationHistory, and seeds each gap-derived cutoff (or an
                   override from --cutoffs). Writes MIGRATION_RESULT.md.

OVERRIDES
  --cutoffs '<code>=<YYYY-MM-DD HH:MM>,<code>=<YYYY-MM-DD>'  (SGT) to override the
  auto-derived cutoff for specific machines. Date-only ⇒ 00:00 SGT.

TIME UNITS
  Vend [Date Time] = OLE Automation float in Singapore wall-clock (epoch 1899-12-30).
  now/cutoffs are converted SGT→OLE so comparisons are apples-to-apples.
"""

import os
import sys
import argparse
from datetime import datetime, timedelta

import pymssql

# ── Connection (env first, like nets_reconcile; config.py fallback for local) ────
def _cfg(name, default=None):
    v = os.environ.get(name)
    if v:
        return v
    try:
        import config
        return getattr(config, name, default)
    except Exception:
        return default

DB_SERVER   = _cfg("DB_SERVER",   "machineserver.database.windows.net")
DB_NAME     = _cfg("DB_NAME",     "Machine DispensedDrink")
DB_USER     = _cfg("DB_USER")
DB_PASSWORD = _cfg("DB_PASSWORD")

# ── OLE date helpers (must match app.py) ────────────────────────────────────────
OLE_EPOCH  = datetime(1899, 12, 30)
SGT_OFFSET = timedelta(hours=8)
GAP_MIN_HOURS = 24.0   # a relocation gap must exceed this to be treated as a move

def to_ole(dt):
    d = dt - OLE_EPOCH
    return d.days + (d.seconds + d.microseconds / 1e6) / 86400.0

def from_ole(v):
    return OLE_EPOCH + timedelta(days=float(v)) if v is not None else None

def now_sgt_ole():
    return to_ole(datetime.utcnow() + SGT_OFFSET)

def parse_sgt(s):
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return to_ole(datetime.strptime(s, fmt))
        except ValueError:
            continue
    raise ValueError(f"Bad cutoff datetime '{s}' (use YYYY-MM-DD or 'YYYY-MM-DD HH:MM')")


def connect():
    if not DB_USER or not DB_PASSWORD:
        sys.exit("ERROR: DB_USER / DB_PASSWORD not set (env or config.py).")
    return pymssql.connect(server=DB_SERVER, database=DB_NAME,
                           user=DB_USER, password=DB_PASSWORD, tds_version="7.4")


# ── Discovery ───────────────────────────────────────────────────────────────────
def find_duplicates(cur):
    """Every MachineCode with >1 MachineLookup row. Returns {code: [names...]}."""
    cur.execute("""
        SELECT CAST(MachineCode AS NVARCHAR(50)) AS code, MachineName,
               ISNULL(IsActive,1) AS act, ISNULL(LastTopupTimestamp,0) AS topup
        FROM MachineLookup
        WHERE CAST(MachineCode AS NVARCHAR(50)) IN (
            SELECT CAST(MachineCode AS NVARCHAR(50))
            FROM MachineLookup
            GROUP BY CAST(MachineCode AS NVARCHAR(50))
            HAVING COUNT(*) > 1
        )
        ORDER BY code, act DESC, topup DESC
    """)
    out = {}
    for code, name, act, topup in cur.fetchall():
        out.setdefault(code, []).append(name)
    return out


def last_movement_to(cur, code):
    cur.execute("""
        SELECT TOP 1 ToLocation FROM WO_MovementOrders
        WHERE CAST(MachineCode AS NVARCHAR(50)) = %s AND StatusCode = 2 AND ToLocation IS NOT NULL
        ORDER BY CompletedAt DESC
    """, (code,))
    r = cur.fetchone()
    return r[0] if r else None


def has_completed_movement(cur, code):
    cur.execute("""
        SELECT TOP 1 1 FROM WO_MovementOrders
        WHERE CAST(MachineCode AS NVARCHAR(50)) = %s AND StatusCode = 2
    """, (code,))
    return cur.fetchone() is not None


def old_coords(cur, code, name):
    cur.execute("""
        SELECT TOP 1 Latitude, Longitude FROM MachineLookup
        WHERE CAST(MachineCode AS NVARCHAR(50)) = %s AND MachineName = %s
    """, (code, name))
    r = cur.fetchone()
    return (r[0], r[1]) if r else (None, None)


def derive_cutoff(cur, code):
    """Largest vend gap (> GAP_MIN_HOURS) for this machine. Returns
    (cutoff_ole, gap_hours, before_ole, after_ole) or (None, None, None, None)."""
    cur.execute("""
        SELECT CAST(mdt.[Date Time] AS FLOAT) AS ole
        FROM [MasterData Table] mdt
        WHERE CAST(mdt.[Machine Code] AS NVARCHAR(50)) = %s
          AND LEN(CAST(mdt.[Event Code] AS NVARCHAR(20))) = 6
          AND CAST(mdt.[Event Code] AS NVARCHAR(20)) LIKE '1%%'
        ORDER BY ole
    """, (code,))
    times = [row[0] for row in cur.fetchall() if row[0] is not None]
    best_gap = 0.0
    best_after = None
    best_before = None
    for a, b in zip(times, times[1:]):
        g = b - a
        if g > best_gap:
            best_gap, best_before, best_after = g, a, b
    gap_hours = best_gap * 24.0
    if gap_hours <= GAP_MIN_HOURS:
        return (None, None, None, None)
    # cutoff = first vend after the gap (inclusive boundary → new location)
    return (best_after, gap_hours, best_before, best_after)


# ── Preview ─────────────────────────────────────────────────────────────────────
def preview(cur, overrides):
    dups = find_duplicates(cur)
    lines = ["# Location-History Migration — PREVIEW (read-only)\n",
             f"_Generated {datetime.utcnow().isoformat()}Z_\n",
             f"\nMachine Codes with duplicate MachineLookup rows: **{len(dups)}**\n"]
    if not dups:
        lines.append("\nNo duplicates found — the doubling source is already clean.\n")
    else:
        lines.append("\n| MachineCode | Names on file | Keeper (current) | Proposed cutoff (SGT) | Gap (h) | Source |\n")
        lines.append("|---|---|---|---|---|---|\n")
        for code, names in dups.items():
            keeper = last_movement_to(cur, code)
            if keeper not in names:
                keeper = names[0]   # ordered active-first, freshest-topup-first
            old_names = [n for n in names if n != keeper]
            if code in overrides:
                cut_ole, src = overrides[code], "override"
                gaph = ""
            else:
                cut_ole, gaph, _, _ = derive_cutoff(cur, code)
                src = "vend-gap" if cut_ole else "NO GAP — needs manual"
                gaph = f"{gaph:.0f}" if gaph else ""
            cut_s = from_ole(cut_ole).strftime("%Y-%m-%d %H:%M") if cut_ole else "—"
            lines.append(f"| {code} | {' / '.join(names)} | {keeper} | {cut_s} | {gaph} | {src} |\n")
        lines.append("\n**Pre-cutoff vends → old name; cutoff onward → keeper (current).** "
                     "Rows marked 'NO GAP' need `--cutoffs <code>=<datetime>` before apply.\n")
    txt = "".join(lines)
    print(txt)
    with open("MIGRATION_PREVIEW.md", "w") as f:
        f.write(txt)


# ── Apply ───────────────────────────────────────────────────────────────────────
DDL = [
    """IF OBJECT_ID('dbo.MachineLocationHistory','U') IS NULL
       CREATE TABLE dbo.MachineLocationHistory (
           HistoryID INT IDENTITY(1,1) PRIMARY KEY,
           MachineCode NVARCHAR(50) NOT NULL,
           LocationName NVARCHAR(255) NOT NULL,
           Latitude FLOAT NULL, Longitude FLOAT NULL,
           ValidFromOle FLOAT NOT NULL, ValidToOle FLOAT NULL,
           Source NVARCHAR(30) NOT NULL, MovementOrderID INT NULL,
           CreatedAt DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME())""",
    """IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_MLH_Code_From')
       CREATE INDEX IX_MLH_Code_From ON dbo.MachineLocationHistory (MachineCode, ValidFromOle)""",
    """IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='UX_MLH_OpenInterval')
       CREATE UNIQUE INDEX UX_MLH_OpenInterval ON dbo.MachineLocationHistory (MachineCode)
       WHERE ValidToOle IS NULL""",
]

def dedupe(cur, dups):
    """Keep one row per duplicated code (keeper = last-move ToLocation, else
    active+freshest-topup). Merge freshest top-up onto keeper, delete losers."""
    removed = 0
    for code, names in dups.items():
        keeper = last_movement_to(cur, code)
        if keeper not in names:
            keeper = names[0]
        # freshest top-up across the group → keeper
        cur.execute("""
            SELECT TOP 1 LastTopupTimestamp, PreviousTopupTimestamp, CountBeforeLastTopup
            FROM MachineLookup
            WHERE CAST(MachineCode AS NVARCHAR(50)) = %s AND LastTopupTimestamp IS NOT NULL
            ORDER BY LastTopupTimestamp DESC
        """, (code,))
        fresh = cur.fetchone()
        if fresh:
            cur.execute("""
                UPDATE MachineLookup
                SET LastTopupTimestamp=%s, PreviousTopupTimestamp=%s, CountBeforeLastTopup=%s
                WHERE CAST(MachineCode AS NVARCHAR(50))=%s AND MachineName=%s
            """, (fresh[0], fresh[1], fresh[2], code, keeper))
        # delete every row for this code that is not the keeper-name; if duplicate
        # keeper-name rows exist, collapse them to one via ROW_NUMBER.
        cur.execute("""
            ;WITH r AS (
                SELECT ROW_NUMBER() OVER (
                    PARTITION BY CAST(MachineCode AS NVARCHAR(50)), MachineName
                    ORDER BY ISNULL(IsActive,1) DESC, ISNULL(LastTopupTimestamp,0) DESC) rn
                FROM MachineLookup
                WHERE CAST(MachineCode AS NVARCHAR(50)) = %s AND MachineName <> %s )
            DELETE FROM r
        """, (code, keeper))
        removed += cur.rowcount or 0
        cur.execute("""
            ;WITH r AS (
                SELECT ROW_NUMBER() OVER (
                    PARTITION BY CAST(MachineCode AS NVARCHAR(50))
                    ORDER BY ISNULL(IsActive,1) DESC, ISNULL(LastTopupTimestamp,0) DESC) rn
                FROM MachineLookup
                WHERE CAST(MachineCode AS NVARCHAR(50)) = %s )
            DELETE FROM r WHERE rn > 1
        """, (code,))
        removed += cur.rowcount or 0
        # Sanity: exactly one row must remain, and it must be the keeper name.
        # Any deviation → raise so apply() rolls back rather than corrupt prod.
        cur.execute("""SELECT COUNT(*), MAX(MachineName) FROM MachineLookup
                       WHERE CAST(MachineCode AS NVARCHAR(50)) = %s""", (code,))
        cnt, remaining = cur.fetchone()
        if cnt != 1 or remaining != keeper:
            raise RuntimeError(
                f"dedupe sanity failed for {code}: rows={cnt} name={remaining!r} keeper={keeper!r}")
    return removed


def ensure_unique_lookup(cur):
    cur.execute("""SELECT 1 FROM MachineLookup
                   GROUP BY MachineCode HAVING COUNT(*) > 1""")
    if cur.fetchone():
        raise RuntimeError("Duplicates remain after de-dup — aborting before UNIQUE index.")
    cur.execute("""IF NOT EXISTS (SELECT 1 FROM sys.indexes
                   WHERE object_id=OBJECT_ID('dbo.MachineLookup') AND name='UX_MachineLookup_MachineCode')
                   CREATE UNIQUE INDEX UX_MachineLookup_MachineCode ON dbo.MachineLookup (MachineCode)""")


def backfill(cur):
    """Build history from completed movements + current lookup. Only if empty."""
    cur.execute("SELECT COUNT(*) FROM MachineLocationHistory")
    if cur.fetchone()[0]:
        return "skipped (already populated)"
    cut = "CAST(CONVERT(datetime, DATEADD(HOUR,8,CompletedAt)) AS FLOAT) + 2.0"
    # seg0 + per-move
    cur.execute(f"""
        ;WITH mv AS (
            SELECT CAST(MachineCode AS NVARCHAR(50)) AS code, MovementType,
                   FromLocation, ToLocation, ToLat, ToLon, {cut} AS CutOle, MovementOrderID,
                   ROW_NUMBER() OVER (PARTITION BY CAST(MachineCode AS NVARCHAR(50)) ORDER BY CompletedAt) rn,
                   COUNT(*) OVER (PARTITION BY CAST(MachineCode AS NVARCHAR(50))) n
            FROM WO_MovementOrders WHERE StatusCode=2 AND CompletedAt IS NOT NULL ),
        mv2 AS ( SELECT *, LEAD(CutOle) OVER (PARTITION BY code ORDER BY CutOle) NextCutOle FROM mv )
        INSERT INTO MachineLocationHistory
            (MachineCode, LocationName, Latitude, Longitude, ValidFromOle, ValidToOle, Source, MovementOrderID)
        SELECT mv2.code, COALESCE(NULLIF(mv2.FromLocation,''), cur.MachineName, mv2.code),
               cur.Latitude, cur.Longitude, 0.0, mv2.CutOle, 'backfill-seg0', NULL
        FROM mv2 LEFT JOIN MachineLookup cur ON CAST(cur.MachineCode AS NVARCHAR(50))=mv2.code
        WHERE mv2.rn = 1
    """)
    cur.execute(f"""
        ;WITH mv AS (
            SELECT CAST(MachineCode AS NVARCHAR(50)) AS code, MovementType,
                   FromLocation, ToLocation, ToLat, ToLon, {cut} AS CutOle, MovementOrderID,
                   ROW_NUMBER() OVER (PARTITION BY CAST(MachineCode AS NVARCHAR(50)) ORDER BY CompletedAt) rn,
                   COUNT(*) OVER (PARTITION BY CAST(MachineCode AS NVARCHAR(50))) n
            FROM WO_MovementOrders WHERE StatusCode=2 AND CompletedAt IS NOT NULL ),
        mv2 AS ( SELECT *, LEAD(CutOle) OVER (PARTITION BY code ORDER BY CutOle) NextCutOle FROM mv )
        INSERT INTO MachineLocationHistory
            (MachineCode, LocationName, Latitude, Longitude, ValidFromOle, ValidToOle, Source, MovementOrderID)
        SELECT mv2.code,
            CASE WHEN mv2.rn=mv2.n AND mv2.MovementType='retrieve' THEN '(decommissioned)'
                 WHEN mv2.rn=mv2.n THEN COALESCE(cur.MachineName, mv2.ToLocation, mv2.code)
                 WHEN mv2.MovementType='retrieve' THEN '(decommissioned)'
                 ELSE COALESCE(NULLIF(mv2.ToLocation,''), cur.MachineName, mv2.code) END,
            CASE WHEN mv2.rn=mv2.n THEN cur.Latitude  ELSE mv2.ToLat END,
            CASE WHEN mv2.rn=mv2.n THEN cur.Longitude ELSE mv2.ToLon END,
            mv2.CutOle,
            CASE WHEN mv2.rn=mv2.n THEN NULL ELSE mv2.NextCutOle END,
            CASE WHEN mv2.rn=mv2.n THEN 'backfill-current' ELSE 'backfill-move' END,
            mv2.MovementOrderID
        FROM mv2 LEFT JOIN MachineLookup cur ON CAST(cur.MachineCode AS NVARCHAR(50))=mv2.code
    """)
    # no-movement machines → single open interval = current name
    cur.execute("""
        INSERT INTO MachineLocationHistory
            (MachineCode, LocationName, Latitude, Longitude, ValidFromOle, ValidToOle, Source)
        SELECT CAST(ml.MachineCode AS NVARCHAR(50)), ml.MachineName, ml.Latitude, ml.Longitude,
               0.0, NULL, 'backfill-current'
        FROM MachineLookup ml
        WHERE NOT EXISTS (SELECT 1 FROM MachineLocationHistory h
                          WHERE h.MachineCode = CAST(ml.MachineCode AS NVARCHAR(50)))
    """)
    cur.execute("SELECT COUNT(*) FROM MachineLocationHistory")
    return f"{cur.fetchone()[0]} intervals"


def apply_corrective(cur, corrective):
    """For each admin-edited dup machine: split the open interval at the cutoff so
    pre-cutoff vends read the OLD name.
    corrective = {code: (old_name, cutoff_ole, old_lat, old_lon)}."""
    applied = []
    for code, (old_name, cut_ole, old_lat, old_lon) in corrective.items():
        # find the current open interval (the keeper/current name from backfill)
        cur.execute("""
            SELECT TOP 1 HistoryID, LocationName, ValidFromOle
            FROM MachineLocationHistory
            WHERE MachineCode=%s AND ValidToOle IS NULL
            ORDER BY ValidFromOle DESC
        """, (code,))
        row = cur.fetchone()
        if not row:
            applied.append((code, "no open interval — skipped"))
            continue
        hid, cur_name, vfrom = row
        if cut_ole <= (vfrom or 0):
            applied.append((code, "cutoff <= interval start — skipped"))
            continue
        # shift open interval to start at cutoff, insert [start, cutoff) = old name
        cur.execute("UPDATE MachineLocationHistory SET ValidFromOle=%s WHERE HistoryID=%s",
                    (cut_ole, hid))
        cur.execute("""
            INSERT INTO MachineLocationHistory
                (MachineCode, LocationName, Latitude, Longitude, ValidFromOle, ValidToOle, Source)
            VALUES (%s, %s, %s, %s, %s, %s, 'corrective')
        """, (code, old_name, old_lat, old_lon, vfrom if vfrom is not None else 0.0, cut_ole))
        applied.append((code, f"{old_name} until {from_ole(cut_ole):%Y-%m-%d %H:%M} → {cur_name}"))
    return applied


def verify(cur):
    out = []
    cur.execute("SELECT COUNT(*) FROM (SELECT MachineCode FROM MachineLookup GROUP BY MachineCode HAVING COUNT(*)>1) z")
    out.append(("Duplicate MachineCodes remaining (want 0)", cur.fetchone()[0]))
    cur.execute("SELECT COUNT(*) FROM (SELECT MachineCode FROM MachineLocationHistory WHERE ValidToOle IS NULL GROUP BY MachineCode HAVING COUNT(*)>1) z")
    out.append(("Machines with >1 open interval (want 0)", cur.fetchone()[0]))
    cur.execute("""SELECT COUNT(*) FROM MachineLocationHistory a JOIN MachineLocationHistory b
        ON a.MachineCode=b.MachineCode AND a.HistoryID<b.HistoryID
        AND a.ValidFromOle < ISNULL(b.ValidToOle,1e9) AND b.ValidFromOle < ISNULL(a.ValidToOle,1e9)""")
    out.append(("Overlapping intervals (want 0)", cur.fetchone()[0]))
    return out


def apply(conn, cur, overrides):
    dups = find_duplicates(cur)
    # capture old names + cutoffs + coords BEFORE de-dup deletes the old rows.
    # ONLY for admin-edited moves (no completed movement record). Machines with a
    # real movement are handled by backfill — a gap-based corrective would corrupt
    # that timeline, so we skip them here.
    corrective = {}
    for code, names in dups.items():
        if has_completed_movement(cur, code):
            continue
        keeper = names[0]
        old = [n for n in names if n != keeper]
        cut_ole = overrides.get(code)
        if cut_ole is None:
            cut_ole, _, _, _ = derive_cutoff(cur, code)
        if old and cut_ole:
            old_lat, old_lon = old_coords(cur, code, old[0])
            corrective[code] = (old[0], cut_ole, old_lat, old_lon)

    report = ["# Location-History Migration — RESULT\n",
              f"_Applied {datetime.utcnow().isoformat()}Z_\n"]
    try:
        for stmt in DDL:
            cur.execute(stmt)
        removed = dedupe(cur, dups)
        ensure_unique_lookup(cur)
        bf = backfill(cur)
        corr = apply_corrective(cur, corrective)
        checks = verify(cur)
        bad = [c for c in checks if c[1] != 0]
        if bad:
            raise RuntimeError(f"Verification failed: {bad}")
        conn.commit()
        report.append(f"\n- Duplicate rows removed: **{removed}**")
        report.append(f"\n- Backfill: **{bf}**")
        report.append(f"\n- Duplicated codes handled: **{len(dups)}**")
        report.append("\n\n## Corrective cutoffs\n")
        report += [f"- `{c}`: {msg}\n" for c, msg in corr] or ["- (none)\n"]
        report.append("\n## Verification\n")
        report += [f"- {label}: {val}\n" for label, val in checks]
        report.append("\n**COMMITTED OK.**\n")
    except Exception as e:
        conn.rollback()
        report.append(f"\n**ROLLED BACK — {e}**\n")
        txt = "".join(report); print(txt)
        open("MIGRATION_RESULT.md", "w").write(txt)
        sys.exit(1)
    txt = "".join(report); print(txt)
    open("MIGRATION_RESULT.md", "w").write(txt)


def parse_overrides(s):
    out = {}
    if not s:
        return out
    for part in s.split(","):
        if "=" in part:
            code, dt = part.split("=", 1)
            out[code.strip()] = parse_sgt(dt)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["preview", "apply"], required=True)
    ap.add_argument("--cutoffs", default="", help="code=YYYY-MM-DD[ HH:MM] comma-separated (SGT)")
    args = ap.parse_args()
    overrides = parse_overrides(args.cutoffs)

    conn = connect()
    cur = conn.cursor()
    if args.mode == "preview":
        preview(cur, overrides)
    else:
        apply(conn, cur, overrides)
    conn.close()


if __name__ == "__main__":
    main()
