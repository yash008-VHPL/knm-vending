# KNM Vending Dashboard — architecture / where things live

Flask + Azure SQL (pymssql) ops app for Kopi Near Me vending machines.
Repo root: `vending-dashboard`. Written against the tree at commit `771441c`
(2026-08-23 cutover, already pushed to `origin/main`).

Three Python modules own all HTTP surface:

| Module | Mount | What it is |
|---|---|---|
| `app.py` | root Flask app (`app:app`) | auth helpers, DB helpers, OLE date maths, legacy dashboard page, `/api/*` |
| `workorders.py` | Blueprint `workorders`, `url_prefix="/api/wo"` | everything work-order: complaints, job orders, delivery orders, movements, KB, visits, schedule, machine attributes |
| `alpha_preview.py` | Blueprint `alpha_preview`, no prefix | the streamlined production UI at `/` and `/alpha`, plus `/alpha/api/*` (SELECT-only) |

Import order matters: `app.py` imports `workorders` (line 1816) and then
`alpha_preview` (line 1829); `workorders.py` imports `app` at module level for
`get_current_user` / `get_role` / `get_connection`; `alpha_preview.py` imports
`app.get_role` **lazily inside `_active_role()`** (line 107) precisely because a
module-level import would be circular.

---

## 1. Deployment & runtime

### Entry point

- `Procfile` — `web: gunicorn --bind=0.0.0.0 --timeout 600 app:app`
- `startup.txt` — same command; this is what Azure App Service actually runs as the startup command.
- `.deployment` — `SCM_DO_BUILD_DURING_DEPLOYMENT=true` (Oryx builds on the platform).
- `requirements.txt` — flask, gunicorn, msal>=1.28, pymssql, requests>=2.32, reportlab>=4.0, openpyxl>=3.1.
  `requirements_additions.txt` is a stale leftover listing a subset; nothing reads it.

**One gunicorn worker, no `--workers`/`--threads`.** `gcal_feed.py:9-13` calls
this out explicitly: any outbound HTTP inside a request would block every other
user for the full timeout, which is why the Google Calendar poll runs on a
background thread and request handlers only read its in-memory cache.

### Work done at import time (every worker boot)

`app.py`, in this order:

1. `init_db()` (line 264, called at 334) — idempotent `ALTER TABLE MachineLookup ADD` for 10 columns, creates index `IX_MasterData_Machine_Time`, and `CREATE OR ALTER VIEW dbo.VendEvents`.
2. `seed_locations()` (line 453, called at 498) — walks the 106-row `LOCATION_SEED` list, **opening and closing a fresh DB connection per row**. Insert-only for existing codes (see landmine L7).
3. Blueprint registration for `workorders_bp` (line 1817), then `alpha_bp`.
4. `init_workorders_db()` (workorders.py:135, called at 1819 — i.e. AFTER its blueprint is registered) — creates the 11 `WO_*` tables if missing, adds 11 nullable columns, creates 9 indexes.
5. `gcal_feed.start(...)` (line 1859) — starts the daemon poll thread, no-op when unconfigured.

Every one of these is wrapped in try/except and only prints on failure, so a
broken migration is silent apart from a log line.

### GitHub Actions workflows

| Workflow | Fires | Does |
|---|---|---|
| `.github/workflows/main_knmdispenseviewer.yml` | push to `main`, or manual dispatch | Build (py 3.11, venv + `pip install -r requirements.txt`) → upload artifact → OIDC login to Azure → `azure/webapps-deploy@v3` to app `KNMDispenseViewer`, slot Production. This is the auto-deploy. |
| `.github/workflows/auresys-daily.yml` | cron `30 22 * * *` UTC = 06:30 SGT daily; manual dispatch with `days` / `from` / `to` / `dry_run` inputs | `python auresys_pull.py --days 10` (default). Concurrency group `auresys-pull`, `cancel-in-progress: false`, so a second run can never overlap a DELETE-then-INSERT in flight. `actions/checkout` is pinned to a commit SHA, not a tag, because the job holds DB credentials. |
| `.github/workflows/nets-reconciliation.yml` | cron `0 1 2 * *` UTC = 09:00 SGT on the 2nd of each month; manual dispatch with optional `year`/`month` | Installs Playwright + chromium, runs `nets_reconcile.py` for the previous month, posts a report to Teams. |
| `.github/workflows/migrate-location-history.yml` | push to branch `ops/loc-migration`, or manual dispatch | One-off. Push with no `MIGRATION_APPLY_REQUEST` file = PREVIEW (read-only); push with that file = APPLY, and the file's contents are passed as `--cutoffs`. Commits `MIGRATION_PREVIEW.md` / `MIGRATION_RESULT.md` back to the ops branch (never `main`). |

### Configuration and environment variables

`config.py` is deliberately tracked in git and carries **no secrets** — the
deploy workflow ships only tracked files, so if it were gitignored `import
config` would fail at boot.

| Variable | Read at | If unset |
|---|---|---|
| `DB_SERVER` | `config.py:10` | falls back to a hardcoded server hostname; fine |
| `DB_NAME` | `config.py:11` | falls back to the hardcoded database name; fine |
| `DB_USER` | `config.py:12` | empty string → every `get_connection()` fails → every page and API 500s |
| `DB_PASSWORD` | `config.py:13` | empty string, no fallback by design → same as above |
| `INTERNAL_API_KEY` | `config.py:17` | empty string → `/api/internal/vend-counts` **fails closed** (the header can never equal `""` because the route rejects an empty key first). `nets_reconcile.py` then cannot fetch DB counts. |
| `DEV_USER_EMAIL`, `DEV_ROLE` | `config.py:24-25` | **deliberate literals, never read from the environment.** The comment at `config.py:20-23` explains why: `get_current_user()` and `get_all_roles()` fall back to them when no Easy Auth principal is present, so `DEV_ROLE="admin"` as an app setting would make every unauthenticated request a full admin. |
| `MS_CLIENT_ID` | `sharepoint_helper.py:73` | `_cfg()` raises `RuntimeError` → every image/PDF upload and the technician dropdown fail |
| `MS_CLIENT_SECRET` | `sharepoint_helper.py:74` | same |
| `MS_TENANT_ID` | `sharepoint_helper.py:75` | same |
| `MS_SITE_ID` | `sharepoint_helper.py:98` | same — no SharePoint drive resolved, no attachment storage |
| `MS_EASYAUTH_SP_OBJECT_ID` | `workorders.py:594`, `:631` (env first, then `config` attribute) | `/api/wo/technicians` returns HTTP **200** with an empty list and an `error` string, so the assignee dropdown silently degrades to free text |
| `MS_OPERATOR_ROLE_ID` | `workorders.py:595`, `:632` | same |
| `GCAL_FEED_URL` | `gcal_feed.py:43` | feed disabled — `gcal_feed.enabled()` false, poller never starts, `/api/wo/schedule/gcal` returns an empty snapshot |
| `GCAL_FEED_SECRET` | `gcal_feed.py:44` | same (both must be set) |
| `GCAL_POLL_SECONDS` | `gcal_feed.py:50` | defaults to 300, floor 60 |
| `GCAL_SYNC` | `gcal_feed.sync_enabled()` | defaults to **off** since 2026-08-24. A sales calendar entry is a REQUEST, not a stop — only dispatch confirms one, on Topups ▸ Plan. Set to `1`/`true`/`on`/`yes` to re-enable the one-way calendar→`WO_DeliveryOrders` sync; every unrecognised value means off. The read-only pane and the Plan tab's request list work either way (they need `GCAL_FEED_URL`/`GCAL_FEED_SECRET`, not this). `topups_api._gcal_sync_on()` delegates here so the comparison exists in one place |
| `AURESYS_USER`, `AURESYS_PASSWORD` | `auresys_pull.py:463-464` | the daily pull cannot log in to the Auresys portal |
| `NETS_CARD_PEPPER` | `auresys_pull.py:465` | card hashing has no pepper; the pull aborts |
| `NETS_DB_USER`, `NETS_DB_PASSWORD` | `auresys_pull.py:283-284` (required, `os.environ[...]`) | the pull raises before connecting. Note this job uses its **own** DB credentials, separate from `DB_USER`/`DB_PASSWORD`; the workflow falls back to the latter |
| `HEARTBEAT_URL` | `auresys_pull.py:565` | optional; no success ping sent |
| `TEAMS_WEBHOOK_URL` | `auresys_pull.py:441`, `nets_reconcile.py:42` | optional; alerts and the monthly report are not posted anywhere |
| `NETS_USERNAME`, `NETS_PASSWORD` | `nets_reconcile.py:39-40` | the monthly Playwright login fails |
| `APP_BASE_URL` | `nets_reconcile.py:44` | defaults to the production App Service URL |

`config.py` is also consulted as a fallback object for `MS_*` names
(`sharepoint_helper._cfg`, `workorders.py:594`) and for `GCAL_*`
(`gcal_feed._cfg`), so those can alternatively live as module attributes — but
`config.py` in the repo defines none of them.

Not an env var, but tuned in code: `HEARTBEAT_THRESHOLD_MINUTES = 225`
(`app.py:507`, mirrored at `alpha_preview.py:49`). `/api/admin/heartbeat-analysis`
exists purely to recalculate it; changing it needs a redeploy.

`config_nets.py` is gitignored and imported opportunistically by
`nets_reconcile.py:34` for local runs.

---

## 2. Route map

109 route registrations total: 21 in `app.py`, 84 in `workorders.py`, 4 in
`alpha_preview.py`.

**Gate legend.** `login_required` / `api_login_required` = any signed-in user
holding *any* app role. `admin_required` / `require_roles(ROLE_ADMIN)` = active
role is exactly `admin`. `dispatch_or_admin_required` = active role in
{admin, dispatch, field_manager}. In `workorders.py` the sets are
`OPERATOR_ROLES = {operator, field_manager, admin, dispatch}`,
`MANAGER_ROLES = DISPATCH_ROLES = {field_manager, admin, dispatch}`,
`SALES_ROLES = MANAGER_ROLES ∪ {sales}`. All of these test the **active** role
(`get_role`), not the full claim set.

### 2.1 Pages (`app.py`, `alpha_preview.py`)

| Route | Methods | Module:function | Gate | Roles | Returns |
|---|---|---|---|---|---|
| `/` | GET | `alpha_preview:alpha_index` | `_gate()` | any role | `templates/alpha_preview.html` |
| `/alpha` | GET | `alpha_preview:alpha_index` | `_gate()` | any role | same template, same view (not a redirect) |
| `/archive2608` | GET | `app:index` | `login_required` + inline claim/active-role checks | admin only (403 + guidance otherwise) | `templates/index.html` |
| `/signed-out` | GET | `app:signed_out` | **none** | anonymous | inline HTML |
| `/logout` | GET | `app:logout` | none | anyone | 302 → `/.auth/logout?post_logout_redirect_uri=/signed-out` |
| `/` | GET | `app:_alpha_down` | none | anyone | **only registered when `ALPHA_OK` is False** (alpha blueprint failed to import): 302 → `/archive2608`, and `index()` drops its admin gate |

`/archive2608` is doubly gated on purpose (`app.py:657-682`): eligibility is
tested against **claims** (`get_all_roles`) so an admin on a `field_manager`
cookie is not locked out of their own rollback path, then the **active** role
must also be admin so the page actually renders the admin template.

### 2.2 `/api/*` (all in `app.py`)

| Route | Methods | Function | Gate | Roles | Returns |
|---|---|---|---|---|---|
| `/api/switch-role/<path:new_role>` | GET | `switch_role` | `login_required` | any role (must actually hold `new_role`) | 302 + sets `knm_active_role` cookie |
| `/api/locations` | GET | `get_locations` | `login_required` | any | JSON list of active machines |
| `/api/location-names` | GET | `get_location_names` | `login_required` | any | JSON list of every name a machine has ever had (history ∪ current) |
| `/api/dispenses` | GET | `get_dispenses` | `login_required` | any | JSON vend counts by SKU for a window |
| `/api/transactions` | GET | `get_transactions` | `login_required` | any | JSON, TOP 2000 individual vends, `capped` flag |
| `/api/messages` | GET | `get_messages` | `login_required` | any | JSON error/exception/event/message log |
| `/api/topups` | GET | `get_topups` | `login_required` | any | JSON top-up state + live vends-since |
| `/api/topups/<path:code>` | POST | `log_topup` | `login_required` | **any role** (see landmine L4) | JSON `{ok}` |
| `/api/topups/<path:code>` | DELETE | `delete_topup` | `login_required` | **any role** (see L4) | JSON `{ok}` |
| `/api/dispatch/plan` | POST | `plan_dispatch` | `dispatch_or_admin_required` | admin, dispatch, field_manager | JSON routes + Google Maps URLs |
| `/api/admin/locations` | POST | `add_location` | `admin_required` | admin | JSON `{ok}` |
| `/api/admin/locations/<path:code>` | PUT | `update_location` | `dispatch_or_admin_required` | admin, dispatch, field_manager | JSON `{ok}` |
| `/api/admin/locations/<path:code>` | DELETE | `delete_location` | `admin_required` | admin | JSON `{ok}` |
| `/api/internal/vend-counts` | GET | `internal_vend_counts` | **`X-Internal-Key` header vs `INTERNAL_API_KEY`** — not Easy Auth | machine-to-machine (GitHub Actions) | JSON per (code, location-at-vend-time) counts |
| `/api/admin/march2026-vends` | GET | `march2026_vends` | `admin_required` | admin | JSON; one-off NETS cross-check, hardcoded to March 2026 |
| `/api/heartbeat` | GET | `get_heartbeat` | `login_required` | any | JSON per-machine green/yellow/red + threshold |
| `/api/admin/heartbeat-analysis` | GET | `heartbeat_analysis` | `admin_required` | admin | JSON gap percentiles to recalibrate `HEARTBEAT_THRESHOLD_MINUTES` |

### 2.3 `/alpha/api/*` (`alpha_preview.py`)

| Route | Methods | Function | Gate | Roles | Returns |
|---|---|---|---|---|---|
| `/alpha/api/bootstrap` | GET | `alpha_bootstrap` | `_gate(is_api=True)` | any role | JSON `{health, machines, work}` — the whole registry + up to `_DELIVERY_CAP` (900) delivery rows, 200 job orders, 200 movements |
| `/alpha/api/board/completed` | GET | `alpha_board_completed` | `_gate(is_api=True)` | any role | JSON `{date, stops, partial}` — completed stops for ONE day, `?date=YYYY-MM-DD` |

Both are SELECT-only. Every write the streamlined UI performs goes to
`/api/wo/*`, so validation, role checks, activity log, top-up sync and movement
cutover logic are shared with the archive.

### 2.4 `/api/wo/*` (`workorders.py`, all prefixed)

#### Technicians / bootstrap / machines

| Route | Methods | Function | Gate | Roles |
|---|---|---|---|---|
| `/technicians` | GET | `api_technicians` | `require_roles(*DISPATCH_ROLES)` | admin, field_manager, dispatch |
| `/technicians/refresh` | POST | `api_technicians_refresh` | DISPATCH_ROLES | admin, field_manager, dispatch |
| `/technicians/diag` | GET | `api_technicians_diag` | `require_roles(ROLE_ADMIN)` | admin |
| `/bootstrap` | GET | `api_bootstrap` | `api_login_required` | any |
| `/machines` | GET | `api_machines` | `api_login_required` | any |

#### Locations

| Route | Methods | Function | Gate | Roles |
|---|---|---|---|---|
| `/locations/historic` | GET | `api_locations_historic` | MANAGER_ROLES | admin, field_manager, dispatch |
| `/locations/<path:code>/decommission` | POST | `api_location_decommission` | MANAGER_ROLES | " |
| `/locations/<path:code>/recommission` | POST | `api_location_recommission` | MANAGER_ROLES | " |

#### Complaints (fault reports)

| Route | Methods | Function | Gate | Roles |
|---|---|---|---|---|
| `/complaints` | POST | `api_complaint_create` | `api_login_required` | any |
| `/complaints` | GET | `api_complaint_list` | `api_login_required` | any (`?scope=mine\|all`) |
| `/complaints/<int:cid>` | GET | `api_complaint_detail` | `api_login_required` | any |
| `/complaints/<int:cid>/refund` | POST | `api_complaint_refund` | `api_login_required` | any |
| `/complaints/close` | POST | `api_complaint_close` | MANAGER_ROLES | admin, field_manager, dispatch |
| `/complaints/link` | POST | `api_complaint_link` | MANAGER_ROLES | " |
| `/complaints/oneoff-suggestions` | GET | `api_complaint_oneoff_suggestions` | MANAGER_ROLES | " |

#### Job orders (service work orders) and tasks

| Route | Methods | Function | Gate | Roles |
|---|---|---|---|---|
| `/joborders` | POST | `api_joborder_create` | MANAGER_ROLES | admin, field_manager, dispatch |
| `/joborders` | GET | `api_joborder_list` | OPERATOR_ROLES | + operator |
| `/joborders/<int:jid>` | GET | `api_joborder_detail` | OPERATOR_ROLES | " |
| `/joborders/<int:jid>` | PATCH | `api_joborder_update` | MANAGER_ROLES | admin, field_manager, dispatch |
| `/joborders/<int:jid>/assign` | POST | `api_joborder_assign` | DISPATCH_ROLES | " |
| `/joborders/<int:jid>/status` | POST | `api_joborder_status` | OPERATOR_ROLES | + operator (status 0/1/2 only) |
| `/joborders/<int:jid>/driver-input` | PATCH | `api_joborder_driver_input` | OPERATOR_ROLES | " |
| `/joborders/<int:jid>/review` | POST | `api_joborder_review` | MANAGER_ROLES | admin, field_manager, dispatch (only path to status 3) |
| `/joborders/<int:jid>/tasks` | GET | `api_tasks_list` | OPERATOR_ROLES | + operator |
| `/joborders/<int:jid>/tasks` | POST | `api_tasks_add` | MANAGER_ROLES | admin, field_manager, dispatch |
| `/tasks/<int:tid>` | PATCH | `api_task_update` | OPERATOR_ROLES | + operator |
| `/tasks/<int:tid>` | DELETE | `api_task_delete` | MANAGER_ROLES | admin, field_manager, dispatch |

#### Knowledge base

| Route | Methods | Function | Gate | Roles |
|---|---|---|---|---|
| `/kb` | GET | `api_kb_list` | `api_login_required` | any (read is open so suggestions work) |
| `/kb` | POST | `api_kb_create` | MANAGER_ROLES | admin, field_manager, dispatch |
| `/kb/<int:kid>` | PATCH | `api_kb_update` | MANAGER_ROLES | " |
| `/kb/<int:kid>` | DELETE | `api_kb_delete` | MANAGER_ROLES | " |
| `/kb/suggest` | GET | `api_kb_suggest` | `api_login_required` | any (`?event_code=8xxxxx`) |
| `/kb/<int:kid>/use` | POST | `api_kb_use` | MANAGER_ROLES | admin, field_manager, dispatch |

#### Admin / deletion / audit

| Route | Methods | Function | Gate | Roles |
|---|---|---|---|---|
| `/admin/complaint/<int:cid>` | DELETE | `api_admin_delete_complaint` | ROLE_ADMIN | admin |
| `/admin/joborder/<int:jid>` | DELETE | `api_admin_delete_joborder` | ROLE_ADMIN | admin |
| `/admin/movementorder/<int:mid>` | DELETE | `api_admin_delete_movementorder` | ROLE_ADMIN | admin (unwinds a completed move) |
| `/admin/wipe-all` | POST | `api_admin_wipe_all` | ROLE_ADMIN | admin |
| `/admin/deleted-log` | GET | `api_deleted_log` | ROLE_ADMIN | admin |
| `/admin/deleted-log/<int:log_id>/restore` | POST | `api_deleted_log_restore` | ROLE_ADMIN | admin |
| `/manager/machine/<path:code>/recorded-move/<int:hid>` | DELETE | `api_delete_recorded_move` | MANAGER_ROLES | admin, field_manager, dispatch |

#### Images (SharePoint-backed)

| Route | Methods | Function | Gate | Roles | Returns |
|---|---|---|---|---|---|
| `/images` | POST | `api_image_upload` | `api_login_required` | any | JSON new image id |
| `/images/<int:image_id>` | GET | `api_image_get` | `api_login_required` | any | raw bytes proxied from SharePoint (falls back to legacy `ImageData`) |
| `/images/<int:image_id>` | DELETE | `api_image_delete` | MANAGER_ROLES | admin, field_manager, dispatch | JSON |

#### Movement orders

| Route | Methods | Function | Gate | Roles |
|---|---|---|---|---|
| `/movementorders` | POST | `api_movement_create` | MANAGER_ROLES | admin, field_manager, dispatch |
| `/movementorders` | GET | `api_movement_list` | `api_login_required` | any |
| `/movementorders/<int:mid>/complete` | POST | `api_movement_complete` | OPERATOR_ROLES | + operator |
| `/movementorders/<int:mid>/assign` | POST | `api_movement_assign` | DISPATCH_ROLES | admin, field_manager, dispatch |
| `/movementorders/<int:mid>/status` | POST | `api_movement_status` | OPERATOR_ROLES | + operator (in_progress only) |

#### Dispatch stops (the Plan board)

| Route | Methods | Function | Gate | Roles |
|---|---|---|---|---|
| `/stops` | POST | `api_stop_create` | DISPATCH_ROLES | admin, field_manager, dispatch |
| `/stops/<kind>/<int:sid>` | PATCH | `api_stop_update` | DISPATCH_ROLES | " |
| `/stops/<kind>/<int:sid>` | DELETE | `api_stop_delete` | DISPATCH_ROLES | " (only cancels untouched work) |

#### Sales schedule

| Route | Methods | Function | Gate | Roles |
|---|---|---|---|---|
| `/schedule` | GET | `api_schedule_list` | SALES_ROLES | admin, field_manager, dispatch, sales |
| `/schedule/stops` | POST | `api_schedule_create` | SALES_ROLES | " (sales cannot name a driver) |
| `/schedule/stops/<int:did>` | DELETE | `api_schedule_delete` | SALES_ROLES | " (sales may only cancel unassigned stops) |
| `/schedule/series/<series_id>` | DELETE | `api_schedule_delete_series` | SALES_ROLES | " |
| `/schedule/gcal` | GET | `api_schedule_gcal` | SALES_ROLES | " — reads `gcal_feed`'s in-memory cache only |

#### Delivery orders

| Route | Methods | Function | Gate | Roles |
|---|---|---|---|---|
| `/deliveryorders` | POST | `api_delivery_create` | MANAGER_ROLES | admin, field_manager, dispatch |
| `/deliveryorders` | GET | `api_delivery_list` | `api_login_required` | any |
| `/deliveryorders/<int:did>` | GET | `api_delivery_detail` | `api_login_required` | any |
| `/deliveryorders/<int:did>` | PATCH | `api_delivery_update` | OPERATOR_ROLES | + operator |
| `/deliveryorders/<int:did>/complete` | POST | `api_delivery_complete` | OPERATOR_ROLES | " |
| `/deliveryorders/<int:did>/assign` | POST | `api_delivery_assign` | DISPATCH_ROLES | admin, field_manager, dispatch |

#### Assignment helpers / manager surfaces

| Route | Methods | Function | Gate | Roles |
|---|---|---|---|---|
| `/assignment/delivery-candidates` | GET | `api_assign_delivery_candidates` | DISPATCH_ROLES | admin, field_manager, dispatch |
| `/assignment/joborder-candidates` | GET | `api_assign_joborder_candidates` | DISPATCH_ROLES | " |
| `/manager/overview` | GET | `api_manager_overview` | MANAGER_ROLES | " |
| `/manager/machines` | GET | `api_manager_machines_search` | OPERATOR_ROLES | + operator |
| `/manager/machine/<path:code>/record-move` | POST | `api_record_move` | MANAGER_ROLES | admin, field_manager, dispatch |
| `/manager/equipment-log` | GET | `api_equipment_log` | OPERATOR_ROLES | + operator |

#### Machine attributes (5 fixed-option properties)

| Route | Methods | Function | Gate | Roles | Returns |
|---|---|---|---|---|---|
| `/machines/attributes` | GET | `api_machine_attrs_list` | SALES_ROLES | + sales | JSON |
| `/machines/<path:code>/attributes` | PATCH | `api_machine_attrs_update` | SALES_ROLES | " | JSON |
| `/machines/attributes/template.csv` | GET | `api_machine_attrs_template` | SALES_ROLES | " | **CSV file** |
| `/machines/attributes/import` | POST | `api_machine_attrs_import` | SALES_ROLES | " | JSON; dry-run unless `apply=1` |

#### Delivery item catalogue

| Route | Methods | Function | Gate | Roles |
|---|---|---|---|---|
| `/admin/delivery-items` | GET | `api_delivery_items_list` | `api_login_required` | any (admins also see inactive) |
| `/admin/delivery-items` | POST | `api_delivery_items_create` | ROLE_ADMIN | admin |
| `/admin/delivery-items/<int:iid>` | PATCH | `api_delivery_items_update` | ROLE_ADMIN | admin |
| `/admin/delivery-items/<int:iid>` | DELETE | `api_delivery_items_delete` | ROLE_ADMIN | admin (soft delete, `IsActive=0`) |

#### Operator (driver) surfaces and visits

| Route | Methods | Function | Gate | Roles | Returns |
|---|---|---|---|---|---|
| `/operator/locations` | GET | `api_operator_locations` | OPERATOR_ROLES | operator, field_manager, admin, dispatch | JSON colour-coded machine list |
| `/operator/location/<path:code>` | GET | `api_operator_location_detail` | OPERATOR_ROLES | " | JSON one machine's open work + item catalogue |
| `/visits/start` | POST | `api_visit_start` | OPERATOR_ROLES | " | JSON visit detail |
| `/visits/<int:vid>` | GET | `api_visit_detail` | OPERATOR_ROLES | " | JSON |
| `/visits/<int:vid>` | PATCH | `api_visit_update` | OPERATOR_ROLES | " | JSON (draft save) |
| `/visits/<int:vid>/finalize` | POST | `api_visit_finalize` | OPERATOR_ROLES | " | JSON; validates, saves signatures, builds the PDF (reportlab), uploads to SharePoint, transitions linked WOs |
| `/visits/<int:vid>/pdf` | GET | `api_visit_pdf` | OPERATOR_ROLES | " | **PDF bytes** proxied from SharePoint |
| `/visits` | GET | `api_visit_list` | OPERATOR_ROLES | " | JSON; operator sees own, manager sees all |

---

## 3. Templates

`templates/` — ignoring every `*.bak*`.

| File | Included by | Screen / tab | Owns |
|---|---|---|---|
| `alpha_preview.html` (2950 lines) | rendered by `alpha_preview:alpha_index` for `/` and `/alpha` | the whole streamlined app | Five areas: **Fleet**, **Service**, **Schedule**, **Plan**, **Admin** (`AREAS` at line 601). Per-role nav and landing tab in `ROLE_NAV` / `ROLE_HOME` (602-612). Client-side rendering into `#main`; embedded legacy panes live in `#embhost` and are switched by the `EMBEDS` map (line 731). Also owns the role picker, sign-out, toast/drawer chrome, the day board, the calendar, and Live status polling. |
| `index.html` (1978 lines) | rendered by `app:index` for `/archive2608` | the archived dashboard | Tab bar (line 395) and the native tabs: Sales, Messages, Topups, Heartbeat, Locations (`tab-admin`), Dispatch (route planner + Leaflet map + route editor). Hosts eight `{% include %}` partials as further tabs. |
| `_tech_support_tab.html` (1181) | **both** `index.html` (`tab-tech_support`) and `alpha_preview.html` (`emb-tech`, manager-only) | Tech Support | Mobile-first WO list, tickbox checklists, block-with-photo, manager edit of diagnosis / proposed fix / assignment. Namespace `.ts-*`, publishes `window.__tsInit`. |
| `_kb_admin_tab.html` (332) | **both** (`tab-kb_admin` / `emb-kb`, manager-only) | Manage Knowledge Base | CRUD over `WO_KB_Entries` + tickboxes that pre-fill new WOs by EventCode. Namespace `.kb-*`, `window.__kbInit`. **No internal role gating** — it relies entirely on the host template hiding it. |
| `_movements_tab.html` (360) | **both** (`tab-movements` / `emb-movements`) | Movements | deploy / relocate / retrieve; manager creates + assigns, driver completes. Namespace `.mv-*`, `window.__mvInit`. |
| `_admin_delete_tab.html` (288) | **both** (`tab-admin_delete` "Test Cleanup" / `emb-cleanup`, admin only) | Test Cleanup | Cascade delete of complaints / WOs / movements, plus Wipe All. Marked TEMPORARY in its own header comment. Namespace `.ad-*`, `window.__adInit`. |
| `_legacy_tabs.html` (1203) | `alpha_preview.html` only | four panes: `lgc-messages`, `lgc-topups`, `lgc-dispatch`, `lgc-locations` | Messages, Topups, Dispatch route planner, Locations — lifted verbatim out of `index.html` in 2026-08-11 so the streamlined app could reuse them. CSS scoped under `.lgc`, JS wrapped in an IIFE, one entry point `window.__lgcInit(key)`. Needs `role` in the template context. |
| `_fault_report_tab.html` (416) | `index.html` only (`tab-fault_report`) | Fault Report | Complaint submission + list. Namespace `.fr-*`, `window.__frInit`. |
| `_operator_work_order_tab.html` (703) | `index.html` only (`tab-work_orders`) | Work Orders (driver) | The paper-Work-Order flow: colour-coded location list → WO page → customer signature popup. Namespace `.owo-*`, `window.__owoInit`. |
| `_machine_history_tab.html` (342) | `index.html` only (`tab-machine_history`) | Machine History | One machine's complaints, WOs, deliveries, activity in one timeline; backed by `/api/wo/manager/equipment-log`. Namespace `.mh-*`, `window.__mhInit`. |
| `_historic_locations_tab.html` (148) | `index.html` only (`tab-historic_locations`) | Historic Locations | Decommissioned machines + lifetime vend counts; decommission/recommission. Namespace `.hl-*`, `window.__hlInit`. |
| `_workorders_tab.html` (1198) | **nothing** | — | Orphaned. No `{% include %}` anywhere; the only reference is a comment in `_fault_report_tab.html:4`. Namespace `.wo-*`. Dead code. |
| `login.html` (121) | **nothing** | — | Orphaned. Pre-Easy-Auth login form; no `render_template("login.html")` exists. Dead code, along with `set_password.py`. |

**Shared partials.** `_tech_support_tab.html`, `_kb_admin_tab.html`,
`_movements_tab.html` and `_admin_delete_tab.html` are included by *both* pages.
Each is self-contained (own namespaced CSS, own IIFE, one `window.__xxInit`)
specifically so the two hosts cannot collide. `alpha_preview.html:492` sets
`MGR = role in ['admin','dispatch','field_manager']` and gates the includes on it.

**External asset.** `alpha_preview.html:470-471` loads Leaflet 1.9.4 from
`unpkg.com` (CSS + JS) for the embedded dispatch route map. `index.html` does
the same in its head. Both pages therefore depend on a public CDN.

---

## 4. Auth & roles

### Decoding a principal

Azure Easy Auth (AAD) injects a base64 JSON blob in the `X-MS-CLIENT-PRINCIPAL`
header. Both modules decode it independently:

- `app._decode_principal()` (`app.py:16`) → `app.get_current_user()` (`:26`) reads the `preferred_username` claim, lowercased.
- `alpha_preview._current_principal()` (`:55`) / `_current_user()` (`:65`) — an exact duplicate, kept so the blueprint has no import-time dependency on `app`.

No principal → `config.DEV_USER_EMAIL` (empty in production), i.e. anonymous.

### `ROLE_ALIASES`

`app.py:40` — `{"dispatch": "field_manager"}`. Dispatch and field manager are
one person at KNM, so `canon_role()` (`app.py:43`) folds the two everywhere.
The comment at `app.py:35-39` says the map exists rather than ~30 template
conditions so the tab list, landing tab and role switcher cannot drift apart;
to split the roles again, delete the map and narrow the widened gates in
`index.html`. Mirrored in `alpha_preview._ROLE_ALIASES` (`:76`), in
`workorders.py` by keeping `ROLE_DISPATCH` inside `OPERATOR_ROLES` and
`MANAGER_ROLES` (`:60-62`), and client-side in `alpha_preview.html:614`.

### `get_all_roles` vs `get_role`

- **`get_all_roles(email)`** (`app.py:48`) — every `roles` claim on the principal, canonicalised and de-duplicated. This is the user's *capability set*. Falls back to `[config.DEV_ROLE]` when there are no claims.
- **`get_role(email)`** (`app.py:64`) — the **one** role the server enforces this request. Reads the `knm_active_role` cookie; honours it *only* if the (canonicalised) value is in `get_all_roles()`, otherwise falls back to `roles[0]`. Wrapped in try/except for `RuntimeError` so it works outside a request context.

Every decorator in the codebase tests `get_role`. The only place `get_all_roles`
decides access is the `/archive2608` eligibility check (`app.py:666`) — see §2.1.

### The `knm_active_role` cookie

Set only by `/api/switch-role/<new_role>` (`app.py:689`). 7-day max-age,
`httponly=False` (the client reads it), `SameSite=Lax`. The route canonicalises
`new_role` first so a bookmarked `/api/switch-role/dispatch` still works, refuses
a role the user does not hold (403), and redirects to `?back=` **allowlisted to
exactly `/`, `/alpha`, `/archive2608`** — anything else falls back to `/`. The
comment at `app.py:698-703` explains why this is an allowlist and not a
`startswith("/")` check: Werkzeug strips tabs and newlines from the `Location`
header *after* the check, so `"/\t/evil.com"` would be emitted as
`"//evil.com"` — a live open redirect on a route that also sets a cookie.

### Decorators

| Decorator | File | Allows | On refusal |
|---|---|---|---|
| `login_required` | `app.py:84` | any signed-in user with any app role | Decides JSON vs redirect via `wants_json = path.startswith("/api/") and endpoint != "switch_role"`. No email: JSON 401, or 302 to `/.auth/login/aad?post_login_redirect_uri=<the page asked for>`. Email but no role: JSON 403, or an HTML Access Denied page with a sign-out link. |
| `admin_required` | `app.py:124` | `get_role(email) == "admin"` | JSON 403 always, even on a page route |
| `dispatch_or_admin_required` | `app.py:134` | `get_role(email) in ("admin","dispatch","field_manager")` | JSON 403. `field_manager` is listed explicitly (comment at `:138-141`): `get_role` resolves ONE role, so without it the same person loses the route planner and location editor whenever their active role happens to be `field_manager`. |
| `api_login_required` | `workorders.py:104` | any signed-in user with any app role | JSON 401 (not signed in) / 403 (no role). Never redirects. |
| `require_roles(*roles)` | `workorders.py:117` | `get_role(email)` in the given set | JSON 401 / 403 |
| `_gate(is_api=False)` | `alpha_preview.py:126` | any signed-in user with at least one role | Returns `None` to proceed, otherwise the response to return. The API/page split is tested **first**, before the sign-in check, and `is_api` is passed explicitly by each API route rather than derived from the path. APIs get JSON 401/403; page loads get the AAD redirect (preserving `request.full_path`) or the branded Access Denied page. |

Two guards appear in both `login_required` (`app.py:104-109`) and `_gate`
(`alpha_preview.py:148-151`): the return-to path must start with `/` and must
not start with `//` or `/\`, because those are emitted as protocol-relative
`Location` headers.

`alpha_preview._active_role()` (`:95`) is what the UI renders. It imports
`app.get_role` lazily and, if that import fails, logs to stderr and falls back
to reading the cookie itself — the docstring is explicit that it must never
fall back *silently*, because a user who switched role would otherwise be
handed their first claim while the server enforced something else.

---

## 5. Data model

Database: Azure SQL, `Machine DispensedDrink` on `machineserver.database.windows.net`.

### 5.1 Machine / vend data (pre-existing; `app.py` owns the reads)

| Table / view | Purpose | Created by |
|---|---|---|
| `[MasterData Table]` | Raw machine telemetry: `[Machine Code]`, `[Event Code]`, `[Date Time]`. Every vend, error, exception, event and message. The app only ever **reads** it. | out of band (the gateway writes it) |
| `MasterCode` | `ItemCode` → `EventName` lookup. Queried through a `GROUP BY ItemCode` subquery that prefers an `EventName` containing a letter, because the same code can carry several names. | out of band |
| `MachineLookup` | The machine registry: `MachineCode`, `MachineName`, `Latitude`, `Longitude`, `IsActive`, `DecommissionedAt`, `DecommissionReason`, `LastTopupTimestamp`, `PreviousTopupTimestamp`, `CountBeforeLastTopup`, plus the five attribute columns. | base table pre-existing; `app.init_db()` (`:264`) adds the 9 nullable columns it knows about. `IsActive` / `DecommissionedAt` / `DecommissionReason` are **not created by any code in this repo** — they are assumed to exist. |
| `MachineLocationHistory` | Effective-dated location intervals: `HistoryID`, `MachineCode`, `LocationName`, `Latitude`, `Longitude`, `ValidFromOle` (inclusive), `ValidToOle` (exclusive, NULL = current), `Source`, `MovementOrderID`. This is what makes a relocation a sharp cutoff instead of a JOIN fan-out. Unique filtered index `UX_MLH_OpenInterval` on `(MachineCode) WHERE ValidToOle IS NULL` — at most one open interval per machine. | `migration_AZURE_2026-07-01.sql`, also by `migrate_location_history.py:199`. Written by `app.mlh_record_change` / `mlh_rename_open` and by `workorders.py` movement + record-move paths. |
| `dbo.VendEvents` | View: the canonical de-duplicated vend feed, for ad-hoc and reporting SQL. | `app.init_db()` (`CREATE OR ALTER VIEW`) |
| index `IX_MasterData_Machine_Time` | `([Machine Code], [Date Time]) INCLUDE ([Event Code])` | `app.init_db()` |

### 5.2 Work-order tables (`workorders.init_workorders_db()`, `workorders.py:135`)

| Table | Purpose |
|---|---|
| `WO_Complaints` | Customer fault reports. Both a legacy `Status NVARCHAR(20)` and the live `StatusCode TINYINT` (0 fresh, 1 assigned, 2 closed, 3 unresolved). Carries `DisplayID` (`KNM-CMP-NNNN-YYMM`), `GroupID` for linked complaints, `RefundIssued`, `EventCode`, `PerceivedUrgency`. |
| `WO_JobOrders` | Service work orders. `StatusCode TINYINT` 0 assigned / 1 needs_assistance / 2 pending_review / 3 closed, plus legacy `Status` text and `Priority`/`PriorityCode` pairs. Holds the manager's `Diagnosis`/`ProposedFix`, the driver's `OnSiteObservations`/`OnSiteChanges`/`TechnicianComments`, `LastBlockReason`, `AttachedKBID`, `DisplayID` (`KNM-WkO-NNNN-YYMM`). |
| `WO_DeliveryOrders` | Top-up / delivery stops — **the unit of dispatch since 2026-08-11**. Status is `NVARCHAR` text only (`'open'` / `'completed'`); there is no `StatusCode` here. `Item1Qty..Item8Qty` are the legacy quantity columns. Columns added idempotently by `init_workorders_db`: `ScheduledDate`, `RouteSeq`, `NeedsService`, `ServiceNote`, `SeriesID`, `SeriesRule`, `RequestedBy`. A `GCalEventID` column is also read/written by `gcal_sync.py` but is **not** in the column list — it comes from `migration_sales_schedule_2026-08-11.sql` / out of band. |
| `WO_MovementOrders` | deploy / relocate / retrieve. `StatusCode TINYINT` 0 scheduled / 1 in_progress / 2 completed. From/To location + coordinates, `ReasonForRetrieval`, `ScheduledDate`, `RouteSeq`. |
| `WO_JobOrderTasks` | Tickbox checklist rows attached to one job order. |
| `WO_Counters` | `(Kind, YYMM) → NextSeq`. The per-month allocator behind `allocate_display_id()` (`workorders.py:475`). |
| `WO_Images` | Attachment metadata. `SPItemID` + `SPWebURL` point at SharePoint; `ImageData VARBINARY(MAX)` is the legacy inline path, still read as a fallback by `/api/wo/images/<id>`. |
| `WO_Activity` | Append-only audit trail: `(ParentType, ParentID, Action, Detail, ByUser, AtTime)`. Written by `_log_activity()` (`workorders.py:418`). |
| `WO_DeletedLog` | Every delete, with a JSON `Snapshot` and a `Reversible` flag. Written by `app.log_deletion()` (`app.py:176`) on the caller's cursor so it commits atomically with the delete. Surfaced at `/api/wo/admin/deleted-log`; only `recorded-move` entries can currently be one-click restored. |

### 5.3 Knowledge base

| Table | Purpose |
|---|---|
| `WO_KB_Entries` | One entry per known fault: `EventCode`, `Title`, `Diagnosis`, `SuggestedFix`, plus the structured fields (`Symptom`, `DiagnosticConfirmation`, `RootCause`, `CorrectiveAction`, `PreventiveAction`, `VerificationOfCompletion`) and `UseCount`. |
| `WO_KB_Tickboxes` | Ordered checklist template for a KB entry; copied into `WO_JobOrderTasks` when a manager accepts a suggestion. |

### 5.4 Visits and the delivery catalogue (NOT created by any code in the repo)

These are read and written by `workorders.py` but appear in no `CREATE TABLE`
here — they were provisioned out of band.

| Table | Purpose | Columns as used |
|---|---|---|
| `WO_VisitSessions` | One on-site visit = one signed Work Order sheet. | `VisitID`, `DisplayID`, `MachineCode`, `MachineNameSnap`, `OperatorEmail`, `VisitDate`, `DispenseCounter`, `Svc_{PMC,CMR,INR,OTH}_{Done,Remarks}`, `ReceivingName`, `ReceivingDate`, `ServiceName`, `ServiceDate`, `CustomerUnavailable`, `CustomerUnavailableReason`, `Status`, `PDFSPWebURL`, `LinkedDeliveryOrderID`, `CreatedAt`, `UpdatedAt`, `SubmittedAt`, `SignedAt` (`_select_visit_sql`, `workorders.py:5653`) |
| `WO_VisitSession_JobOrders` | Many-to-many: which job orders a visit covers. | `VisitID`, `JobOrderID` |
| `WO_DeliveryItems` | The delivery item catalogue — data, not code. | `ItemID`, `Name`, `Unit`, `Content`, `SortOrder`, `IsActive`, `CreatedBy/At`, `UpdatedBy/At` |
| `WO_DeliveryOrderLines` | Per-item quantities for a delivery order, written through a visit. | `DeliveryOrderID`, `ItemID`, `QtyDelivered` |
| `dbo.GCalSiteAlias` | `CalendarText` → `MachineCode`. Maps a Google Calendar event title onto machines. | read by `gcal_feed.load_aliases()` (`:87`) |

### 5.5 NETS / Auresys (batch jobs only — the web app never touches these)

| Table | Purpose | Owner |
|---|---|---|
| `dbo.NETS_Transaction` | One row per payment-terminal transaction pulled from Auresys. Keyed on terminal + date; loaded DELETE-then-INSERT per `(terminal, date)`. | `auresys_pull.py` |
| `dbo.NETS_Load_Audit` | Per-load record: rows in, rows kept, shrink-guard skips. Keys on a GUID. | `auresys_pull.py:257,409` |
| `dbo.NETS_Pull_Run` | One row per pull run with `Status` (`SUCCESS`/`FAILED`) and `Error_Text`. | `auresys_pull.py:296,426,432` |
| `dbo.NETS_Terminal_Outlet_Seen` | MERGE target tracking which terminal has been seen at which outlet, feeding a reassignment view (`vw_NETS_Terminal_Reassigned`, referenced in `nets_mapping.py`'s docstring). | `auresys_pull.py:261` |

### 5.6 Known data quirks the code calls out

- **`[Date Time]` is an OLE Automation float, not a datetime.** Epoch `1899-12-30` (`app.OLE_EPOCH`, `:150`). Convert with `to_ole_date` / `from_ole_date` (`app.py:153,158`).
- **The float is Singapore wall-clock (UTC+8), not UTC.** `app.py:167-173` and `now_sgt_ole()`; `alpha_preview._fetch_sales` (`:249`) shifts `utcnow()` by 8 hours before comparing. But `app.get_heartbeat` (`:1699`) computes `now_ole` from bare `datetime.utcnow()` — an asymmetry that biases every heartbeat age by +480 minutes unless the underlying feed is actually UTC. Worth confirming against real data. (`heartbeat_analysis`, def `:1733`, is NOT affected: its only `utcnow()` is the 90-day window start at `:1741`, and it computes no per-machine age.) `workorders.py:3126` converts in SQL instead: `CAST(CONVERT(datetime, DATEADD(HOUR, 8, SYSUTCDATETIME())) AS FLOAT) + 2.0` — the `+ 2.0` is the SQL Server (1900-01-01) → OLE (1899-12-30) epoch offset.
- **Event-code prefix ranges.** A vend is a **6-digit** code beginning `1` (`LEN(...) = 6 AND ... LIKE '1%'`, used in ~15 queries). `MSG_TYPE_PREFIX` (`app.py:631`): `2` = error, `3` = exception, `4` = event, `5` = message. `6xxxxx` = customer complaint and `8xxxxx` = work done / repair, enforced only as CHECK constraints on the range (`ERROR_CODE_TAXONOMY.md`; the detailed taxonomy is explicitly parked).
- **Vend de-duplication keys on `(machine, timestamp)` ONLY — never on the event code.** `app.py:293-302`: the gateway sometimes writes the same physical vend twice, occasionally under a *different* event code at the same instant. `ORDER BY [Event Code] ASC` makes the survivor deterministic. `/api/messages` uses a *different* dedup key that *does* include the event code (`app.py:1033-1034`), because there the code is the payload.
- **Legacy text status vs TINYINT codes.** `WO_Complaints` and `WO_JobOrders` carry both `Status NVARCHAR(20)` (legacy, default `'open'`) and `StatusCode TINYINT` (live). `WO_MovementOrders` has only `StatusCode`. `WO_DeliveryOrders` has only the text `Status`. Filtering delivery orders means `Status = 'completed'` / `Status <> 'completed'`, not a code.
- **Delivery quantities live in two places.** `WO_DeliveryOrderLines` whenever the order went through a visit; the legacy `Item1Qty..Item8Qty` columns for pre-visit-era orders. `workorders.py:4137-4146` reads lines first and falls back.
- **`pymssql` %-formats the query before sending it.** Any statement that also passes parameters must double a literal `%` — `'1%%'`. See L1.
- **Schema drift is expected and probed for.** `init_workorders_db` swallows each `ALTER` independently, so one table can have `ScheduledDate` and another not; `alpha_preview._wo_has_scheduled()` (`:274`) probes `INFORMATION_SCHEMA` **per table** for exactly this reason.

---

## 6. Background jobs & integrations

| Component | Runs | Triggered by | What it does |
|---|---|---|---|
| `sharepoint_helper.py` | in-process, on demand | called from `workorders.py` (image upload/download/delete, technician lookup) | Microsoft Graph client, app-only auth via MSAL client credentials against the `AppDataBackEnd` SharePoint site, `Sites.Selected` scope. Uploads to `Documents/ComplaintUploads/{YYYY}/{MM}/{DisplayID}/` and `Documents/WorkOrderUploads/...`. The DB stores only `SPItemID` + `SPWebURL`; bytes are proxied back through `/api/wo/images/<id>` and `/api/wo/visits/<id>/pdf` so Easy Auth gates every read. Also `list_users_by_role()` for the technician dropdown (10-minute cache). `_selftest()` is manual-only — never run at import. Setup notes in `SHAREPOINT_SETUP.md`. |
| `gcal_feed.py` | **automatic**, background daemon thread | started from `app.py:1859` at import; polls every `GCAL_POLL_SECONDS` (default 300s) | GETs the sales team's Google Apps Script `/exec` endpoint, resolves each event title to machine codes via `dbo.GCalSiteAlias`, caches the result in memory. `/api/wo/schedule/gcal` reads the cache only and never blocks. No-op unless `GCAL_FEED_URL` **and** `GCAL_FEED_SECRET` are set. |
| `gcal_sync.py` | **OFF by default since 2026-08-24** (`GCAL_SYNC` must be explicitly on). When on: automatic, on the same thread after each successful poll | `gcal_feed.refresh_once()`, only if `gcal_feed.sync_enabled()`. The backfill it left behind is retired by `purge_gcal_stops.py` | One-way sync Google Calendar → `WO_DeliveryOrders`. 28-day rolling horizon. Only `status == "ok"` events sync; `unmapped`/`partial`/`over`/`unknown` are reported as exceptions and never guessed. A removed or moved event cancels its stop **only while unassigned**; once dispatch has named a driver the row is left alone. Never books over an existing stop for that machine+date from any source. `plan()` is a pure function; `apply()` only executes what it decided. System user string: `google-calendar@feed`. |
| `auresys_pull.py` | **automatic**, GitHub Actions | `auresys-daily.yml` cron 06:30 SGT, or manual dispatch | Calls the Auresys VMS JSON API directly (no browser): `POST /api/login`, `GET /vms/report/transactions` for csrfToken + roster, then paginated `POST /api/report/getTransaction`. Verifies `recordsFiltered` against rows collected and `totalAmount` to the cent. Loads DELETE-then-INSERT per `(terminal, date)` with a `NO_CHANGE` short-circuit and a shrink guard. Alerts to Teams, pings `HEARTBEAT_URL` on success. `--dry-run` and `--probe` supported. |
| `nets_mapping.py` | imported, not run | by `auresys_pull.py` and `nets_reconcile.py` | Single source of truth mapping Auresys terminal id (`SGKN_Mnnnn`) → `(machine_code, machine_name, outlet_name)`. Keyed on terminal id, not outlet name, because outlets change when a machine moves. `None` = no matching machine; the loader treats an unmapped terminal that is actually trading as a hard alert, never a silent skip. Roster captured 2026-08-14; ~5 reassignments a month, fixed here and in `MachineLookup`. |
| `nets_reconcile.py` | **automatic**, GitHub Actions | `nets-reconciliation.yml` cron 09:00 SGT on the 2nd, or manual dispatch | Playwright/chromium logs into the NETS/Auresys portal, downloads the month's CSV, fetches DB counts from `/api/internal/vend-counts` using `INTERNAL_API_KEY`, compares, posts a report to Teams. Carries its own `NETS_TO_DB` outlet-name → `MachineName` map (separate from `nets_mapping.py`). |
| `migrate_location_history.py` | **by hand / by git push**, GitHub Actions | `migrate-location-history.yml` — push to `ops/loc-migration` (preview, or apply when `MIGRATION_APPLY_REQUEST` exists) or manual dispatch | One-off. De-dupes `MachineLookup` rows that share a `MachineCode`, adds `UNIQUE(MachineCode)`, creates and back-fills `MachineLocationHistory`, and derives each relocation cutoff from the machine's vend gap. `--mode preview` is read-only. Writes `MIGRATION_PREVIEW.md` / `MIGRATION_RESULT.md`. |
| `migrate_joborders_into_delivery.py` | **by hand only** | `python3 migrate_joborders_into_delivery.py` (+ `--apply`) | One-off. Folds open, manually-raised job orders into delivery orders under the 2026-08-11 one-row model. Strict in-scope test (StatusCode 0, no complaint, no tasks, no KB, no images, not on a visit sheet). Preview by default; `--apply` commits one job order per transaction. **Self-contained by design — never imports `app` or `workorders`** (that would re-run `init_db()` and `seed_locations()` against production, and is circular anyway). |
| `seed_locations.py` | **by hand only** (and duplicated inside `app.py`) | `python3 seed_locations.py` | One-time upsert of location names + coordinates into `MachineLookup`. Note the same data exists as `LOCATION_SEED` in `app.py:341` and `app.seed_locations()` runs on **every worker boot** — but insert-only for existing codes (see L7). |
| `set_delivery_item.py` | **by hand only** | `python3 set_delivery_item.py --id N --content X --apply` | Changes one `WO_DeliveryItems.Content` value. Preview by default. Self-contained (pymssql + config only) for the same anti-circular-import reason. |
| `set_password.py` | **by hand**, and **dead** | — | Prints a `werkzeug` password hash "to copy into config.py". Predates Easy Auth; nothing in the app reads a password, `werkzeug.security` is not in `requirements.txt`, and `templates/login.html` is orphaned. Safe to delete. |
| `topups-2026-08-23/` | **not wired, not applied** | — | A staged change set (`topups_api.py`, `_topups_tab.html`, `apply_topups_ui.py`, `migration_topups_2026-08-23.sql`, `seed_flag_cards.py`, `probe_flag_card.py`). `TOPUPS_APPLY.md` opens with "**HOLD. Nothing here has been applied.**" Nothing outside that directory references it. Its step 2 requires setting `GCAL_SYNC = 0` first, because `gcal_sync` hard-deletes unassigned stops it does not recognise. |

Also present as reference SQL, run by hand in the Azure portal Query Editor:
`migration_2026-06-03.sql` (the V2 schema cutover), `migration_AZURE_2026-07-01.sql`,
`migration_location_history_2026-06-29_PART{A,B}*.sql`,
`migration_sales_schedule_2026-08-11.sql`, `history_repair_worksheet.sql`.

---

## 7. Known landmines

Each of these is a comment the code carries because something already broke.

**L1 — `pymssql` %-formatting. `workorders.py:2882-2886`.**
> "`'1%%'` — pymssql %-formats the query before sending it, so a literal `%` MUST be doubled in any statement that also passes parameters. (This was `'1%'` and raised TypeError on every call, i.e. delivery completion has never actually succeeded. Do not 'simplify' it back.)"

**L2 — vend dedup must not key on the event code. `app.py:293-302`.**
The machines sometimes write the same physical vend twice, occasionally under a
*different* event code at the same instant. Dedup keys on `(machine, timestamp)`
only. `dbo.VendEvents` is the canonical reference, but the hot-path queries
embed the same dedup inline with date filters *inside* the window subquery so
predicates apply before the window function.

**L3 — open redirect via `Location`-header whitespace stripping. `app.py:698-703`, echoed at `app.py:104-107` and `alpha_preview.py:148-151`.**
A `startswith("/")` guard is defeated because Werkzeug strips tabs and newlines
from the `Location` header *after* the check, so `"/\t/evil.com"` is emitted as
`"//evil.com"`. `switch_role` therefore uses a hard allowlist of exactly `/`,
`/alpha`, `/archive2608`. Embedded CR/LF additionally 500s.

**L4 — `/api/topups` is only `@login_required`. `templates/_legacy_tabs.html:12-13`.**
> "'Undo Last Topup' is Jinja-gated. `/api/topups` is only `@login_required`, so the server will happily let a driver rewrite any machine's `LastTopupTimestamp`."

The gate is in the template, not the server. Same applies to the POST.

**L5 — an expired session used to paint the demo seed. `alpha_preview.py:133-137`.**
> "A fetch() answered with a 302 to AAD makes `res.json()` throw, and `boot()` in alpha_preview.html catches that and falls back to its DEMO seed: on an expired session this app would have shown synthetic machines as if they were the fleet."

Fixed by testing API-vs-page *before* the sign-in check, and by passing `is_api`
explicitly per route rather than sniffing `request.path` (`:139-143`) now that
the blueprint owns `/`.

**L6 — the archive's rollback path must check claims AND active role. `app.py:657-682`.**
An admin whose `knm_active_role` cookie says `field_manager` would be locked out
of their own rollback path (claims check), or handed a field_manager template
whose every admin call 403s (active-role check). Both checks are present, and
`ALPHA_OK` (`app.py:1827-1846`) drops the admin gate entirely if the production
blueprint fails to import.

**L7 — the static seed used to revert live data. `app.py:469-472`.**
> "Machine already exists — NEVER overwrite from the static seed list. (The old UPDATE re-applied 2026-era names/coords on every deploy, silently reverting renames and completed moves.)"

Related cost: `seed_locations()` still runs at every worker boot and opens one
DB connection per seed row (106 connect/close cycles).

**L8 — duplicate `MachineLookup` rows caused the transaction-doubling bug. `app.py:1383-1385`; `migrate_location_history.py:11-20`.**
> "UPSERT: never create a second row for an existing MachineCode (that was the JOIN fan-out / 'doubling' bug)."

The permanent fix is `MachineLocationHistory` + per-vend location resolution +
`UNIQUE(MachineCode)`.

**L9 — location-filtered views silently lost machines. `app.py:816-819`.**
> "Fall back to the CURRENT MachineLookup name when no history interval covers the vend (machine added without a history row) — otherwise those machines silently vanish from location-filtered views."

Hence `COALESCE(loc.LocationName, ml.MachineName)` everywhere a location filter
is applied.

**L10 — a name change must declare rename vs move. `app.py:1436-1442`.**
RENAME relabels the open interval in place (history stays continuous); MOVE
closes the old interval and opens a new one at the effective time. `PUT
/api/admin/locations/<code>` 400s if `name_change_mode` is absent on a name
change, refuses a future move time, and refuses a move time that predates the
current stay.

**L11 — reassignment used to resurrect closed work orders. `workorders.py:1647-1654`.**
> "This route used to force StatusCode=0 unconditionally... it would resurrect a job order the manager had already closed (3) or accepted into review (2), leaving an open WO hanging off a closed complaint and silently emptying the review queue."

**L12 — a missing `scheduled_date` used to silently become today. `workorders.py:3237-3241`.**
> "Was: `or _sgt_today()`, which silently relabelled the stop with today's date and still returned success — the stop then sat on a day nobody was looking at."

**L13 — the board cap. `alpha_preview.py:342-350` and `:407-418`.**
`TOP 200 ORDER BY CreatedAt` would have pushed this morning's stops off the
board the moment sales keyed a quarter ahead. Now `_DELIVERY_CAP = 900`, rows
completed more than 30 days ago are dropped, and truncation is *reported*
(`health.workTruncated`) rather than silent. Completed stops are deliberately a
separate endpoint keyed on one date — both because they were the first thing
truncation threw away, and so a completed stop can never leak into
`openWork()` / `autoPlan` / `assignSite`.

**L14 — `partial`, never `error`, on the completed-day endpoint. `alpha_preview.py:641-644`.**
> "The client's `api()` helper treats ANY payload carrying `.error` as a hard failure — it returns null and toasts the value — so an `error` key here would both discard the rows that DID load and pop a bare word at the dispatcher."

**L15 — per-table `ScheduledDate` probing. `alpha_preview.py:274-279`.**
> "The ScheduledDate columns are added by `workorders.init_workorders_db`, which swallows each ALTER independently — so one table can have them and another not. Probe PER TABLE: getting this wrong makes an entire order type vanish from the UI behind a swallowed exception."

**L16 — the Apps Script feed must be GET, not POST. `gcal_feed.py:174-179`.**
> "Apps Script `/exec` answers with a 302 to `script.googleusercontent.com`... and requests downgrades a redirected POST to GET — which silently drops the JSON body, so doGet ran with no parameters and the script answered 'unauthorized'."

**L17 — outbound calls must never happen in a request. `gcal_feed.py:9-13`.**
> "app.py runs under a single gunicorn sync worker (Procfile has no `--workers`/`--threads`), so any outbound call made inside a request would block every other user for its full timeout."

**L18 — the legacy partials need the IIFE. `templates/_legacy_tabs.html:8-11`.**
> "the JS is wrapped in an IIFE and the names the inline `onclick=` attributes need are re-exported to `window`. Without the IIFE, `let _salesView` here and in alpha_preview.html is a SyntaxError that kills BOTH scripts."

Its CSS is scoped under `.lgc` for the same reason: `index.html`'s bare `input`,
`table`, `.card`, `.btn` rules would otherwise restyle the whole streamlined UI.

**L19 — two EMBEDS keys, one DOM node. `templates/alpha_preview.html:741-744`.**
> "Same pane as `fleet:refill#topups`. Two keys onto one element is fine... but it IS one shared DOM instance, so a half-filled form carries between the two entry points."

**L20 — the Knowledge Base pane has no server-side UI gating. `templates/alpha_preview.html:804-809`.**
> "Knowledge Base is manager-only because the embedded screen is the CAR EDITOR — it has no internal role gating, it relied on index.html hiding the tab, and every one of its Save/Delete buttons 403s for anyone else."

**L21 — the Azure Query Editor runs the whole pane as one batch. `migration_sales_schedule_2026-08-11.sql:9-17`.**
> "the Azure portal Query Editor executes the WHOLE PANE as one batch and ignores GO. A column added in the same batch as an index that references it fails to compile ('Invalid column name SeriesID') and takes the ALTERs down with it — the same trap that broke rev 1 of `migration_AZURE_2026-07-01.sql`."

Run these files one highlighted block at a time.

**L22 — movement unwind must branch on type, never guess. `workorders.py:2537-2556`.**
Deleting a *completed* movement order unwinds its dated history split: the
interval the move OPENED is deleted first so there are never two open intervals
(the filtered unique index `UX_MLH_OpenInterval` would be violated), then the
previous interval is re-opened by `HistoryID`.

**L23 — the Live status poller outlives its DOM. `templates/alpha_preview.html:754-758`.**
> "Leave it running and it writes into a `#fhGrid` that render() already destroyed, once every two minutes, forever."

`showEmbed()` calls `fhStop()`/`pbStop()` on **every** render, including back
into Live status itself.

**L24 — `GCAL_SYNC` hard-deletes unassigned stops. `topups-2026-08-23/TOPUPS_APPLY.md`, step 2.**
> "while it is on, `gcal_sync` creates its own stops for the next 28 days and **hard-deletes** any unassigned stop it does not recognise."

Anything keyed by another surface into an unassigned, in-horizon slot can
disappear within one poll interval.

---

## Loose ends worth a look

- **`templates/_workorders_tab.html` (1198 lines) and `templates/login.html` are orphaned** — no `{% include %}`, no `render_template`. So is `set_password.py` (and its `werkzeug` dependency, which is not in `requirements.txt`).
- **`CUTOVER_2026-08-23.md` says "code applied locally, NOT pushed"** but `origin/main` is at `771441c` and `git rev-list --count origin/main..HEAD` is 0 — it shipped. The doc is stale. Its "Open decisions" list (page title still says "beta", `/alpha` still serves the same code as `/`) is still accurate.
- **Heartbeat UTC/SGT asymmetry** — see §5.6. `app.py:1699` (`get_heartbeat`) uses bare `utcnow()` against a timebase every other module treats as SGT. `heartbeat_analysis` is not affected.
- **`/api/admin/march2026-vends`** is a hardcoded one-off from a past NETS cross-check and is still routed.
- **`requirements_additions.txt`** duplicates a subset of `requirements.txt` and is not read by anything.
- **`.bak` files litter the tree, and 8 of them are COMMITTED** — tracked: `app.py.bak-2026-06-03`, `app.py.bak-2026-07-04`, `app.py.bak-20260709-182019`, `app.py.bak-20260709-204306`, `workorders.py.bak-2026-06-03`, `templates/_workorders_tab.html.bak-2026-06-03`, `templates/index.html.bak-2026-06-03`, `templates/index.html.bak-2026-07-04`. The rest (e.g. `app.py.bak-20260823`, `workorders.py.bak-2026-08-11`) are untracked. `.gitignore` covers none of them.
