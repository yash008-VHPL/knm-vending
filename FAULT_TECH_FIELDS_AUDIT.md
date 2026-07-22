# Fault Report + Tech Support — Fields Audit

Date: 2026-06-03  ·  Owner: Yash  ·  Status: AWAITING APPROVAL

Cross-reference of every field you specified against the existing `WO_*` schema. Only the **deltas** (additions / type-changes) need migration. No tables are duplicated.

---

## A. WO_Complaints  →  "Fault Report" tab

| Your field | Existing column | Action |
|---|---|---|
| Serial No of machine | `MachineCode NVARCHAR(50)` | **Keep** (rename label in UI to "Machine Code / Serial") |
| DateTime (system entry) | `SubmittedAt DATETIME2` | **Keep** |
| First report time/date (from customer) | — | **ADD** `FirstReportedAt DATETIME2 NULL` |
| Reported by (text — who phoned/walked in) | — | **ADD** `ReportedBy NVARCHAR(255) NULL` |
| Submitter (CS staff entering) | `SubmitterEmail NVARCHAR(255)` | **Keep** (auto from auth) |
| Verbatim report | `Description NVARCHAR(MAX)` | **Keep** (rename label) |
| EventCode (6-digit, from your parser) | — | **ADD** `EventCode INT NULL` |
| Perceived urgency | — | **ADD** `PerceivedUrgency TINYINT NOT NULL DEFAULT 1` (0=low, 1=normal, 2=high) |
| Status | `Status NVARCHAR(20)` ('open'/'in_progress'/'closed') | **CHANGE** to `TINYINT NOT NULL DEFAULT 0` (0=fresh, 1=assigned, 2=closed). Migrate existing rows. |
| Source (self vs customer_chat) | `Source NVARCHAR(20)` | **Keep** — add value `'fault'` if you want to distinguish, otherwise leave. |
| Impact description | `ImpactDescription NVARCHAR(MAX)` | **Keep** (optional in fault form) |
| Impact amount $ | `ImpactAmount DECIMAL(18,2)` | **Keep** (optional in fault form) |
| ComplaintID | `ComplaintID INT IDENTITY PK` | **Keep** |
| Linked WorkOrder ID | `JobOrderID INT NULL` | **Keep** |
| Images | via `WO_Images` (ParentType='complaint') | **Keep** — bytes move to Azure Blob (see §C) |

**Net for Complaints: 4 ADDs, 1 TYPE CHANGE.**

---

## B. WO_JobOrders  →  "Tech Support" tab

| Your field | Existing column | Action |
|---|---|---|
| Serial No of machine | `MachineCode NVARCHAR(50)` | **Keep** |
| DateTime (manager creates WO) | `CreatedAt DATETIME2` | **Keep** |
| EventCode (6-digit, carried from complaint) | — | **ADD** `EventCode INT NULL` |
| WorkOrder ID | `JobOrderID INT IDENTITY PK` | **Keep** |
| Assigned to (UserID) | `AssignedTo NVARCHAR(255)` | **Keep** |
| Priority | `Priority NVARCHAR(10)` ('low'/'normal'/'high') | **CHANGE** to `TINYINT NOT NULL DEFAULT 1` (0=low, 1=normal, 2=high). Migrate existing rows. |
| Status | `Status NVARCHAR(20)` ('open'/'in_progress'/'completed') | **CHANGE** to `TINYINT NOT NULL DEFAULT 0` (0=assigned, 1=needs_assistance, 2=closed). Migrate existing rows. |
| Diagnosis (manager's initial hypothesis) | — | **ADD** `Diagnosis NVARCHAR(MAX) NULL` |
| Suspected fix / proposed steps (manager's plan) | — | **ADD** `ProposedFix NVARCHAR(MAX) NULL` |
| Free notes | `Notes NVARCHAR(MAX)` | **Keep** |
| Final tech report | `Report NVARCHAR(MAX)` | **Keep** |
| Root cause (post-fix actual) | `RootCause NVARCHAR(MAX)` | **Keep** |
| Corrective action (what was done) | `CorrectiveAction NVARCHAR(MAX)` | **Keep** |
| Preventive action | `PreventiveAction NVARCHAR(MAX)` | **Keep** |
| Why stuck (when status=1) | — | **ADD** `LastBlockReason NVARCHAR(MAX) NULL` |
| ComplaintID link | `ComplaintID INT NULL` | **Keep** |
| Created by | `CreatedBy NVARCHAR(255)` | **Keep** (auto from auth) |
| Completed by / at | `CompletedBy`, `CompletedAt` | **Keep** |

**Net for JobOrders: 4 ADDs, 2 TYPE CHANGES.**

---

## C. WO_Images  →  SharePoint migration

| Your need | Existing column | Action |
|---|---|---|
| SP item id (Graph driveItem.id) | — | **ADD** `SPItemID NVARCHAR(255) NULL` |
| SP web URL | — | **ADD** `SPWebURL NVARCHAR(1024) NULL` |
| Inline bytes (legacy + rollback) | `ImageData VARBINARY(MAX) NOT NULL` | **CHANGE** to `NULL`. Keep old rows readable; new rows go to SP. |
| Parent linkage | `ParentType + ParentID + Stage` | **Keep**. Add stage values: `'task_done'`, `'task_blocked'`. |
| File name, content type, uploaded by/at | existing | **Keep** |

**SharePoint setup (CONFIRMED 2026-06-03)**
- Site: `https://kopinearme.sharepoint.com/sites/AppDataBackEnd`
- Complaint files: `Documents/ComplaintUploads/`
- Work Order files: `Documents/WorkOrderUploads/`
- Subfolder convention: `{YYYY}/{MM}/{DisplayID}/` for browsability at scale
- Auth: Graph API via Azure App registration with delegated `Sites.ReadWrite.All` (or Application permission if running as service principal). Will need `MSAL` Python lib + new `config.py` keys: `MS_TENANT_ID`, `MS_CLIENT_ID`, `MS_CLIENT_SECRET` (Key Vault recommended).
- Delivery: backend proxies via `/api/wo/images/<id>` so Easy Auth gate still applies. Never expose raw SP URLs to browser.

**Display ID convention (CONFIRMED 2026-06-03 — pending format reconcile)**
- Complaint: `KNM-CMP-NNNN-YYMM` (NNNN resets monthly, zero-padded)
- Work Order: `KNM-WkO-NNNN-YYMM` (NNNN resets monthly, zero-padded)
- Add column `DisplayID NVARCHAR(30) NOT NULL` to both `WO_Complaints` and `WO_JobOrders`. Generated at insert time via per-month counter query.
- Indexed UNIQUE on DisplayID.
- SP filenames use the DisplayID + uploader-supplied filename: `{DisplayID}_{original-filename}`.

**One-time backfill script** (separate, run once): for each row with `ImageData`, upload bytes to SP, write `SPItemID` + `SPWebURL`, NULL out `ImageData`.

---

## D-KB. Knowledge Base (NEW tables — for auto-suggested diagnoses + tickboxes)

### `WO_KB_Entries`
| Column | Type | Notes |
|---|---|---|
| `KBID` | `INT IDENTITY PK` | |
| `EventCode` | `INT NULL` | 6xxxxx (complaint) or 8xxxxx (repair). Indexed. |
| `Title` | `NVARCHAR(255) NOT NULL` | Short label, e.g. "Coin jam — 50¢ slot" |
| `Diagnosis` | `NVARCHAR(MAX) NULL` | Suggested diagnosis paragraph |
| `SuggestedFix` | `NVARCHAR(MAX) NULL` | Suggested fix paragraph (free text) |
| `UseCount` | `INT NOT NULL DEFAULT 0` | Increments when manager accepts this KB into a WO |
| `CreatedBy` | `NVARCHAR(255)` | |
| `CreatedAt` | `DATETIME2 DEFAULT SYSUTCDATETIME()` | |
| `UpdatedBy` | `NVARCHAR(255) NULL` | |
| `UpdatedAt` | `DATETIME2 NULL` | |

Index: `IX_WOKB_EventCode ON (EventCode)`.

### `WO_KB_Tickboxes`
| Column | Type | Notes |
|---|---|---|
| `TBID` | `INT IDENTITY PK` | |
| `KBID` | `INT NOT NULL` | FK → `WO_KB_Entries` |
| `SeqNum` | `INT NOT NULL` | Display order |
| `Label` | `NVARCHAR(500) NOT NULL` | Tickbox text |

Index: `IX_WOKBTB_KB ON (KBID, SeqNum)`.

**Flow**
1. Manager creates WO → enters EventCode → backend queries `WO_KB_Entries WHERE EventCode=?` → returns array of KB entries (sorted by `UseCount DESC`).
2. UI shows suggestions; manager can accept one (auto-fills `Diagnosis`, `ProposedFix`, and copies tickboxes into `WO_JobOrderTasks`) or skip and type free-text.
3. On accept: `UPDATE WO_KB_Entries SET UseCount = UseCount + 1 WHERE KBID = ?`.
4. Free-text-tickbox input always present, even when KB suggestions exist.
5. Manager-only sub-tab **"Manage KB"** for CRUD on `WO_KB_Entries` + `WO_KB_Tickboxes`. Field staff cannot see/edit.

**Seed**: ship empty. Will populate over time as KB matures.

---

## D. WO_JobOrderTasks (NEW table — tickboxes)

The only genuinely new table.

| Column | Type | Notes |
|---|---|---|
| `TaskID` | `INT IDENTITY PK` | |
| `JobOrderID` | `INT NOT NULL` | FK → WO_JobOrders |
| `SeqNum` | `INT NOT NULL` | Display order |
| `Label` | `NVARCHAR(500) NOT NULL` | Tickbox text (manager-defined per WO) |
| `Done` | `BIT NOT NULL DEFAULT 0` | Worker tick |
| `BlockedNote` | `NVARCHAR(MAX) NULL` | Worker's note if blocked on this step |
| `CompletedBy` | `NVARCHAR(255) NULL` | Auth email |
| `CompletedAt` | `DATETIME2 NULL` | |

Photos per task: stored in `WO_Images` with `ParentType='task'`, `ParentID=TaskID`, `Stage='task_done'|'task_blocked'`.

Index: `IX_WOJOTask_JobOrder ON (JobOrderID, SeqNum)`.

---

## E. Status code lookup (Python enums — single source of truth)

To answer your point #3 (storage vs readability): store TINYINT, expose labels in code so SQL stays small but logs/UI stay human.

```python
# workorders.py (new section)

COMPLAINT_STATUS = {0: "fresh", 1: "assigned", 2: "closed"}
JOBORDER_STATUS  = {0: "assigned", 1: "needs_assistance", 2: "closed"}
PRIORITY         = {0: "low", 1: "normal", 2: "high"}

def status_label(table, code):
    return {"complaint": COMPLAINT_STATUS,
            "joborder":  JOBORDER_STATUS,
            "priority":  PRIORITY}[table].get(code, str(code))
```

Backend always serializes both the int and the label to the frontend:
```json
{"status": 1, "status_label": "assigned"}
```
This keeps queries fast and logs grep-able with no string columns.

---

## F. Things I am NOT doing (per your directives)

- **Not** hooking to `/api/heartbeat`. Will not auto-fill MachineCode from red machines. (revisit later)
- **Not** auto-creating tickets from heartbeat events.
- **Not** removing the legacy V1 drop block in `init_workorders_db()` yet — keep until stable.
- **Not** editing any existing file. All work happens on `.py` / `.html` copies; backups already made (`*.bak-2026-06-03`).
- **Not** generating fabricated names anywhere. Reported-by text is free-form user input.

---

## G. Migration order (when you approve)

1. **Schema**: ALTER WO_Complaints + ALTER WO_JobOrders + ALTER WO_Images + CREATE WO_JobOrderTasks. Idempotent SQL added to `init_workorders_db()`.
2. **Data migrate**: convert existing Status/Priority strings → TINYINT.
3. **Routes**: extend `/api/wo/complaints/*` and `/api/wo/joborders/*` for new fields; add `/api/wo/joborders/<id>/tasks` CRUD; switch `/api/wo/images` upload to Azure Blob.
4. **UI**: rename existing "Work Orders" tab → split into **"Fault Report"** (current Complaint Submission view) + **"Tech Support"** (current Field Ops view). Add new fields to forms. Add tickbox checklist UI to JobOrder detail.
5. **Backfill script**: separate one-shot for migrating existing image bytes → blob.

---

## H. Decisions locked (2026-06-03)

1. **EventCode**: CS may leave blank at complaint time; populated later by parser or manager. CHECK constraint enforces 6xxxxx (complaints) / 8xxxxx (WOs).
2. **Tickboxes**: Manager free-text per WO. KB-driven suggestions kick in once `WO_KB_Entries` is populated. Free-text input always present.
3. **Storage**: SharePoint — `kopinearme.sharepoint.com/sites/AppDataBackEnd`, `Documents/ComplaintUploads/` and `Documents/WorkOrderUploads/`. No Azure Blob.
4. **Visibility**: Worker sees only own assigned WOs (`scope=mine` enforced server-side per role). Manager/admin see all.
5. **"Reported by" PII**: Internal-only (CS rep + management + field). No external sharing. Free text allowed.

**DisplayID format (CONFIRMED)**: `KNM-CMP-NNNN-YYMM` and `KNM-WkO-NNNN-YYMM`. NNNN zero-padded, resets monthly. Unique index per table.

Ready to write idempotent migration SQL + route/UI diffs.
