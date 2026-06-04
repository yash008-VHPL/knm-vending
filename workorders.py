"""KNM Work Orders v2 — Fault Report + Tech Support module.

This file is a side-by-side rewrite of workorders.py for the 2026-06-03 schema
migration. It is NOT loaded until cutover (rename to workorders.py).

Changes vs v1:
  • Status / Priority stored as TINYINT (StatusCode / PriorityCode).
  • Complaint: + FirstReportedAt, ReportedBy, EventCode, PerceivedUrgency, DisplayID.
  • JobOrder:  + EventCode, Diagnosis, ProposedFix, LastBlockReason, DisplayID.
  • Images now stored in SharePoint (Documents/ComplaintUploads/ and
    Documents/WorkOrderUploads/) via sharepoint_helper. DB stores SPItemID +
    SPWebURL only; raw bytes proxied through /api/wo/images/<id> for Easy Auth.
  • New tables: WO_JobOrderTasks (tickbox checklist), WO_KB_Entries +
    WO_KB_Tickboxes (knowledge base), WO_Counters (per-month NNNN allocator).
  • New endpoints: /joborders/<id>/tasks*, /kb*, /kb/suggest.
  • DisplayID format: KNM-CMP-NNNN-YYMM (complaints), KNM-WkO-NNNN-YYMM (WOs).

Heartbeat hook deferred per Yash's directive (2026-06-03).
"""
from __future__ import annotations

from functools import wraps
from datetime import datetime
import base64
import io

from flask import Blueprint, request, jsonify, Response

# Reuse helpers from the existing vending app so auth and DB stay consistent.
from app import (
    get_current_user, get_role, get_connection,
    to_ole_date, from_ole_date,
)

# SharePoint helper — module-level import; nothing executes here.
import sharepoint_helper as sp

workorders_bp = Blueprint("workorders", __name__)


# ── Roles ─────────────────────────────────────────────────────────────────────

ROLE_ADMIN         = "admin"
ROLE_OPERATOR      = "operator"
ROLE_FIELD_MANAGER = "field_manager"

OPERATOR_ROLES = {ROLE_OPERATOR, ROLE_FIELD_MANAGER, ROLE_ADMIN}
MANAGER_ROLES  = {ROLE_FIELD_MANAGER, ROLE_ADMIN}


# ── Status / Priority code maps ───────────────────────────────────────────────
# Stored as TINYINT in DB; expanded to strings in API responses.

COMPLAINT_STATUS = {0: "fresh",    1: "assigned",         2: "closed"}
JOBORDER_STATUS  = {0: "assigned", 1: "needs_assistance", 2: "closed"}
PRIORITY         = {0: "low",      1: "normal",           2: "high"}


def _label(mapping: dict, code) -> str:
    if code is None:
        return ""
    try:
        return mapping.get(int(code), str(code))
    except (TypeError, ValueError):
        return str(code)


# ── 8 standard delivery items (names provided by Yash; placeholder) ───────────

DELIVERY_ITEMS = [
    "Item 1", "Item 2", "Item 3", "Item 4",
    "Item 5", "Item 6", "Item 7", "Item 8",
]


# ── Auth helpers (API-style: JSON 401/403, never redirect) ────────────────────

def api_login_required(f):
    """Any signed-in user with any app role can access."""
    @wraps(f)
    def inner(*args, **kwargs):
        email = get_current_user()
        if not email:
            return jsonify({"error": "Not signed in."}), 401
        if not get_role(email):
            return jsonify({"error": "You are not authorised to use this app."}), 403
        return f(*args, **kwargs)
    return inner


def require_roles(*roles):
    allowed = set(roles)

    def deco(f):
        @wraps(f)
        def inner(*args, **kwargs):
            email = get_current_user()
            if not email:
                return jsonify({"error": "Not signed in."}), 401
            if get_role(email) not in allowed:
                return jsonify({"error": "You do not have permission for this action."}), 403
            return f(*args, **kwargs)
        return inner
    return deco


# ── Schema init (idempotent; runs on app startup) ─────────────────────────────

def init_workorders_db():
    """Create WO_* tables if missing. Safe to run on every startup.

    NOTE: schema migrations (column additions, type changes, indexes) are
    handled by migration_2026-06-03.sql. This function only handles the
    fresh-install path so a new environment starts up cleanly.
    """
    tables = [
        ("WO_Complaints", """
            CREATE TABLE WO_Complaints (
                ComplaintID       INT IDENTITY(1,1) PRIMARY KEY,
                Description       NVARCHAR(MAX)  NOT NULL,
                Source            NVARCHAR(20)   NOT NULL DEFAULT 'self',
                ImpactDescription NVARCHAR(MAX)  NULL,
                ImpactAmount      DECIMAL(18,2)  NULL,
                ImpactSeverity    TINYINT        NULL,
                MachineName       NVARCHAR(255)  NULL,
                MachineCode       NVARCHAR(50)   NULL,
                Status            NVARCHAR(20)   NOT NULL DEFAULT 'open',
                StatusCode        TINYINT        NOT NULL DEFAULT 0,
                JobOrderID        INT            NULL,
                SubmitterEmail    NVARCHAR(255)  NOT NULL,
                SubmittedAt       DATETIME2      NOT NULL DEFAULT SYSUTCDATETIME(),
                FirstReportedAt   DATETIME2      NULL,
                ReportedBy        NVARCHAR(255)  NULL,
                EventCode         INT            NULL,
                PerceivedUrgency  TINYINT        NOT NULL DEFAULT 1,
                DisplayID         NVARCHAR(30)   NULL
            )
        """),
        ("WO_JobOrders", """
            CREATE TABLE WO_JobOrders (
                JobOrderID        INT IDENTITY(1,1) PRIMARY KEY,
                ComplaintID       INT            NULL,
                MachineName       NVARCHAR(255)  NOT NULL,
                MachineCode       NVARCHAR(50)   NULL,
                Notes             NVARCHAR(MAX)  NULL,
                AssignedTo        NVARCHAR(255)  NULL,
                Priority          NVARCHAR(10)   NOT NULL DEFAULT 'normal',
                PriorityCode      TINYINT        NOT NULL DEFAULT 1,
                Status            NVARCHAR(20)   NOT NULL DEFAULT 'open',
                StatusCode        TINYINT        NOT NULL DEFAULT 0,
                Report            NVARCHAR(MAX)  NULL,
                RootCause         NVARCHAR(MAX)  NULL,
                CorrectiveAction  NVARCHAR(MAX)  NULL,
                PreventiveAction  NVARCHAR(MAX)  NULL,
                EventCode         INT            NULL,
                Diagnosis         NVARCHAR(MAX)  NULL,
                ProposedFix       NVARCHAR(MAX)  NULL,
                LastBlockReason   NVARCHAR(MAX)  NULL,
                DisplayID         NVARCHAR(30)   NULL,
                CreatedBy         NVARCHAR(255)  NOT NULL,
                CreatedAt         DATETIME2      NOT NULL DEFAULT SYSUTCDATETIME(),
                CompletedBy       NVARCHAR(255)  NULL,
                CompletedAt       DATETIME2      NULL
            )
        """),
        ("WO_DeliveryOrders", """
            CREATE TABLE WO_DeliveryOrders (
                DeliveryOrderID   INT IDENTITY(1,1) PRIMARY KEY,
                MachineName       NVARCHAR(255)  NOT NULL,
                MachineCode       NVARCHAR(50)   NULL,
                Notes             NVARCHAR(MAX)  NULL,
                AssignedTo        NVARCHAR(255)  NULL,
                Priority          NVARCHAR(10)   NOT NULL DEFAULT 'normal',
                Status            NVARCHAR(20)   NOT NULL DEFAULT 'open',
                Item1Qty INT NOT NULL DEFAULT 0, Item2Qty INT NOT NULL DEFAULT 0,
                Item3Qty INT NOT NULL DEFAULT 0, Item4Qty INT NOT NULL DEFAULT 0,
                Item5Qty INT NOT NULL DEFAULT 0, Item6Qty INT NOT NULL DEFAULT 0,
                Item7Qty INT NOT NULL DEFAULT 0, Item8Qty INT NOT NULL DEFAULT 0,
                RecipientName     NVARCHAR(255)  NULL,
                CreatedBy         NVARCHAR(255)  NOT NULL,
                CreatedAt         DATETIME2      NOT NULL DEFAULT SYSUTCDATETIME(),
                CompletedBy       NVARCHAR(255)  NULL,
                CompletedAt       DATETIME2      NULL
            )
        """),
        ("WO_Images", """
            CREATE TABLE WO_Images (
                ImageID      INT IDENTITY(1,1) PRIMARY KEY,
                ParentType   NVARCHAR(20)   NOT NULL,
                ParentID     INT            NOT NULL,
                Stage        NVARCHAR(20)   NOT NULL,
                ImageData    VARBINARY(MAX) NULL,
                SPItemID     NVARCHAR(255)  NULL,
                SPWebURL     NVARCHAR(1024) NULL,
                ContentType  NVARCHAR(100)  NOT NULL,
                FileName     NVARCHAR(255)  NULL,
                UploadedBy   NVARCHAR(255)  NOT NULL,
                UploadedAt   DATETIME2      NOT NULL DEFAULT SYSUTCDATETIME()
            )
        """),
        ("WO_Activity", """
            CREATE TABLE WO_Activity (
                ActivityID   INT IDENTITY(1,1) PRIMARY KEY,
                ParentType   NVARCHAR(20)   NOT NULL,
                ParentID     INT            NOT NULL,
                Action       NVARCHAR(50)   NOT NULL,
                Detail       NVARCHAR(MAX)  NULL,
                ByUser       NVARCHAR(255)  NOT NULL,
                AtTime       DATETIME2      NOT NULL DEFAULT SYSUTCDATETIME()
            )
        """),
        ("WO_JobOrderTasks", """
            CREATE TABLE WO_JobOrderTasks (
                TaskID        INT IDENTITY(1,1) PRIMARY KEY,
                JobOrderID    INT             NOT NULL,
                SeqNum        INT             NOT NULL,
                Label         NVARCHAR(500)   NOT NULL,
                Done          BIT             NOT NULL DEFAULT 0,
                BlockedNote   NVARCHAR(MAX)   NULL,
                CompletedBy   NVARCHAR(255)   NULL,
                CompletedAt   DATETIME2       NULL
            )
        """),
        ("WO_KB_Entries", """
            CREATE TABLE WO_KB_Entries (
                KBID          INT IDENTITY(1,1) PRIMARY KEY,
                EventCode     INT             NULL,
                Title         NVARCHAR(255)   NOT NULL,
                Diagnosis     NVARCHAR(MAX)   NULL,
                SuggestedFix  NVARCHAR(MAX)   NULL,
                Symptom                   NVARCHAR(MAX) NULL,
                DiagnosticConfirmation    NVARCHAR(MAX) NULL,
                RootCause                 NVARCHAR(MAX) NULL,
                CorrectiveAction          NVARCHAR(MAX) NULL,
                PreventiveAction          NVARCHAR(MAX) NULL,
                VerificationOfCompletion  NVARCHAR(MAX) NULL,
                UseCount      INT             NOT NULL DEFAULT 0,
                CreatedBy     NVARCHAR(255)   NOT NULL,
                CreatedAt     DATETIME2       NOT NULL DEFAULT SYSUTCDATETIME(),
                UpdatedBy     NVARCHAR(255)   NULL,
                UpdatedAt     DATETIME2       NULL
            )
        """),
        ("WO_KB_Tickboxes", """
            CREATE TABLE WO_KB_Tickboxes (
                TBID    INT IDENTITY(1,1) PRIMARY KEY,
                KBID    INT             NOT NULL,
                SeqNum  INT             NOT NULL,
                Label   NVARCHAR(500)   NOT NULL
            )
        """),
        ("WO_Counters", """
            CREATE TABLE WO_Counters (
                Kind      NVARCHAR(10)  NOT NULL,
                YYMM      CHAR(4)       NOT NULL,
                NextSeq   INT           NOT NULL,
                CONSTRAINT PK_WO_Counters PRIMARY KEY (Kind, YYMM)
            )
        """),
    ]
    indexes = [
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_WOImg_Parent') "
        "CREATE INDEX IX_WOImg_Parent ON WO_Images (ParentType, ParentID)",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_WOAct_Parent') "
        "CREATE INDEX IX_WOAct_Parent ON WO_Activity (ParentType, ParentID)",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_WOJO_Assigned') "
        "CREATE INDEX IX_WOJO_Assigned ON WO_JobOrders (AssignedTo, StatusCode)",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_WODO_Assigned') "
        "CREATE INDEX IX_WODO_Assigned ON WO_DeliveryOrders (AssignedTo, Status)",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_WOJOTask_JobOrder') "
        "CREATE INDEX IX_WOJOTask_JobOrder ON WO_JobOrderTasks (JobOrderID, SeqNum)",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_WOKB_EventCode') "
        "CREATE INDEX IX_WOKB_EventCode ON WO_KB_Entries (EventCode, UseCount DESC)",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_WOKBTB_KB') "
        "CREATE INDEX IX_WOKBTB_KB ON WO_KB_Tickboxes (KBID, SeqNum)",
    ]

    conn = get_connection()
    cursor = conn.cursor()
    for name, create_sql in tables:
        try:
            cursor.execute(f"""
                IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = '{name}')
                {create_sql}
            """)
            conn.commit()
        except Exception as e:
            print(f"[init_workorders_db] create {name} failed: {e}")
    for idx_sql in indexes:
        try:
            cursor.execute(idx_sql)
            conn.commit()
        except Exception as e:
            print(f"[init_workorders_db] index skipped: {e}")
    conn.close()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _iso(dt):
    if not dt:
        return None
    return dt.replace(microsecond=0).isoformat() + "Z"


def _log_activity(cursor, parent_type, parent_id, action, detail, user):
    cursor.execute(
        "INSERT INTO WO_Activity (ParentType, ParentID, Action, Detail, ByUser) "
        "VALUES (%s, %s, %s, %s, %s)",
        (parent_type, parent_id, action, detail, user),
    )


def _images_for(cursor, parent_type, parent_id, stage=None):
    if stage:
        cursor.execute(
            "SELECT ImageID, Stage, ContentType, FileName, UploadedBy, UploadedAt, SPWebURL "
            "FROM WO_Images WHERE ParentType=%s AND ParentID=%s AND Stage=%s "
            "ORDER BY UploadedAt, ImageID",
            (parent_type, parent_id, stage),
        )
    else:
        cursor.execute(
            "SELECT ImageID, Stage, ContentType, FileName, UploadedBy, UploadedAt, SPWebURL "
            "FROM WO_Images WHERE ParentType=%s AND ParentID=%s "
            "ORDER BY UploadedAt, ImageID",
            (parent_type, parent_id),
        )
    return [{
        "id": r[0], "stage": r[1], "content_type": r[2],
        "file_name": r[3], "uploaded_by": r[4], "uploaded_at": _iso(r[5]),
        "sp_web_url": r[6],
    } for r in cursor.fetchall()]


def _activity_for(cursor, parent_type, parent_id):
    cursor.execute(
        "SELECT Action, Detail, ByUser, AtTime FROM WO_Activity "
        "WHERE ParentType=%s AND ParentID=%s ORDER BY AtTime DESC, ActivityID DESC",
        (parent_type, parent_id),
    )
    return [{
        "action": r[0], "detail": r[1], "by": r[2], "at": _iso(r[3]),
    } for r in cursor.fetchall()]


def _decode_data_url(data_url):
    """('data:image/jpeg;base64,XXXX', ...) -> (bytes, content_type)."""
    if not isinstance(data_url, str) or not data_url.startswith("data:"):
        return None, None
    try:
        header, b64 = data_url.split(",", 1)
        content_type = header.split(";")[0].replace("data:", "").strip() or "image/jpeg"
        if not content_type.startswith("image/"):
            return None, None
        return base64.b64decode(b64), content_type
    except Exception:
        return None, None


# ── DisplayID generator (atomic per-month NNNN allocator) ─────────────────────

def allocate_display_id(cursor, kind: str) -> str:
    """
    kind: 'CMP' (complaints) or 'WkO' (work orders)
    Returns: 'KNM-CMP-NNNN-YYMM' or 'KNM-WkO-NNNN-YYMM'.

    Uses WO_Counters with row-level locking to atomically allocate a new
    sequence number for the current YYMM. Caller must commit.
    """
    if kind not in ("CMP", "WkO"):
        raise ValueError(f"Unknown DisplayID kind: {kind!r}")
    now = datetime.utcnow()
    yymm = now.strftime("%y%m")

    # MERGE upsert with OUTPUT — atomic next-seq under WO_Counters PK lock.
    cursor.execute("""
        MERGE WO_Counters WITH (HOLDLOCK) AS tgt
        USING (SELECT %s AS Kind, %s AS YYMM) AS src
           ON tgt.Kind = src.Kind AND tgt.YYMM = src.YYMM
        WHEN MATCHED THEN
            UPDATE SET NextSeq = tgt.NextSeq + 1
        WHEN NOT MATCHED THEN
            INSERT (Kind, YYMM, NextSeq) VALUES (src.Kind, src.YYMM, 2)
        OUTPUT
            CASE WHEN $action = 'INSERT' THEN 1 ELSE inserted.NextSeq - 1 END AS Seq;
    """, (kind, yymm))
    row = cursor.fetchone()
    if not row:
        raise RuntimeError("Counter allocation returned no row.")
    seq = int(row[0])
    return f"KNM-{kind}-{seq:04d}-{yymm}"


# ── Image upload helpers (SP-backed) ──────────────────────────────────────────

def _kind_for_parent(parent_type: str) -> str:
    """Map parent_type → SP folder kind."""
    if parent_type == "complaint":
        return "complaint"
    # joborder, task → workorder folder
    return "workorder"


def _display_for_parent(cursor, parent_type: str, parent_id: int) -> str:
    """Fetch the DisplayID for the parent so SP files land in the right folder."""
    if parent_type == "complaint":
        cursor.execute("SELECT DisplayID FROM WO_Complaints WHERE ComplaintID=%s", (parent_id,))
    elif parent_type == "joborder":
        cursor.execute("SELECT DisplayID FROM WO_JobOrders WHERE JobOrderID=%s", (parent_id,))
    elif parent_type == "task":
        cursor.execute("""
            SELECT j.DisplayID FROM WO_JobOrderTasks t
            INNER JOIN WO_JobOrders j ON j.JobOrderID = t.JobOrderID
            WHERE t.TaskID = %s
        """, (parent_id,))
    else:
        return "UNKNOWN"
    row = cursor.fetchone()
    return (row[0] if row and row[0] else f"{parent_type}-{parent_id}")


def _save_image_to_sp(cursor, parent_type, parent_id, stage, file_name,
                      content_type, raw_bytes, uploaded_by) -> int:
    """Upload to SP, insert WO_Images row, return ImageID. Caller commits."""
    display_id = _display_for_parent(cursor, parent_type, parent_id)
    now = datetime.utcnow()
    sp_item_id, web_url, _path = sp.upload_bytes(
        kind        = _kind_for_parent(parent_type),
        display_id  = display_id,
        year        = now.year,
        month       = now.month,
        file_name   = file_name or f"{stage}.bin",
        data        = raw_bytes,
        content_type = content_type or "application/octet-stream",
    )
    cursor.execute("""
        INSERT INTO WO_Images
            (ParentType, ParentID, Stage, ImageData, SPItemID, SPWebURL,
             ContentType, FileName, UploadedBy)
        OUTPUT INSERTED.ImageID
        VALUES (%s, %s, %s, NULL, %s, %s, %s, %s, %s)
    """, (parent_type, parent_id, stage, sp_item_id, web_url,
          content_type, file_name, uploaded_by))
    return int(cursor.fetchone()[0])


def _ingest_data_urls(cursor, parent_type, parent_id, stage, data_urls, user):
    """Accept a list of data-URL strings; upload each to SP. Returns count."""
    if not data_urls:
        return 0
    count = 0
    for du in data_urls:
        raw, ctype = _decode_data_url(du)
        if not raw:
            continue
        _save_image_to_sp(
            cursor, parent_type, parent_id, stage,
            file_name=f"{stage}-{count+1}.jpg",
            content_type=ctype or "image/jpeg",
            raw_bytes=raw, uploaded_by=user,
        )
        count += 1
    return count


# ── Bootstrap ─────────────────────────────────────────────────────────────────

@workorders_bp.route("/bootstrap")
@api_login_required
def api_bootstrap():
    """Tells the UI who the user is, what role, what tabs to show, delivery
    item labels, and status/priority code maps."""
    email = get_current_user()
    role  = get_role(email)
    tabs = ["fault_report"]
    if role in MANAGER_ROLES:
        tabs += ["tech_support", "kb_admin", "field_ops", "delivery", "manager"]
    elif role in OPERATOR_ROLES:
        tabs += ["tech_support", "field_ops", "delivery"]
    return jsonify({
        "email": email,
        "role":  role,
        "tabs":  tabs,
        "delivery_items": DELIVERY_ITEMS,
        "complaint_status": COMPLAINT_STATUS,
        "joborder_status":  JOBORDER_STATUS,
        "priority":         PRIORITY,
    })


# ── Machines (for dropdowns in submission forms) ──────────────────────────────

@workorders_bp.route("/machines")
@api_login_required
def api_machines():
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT MachineName, MachineCode FROM MachineLookup
            ORDER BY MachineName
        """)
        rows = cursor.fetchall()
        conn.close()
        return jsonify([{"name": r[0], "code": str(r[1])} for r in rows])
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500


# ── Complaints  →  "Fault Report" tab ─────────────────────────────────────────

@workorders_bp.route("/complaints", methods=["POST"])
@api_login_required
def api_complaint_create():
    """Customer-service rep submits a fault report.
    Body (JSON):
        description        (required)
        machine_code       (recommended)
        machine_name       (optional — looked up if missing)
        first_reported_at  (ISO8601 — when customer originally reported)
        reported_by        (free text — who called in)
        event_code         (optional int 600000–699999)
        perceived_urgency  (0=low, 1=normal, 2=high; default 1)
        impact_description (optional)
        impact_severity    (optional int 1-5; 1=minimal, 5=critical)
        source             ('self' | 'customer_chat'; default 'self')
        images             (optional list of data: URLs)
    """
    data = request.get_json(silent=True) or {}
    desc = (data.get("description") or "").strip()
    if not desc:
        return jsonify({"error": "Please describe the complaint."}), 400

    source = (data.get("source") or "self").strip().lower()
    if source not in ("self", "customer_chat"):
        source = "self"

    impact_desc = (data.get("impact_description") or "").strip() or None
    impact_severity = data.get("impact_severity")
    if impact_severity in (None, "", "null"):
        impact_severity = None
    else:
        try:
            impact_severity = int(impact_severity)
            if not (1 <= impact_severity <= 5):
                return jsonify({"error": "Impact severity must be 1-5."}), 400
        except (TypeError, ValueError):
            return jsonify({"error": "Impact severity must be an integer 1-5."}), 400

    machine_name = (data.get("machine_name") or "").strip() or None
    machine_code = (data.get("machine_code") or "").strip() or None
    reported_by  = (data.get("reported_by") or "").strip() or None

    first_reported_at = None
    fr_raw = data.get("first_reported_at")
    if fr_raw:
        try:
            first_reported_at = datetime.fromisoformat(str(fr_raw).replace("Z", ""))
        except ValueError:
            return jsonify({"error": "first_reported_at must be ISO8601."}), 400

    event_code = data.get("event_code")
    if event_code not in (None, "", "null"):
        try:
            event_code = int(event_code)
            if not (600000 <= event_code <= 699999):
                return jsonify({"error": "Complaint EventCode must be 600000–699999."}), 400
        except (TypeError, ValueError):
            return jsonify({"error": "EventCode must be an integer."}), 400
    else:
        event_code = None

    urgency = data.get("perceived_urgency", 1)
    try:
        urgency = int(urgency)
        if urgency not in (0, 1, 2):
            urgency = 1
    except (TypeError, ValueError):
        urgency = 1

    images = data.get("images") or []

    user = get_current_user()
    try:
        conn = get_connection()
        cursor = conn.cursor()

        # Resolve MachineName if only code given
        if machine_code and not machine_name:
            cursor.execute(
                "SELECT MachineName FROM MachineLookup WHERE MachineCode=%s",
                (machine_code,),
            )
            row = cursor.fetchone()
            if row:
                machine_name = row[0]

        display_id = allocate_display_id(cursor, "CMP")

        cursor.execute("""
            INSERT INTO WO_Complaints
                (Description, Source, ImpactDescription, ImpactSeverity,
                 MachineName, MachineCode, SubmitterEmail,
                 FirstReportedAt, ReportedBy, EventCode, PerceivedUrgency,
                 StatusCode, DisplayID)
            OUTPUT INSERTED.ComplaintID
            VALUES (%s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    0, %s)
        """, (desc, source, impact_desc, impact_severity,
              machine_name, machine_code, user,
              first_reported_at, reported_by, event_code, urgency,
              display_id))
        new_id = int(cursor.fetchone()[0])

        _log_activity(cursor, "complaint", new_id, "submitted",
                      f"Submitted as {display_id}", user)

        # Upload images (if any) to SP
        img_count = _ingest_data_urls(cursor, "complaint", new_id, "before", images, user)

        conn.commit()
        conn.close()
        return jsonify({
            "ok": True,
            "id": new_id,
            "display_id": display_id,
            "images_uploaded": img_count,
        })
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500


@workorders_bp.route("/complaints")
@api_login_required
def api_complaint_list():
    """?scope=mine|all  ?status=fresh|assigned|closed|all  ?machine_code=…"""
    scope        = (request.args.get("scope") or "mine").strip().lower()
    status_filter = (request.args.get("status") or "all").strip().lower()
    machine_code = (request.args.get("machine_code") or "").strip() or None
    user         = get_current_user()

    # Managers can scope=all; ordinary submitters default to their own.
    if scope == "all" and get_role(user) not in MANAGER_ROLES:
        scope = "mine"

    where, params = [], []
    if scope == "mine":
        where.append("SubmitterEmail = %s")
        params.append(user)

    rev = {v: k for k, v in COMPLAINT_STATUS.items()}
    if status_filter in rev:
        where.append("StatusCode = %s")
        params.append(rev[status_filter])

    if machine_code:
        where.append("MachineCode = %s")
        params.append(machine_code)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT TOP 200
            ComplaintID, DisplayID, Description, Source,
            ImpactDescription, ImpactSeverity,
            MachineName, MachineCode,
            StatusCode, JobOrderID,
            SubmitterEmail, SubmittedAt,
            FirstReportedAt, ReportedBy, EventCode, PerceivedUrgency,
            (SELECT COUNT(*) FROM WO_Images
                WHERE ParentType='complaint' AND ParentID = wc.ComplaintID) AS ImgCount
        FROM WO_Complaints wc
        {where_sql}
        ORDER BY SubmittedAt DESC, ComplaintID DESC
    """
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(sql, tuple(params))
        rows = cursor.fetchall()
        conn.close()
        return jsonify([{
            "id": r[0], "display_id": r[1], "description": r[2], "source": r[3],
            "impact_description": r[4],
            "impact_severity": int(r[5]) if r[5] is not None else None,
            "machine_name": r[6], "machine_code": r[7],
            "status_code":  int(r[8]),
            "status_label": _label(COMPLAINT_STATUS, r[8]),
            "job_order_id": r[9],
            "submitter": r[10], "submitted_at": _iso(r[11]),
            "first_reported_at": _iso(r[12]),
            "reported_by": r[13],
            "event_code": int(r[14]) if r[14] is not None else None,
            "perceived_urgency_code":  int(r[15]),
            "perceived_urgency_label": _label(PRIORITY, r[15]),
            "image_count": int(r[16]),
        } for r in rows])
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500


@workorders_bp.route("/complaints/<int:cid>")
@api_login_required
def api_complaint_detail(cid):
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT ComplaintID, DisplayID, Description, Source,
                   ImpactDescription, ImpactSeverity,
                   MachineName, MachineCode,
                   StatusCode, JobOrderID,
                   SubmitterEmail, SubmittedAt,
                   FirstReportedAt, ReportedBy, EventCode, PerceivedUrgency
            FROM WO_Complaints WHERE ComplaintID = %s
        """, (cid,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "Complaint not found."}), 404
        images   = _images_for(cursor, "complaint", cid)
        activity = _activity_for(cursor, "complaint", cid)
        conn.close()
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500
    return jsonify({
        "id": row[0], "display_id": row[1], "description": row[2], "source": row[3],
        "impact_description": row[4],
        "impact_severity": int(row[5]) if row[5] is not None else None,
        "machine_name": row[6], "machine_code": row[7],
        "status_code":  int(row[8]),
        "status_label": _label(COMPLAINT_STATUS, row[8]),
        "job_order_id": row[9],
        "submitter": row[10], "submitted_at": _iso(row[11]),
        "first_reported_at": _iso(row[12]),
        "reported_by": row[13],
        "event_code": int(row[14]) if row[14] is not None else None,
        "perceived_urgency_code":  int(row[15]),
        "perceived_urgency_label": _label(PRIORITY, row[15]),
        "images": images, "activity": activity,
    })


# ── Job Orders  →  "Tech Support" tab ─────────────────────────────────────────

@workorders_bp.route("/joborders", methods=["POST"])
@require_roles(*MANAGER_ROLES)
def api_joborder_create():
    """Field manager creates a work order. Optionally linked from a complaint.
    Body:
        complaint_id   (optional int)
        machine_name   (required if no complaint)
        machine_code   (optional)
        diagnosis      (manager's initial hypothesis)
        proposed_fix   (suggested fix plan — free text)
        notes          (free text)
        priority       (0/1/2 — TINYINT; default 1)
        assigned_to    (email of operator)
        event_code     (optional int 800000–899999)
        tasks          (optional list of tickbox label strings — creates WO_JobOrderTasks)
    """
    data = request.get_json(silent=True) or {}
    complaint_id = data.get("complaint_id")
    machine_name = (data.get("machine_name") or "").strip()
    machine_code = (data.get("machine_code") or "").strip() or None
    diagnosis    = (data.get("diagnosis") or "").strip() or None
    proposed_fix = (data.get("proposed_fix") or "").strip() or None
    notes        = (data.get("notes") or "").strip() or None
    assigned     = (data.get("assigned_to") or "").strip().lower() or None
    tasks        = data.get("tasks") or []

    priority = data.get("priority", 1)
    try:
        priority = int(priority)
        if priority not in (0, 1, 2):
            priority = 1
    except (TypeError, ValueError):
        priority = 1

    event_code = data.get("event_code")
    if event_code not in (None, "", "null"):
        try:
            event_code = int(event_code)
            if not (800000 <= event_code <= 899999):
                return jsonify({"error": "JobOrder EventCode must be 800000–899999."}), 400
        except (TypeError, ValueError):
            return jsonify({"error": "EventCode must be an integer."}), 400
    else:
        event_code = None

    if complaint_id:
        try:
            complaint_id = int(complaint_id)
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid complaint id."}), 400

    user = get_current_user()
    try:
        conn = get_connection()
        cursor = conn.cursor()

        # If from complaint, pull machine + event_code if not supplied
        if complaint_id:
            cursor.execute(
                "SELECT MachineName, MachineCode, EventCode FROM WO_Complaints WHERE ComplaintID = %s",
                (complaint_id,),
            )
            crow = cursor.fetchone()
            if not crow:
                conn.close()
                return jsonify({"error": "Complaint not found."}), 404
            if not machine_name and crow[0]:
                machine_name = crow[0]
            if not machine_code and crow[1]:
                machine_code = crow[1]

        if not machine_name:
            conn.close()
            return jsonify({"error": "Machine name is required."}), 400

        display_id = allocate_display_id(cursor, "WkO")

        cursor.execute("""
            INSERT INTO WO_JobOrders
                (ComplaintID, MachineName, MachineCode, Notes,
                 AssignedTo, PriorityCode, StatusCode,
                 EventCode, Diagnosis, ProposedFix,
                 DisplayID, CreatedBy)
            OUTPUT INSERTED.JobOrderID
            VALUES (%s, %s, %s, %s,
                    %s, %s, 0,
                    %s, %s, %s,
                    %s, %s)
        """, (complaint_id, machine_name, machine_code, notes,
              assigned, priority,
              event_code, diagnosis, proposed_fix,
              display_id, user))
        new_id = int(cursor.fetchone()[0])

        _log_activity(cursor, "joborder", new_id, "created",
                      f"Created as {display_id} for {machine_name}", user)
        if assigned:
            _log_activity(cursor, "joborder", new_id, "assigned",
                          f"Assigned to {assigned}", user)

        # Tickboxes
        for i, label in enumerate(tasks):
            label = str(label).strip()
            if not label:
                continue
            cursor.execute("""
                INSERT INTO WO_JobOrderTasks (JobOrderID, SeqNum, Label, Done)
                VALUES (%s, %s, %s, 0)
            """, (new_id, i + 1, label[:500]))

        # If created from complaint, mark complaint as assigned
        if complaint_id:
            cursor.execute("""
                UPDATE WO_Complaints SET JobOrderID = %s, StatusCode = 1
                WHERE ComplaintID = %s
            """, (new_id, complaint_id))
            _log_activity(cursor, "complaint", complaint_id, "linked",
                          f"Linked to {display_id}", user)

        conn.commit()
        conn.close()
        return jsonify({
            "ok": True, "id": new_id, "display_id": display_id,
            "tasks_created": len([t for t in tasks if str(t).strip()]),
        })
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500


@workorders_bp.route("/joborders")
@require_roles(*OPERATOR_ROLES)
def api_joborder_list():
    """?scope=mine|all  ?status=assigned|needs_assistance|closed|all  ?machine_code=…
    Operators always see mine. Managers can see all.
    """
    scope         = (request.args.get("scope") or "mine").strip().lower()
    status_filter = (request.args.get("status") or "all").strip().lower()
    machine_code  = (request.args.get("machine_code") or "").strip() or None
    user          = get_current_user()

    if get_role(user) not in MANAGER_ROLES:
        scope = "mine"

    where, params = [], []
    if scope == "mine":
        where.append("AssignedTo = %s")
        params.append(user)

    rev = {v: k for k, v in JOBORDER_STATUS.items()}
    if status_filter in rev:
        where.append("StatusCode = %s")
        params.append(rev[status_filter])

    if machine_code:
        where.append("MachineCode = %s")
        params.append(machine_code)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT TOP 200
            JobOrderID, DisplayID, ComplaintID,
            MachineName, MachineCode,
            AssignedTo, PriorityCode, StatusCode,
            EventCode, Diagnosis, ProposedFix, LastBlockReason,
            CreatedBy, CreatedAt, CompletedBy, CompletedAt,
            (SELECT COUNT(*) FROM WO_JobOrderTasks WHERE JobOrderID = wj.JobOrderID) AS TaskCount,
            (SELECT COUNT(*) FROM WO_JobOrderTasks
                WHERE JobOrderID = wj.JobOrderID AND Done = 1) AS TasksDone
        FROM WO_JobOrders wj
        {where_sql}
        ORDER BY CreatedAt DESC, JobOrderID DESC
    """
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(sql, tuple(params))
        rows = cursor.fetchall()
        conn.close()
        return jsonify([{
            "id": r[0], "display_id": r[1], "complaint_id": r[2],
            "machine_name": r[3], "machine_code": r[4],
            "assigned_to": r[5],
            "priority_code":  int(r[6]),
            "priority_label": _label(PRIORITY, r[6]),
            "status_code":    int(r[7]),
            "status_label":   _label(JOBORDER_STATUS, r[7]),
            "event_code":     int(r[8]) if r[8] is not None else None,
            "diagnosis":      r[9],
            "proposed_fix":   r[10],
            "last_block_reason": r[11],
            "created_by": r[12], "created_at": _iso(r[13]),
            "completed_by": r[14], "completed_at": _iso(r[15]),
            "task_count": int(r[16] or 0),
            "tasks_done": int(r[17] or 0),
        } for r in rows])
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500


@workorders_bp.route("/joborders/<int:jid>")
@require_roles(*OPERATOR_ROLES)
def api_joborder_detail(jid):
    user = get_current_user()
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT JobOrderID, DisplayID, ComplaintID,
                   MachineName, MachineCode, Notes,
                   AssignedTo, PriorityCode, StatusCode,
                   Report, RootCause, CorrectiveAction, PreventiveAction,
                   EventCode, Diagnosis, ProposedFix, LastBlockReason,
                   CreatedBy, CreatedAt, CompletedBy, CompletedAt
            FROM WO_JobOrders WHERE JobOrderID = %s
        """, (jid,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "Job order not found."}), 404

        # Operators can only see their own
        if get_role(user) not in MANAGER_ROLES and (row[6] or "").lower() != user.lower():
            conn.close()
            return jsonify({"error": "Not assigned to you."}), 403

        cursor.execute("""
            SELECT TaskID, SeqNum, Label, Done, BlockedNote, CompletedBy, CompletedAt
            FROM WO_JobOrderTasks WHERE JobOrderID = %s ORDER BY SeqNum, TaskID
        """, (jid,))
        tasks = [{
            "id": tr[0], "seq": tr[1], "label": tr[2],
            "done": bool(tr[3]), "blocked_note": tr[4],
            "completed_by": tr[5], "completed_at": _iso(tr[6]),
        } for tr in cursor.fetchall()]

        images   = _images_for(cursor, "joborder", jid)
        activity = _activity_for(cursor, "joborder", jid)
        conn.close()
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500
    return jsonify({
        "id": row[0], "display_id": row[1], "complaint_id": row[2],
        "machine_name": row[3], "machine_code": row[4], "notes": row[5],
        "assigned_to": row[6],
        "priority_code": int(row[7]),  "priority_label": _label(PRIORITY, row[7]),
        "status_code":   int(row[8]),  "status_label":   _label(JOBORDER_STATUS, row[8]),
        "report":            row[9],
        "root_cause":        row[10],
        "corrective_action": row[11],
        "preventive_action": row[12],
        "event_code":     int(row[13]) if row[13] is not None else None,
        "diagnosis":      row[14],
        "proposed_fix":   row[15],
        "last_block_reason": row[16],
        "created_by": row[17], "created_at": _iso(row[18]),
        "completed_by": row[19], "completed_at": _iso(row[20]),
        "tasks": tasks,
        "images": images, "activity": activity,
    })


@workorders_bp.route("/joborders/<int:jid>", methods=["PATCH"])
@require_roles(*MANAGER_ROLES)
def api_joborder_update(jid):
    """Manager edits a WO. Updatable fields: notes, priority, diagnosis,
    proposed_fix, event_code, report, root_cause, corrective_action,
    preventive_action."""
    data = request.get_json(silent=True) or {}
    sets, params = [], []

    if "notes" in data:
        sets.append("Notes = %s"); params.append(data["notes"] or None)
    if "diagnosis" in data:
        sets.append("Diagnosis = %s"); params.append(data["diagnosis"] or None)
    if "proposed_fix" in data:
        sets.append("ProposedFix = %s"); params.append(data["proposed_fix"] or None)
    if "report" in data:
        sets.append("Report = %s"); params.append(data["report"] or None)
    if "root_cause" in data:
        sets.append("RootCause = %s"); params.append(data["root_cause"] or None)
    if "corrective_action" in data:
        sets.append("CorrectiveAction = %s"); params.append(data["corrective_action"] or None)
    if "preventive_action" in data:
        sets.append("PreventiveAction = %s"); params.append(data["preventive_action"] or None)
    if "priority" in data:
        try:
            p = int(data["priority"])
            if p in (0, 1, 2):
                sets.append("PriorityCode = %s"); params.append(p)
        except (TypeError, ValueError):
            pass
    if "event_code" in data:
        ev = data["event_code"]
        if ev in (None, "", "null"):
            sets.append("EventCode = NULL")
        else:
            try:
                ev = int(ev)
                if not (800000 <= ev <= 899999):
                    return jsonify({"error": "EventCode must be 800000–899999."}), 400
                sets.append("EventCode = %s"); params.append(ev)
            except (TypeError, ValueError):
                return jsonify({"error": "EventCode must be an integer."}), 400

    if not sets:
        return jsonify({"error": "No updatable fields supplied."}), 400

    user = get_current_user()
    try:
        conn = get_connection()
        cursor = conn.cursor()
        params.append(jid)
        cursor.execute(
            f"UPDATE WO_JobOrders SET {', '.join(sets)} WHERE JobOrderID = %s",
            tuple(params),
        )
        _log_activity(cursor, "joborder", jid, "updated",
                      "; ".join(s.split(" = ")[0] for s in sets), user)
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500


@workorders_bp.route("/joborders/<int:jid>/assign", methods=["POST"])
@require_roles(*MANAGER_ROLES)
def api_joborder_assign(jid):
    data = request.get_json(silent=True) or {}
    assigned = (data.get("assigned_to") or "").strip().lower() or None
    user = get_current_user()
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE WO_JobOrders SET AssignedTo=%s, StatusCode=0 WHERE JobOrderID=%s",
            (assigned, jid),
        )
        _log_activity(cursor, "joborder", jid, "assigned",
                      f"Assigned to {assigned or '(unassigned)'}", user)
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500


@workorders_bp.route("/joborders/<int:jid>/status", methods=["POST"])
@require_roles(*OPERATOR_ROLES)
def api_joborder_status(jid):
    """Operator updates status:
        0 assigned, 1 needs_assistance, 2 closed.
    For status=1: body MUST include 'block_reason' (stored in LastBlockReason).
    For status=2: completion timestamp + completer email captured.
    """
    data = request.get_json(silent=True) or {}
    user = get_current_user()
    try:
        status_code = int(data.get("status_code"))
    except (TypeError, ValueError):
        return jsonify({"error": "status_code (0/1/2) required."}), 400
    if status_code not in (0, 1, 2):
        return jsonify({"error": "status_code must be 0, 1, or 2."}), 400

    block_reason = (data.get("block_reason") or "").strip() or None
    if status_code == 1 and not block_reason:
        return jsonify({"error": "block_reason required when marking needs_assistance."}), 400

    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT AssignedTo, DisplayID FROM WO_JobOrders WHERE JobOrderID=%s",
            (jid,),
        )
        row = cursor.fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "Job order not found."}), 404
        if get_role(user) not in MANAGER_ROLES and (row[0] or "").lower() != user.lower():
            conn.close()
            return jsonify({"error": "Not assigned to you."}), 403

        if status_code == 2:
            cursor.execute("""
                UPDATE WO_JobOrders
                SET StatusCode=2, LastBlockReason=NULL,
                    CompletedBy=%s, CompletedAt=SYSUTCDATETIME()
                WHERE JobOrderID=%s
            """, (user, jid))
            # If linked complaint exists, mark it closed
            cursor.execute("""
                UPDATE WO_Complaints SET StatusCode=2
                WHERE JobOrderID=%s
            """, (jid,))
        else:
            cursor.execute("""
                UPDATE WO_JobOrders
                SET StatusCode=%s, LastBlockReason=%s
                WHERE JobOrderID=%s
            """, (status_code, block_reason, jid))

        _log_activity(cursor, "joborder", jid, "status",
                      f"{_label(JOBORDER_STATUS, status_code)}"
                      + (f" — {block_reason}" if block_reason else ""),
                      user)
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500


# ── JobOrder Tasks (tickboxes per WO) ─────────────────────────────────────────

@workorders_bp.route("/joborders/<int:jid>/tasks", methods=["GET"])
@require_roles(*OPERATOR_ROLES)
def api_tasks_list(jid):
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT TaskID, SeqNum, Label, Done, BlockedNote, CompletedBy, CompletedAt
            FROM WO_JobOrderTasks WHERE JobOrderID = %s ORDER BY SeqNum, TaskID
        """, (jid,))
        rows = cursor.fetchall()
        conn.close()
        return jsonify([{
            "id": r[0], "seq": r[1], "label": r[2],
            "done": bool(r[3]), "blocked_note": r[4],
            "completed_by": r[5], "completed_at": _iso(r[6]),
        } for r in rows])
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500


@workorders_bp.route("/joborders/<int:jid>/tasks", methods=["POST"])
@require_roles(*MANAGER_ROLES)
def api_tasks_add(jid):
    """Body: {label, seq?}  — manager adds a tickbox to an existing WO."""
    data = request.get_json(silent=True) or {}
    label = (data.get("label") or "").strip()
    if not label:
        return jsonify({"error": "Task label required."}), 400
    user = get_current_user()
    try:
        conn = get_connection()
        cursor = conn.cursor()
        seq = data.get("seq")
        if seq is None:
            cursor.execute(
                "SELECT ISNULL(MAX(SeqNum), 0) + 1 FROM WO_JobOrderTasks WHERE JobOrderID = %s",
                (jid,),
            )
            seq = int(cursor.fetchone()[0])
        else:
            try:
                seq = int(seq)
            except (TypeError, ValueError):
                seq = 1
        cursor.execute("""
            INSERT INTO WO_JobOrderTasks (JobOrderID, SeqNum, Label, Done)
            OUTPUT INSERTED.TaskID
            VALUES (%s, %s, %s, 0)
        """, (jid, seq, label[:500]))
        new_id = int(cursor.fetchone()[0])
        _log_activity(cursor, "joborder", jid, "task_added", label[:200], user)
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "id": new_id, "seq": seq})
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500


@workorders_bp.route("/tasks/<int:tid>", methods=["PATCH"])
@require_roles(*OPERATOR_ROLES)
def api_task_update(tid):
    """Body: {done?: bool, blocked_note?: str, label?: str (manager only)}
    Optional 'image_data_url' uploads a per-step photo (stage='task_done' or
    'task_blocked' depending on Done state).
    """
    data = request.get_json(silent=True) or {}
    user = get_current_user()
    role = get_role(user)
    sets, params = [], []
    stage = None

    if "label" in data:
        if role not in MANAGER_ROLES:
            return jsonify({"error": "Only managers can edit task labels."}), 403
        sets.append("Label = %s")
        params.append(str(data["label"])[:500])

    if "done" in data:
        done = bool(data["done"])
        sets.append("Done = %s"); params.append(1 if done else 0)
        if done:
            sets.append("CompletedBy = %s"); params.append(user)
            sets.append("CompletedAt = SYSUTCDATETIME()")
            stage = "task_done"
        else:
            sets.append("CompletedBy = NULL")
            sets.append("CompletedAt = NULL")

    if "blocked_note" in data:
        bn = (data["blocked_note"] or "").strip() or None
        sets.append("BlockedNote = %s"); params.append(bn)
        if bn:
            stage = stage or "task_blocked"

    if not sets:
        return jsonify({"error": "No updatable fields supplied."}), 400

    try:
        conn = get_connection()
        cursor = conn.cursor()
        # Ensure task exists; capture parent JobOrderID for activity log
        cursor.execute("SELECT JobOrderID FROM WO_JobOrderTasks WHERE TaskID=%s", (tid,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "Task not found."}), 404
        jid = int(row[0])

        # Operators must be assigned to the parent WO
        if role not in MANAGER_ROLES:
            cursor.execute("SELECT AssignedTo FROM WO_JobOrders WHERE JobOrderID=%s", (jid,))
            ar = cursor.fetchone()
            if not ar or (ar[0] or "").lower() != user.lower():
                conn.close()
                return jsonify({"error": "Not assigned to you."}), 403

        params_tuple = tuple(params) + (tid,)
        # Stitch '= SYSUTCDATETIME()' literal back into the SET clause
        cursor.execute(
            f"UPDATE WO_JobOrderTasks SET {', '.join(sets)} WHERE TaskID = %s",
            params_tuple,
        )

        # Optional per-step image
        img_data_url = data.get("image_data_url")
        if img_data_url and stage:
            raw, ctype = _decode_data_url(img_data_url)
            if raw:
                _save_image_to_sp(
                    cursor, parent_type="task", parent_id=tid, stage=stage,
                    file_name=f"task-{tid}-{stage}.jpg",
                    content_type=ctype or "image/jpeg",
                    raw_bytes=raw, uploaded_by=user,
                )

        _log_activity(cursor, "joborder", jid, "task_update",
                      f"task#{tid}: " + ", ".join(s.split(" = ")[0] for s in sets), user)
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500


@workorders_bp.route("/tasks/<int:tid>", methods=["DELETE"])
@require_roles(*MANAGER_ROLES)
def api_task_delete(tid):
    user = get_current_user()
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT JobOrderID FROM WO_JobOrderTasks WHERE TaskID=%s", (tid,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "Task not found."}), 404
        jid = int(row[0])
        cursor.execute("DELETE FROM WO_JobOrderTasks WHERE TaskID=%s", (tid,))
        _log_activity(cursor, "joborder", jid, "task_deleted", f"task#{tid}", user)
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500


# ── Knowledge Base (Manage-KB sub-tab) ────────────────────────────────────────

@workorders_bp.route("/kb")
@api_login_required
def api_kb_list():
    """All KB entries. Manager-only edit; everyone can read so suggestions work."""
    event_code = request.args.get("event_code")
    where, params = [], []
    if event_code:
        try:
            where.append("EventCode = %s")
            params.append(int(event_code))
        except (TypeError, ValueError):
            return jsonify({"error": "event_code must be an integer."}), 400
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT KBID, EventCode, Title, UseCount,
                   CreatedBy, CreatedAt, UpdatedBy, UpdatedAt,
                   Symptom, DiagnosticConfirmation, RootCause,
                   CorrectiveAction, PreventiveAction, VerificationOfCompletion
            FROM WO_KB_Entries {where_sql}
            ORDER BY UseCount DESC, KBID DESC
        """, tuple(params))
        rows = cursor.fetchall()
        # Pre-fetch tickboxes grouped by KBID
        cursor.execute("""
            SELECT TBID, KBID, SeqNum, Label
            FROM WO_KB_Tickboxes ORDER BY KBID, SeqNum, TBID
        """)
        by_kb = {}
        for tb in cursor.fetchall():
            by_kb.setdefault(int(tb[1]), []).append(
                {"id": tb[0], "seq": tb[2], "label": tb[3]}
            )
        conn.close()
        return jsonify([{
            "id": r[0],
            "event_code": int(r[1]) if r[1] is not None else None,
            "title": r[2],
            "use_count": int(r[3]),
            "created_by": r[4], "created_at": _iso(r[5]),
            "updated_by": r[6], "updated_at": _iso(r[7]),
            "symptom":                    r[8],
            "diagnostic_confirmation":    r[9],
            "root_cause":                 r[10],
            "corrective_action":          r[11],
            "preventive_action":          r[12],
            "verification_of_completion": r[13],
            "tickboxes": by_kb.get(int(r[0]), []),
        } for r in rows])
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500


@workorders_bp.route("/kb", methods=["POST"])
@require_roles(*MANAGER_ROLES)
def api_kb_create():
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "Title required."}), 400
    ev = data.get("event_code")
    if ev not in (None, "", "null"):
        try:
            ev = int(ev)
            if not ((600000 <= ev <= 699999) or (800000 <= ev <= 899999)):
                return jsonify({"error": "EventCode must be 6xxxxx or 8xxxxx."}), 400
        except (TypeError, ValueError):
            return jsonify({"error": "EventCode must be an integer."}), 400
    else:
        ev = None
    car = {
        "symptom":                    (data.get("symptom") or "").strip() or None,
        "diagnostic_confirmation":    (data.get("diagnostic_confirmation") or "").strip() or None,
        "root_cause":                 (data.get("root_cause") or "").strip() or None,
        "corrective_action":          (data.get("corrective_action") or "").strip() or None,
        "preventive_action":          (data.get("preventive_action") or "").strip() or None,
        "verification_of_completion": (data.get("verification_of_completion") or "").strip() or None,
    }
    tickboxes = data.get("tickboxes") or []
    user = get_current_user()
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO WO_KB_Entries
                (EventCode, Title, CreatedBy,
                 Symptom, DiagnosticConfirmation, RootCause,
                 CorrectiveAction, PreventiveAction, VerificationOfCompletion)
            OUTPUT INSERTED.KBID
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (ev, title[:255], user,
              car["symptom"], car["diagnostic_confirmation"], car["root_cause"],
              car["corrective_action"], car["preventive_action"], car["verification_of_completion"]))
        new_id = int(cursor.fetchone()[0])
        for i, lbl in enumerate(tickboxes):
            lbl = str(lbl).strip()
            if not lbl:
                continue
            cursor.execute("""
                INSERT INTO WO_KB_Tickboxes (KBID, SeqNum, Label)
                VALUES (%s, %s, %s)
            """, (new_id, i + 1, lbl[:500]))
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "id": new_id})
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500


@workorders_bp.route("/kb/<int:kid>", methods=["PATCH"])
@require_roles(*MANAGER_ROLES)
def api_kb_update(kid):
    data = request.get_json(silent=True) or {}
    sets, params = [], []
    if "title" in data:
        sets.append("Title = %s"); params.append(str(data["title"])[:255])
    _car_map = {
        "symptom":                    "Symptom",
        "diagnostic_confirmation":    "DiagnosticConfirmation",
        "root_cause":                 "RootCause",
        "corrective_action":          "CorrectiveAction",
        "preventive_action":          "PreventiveAction",
        "verification_of_completion": "VerificationOfCompletion",
    }
    for k, col in _car_map.items():
        if k in data:
            sets.append(f"{col} = %s")
            params.append((data[k] or "").strip() or None)
    if "event_code" in data:
        ev = data["event_code"]
        if ev in (None, "", "null"):
            sets.append("EventCode = NULL")
        else:
            try:
                ev = int(ev)
                if not ((600000 <= ev <= 699999) or (800000 <= ev <= 899999)):
                    return jsonify({"error": "EventCode must be 6xxxxx or 8xxxxx."}), 400
                sets.append("EventCode = %s"); params.append(ev)
            except (TypeError, ValueError):
                return jsonify({"error": "EventCode must be an integer."}), 400
    user = get_current_user()
    sets.append("UpdatedBy = %s"); params.append(user)
    sets.append("UpdatedAt = SYSUTCDATETIME()")
    params.append(kid)
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE WO_KB_Entries SET {', '.join(sets)} WHERE KBID = %s",
            tuple(params),
        )
        # Replace tickboxes if provided
        if "tickboxes" in data:
            cursor.execute("DELETE FROM WO_KB_Tickboxes WHERE KBID=%s", (kid,))
            for i, lbl in enumerate(data["tickboxes"] or []):
                lbl = str(lbl).strip()
                if not lbl:
                    continue
                cursor.execute("""
                    INSERT INTO WO_KB_Tickboxes (KBID, SeqNum, Label)
                    VALUES (%s, %s, %s)
                """, (kid, i + 1, lbl[:500]))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500


@workorders_bp.route("/kb/<int:kid>", methods=["DELETE"])
@require_roles(*MANAGER_ROLES)
def api_kb_delete(kid):
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM WO_KB_Tickboxes WHERE KBID=%s", (kid,))
        cursor.execute("DELETE FROM WO_KB_Entries WHERE KBID=%s", (kid,))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500


@workorders_bp.route("/kb/suggest")
@api_login_required
def api_kb_suggest():
    """?event_code=8xxxxx → ranked list of KB entries matching that EventCode
    (UseCount DESC), each with its tickbox list. Used by the WO-creation form
    to pre-fill Diagnosis / ProposedFix / Tasks."""
    ev = request.args.get("event_code")
    if not ev:
        return jsonify([])
    try:
        ev = int(ev)
    except (TypeError, ValueError):
        return jsonify({"error": "event_code must be an integer."}), 400
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT KBID, EventCode, Title, UseCount,
                   Symptom, DiagnosticConfirmation, RootCause,
                   CorrectiveAction, PreventiveAction, VerificationOfCompletion
            FROM WO_KB_Entries WHERE EventCode = %s
            ORDER BY UseCount DESC, KBID DESC
        """, (ev,))
        rows = cursor.fetchall()
        out = []
        for r in rows:
            kb_id = int(r[0])
            cursor.execute("""
                SELECT SeqNum, Label FROM WO_KB_Tickboxes
                WHERE KBID = %s ORDER BY SeqNum, TBID
            """, (kb_id,))
            tbs = [{"seq": x[0], "label": x[1]} for x in cursor.fetchall()]
            out.append({
                "id": kb_id, "event_code": int(r[1]) if r[1] is not None else None,
                "title": r[2], "use_count": int(r[3]),
                "symptom":                    r[4],
                "diagnostic_confirmation":    r[5],
                "root_cause":                 r[6],
                "corrective_action":          r[7],
                "preventive_action":          r[8],
                "verification_of_completion": r[9],
                "tickboxes": tbs,
            })
        conn.close()
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500


@workorders_bp.route("/kb/<int:kid>/use", methods=["POST"])
@require_roles(*MANAGER_ROLES)
def api_kb_use(kid):
    """Increment UseCount when a manager accepts a KB suggestion into a WO."""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE WO_KB_Entries SET UseCount = UseCount + 1 WHERE KBID = %s",
            (kid,),
        )
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500


# ── Images (SP-backed proxy) ──────────────────────────────────────────────────

@workorders_bp.route("/images", methods=["POST"])
@api_login_required
def api_image_upload():
    """Body: {parent_type, parent_id, stage, image_data_url}.
    Uploads to SP, inserts WO_Images row, returns the new image id."""
    data = request.get_json(silent=True) or {}
    parent_type = (data.get("parent_type") or "").strip().lower()
    if parent_type not in ("complaint", "joborder", "task"):
        return jsonify({"error": "parent_type must be complaint, joborder, or task."}), 400
    try:
        parent_id = int(data.get("parent_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "parent_id required."}), 400
    stage = (data.get("stage") or "before").strip().lower()
    raw, ctype = _decode_data_url(data.get("image_data_url") or "")
    if not raw:
        return jsonify({"error": "image_data_url required (data: URL)."}), 400

    user = get_current_user()
    try:
        conn = get_connection()
        cursor = conn.cursor()
        new_id = _save_image_to_sp(
            cursor, parent_type, parent_id, stage,
            file_name=data.get("file_name") or f"{stage}.jpg",
            content_type=ctype, raw_bytes=raw, uploaded_by=user,
        )
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "id": new_id})
    except Exception as e:
        return jsonify({"error": f"Upload failed: {str(e)}"}), 500


@workorders_bp.route("/images/<int:image_id>")
@api_login_required
def api_image_get(image_id):
    """Proxy the SP file body so Easy Auth gates every read.
    Falls back to legacy ImageData if SPItemID is NULL."""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT ContentType, SPItemID, ImageData, FileName
            FROM WO_Images WHERE ImageID = %s
        """, (image_id,))
        row = cursor.fetchone()
        conn.close()
        if not row:
            return jsonify({"error": "Image not found."}), 404
        ctype = row[0] or "application/octet-stream"
        sp_id = row[1]
        legacy_bytes = row[2]

        if sp_id:
            body, ctype = sp.download_bytes(sp_id)
        elif legacy_bytes:
            body = bytes(legacy_bytes)
        else:
            return jsonify({"error": "Image has no content."}), 410

        return Response(body, mimetype=ctype)
    except Exception as e:
        return jsonify({"error": f"Image read failed: {str(e)}"}), 500


@workorders_bp.route("/images/<int:image_id>", methods=["DELETE"])
@require_roles(*MANAGER_ROLES)
def api_image_delete(image_id):
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT SPItemID FROM WO_Images WHERE ImageID=%s", (image_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "Image not found."}), 404
        sp_id = row[0]
        cursor.execute("DELETE FROM WO_Images WHERE ImageID=%s", (image_id,))
        conn.commit()
        conn.close()
        if sp_id:
            try:
                sp.delete_item(sp_id)
            except Exception as e:
                # Log but don't fail the deletion of the DB row.
                print(f"[images/delete] SP delete failed for {sp_id}: {e}")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": f"Delete failed: {str(e)}"}), 500


# ─────────────────────────────────────────────────────────────────────────────
# PORTED FROM V1 (workorders.py.bak-2026-06-03) — verbatim, unchanged.
# These routes still use text Status/Priority (V1 columns kept alongside V2
# TINYINT columns post-migration). To be modernized in a future pass.
# ─────────────────────────────────────────────────────────────────────────────


def _sync_topup(cursor, machine_code):
    """Replicates the vending dashboard's log_topup behaviour. Returns a status string."""
    now_ole = to_ole_date(datetime.utcnow())
    cursor.execute(
        "SELECT LastTopupTimestamp FROM MachineLookup WHERE MachineCode = %s",
        (machine_code,),
    )
    row = cursor.fetchone()
    if not row:
        return "Machine code not found in vending data — topup not synced."
    current_ole = row[0]
    if current_ole is not None:
        cursor.execute(f"""
            SELECT COUNT(*) FROM [MasterData Table]
            WHERE CAST([Machine Code] AS NVARCHAR(50)) = %s
              AND LEN(CAST([Event Code] AS NVARCHAR(20))) = 6
              AND CAST([Event Code] AS NVARCHAR(20)) LIKE '1%'
              AND CAST([Date Time] AS FLOAT) >= {float(current_ole)}
        """, (machine_code,))
    else:
        cursor.execute("""
            SELECT COUNT(*) FROM [MasterData Table]
            WHERE CAST([Machine Code] AS NVARCHAR(50)) = %s
              AND LEN(CAST([Event Code] AS NVARCHAR(20))) = 6
              AND CAST([Event Code] AS NVARCHAR(20)) LIKE '1%'
        """, (machine_code,))
    vends_since = int(cursor.fetchone()[0])
    cursor.execute(f"""
        UPDATE MachineLookup
        SET PreviousTopupTimestamp = LastTopupTimestamp,
            LastTopupTimestamp     = {now_ole},
            CountBeforeLastTopup   = %s
        WHERE MachineCode = %s
    """, (vends_since, machine_code))
    return f"Topup logged ({vends_since} vends since previous topup)."


# ── Delivery Orders ───────────────────────────────────────────────────────────

@workorders_bp.route("/deliveryorders", methods=["POST"])
@require_roles(*MANAGER_ROLES)
def api_delivery_create():
    data = request.get_json(silent=True) or {}
    machine_name = (data.get("machine_name") or "").strip()
    machine_code = (data.get("machine_code") or "").strip() or None
    notes        = (data.get("notes") or "").strip() or None
    assigned     = (data.get("assigned_to") or "").strip().lower() or None
    priority     = (data.get("priority") or "normal").strip().lower()
    if priority not in ("low", "normal", "high"):
        priority = "normal"
    if not machine_name:
        return jsonify({"error": "Machine name is required."}), 400
    user = get_current_user()
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO WO_DeliveryOrders
                (MachineName, MachineCode, Notes, AssignedTo, Priority, CreatedBy)
            OUTPUT INSERTED.DeliveryOrderID
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (machine_name, machine_code, notes, assigned, priority, user))
        new_id = cursor.fetchone()[0]
        _log_activity(cursor, "deliveryorder", new_id, "created",
                      f"Delivery order created for {machine_name}", user)
        if assigned:
            _log_activity(cursor, "deliveryorder", new_id, "assigned",
                          f"Assigned to {assigned}", user)
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "id": new_id})
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500


@workorders_bp.route("/deliveryorders")
@api_login_required
def api_delivery_list():
    tab = (request.args.get("tab") or "mine").strip().lower()
    user = get_current_user()

    where, params = [], []
    if tab == "mine":
        where.append("AssignedTo = %s"); params.append(user)
        where.append("Status <> 'completed'")
    elif tab == "open":
        where.append("Status <> 'completed'")
    elif tab == "completed":
        where.append("Status = 'completed'")
    elif tab == "unassigned":
        where.append("AssignedTo IS NULL")
        where.append("Status <> 'completed'")

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT TOP 300 DeliveryOrderID, MachineName, MachineCode, AssignedTo,
               Priority, Status, CreatedBy, CreatedAt, CompletedAt
        FROM WO_DeliveryOrders
        {where_sql}
        ORDER BY CASE Priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END,
                 CreatedAt DESC
    """
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(sql, tuple(params))
        rows = cursor.fetchall()
        conn.close()
        return jsonify([{
            "id": r[0], "machine_name": r[1], "machine_code": r[2],
            "assigned_to": r[3], "priority": r[4], "status": r[5],
            "created_by": r[6], "created_at": _iso(r[7]),
            "completed_at": _iso(r[8]),
        } for r in rows])
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500


@workorders_bp.route("/deliveryorders/<int:did>")
@api_login_required
def api_delivery_detail(did):
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DeliveryOrderID, MachineName, MachineCode, Notes, AssignedTo,
                   Priority, Status,
                   Item1Qty, Item2Qty, Item3Qty, Item4Qty,
                   Item5Qty, Item6Qty, Item7Qty, Item8Qty,
                   RecipientName, CreatedBy, CreatedAt, CompletedBy, CompletedAt
            FROM WO_DeliveryOrders WHERE DeliveryOrderID = %s
        """, (did,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "Delivery order not found."}), 404
        activity = _activity_for(cursor, "deliveryorder", did)
        conn.close()
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500

    items = [{"index": i + 1, "label": DELIVERY_ITEMS[i],
              "quantity": int(row[7 + i] or 0)} for i in range(8)]
    return jsonify({
        "id": row[0], "machine_name": row[1], "machine_code": row[2],
        "notes": row[3], "assigned_to": row[4], "priority": row[5],
        "status": row[6], "items": items, "recipient_name": row[15],
        "created_by": row[16], "created_at": _iso(row[17]),
        "completed_by": row[18], "completed_at": _iso(row[19]),
        "activity": activity,
    })


@workorders_bp.route("/deliveryorders/<int:did>", methods=["PATCH"])
@require_roles(*OPERATOR_ROLES)
def api_delivery_update(did):
    data = request.get_json(silent=True) or {}
    user = get_current_user()
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT AssignedTo, Status FROM WO_DeliveryOrders WHERE DeliveryOrderID = %s",
            (did,),
        )
        row = cursor.fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "Delivery order not found."}), 404
        role = get_role(user)
        if role == ROLE_OPERATOR and (row[0] or "").lower() != user:
            conn.close()
            return jsonify({"error": "You can only update your own delivery orders."}), 403

        quantities = data.get("quantities") or []
        if not isinstance(quantities, list) or len(quantities) != 8:
            return jsonify({"error": "Provide all 8 item quantities."}), 400
        clean = []
        for q in quantities:
            try:
                v = int(q)
            except (TypeError, ValueError):
                return jsonify({"error": "Quantities must be whole numbers."}), 400
            if v < 0 or v > 20:
                return jsonify({"error": "Each quantity must be between 0 and 20."}), 400
            clean.append(v)
        recipient = (data.get("recipient_name") or "").strip()[:255] or None

        cursor.execute("""
            UPDATE WO_DeliveryOrders
            SET Item1Qty=%s, Item2Qty=%s, Item3Qty=%s, Item4Qty=%s,
                Item5Qty=%s, Item6Qty=%s, Item7Qty=%s, Item8Qty=%s,
                RecipientName=%s
            WHERE DeliveryOrderID = %s
        """, (*clean, recipient, did))
        _log_activity(cursor, "deliveryorder", did, "updated",
                      "Quantities / recipient updated", user)
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500


@workorders_bp.route("/deliveryorders/<int:did>/complete", methods=["POST"])
@require_roles(*OPERATOR_ROLES)
def api_delivery_complete(did):
    user = get_current_user()
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT AssignedTo, MachineCode, RecipientName FROM WO_DeliveryOrders "
            "WHERE DeliveryOrderID = %s",
            (did,),
        )
        row = cursor.fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "Delivery order not found."}), 404
        role = get_role(user)
        if role == ROLE_OPERATOR and (row[0] or "").lower() != user:
            conn.close()
            return jsonify({"error": "You can only complete your own delivery orders."}), 403
        if not row[2]:
            conn.close()
            return jsonify({"error": "Add the recipient name (typed signature) before completing."}), 400

        cursor.execute("""
            UPDATE WO_DeliveryOrders
            SET Status='completed', CompletedBy=%s, CompletedAt=SYSUTCDATETIME()
            WHERE DeliveryOrderID = %s
        """, (user, did))
        _log_activity(cursor, "deliveryorder", did, "completed",
                      f"Signed for by {row[2]}", user)
        topup_note = None
        if row[1]:
            topup_note = _sync_topup(cursor, str(row[1]))
            _log_activity(cursor, "deliveryorder", did, "topup_synced", topup_note, user)
        else:
            topup_note = "No machine code on file — topup not synced to vending data."
            _log_activity(cursor, "deliveryorder", did, "topup_sync_skipped", topup_note, user)
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "topup_note": topup_note})
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500


@workorders_bp.route("/deliveryorders/<int:did>/assign", methods=["POST"])
@require_roles(*MANAGER_ROLES)
def api_delivery_assign(did):
    data = request.get_json(silent=True) or {}
    assigned = (data.get("assigned_to") or "").strip().lower() or None
    priority = data.get("priority")
    user = get_current_user()
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM WO_DeliveryOrders WHERE DeliveryOrderID = %s", (did,))
        if not cursor.fetchone():
            conn.close()
            return jsonify({"error": "Delivery order not found."}), 404
        sets, params = ["AssignedTo = %s"], [assigned]
        if priority and priority in ("low", "normal", "high"):
            sets.append("Priority = %s"); params.append(priority)
        params.append(did)
        cursor.execute(
            f"UPDATE WO_DeliveryOrders SET {', '.join(sets)} WHERE DeliveryOrderID = %s",
            tuple(params),
        )
        detail = f"Assigned to {assigned or '(unassigned)'}"
        if priority:
            detail += f"; priority -> {priority}"
        _log_activity(cursor, "deliveryorder", did, "assigned", detail, user)
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500


# ── Assignment candidates ─────────────────────────────────────────────────────

@workorders_bp.route("/assignment/delivery-candidates")
@require_roles(*MANAGER_ROLES)
def api_assign_delivery_candidates():
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT ml.MachineName, ml.MachineCode, ml.LastTopupTimestamp,
                (
                    SELECT COUNT(*) FROM [MasterData Table] mdt
                    WHERE CAST(mdt.[Machine Code] AS NVARCHAR(50)) = CAST(ml.MachineCode AS NVARCHAR(50))
                      AND LEN(CAST(mdt.[Event Code] AS NVARCHAR(20))) = 6
                      AND CAST(mdt.[Event Code] AS NVARCHAR(20)) LIKE '1%'
                      AND (ml.LastTopupTimestamp IS NULL
                           OR CAST(mdt.[Date Time] AS FLOAT) >= ml.LastTopupTimestamp)
                ) AS VendsSince
            FROM MachineLookup ml
            WHERE ml.MachineCode IS NOT NULL
        """)
        rows = cursor.fetchall()
        conn.close()
        out = []
        for r in rows:
            last_dt = from_ole_date(r[2])
            out.append({
                "machine_name": r[0],
                "machine_code": str(r[1]) if r[1] is not None else None,
                "last_topup":   last_dt.strftime("%Y-%m-%d %H:%M") if last_dt else None,
                "vends_since":  int(r[3]) if r[3] is not None else 0,
            })
        out.sort(key=lambda x: x["vends_since"], reverse=True)
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500


@workorders_bp.route("/assignment/joborder-candidates")
@require_roles(*MANAGER_ROLES)
def api_assign_joborder_candidates():
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT ml.MachineName, ml.MachineCode,
                   ISNULL((
                       SELECT COUNT(*) FROM WO_Complaints c
                       WHERE c.StatusCode <> 2
                         AND (
                             (c.MachineCode IS NOT NULL AND c.MachineCode = ml.MachineCode)
                             OR (c.MachineCode IS NULL AND c.MachineName = ml.MachineName)
                         )
                   ), 0) AS OpenComplaints
            FROM MachineLookup ml
        """)
        rows = cursor.fetchall()
        conn.close()
        out = [{
            "machine_name": r[0],
            "machine_code": str(r[1]) if r[1] is not None else None,
            "open_complaints": int(r[2]),
        } for r in rows]
        out.sort(key=lambda x: x["open_complaints"], reverse=True)
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500


# ── Manager views ────────────────────────────────────────────────────────────

@workorders_bp.route("/manager/overview")
@require_roles(*MANAGER_ROLES)
def api_manager_overview():
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT JobOrderID, DisplayID, MachineName, AssignedTo,
                   PriorityCode, StatusCode, CreatedAt
            FROM WO_JobOrders WHERE StatusCode <> 2
            ORDER BY PriorityCode DESC, CreatedAt
        """)
        jobs = [{
            "id": r[0], "display_id": r[1], "machine_name": r[2], "assigned_to": r[3],
            "priority_code":  int(r[4]), "priority_label": _label(PRIORITY, r[4]),
            "status_code":    int(r[5]), "status_label":   _label(JOBORDER_STATUS, r[5]),
            "created_at": _iso(r[6]),
        } for r in cursor.fetchall()]

        cursor.execute("""
            SELECT DeliveryOrderID, MachineName, AssignedTo, Priority, Status, CreatedAt
            FROM WO_DeliveryOrders WHERE Status <> 'completed'
            ORDER BY CASE Priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END,
                     CreatedAt
        """)
        deliveries = [{
            "id": r[0], "machine_name": r[1], "assigned_to": r[2],
            "priority": r[3], "status": r[4], "created_at": _iso(r[5]),
        } for r in cursor.fetchall()]
        conn.close()
        return jsonify({"job_orders": jobs, "delivery_orders": deliveries})
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500


@workorders_bp.route("/manager/machines")
@require_roles(*MANAGER_ROLES)
def api_manager_machines_search():
    q = (request.args.get("q") or "").strip()
    try:
        conn = get_connection()
        cursor = conn.cursor()
        if q:
            cursor.execute("""
                SELECT MachineName, MachineCode FROM MachineLookup
                WHERE MachineCode IS NOT NULL
                  AND (MachineName LIKE %s OR CAST(MachineCode AS NVARCHAR(50)) LIKE %s)
                ORDER BY MachineName
            """, (f"%{q}%", f"%{q}%"))
        else:
            cursor.execute("""
                SELECT MachineName, MachineCode FROM MachineLookup
                WHERE MachineCode IS NOT NULL ORDER BY MachineName
            """)
        rows = cursor.fetchall()
        conn.close()
        seen = {}
        for r in rows:
            code = str(r[1]) if r[1] is not None else None
            if not code:
                continue
            if code not in seen:
                seen[code] = {"code": code, "names": []}
            if r[0] and r[0] not in seen[code]["names"]:
                seen[code]["names"].append(r[0])
        out = [{"code": v["code"], "name": " / ".join(v["names"]) or v["code"]}
               for v in seen.values()]
        return jsonify(out[:30])
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500


@workorders_bp.route("/manager/equipment-log")
@require_roles(*MANAGER_ROLES)
def api_equipment_log():
    """Chronological history for one MachineCode."""
    code = (request.args.get("machine_code") or "").strip()
    if not code:
        return jsonify({"error": "machine_code is required."}), 400
    try:
        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT MachineName, Latitude, Longitude,
                   LastTopupTimestamp, PreviousTopupTimestamp, CountBeforeLastTopup
            FROM MachineLookup WHERE MachineCode = %s
        """, (code,))
        ml_rows = cursor.fetchall()
        if not ml_rows:
            conn.close()
            return jsonify({"error": "Machine not found in MachineLookup."}), 404

        machine_names = sorted({r[0] for r in ml_rows if r[0]})
        last_topup    = from_ole_date(ml_rows[0][3])
        prev_topup    = from_ole_date(ml_rows[0][4])
        vends_before  = int(ml_rows[0][5]) if ml_rows[0][5] is not None else None

        events = []

        cursor.execute("""
            SELECT ComplaintID, DisplayID, Description, Source,
                   ImpactDescription, ImpactSeverity,
                   StatusCode, SubmitterEmail, SubmittedAt, JobOrderID
            FROM WO_Complaints WHERE MachineCode = %s
        """, (code,))
        complaint_ids = []
        for r in cursor.fetchall():
            complaint_ids.append(r[0])
            summary = (r[2] or "")[:120]
            if r[5] is not None:
                summary += f" (severity {int(r[5])}/5)"
            events.append({
                "at": _iso(r[8]), "type": "complaint", "id": r[0],
                "display_id": r[1],
                "action": "submitted", "by": r[7], "summary": summary,
                "status": _label(COMPLAINT_STATUS, r[6]),
                "linked_job_order": r[9],
            })

        cursor.execute("""
            SELECT JobOrderID, DisplayID, ComplaintID, AssignedTo,
                   PriorityCode, StatusCode,
                   CreatedBy, CreatedAt, CompletedBy, CompletedAt, Notes
            FROM WO_JobOrders WHERE MachineCode = %s
        """, (code,))
        job_ids = []
        for r in cursor.fetchall():
            job_ids.append(r[0])
            link = f" (from complaint #{r[2]})" if r[2] else ""
            events.append({
                "at": _iso(r[7]), "type": "joborder", "id": r[0],
                "display_id": r[1],
                "action": "created", "by": r[6],
                "summary": f"Assigned to {r[3] or 'unassigned'} ({_label(PRIORITY, r[4])} priority){link}",
                "status": _label(JOBORDER_STATUS, r[5]),
            })
            if r[9]:
                events.append({
                    "at": _iso(r[9]), "type": "joborder", "id": r[0],
                    "display_id": r[1],
                    "action": "completed", "by": r[8],
                    "summary": "Completed", "status": "closed",
                })

        cursor.execute("""
            SELECT DeliveryOrderID, AssignedTo, Priority, Status,
                   CreatedBy, CreatedAt, CompletedBy, CompletedAt, RecipientName
            FROM WO_DeliveryOrders WHERE MachineCode = %s
        """, (code,))
        delivery_ids = []
        for r in cursor.fetchall():
            delivery_ids.append(r[0])
            events.append({
                "at": _iso(r[5]), "type": "deliveryorder", "id": r[0],
                "action": "created", "by": r[4],
                "summary": f"Assigned to {r[1] or 'unassigned'} ({r[2]} priority)",
                "status": r[3],
            })
            if r[7]:
                events.append({
                    "at": _iso(r[7]), "type": "deliveryorder", "id": r[0],
                    "action": "completed", "by": r[6],
                    "summary": f"Signed for by {r[8] or '—'}",
                    "status": "completed",
                })

        def _activity_in(parent_type, ids):
            if not ids:
                return
            placeholders = ", ".join(["%s"] * len(ids))
            cursor.execute(f"""
                SELECT ParentID, Action, Detail, ByUser, AtTime
                FROM WO_Activity
                WHERE ParentType = %s AND ParentID IN ({placeholders})
                  AND Action NOT IN ('created', 'submitted', 'completed')
            """, (parent_type,) + tuple(ids))
            for r in cursor.fetchall():
                events.append({
                    "at": _iso(r[4]), "type": parent_type, "id": r[0],
                    "action": r[1], "by": r[3],
                    "summary": r[2] or r[1],
                })

        _activity_in("complaint",     complaint_ids)
        _activity_in("joborder",      job_ids)
        _activity_in("deliveryorder", delivery_ids)
        conn.close()
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500

    events.sort(key=lambda e: e.get("at") or "", reverse=True)

    return jsonify({
        "machine": {
            "code": code,
            "names": machine_names,
            "last_topup":              _iso(last_topup),
            "previous_topup":          _iso(prev_topup),
            "vends_before_last_topup": vends_before,
        },
        "counts": {
            "complaints":      len(complaint_ids),
            "job_orders":      len(job_ids),
            "delivery_orders": len(delivery_ids),
        },
        "events": events[:300],
    })
