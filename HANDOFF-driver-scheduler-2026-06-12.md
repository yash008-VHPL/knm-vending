# Handoff — Driver Scheduler

> **Created:** 2026-06-12 by previous agent.
> **Owner:** Yash Bhawe (Kopi Near Me Pte Ltd).
> **Purpose:** Self-contained brief for a new agent session to build the **Driver Scheduler** in a separate working tab. Read this before touching code.

---

## 1. What you are building

A new **Driver Scheduler** for the maintenance manager / field manager / admin.

**Inputs the manager has today:**
- All `open` (StatusCode 0 or 1) **Service Work Orders** (`WO_JobOrders`).
- All `open` (Status <> 'completed') **Delivery Work Orders** (`WO_DeliveryOrders`).
- All scheduled **Movement Orders** (`WO_MovementOrders`).
- The pool of available drivers (users with AAD `Operator` role — pulled via `/api/wo/technicians`).
- Machine geo (lat/lon on `MachineLookup`).

**Outputs the scheduler must produce:**
- For each upcoming day (week ahead), an assignment plan: which driver visits which machines in what order.
- Each plan item ties to one of the open WOs by setting `AssignedTo` on it.
- Manager can adjust mid-shift; operator's "Work Orders" tab (already built) automatically reflects the current `AssignedTo`.

The scheduler is **manager-facing only**. The downstream operator UI for executing a route is already done — operator opens `Work Orders` tab and sees a colour-coded list of machines assigned to them, then taps each to file the standardized Work Order document (services + delivery + signature). Don't rebuild that.

---

## 2. State of the codebase as of 2026-06-12

Repo: `https://github.com/yash008-VHPL/knm-vending.git` — branch `main` → auto-deploys to Azure App Service (`knmdispenseviewer-eqdjbscahtfufxfj.southeastasia-01.azurewebsites.net`).

Stack: Python 3.12 · Flask · Gunicorn · `pymssql` · Azure SQL (`Machine DispensedDrink` database) · Microsoft Graph (delegated app for SharePoint + AAD lookup) · ReportLab for PDF generation. Frontend: vanilla JS + Jinja2 templates, no build step.

**Active project files**

```
app.py                             Main Flask app (~1,500 lines): auth, sales, heartbeat, locations, dispatch
workorders.py                      Work Orders blueprint (/api/wo/*) — ~3,000 lines covering
                                   complaints, joborders, movement orders, delivery orders, KB,
                                   visit sessions, technicians (Graph), admin cleanup, etc.
sharepoint_helper.py               MSAL + Graph: SP upload/download + list_users_by_role for tech dropdown
config.py                          DB creds + DEV_USER_EMAIL/DEV_ROLE
templates/index.html               Tab layout + Locations + Dispatch tab (route planner already exists)
templates/_operator_work_order_tab.html  NEW: operator's PDF-template Work Order flow
templates/_tech_support_tab.html   Manager triage / WO creation + review
templates/_fault_report_tab.html   CS rep complaint submission
templates/_kb_admin_tab.html       KB CAR-format CRUD
templates/_movements_tab.html      Manager movement orders create + driver complete
templates/_machine_history_tab.html
templates/_historic_locations_tab.html
templates/_admin_delete_tab.html   Admin cascade-delete (test cleanup)
```

**Past handoff files**
- `HANDSHAKE.md` — pre-2026-06-03 codebase state (heartbeat, sales, dispatch). Useful for OLE date format, MasterCode dedup quirks, and Azure auth specifics.
- `FAULT_TECH_FIELDS_AUDIT.md` — schema decisions during fault-report build.
- `CUTOVER_PLAN.md` — most recent cutover plan + cleanup TODOs.
- `ERROR_CODE_TAXONOMY.md` — placeholder; codes are catch-all 6xxxxx / 8xxxxx for now.

---

## 3. Relevant data model (read this before designing the scheduler schema)

### Existing tables you will read from

| Table | Purpose | Key cols for scheduler |
|---|---|---|
| `MachineLookup` | All machines | `MachineCode` (PK), `MachineName`, `Latitude`, `Longitude`, `IsActive`, `LastTopupTimestamp` (OLE float — see §4) |
| `WO_JobOrders` | Service work orders | `JobOrderID`, `MachineCode`, `AssignedTo` (email), `PriorityCode` (TINYINT 0-2), `StatusCode` (TINYINT 0-3), `EventCode` (8xxxxx), `CreatedAt` |
| `WO_DeliveryOrders` | Delivery work orders (top-up) | `DeliveryOrderID`, `MachineCode`, `AssignedTo`, `Priority` (text low/normal/high, NOT migrated to TINYINT yet — flag), `Status` (text 'open'/'completed'), `CreatedAt`. Note: delivery WO logic is being redesigned by Yash; expect this table to grow `ScheduledDate`, `WindowStart`, `WindowEnd`, `EstimatedFullness` columns soon. |
| `WO_MovementOrders` | Machine deploy / relocate / retrieve | `MovementOrderID`, `MovementType` ('deploy'/'relocate'/'retrieve'), `MachineCode`, `AssignedTo`, `StatusCode` 0-2, `FromLocation`, `ToLocation` |
| `WO_DeliveryOrderLines` | Per-item delivery qty (NEW 2026-06-12) | `LineID`, `DeliveryOrderID`, `ItemID`, `QtyOrdered`, `QtyDelivered` |
| `WO_DeliveryItems` | Admin-editable item config (17 items seeded from KNM PDF template) | `ItemID`, `Name`, `Unit`, `Content`, `SortOrder`, `IsActive` |
| `WO_VisitSessions` | The signed Work Order document per operator visit | `VisitID`, `DisplayID` (`KNM-VIS-NNNN-YYMM`), `OperatorEmail`, `VisitDate`, `Status` ('draft'/'submitted'/'signed'/'pending_email_signature'), `PDFSPItemID`, `SignedAt` |
| `WO_VisitSession_JobOrders` | Junction — which JobOrders a visit covered | `VisitID`, `JobOrderID` |

### Existing routes you can call

- `GET /api/wo/operator/locations` — operator-side: machines with open work assigned to me, colour-coded red/blue/both. **The scheduler must set `AssignedTo` on the WOs so this endpoint serves the right list per driver.**
- `GET /api/wo/technicians` — Graph-backed list of users with `Operator` role assigned in AAD. Use this for the driver picker. Cache: 10 min; bust via `POST /api/wo/technicians/refresh`.
- `GET /api/wo/assignment/delivery-candidates` — machines sorted by vends-since-last-topup (manager's planning aid).
- `GET /api/wo/assignment/joborder-candidates` — machines sorted by open-complaint count.
- `GET /api/wo/manager/equipment-log?machine_code=X` — full history for one machine.
- `POST /api/wo/joborders/<jid>/assign` body `{assigned_to: email, priority: 0..2}` — already implemented; you'll call this when applying a schedule.
- `POST /api/wo/deliveryorders/<did>/assign` body `{assigned_to: email, priority: 'low'/'normal'/'high'}`.
- `POST /api/wo/movementorders` — create new movement order (manager).
- **Existing Dispatch tab route planner** in `app.py` (`/api/dispatch/plan`) does nearest-neighbour TSP from a hard-coded depot at `1.3407711524195856, 103.8896748329062`. Useful as a starting algorithm; should be moved into the scheduler service and made more sophisticated (time windows, mixed pickups).

### Roles & gating

AAD app roles in use: `admin`, `dispatch`, `sales`, `operator`, `field_manager`, `customer_service`. The scheduler tab should be visible to **admin + field_manager + dispatch**. Operators don't see it (they see `Work Orders` tab — already gated). Multi-role users use the header role switcher (`/api/switch-role/<role>` sets `knm_active_role` cookie; only honored if it matches an actual AAD claim).

---

## 4. Gotchas you must know about

1. **OLE Automation dates.** Vending-machine event timestamps in `[MasterData Table]` and `MachineLookup.LastTopupTimestamp` are stored as **OLE float** (epoch 1899-12-30). Use `to_ole_date()` / `from_ole_date()` in `app.py` / `workorders.py`. *Never* compare with SQL `DATETIME` operators directly.

2. **Status type mismatch between tables.** `WO_JobOrders.StatusCode` is **TINYINT 0-3** (0 assigned, 1 needs_assistance, 2 pending_review, 3 closed). `WO_DeliveryOrders.Status` is still a **NVARCHAR text** field ('open'/'completed'). Movement orders use `StatusCode` TINYINT 0-2. Be defensive when joining or filtering.

3. **`Priority` columns** vary: `WO_JobOrders.PriorityCode` (TINYINT 0-2), `WO_DeliveryOrders.Priority` (text 'low'/'normal'/'high'), `WO_MovementOrders` has no priority column. Normalize in your scheduler.

4. **SharePoint helpers.** `sharepoint_helper.py` exposes `upload_bytes(kind, display_id, year, month, file_name, data, content_type)` where `kind` must be `'complaint'` or `'workorder'`. Schedule PDFs (route sheets) should use `kind='workorder'`.

5. **Multi-role users.** Don't assume `get_role()` is stable across requests for an admin who toggled role. Read the active role from `get_role()` only; if you need the union of capabilities use `get_all_roles()`.

6. **App is already on Azure with full env vars.** `MS_TENANT_ID`, `MS_CLIENT_ID`, `MS_CLIENT_SECRET`, `MS_SITE_ID`, `MS_EASYAUTH_SP_OBJECT_ID`, `MS_OPERATOR_ROLE_ID`. Don't ask Yash to re-set these.

7. **Yash's working style** (from `.claude/CLAUDE.md`): terse, ready-to-run deliverables, plan before execute, never edit files in place (copy with suffix or commit-by-commit), never assume names (ask), use `Yash Bhawe` casually / `Yashodhan Bhawe` for legal docs. Push commits explicitly. Off-hours pushes preferred (21:00 SGT).

8. **HANDSHAKE.md §10** documents the GX-10 MCP server (Tailscale `100.90.254.13`) for offloading bulk compute. The scheduler's optimization step (TSP / VRP with constraints) may benefit from a GX-10 background job rather than blocking the request thread.

---

## 5. Open design questions to settle with Yash before coding

1. **Time windows.** Does each WO have a required service window (e.g., delivery must happen between 09:00–14:00)? If so, this needs a new column on `WO_DeliveryOrders` (`WindowStart`, `WindowEnd`). Yash is redesigning delivery WO logic post-COO discussion — coordinate.
2. **Driver shifts.** Are there fixed shift patterns per driver (e.g., 08:00–17:00) or variable? Store in a new `WO_DriverShifts` table or pull from AAD attributes?
3. **Travel-time model.** Start with straight-line distance / 30 km/h. Upgrade later to a real distance matrix (Google Distance Matrix, OSRM)? Singapore is small — straight-line is acceptable for V1.
4. **Constraints.** Capacity (driver vehicle volume — count of jerry cans / cup packs)? Skills (only some operators can do `Machine Installation or Replacement`)? Mandatory pair-visits (e.g., service + delivery together)?
5. **Re-optimization frequency.** Static morning plan, or rolling re-optimization mid-shift when (a) new WO arrives, (b) a driver gets stuck, (c) customer cancels?
6. **Conflict policy.** If a driver is in the middle of a route and manager reassigns one of their WOs, do we (a) keep current visit, push remaining back to the optimizer; (b) hard re-route immediately; (c) require manager confirm? Recommend (a).
7. **Output artefacts.** Just an in-app screen, or also a printable / PDF route sheet (mirroring the paper Work Order template) for each driver per day? ReportLab is already wired up (`workorders.py` → `_build_visit_pdf`). Reuse pattern.
8. **Algorithm choice.** Pre-alpha can use greedy nearest-neighbour + 2-opt local search (the Dispatch tab does this for top-up routing). For V2, consider Google OR-Tools VRP. OR-Tools is heavy — host on GX-10 if so.

Ask Yash 1–4 first; the rest can be defaulted.

---

## 6. Suggested implementation steps (pick & choose; this is not a contract)

1. **Schema additions** (one ALTER per session, run them in `Machine DispensedDrink`):
   - `WO_DriverShifts` (DriverEmail, DayOfWeek or specific Date, ShiftStart, ShiftEnd, IsActive).
   - Optional `WO_DeliveryOrders` extras: `ScheduledDate`, `WindowStart`, `WindowEnd`, `RouteSeq` (operator's stop number in their route).
   - New `WO_ScheduleRuns` table (planning history) — `RunID`, `RunAt`, `RunBy`, `RunHorizon` (date range), `Method` ('manual'/'auto'/'mixed'), `JSON` (snapshot of inputs/outputs).
2. **Backend** — new Blueprint or extend `workorders.py`:
   - `POST /api/wo/scheduler/plan` body `{date, drivers: [...], options: {...}}` → returns a proposed plan (does NOT save).
   - `POST /api/wo/scheduler/apply` body `{plan}` → applies assignments to the relevant WOs (idempotent).
   - `GET /api/wo/scheduler/current?date=YYYY-MM-DD` → returns the current applied plan (derived from `AssignedTo` + `RouteSeq` on WOs).
   - `POST /api/wo/scheduler/reroute/<driver_email>` → run a fresh route for one driver mid-shift (useful when adjustments needed).
3. **UI** — new `templates/_scheduler_tab.html` with:
   - Day picker.
   - List of drivers (left column) with their current load.
   - List of unassigned WOs (right column) — service / delivery / movement, with priority.
   - Map view (Leaflet — already a CDN dep in `index.html`).
   - "Auto-plan" button → calls `/scheduler/plan` → shows proposal as ghost lines on map; "Apply" commits.
   - Drag-and-drop to reassign a WO to a different driver.
4. **Wire into `index.html`** under the existing manager-gated tabs.

Default tab placement: between **Dispatch** and **Tech Support** for `field_manager`/`dispatch`/`admin`.

---

## 7. Quick start for the new agent

```bash
# Clone (or your working copy)
cd "/Users/yash008/Documents/Coding/Coding/KNM Apps/vending-dashboard"

# Skim these in order:
cat HANDSHAKE.md
cat HANDOFF-driver-scheduler-2026-06-12.md   # this file
cat FAULT_TECH_FIELDS_AUDIT.md               # schema thinking
grep -n "operator/locations\|api_assign_\|api_delivery_list\|dispatch_plan" workorders.py app.py

# Identify the existing route planner code in app.py
grep -n "planRoutes\|nearest" app.py

# Run locally (DEV_USER_EMAIL / DEV_ROLE set in config.py)
source .venv/bin/activate
python app.py  # http://localhost:5000
```

**Push convention.** Each commit: present commands to Yash to run; do not `git push` for him unless he asks. Format: small focused commits, run SQL ALTERs separately and confirm with Yash before any code that depends on them.

---

## 8. Known TODOs across the codebase (so you don't duplicate work)

- Delivery WO redesign post-COO meeting (Yash will share spec soon).
- Sales/Messages tabs still show data for inactive (decommissioned) machines — by design. Heartbeat/Locations/Topups filter `IsActive = 1` already.
- Event-code taxonomy: catch-all 6xxxxx / 8xxxxx today; bucketing matrix pending real-world distribution data.
- KB CAR fields exist (`Symptom`, `DiagnosticConfirmation`, `RootCause`, `CorrectiveAction`, `PreventiveAction`, `VerificationOfCompletion`) — operator WO flow shows them via "View KB" link.
- Old `Status` text columns on `WO_Complaints` and `WO_JobOrders` retained for rollback safety; will be dropped in a future cleanup migration.
- Per-location contact person table (for customer-unavailable email-PDF path) was deferred. New WO tab might need it.

Good luck.
