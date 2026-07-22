# KNM Vending — Session Handoff (2026-07)

Drag this into a new session to restore context on the location-history work.

## App / deploy
- Flask + Azure SQL app: `Coding/KNM Apps/vending-dashboard`. Repo: `github.com/yash008-VHPL/knm-vending`.
- Deploy = `git push origin main` → Azure Web App **KNMDispenseViewer** auto-builds.
- Live: `https://knmdispenseviewer-eqdjbscahtfufxfj.southeastasia-01.azurewebsites.net` (Azure AD login).
- DB: Azure SQL "Machine DispensedDrink". Key tables: `MachineLookup` (current location, 1 row/machine),
  `[MasterData Table]` (vends), `MachineLocationHistory` (dated stays), `WO_*` (work orders/movements/log).
- Claude constraints: sandbox can't reach the DB (firewall) or `git push` (no creds). Claude edits files +
  gives SQL for the **Azure Query Editor** and git commands for the user to run. init_workorders_db() runs on
  startup and auto-creates WO_* tables, so new tables added to its list deploy automatically.

## Problem fixed
Relocating a machine had no sharp cutoff — vends doubled under the old location name (a JOIN fan-out from
duplicate `MachineLookup` rows), and location was resolved from the machine's *current* name only.

## Solution (all deployed)
1. **De-dupe + `UNIQUE(MachineCode)`** on MachineLookup → kills the doubling; `add_location` is now UPSERT.
2. **`MachineLocationHistory`** effective-dated table: `(MachineCode, LocationName, Lat, Lon, ValidFromOle,
   ValidToOle NULL, Source, MovementOrderID)`. One OPEN interval/machine (filtered unique index).
3. **Per-vend resolution**: a vend reads the interval whose `ValidFromOle <= vend < ValidToOle` contains it.
   Applied to /api/transactions, /api/dispenses, nets_reconcile, vend-counts. Location filter is now by NAME.
4. **Machine History card**: "where it's been" timeline + **Record a past move** (previous location + date →
   splits history). Movements tab still does live moves (Complete stamps now + updates current location).
5. **Deletes**: movement-order Delete UNWINDS a completed move (fail-closed: latest move only, refuses if
   history hand-edited); recorded moves have a × to remove. **All deletes snapshot to `WO_DeletedLog`**
   (Admin → "Deleted log" tab; one-click restore for recorded moves, snapshot-only for the rest).
6. UI: uniform **40px** field heights (select/date/time/search align).

## Time-unit gotcha (important)
Vend `[Date Time]` = OLE Automation float in **Singapore wall-clock**, epoch 1899-12-30. Convert a UTC
DATETIME2 via `CAST(CONVERT(datetime, DATEADD(HOUR,8,x)) AS FLOAT) + 2.0`. Python: `to_ole_date(utcnow()+8h)`.

## Open items
1. **55120031** moved Kranji Camp Blk 808 → Inzy Group (admin relabel, no movement record). De-duped to
   "Inzy Group" but history NOT yet split. Find the move date (old `WorkOrders_legacy_2026_06_03` table /
   knm-workorders app — CreatedAt/CompletedAt/Description), then use "Record a past move" (prev = Kranji, that date).
2. One-click **restore** wired for recorded-moves only; other entity types are snapshot-only (reversible
   manually from the JSON snapshot). Optional: extend restore to location/top-up.

## Working rules the user expects
Plan/QC before execute; ALWAYS fire a second agent to check points of failure before shipping a hard change;
terse replies; confirm before assuming; name = Yash Bhawe (Yashodhan Bhawe for legal).
