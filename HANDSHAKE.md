# Vending Dashboard — Agent Handshake

> **Purpose:** Context file for a second agent continuing development on this codebase.  
> **Owner:** Yash (Kopi Near Me Pte Ltd)  
> **Last updated:** 2026-05-06

---

## 1. Project overview

A private Flask web dashboard for KNM's vending machine fleet (~100 machines, Singapore).  
Deployed on **Azure App Service** (Linux), backed by **Azure SQL**.  
Auth via **Azure Easy Auth (AAD)** — the app itself never handles passwords.

**Live URL:** `https://knmdispenseviewer-eqdjbscahtfufxfj.southeastasia-01.azurewebsites.net`  
**Repo:** `https://github.com/yash008-VHPL/knm-vending.git` (private)  
**Branch:** `main` — App Service deploys automatically on push via Kudu SCM.

---

## 2. Tech stack

| Layer | Choice |
|---|---|
| Backend | Python 3.12 · Flask · Gunicorn |
| DB driver | `pymssql` (Azure SQL / SQL Server) |
| Frontend | Vanilla JS + CSS (no build step) · Jinja2 templates |
| Map | Leaflet.js (CDN) + OpenStreetMap tiles |
| Auth | Azure Easy Auth (AAD roles) |
| Hosting | Azure App Service (Linux) |
| DB | Azure SQL — server `machineserver.database.windows.net` |
| CI/CD | GitHub Actions (NETS reconciliation only) |

**No npm, no webpack, no React.** Everything is in `app.py` + `templates/index.html`.

---

## 3. Files you care about

```
app.py                  Main Flask app — all API routes (~1,280 lines)
config.py               DB creds + DEV_USER_EMAIL / DEV_ROLE for local dev
templates/index.html    Single-page dashboard (~1,600 lines, all JS inline)
nets_reconcile.py       Monthly NETS vs DB reconciliation script (standalone)
.github/workflows/      nets-reconciliation.yml — runs on 2nd of each month
requirements.txt        flask, pymssql, gunicorn (keep it minimal)
seed_locations.py       One-off seed script — already ran, don't re-run
```

---

## 4. Database schema (Azure SQL)

### `MachineLookup` — master list of machines

| Column | Type | Notes |
|---|---|---|
| `MachineCode` | VARCHAR | Primary identifier. Used as FK everywhere. |
| `MachineName` | VARCHAR | Display name (e.g. "Ubi Tech Park A") |
| `Latitude` | FLOAT NULL | Added by `init_db()` on startup |
| `Longitude` | FLOAT NULL | Added by `init_db()` on startup |
| `LastTopupTimestamp` | FLOAT NULL | OLE date — added by `init_db()` |
| `PreviousTopupTimestamp` | FLOAT NULL | OLE date — added by `init_db()` |
| `CountBeforeLastTopup` | INT NULL | Added by `init_db()` |

### `[MasterData Table]` — every event from every machine

| Column | Type | Notes |
|---|---|---|
| `[Date Time]` | stored as OLE float (FLOAT) | See OLE date section below |
| `[Machine Code]` | matches `MachineLookup.MachineCode` |
| `[Event Code]` | int-like — 6-digit codes starting with `1` = vend events |
| `[Event Name]` | raw description |

**Critical:** The table name has a space — always wrap in `[MasterData Table]`.

### `MasterCode` — event code → beverage name mapping

| Column | Notes |
|---|---|
| `ItemCode` | matches `[Event Code]` |
| `EventName` | beverage name OR raw code — see SKU fix below |

**Gotcha:** Multiple `EventName` rows per `ItemCode` exist. Always deduplicate before joining:
```sql
INNER JOIN (
    SELECT ItemCode,
        COALESCE(
            MIN(CASE WHEN EventName LIKE '%[a-zA-Z]%' THEN EventName END),
            MIN(EventName)
        ) AS EventName
    FROM MasterCode
    GROUP BY ItemCode
) mc ON mdt.[Event Code] = mc.ItemCode
```
This prefers descriptive names (containing letters) over raw numeric codes.  
**Never use a bare `JOIN MasterCode`** — you'll get fan-out and double/triple counts.

---

## 5. OLE date format

`[Date Time]` is stored as an OLE Automation Date (a float, epoch = 1899-12-30).

```python
OLE_EPOCH = datetime(1899, 12, 30)

def to_ole_date(dt_obj):
    delta = dt_obj - OLE_EPOCH
    return delta.days + (delta.seconds + delta.microseconds / 1e6) / 86400.0

def from_ole_date(ole_value):
    return OLE_EPOCH + timedelta(days=float(ole_value))
```

All date range queries use:
```sql
WHERE CAST(mdt.[Date Time] AS FLOAT) >= {start_ole}
  AND CAST(mdt.[Date Time] AS FLOAT) <= {end_ole}
```

---

## 6. Auth system

**Azure Easy Auth (AAD)** intercepts all requests before Flask sees them.  
The AAD token is forwarded as the `X-MS-CLIENT-PRINCIPAL` header (base64 JSON).

### Roles (defined in Azure App Registration → App Roles)

| Role | Access |
|---|---|
| `admin` | Full access — all tabs including Locations edit/delete |
| `dispatch` | Locations (read+edit, no delete) + Dispatch tab |
| `sales` | Sales + Messages + Topups (read-only) |

### Auth decorators in app.py

```python
@login_required          # any authenticated user with a valid role
@admin_required          # admin only
@dispatch_or_admin_required  # admin or dispatch
```

### Local development

Set in `config.py` (never commit real values):
```python
DEV_USER_EMAIL = "ybhawe@kopinearme.com"  # simulates logged-in user
DEV_ROLE       = "admin"                   # simulates role
```
Leave both as `""` before deploying.

### Important: Easy Auth blocks all unauthenticated requests

The Azure App Service's Easy Auth intercepts requests **before Flask** — including to API endpoints. This means:
- You **cannot** call the Flask API from external scripts without either being in-browser (AAD cookie) or adding the App Service to the unauthenticated passlist.
- The `nets_reconcile.py` script bypasses this by connecting **directly to Azure SQL** via `pymssql` (not via the Flask API). GitHub Actions is allowed through the SQL firewall via the "Allow Azure services" rule.

---

## 7. API endpoints (complete list)

### Public (login_required)
| Method | Route | Description |
|---|---|---|
| GET | `/` | Main dashboard (renders index.html) |
| GET | `/api/locations` | All machines: `[{name, code, lat, lon}]` |
| GET | `/api/dispenses` | Sales summary grouped by SKU. Params: `start`, `end`, `machine` |
| GET | `/api/transactions` | Individual vend events, newest first. Params: `start`, `end`, `machine`. Capped 2,000 rows. |
| GET | `/api/messages` | Event log. Params: `start`, `end`, `type` (error/exception/event/message), `machine` |
| GET | `/api/topups` | All machines with last-topup and vend counts |
| POST | `/api/topups/<code>` | Log a topup. Body: `{timestamp: "YYYY-MM-DD HH:MM"}` |
| DELETE | `/api/topups/<code>` | Undo last topup |
| GET | `/api/heartbeat` | Fleet comms status per machine (green/yellow/red) |

### Admin/Dispatch
| Method | Route | Description |
|---|---|---|
| POST | `/api/admin/locations` | Add a new location |
| PUT | `/api/admin/locations/<code>` | Update location. Body: `{name, new_code?, lat, lon}` |
| DELETE | `/api/admin/locations/<code>` | **admin only** — delete location |
| POST | `/api/dispatch/plan` | Route planning. Body: `{machine_codes: [], num_drivers: N}` |

### Internal (used by nets_reconcile.py — now bypassed in favour of direct SQL)
| Method | Route | Description |
|---|---|---|
| GET | `/api/admin/heartbeat-analysis` | Off-hours gap analysis (used to calibrate threshold) |

---

## 8. Dashboard tabs & features

### Sales tab
- Location + date/time range filter
- **Summary view** (default): SKU table with count, %, bar chart. Includes Item Code column.
- **Transactions view** (toggle): Individual vend events — Date, Time, Item, Machine. Newest first. Capped 2,000.
- **Export CSV** button appears in both views — client-side, reads rendered table.

### Messages tab
- Filter by date range + machine + message type (error / exception / event / message)
- Returns raw events from `[MasterData Table]` (no vend filter)

### Topups tab
- Log / undo topups per machine (stored in `MachineLookup.LastTopupTimestamp`)
- Table shows: last topup date, vends before that topup, vends since

### Heartbeat tab (💓)
- Shows every machine as a colour-coded card: 💚 / 💛 / ❤️
- Threshold: **225 minutes** silence = red. Calibrated from fleet's off-hours p95 gap (180 min) + 25% headroom.
- Yellow = recent error/exception event (within 60 min)
- Status badges are clickable to filter grid by colour
- Auto-refreshes every 2 minutes

### Locations tab (admin + dispatch)
- Table of all machines with inline edit (name, machine code, lat/lon)
- **Delete button** (admin only) with confirm dialog
- Add form at bottom (admin only)

### Dispatch tab (admin + dispatch)
- Checkbox list of locations, sort by name/topup/vends
- Route planner: splits selected machines across N drivers (nearest-neighbour TSP from depot)
- Interactive Leaflet map with coloured polylines per route
- Route editor: reorder stops (▲▼), transfer stops between drivers — map updates live
- Google Maps links per route (split across 2 if >10 stops)
- Depot coordinates hardcoded: `1.3407711524195856, 103.8896748329062` (the factory)

---

## 9. Vend event filter (critical — use this everywhere)

Only 6-digit event codes starting with `1` are vend events:
```sql
AND LEN(CAST(mdt.[Event Code] AS NVARCHAR(20))) = 6
AND CAST(mdt.[Event Code] AS NVARCHAR(20)) LIKE '1%'
```
The `LEN = 6` requirement is mandatory — without it, the single-digit code `1` ("success") gets included and massively inflates counts.

---

## 10. NETS reconciliation (separate concern)

`nets_reconcile.py` is a standalone script that runs monthly via GitHub Actions.

- Uses **Playwright** (headless Chromium) to log into `https://autwp.auresys.solutions` and download a CSV of NETS payment terminal transactions.
- Connects **directly to Azure SQL** via `pymssql` (bypasses Flask/Easy Auth entirely).
- Compares NETS transaction counts vs DB vend counts per location.
- Posts a 🔴/🟡/✅ report to a Teams webhook.
- Triggered: 2nd of each month at 09:00 SGT, or manually via GitHub Actions UI.
- GitHub Actions secrets needed: `NETS_USERNAME`, `NETS_PASSWORD`, `DB_USER`, `DB_PASSWORD`, `TEAMS_WEBHOOK_URL`.
- `NETS_TO_DB` dict in the script maps NETS outlet names to DB `MachineName` values (50+ locations — keep this in sync when adding machines).

---

## 11. What Yash built vs what the agent built

### Yash built (before this session series)
- Initial Flask app skeleton and Azure App Service deployment pipeline
- `MachineLookup` table schema and initial seed data
- Basic sales query and messages query
- Topup logging feature
- Dispatch route planning algorithm (nearest-neighbour TSP)
- Google Maps URL generation for routes
- Initial location seed (`seed_locations.py`)

### Agent built (across this session)
- **Heartbeat tab** — `/api/heartbeat`, `/api/admin/heartbeat-analysis`, fleet grid with colour badges, 225-min threshold calibration from off-hours gap analysis
- **Sales double-count fix** — deduplicated `MasterCode` JOIN
- **Vend filter fix** — `LEN=6 AND LIKE '1%'` to exclude spurious codes
- **SKU name fix** — `COALESCE(MIN(letters-only), MIN(all))` to prefer descriptive names
- **Transaction log** — `/api/transactions` endpoint + Transactions view in Sales tab
- **CSV export** — client-side, both Summary and Transactions views
- **Location delete** — `DELETE /api/admin/locations/<code>` + Delete button in UI
- **Machine code editing** — `PUT` now accepts `new_code` with duplicate check
- **Leaflet map** in Dispatch tab with route polylines and numbered stop markers
- **Route editor** — reorder/transfer stops with live map update
- **`nets_reconcile.py`** — full automated reconciliation script
- **GitHub Actions workflow** — `.github/workflows/nets-reconciliation.yml`

---

## 12. Known issues & gotchas

1. **`[Date Time]` is not a proper SQL datetime** — it's stored as OLE float. All date comparisons must cast to float and compare against computed OLE values. Never use `WHERE [Date Time] BETWEEN ...` directly.

2. **MasterCode has duplicates** — same `ItemCode`, multiple `EventName` rows. Always deduplicate before joining (see §4).

3. **Table name has a space** — always `[MasterData Table]`, never `MasterData`.

4. **Azure Easy Auth blocks unauthenticated API calls** — external scripts/tools cannot hit the Flask API directly. Use direct `pymssql` connections for any automation.

5. **`init_db()` runs on every startup** — it's idempotent (uses `IF NOT EXISTS`), but the `MachineLookup` schema must remain backward compatible.

6. **Dispatch tab depot coordinates** are hardcoded in `index.html` (`DEPOT_LAT`, `DEPOT_LON`). Update these if the factory moves.

7. **Heartbeat threshold** (`HEARTBEAT_THRESHOLD_MINUTES = 225`) is set in `app.py`. If the fleet communication pattern changes (new machines, different polling intervals), re-run the gap analysis via `/api/admin/heartbeat-analysis`.

8. **NETS_TO_DB mapping** in `nets_reconcile.py` is manually maintained. When adding or renaming a location, update both `MachineLookup` in the DB and this dict.

9. **Transaction log is capped at 2,000 rows** — by design (SQL `TOP 2000`). Narrow the date range to see full data. A "Results capped" warning appears in the UI.

10. **GX-10 MCP server** — the team has an ASUS GX-10 AI workstation (Tailscale `100.90.254.13`) running two MCP servers:
    - Port 8080: FastMCP 3.2.4 (`/home/workmonkey/mcp-server/server.py`) — for Claude.ai cowork sessions (protocol `2025-03-26`)
    - Port 8081: Custom raw SSE server (`/home/workmonkey/mcp-server/server_compat.py`) — for Claude Code CLI (protocol `2024-11-05`)
    The compat server starts via `@reboot` crontab (no sudo). Claude Code's `~/.claude.json` points to port 8081.

---

## 13. Local development setup

```bash
cd "/Users/yash008/Documents/Coding/Coding/KNM Apps/vending-dashboard"
source .venv/bin/activate
# Set DEV_USER_EMAIL and DEV_ROLE in config.py
python app.py
# → http://localhost:5000
```

`config.py` is gitignored and holds the real DB credentials. Never commit it.

---

## 14. Deployment

Push to `main` → Kudu builds automatically → App Service restarts.  
Build time: ~2–3 minutes. No manual steps needed.

**Yash's convention:** push during off-hours (21:00 SGT) when day shift has left and night shift hasn't started, to avoid disrupting live users.

```bash
cd "/Users/yash008/Documents/Coding/Coding/KNM Apps/vending-dashboard"
git push origin main
```
