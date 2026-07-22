-- ─────────────────────────────────────────────────────────────────────────────
-- KNM Vending Dashboard — Fault Report + Tech Support migration  (SELF-SUFFICIENT)
-- Date     : 2026-06-03
-- Author   : Yash Bhawe
-- Target   : Azure SQL — database "Machine DispensedDrink"
--
-- This script handles BOTH cases:
--   (a) Fresh prod state: only legacy V1 tables exist (WorkOrders / ...).
--   (b) Re-run on an already-migrated DB (every statement is idempotent).
--
-- Order:
--   0. Rename legacy V1 tables to *_legacy_2026_06_03 (preserve, do NOT drop).
--   1. Create base WO_* tables if missing.
--   2. Apply column additions + type migrations (Status/Priority → TINYINT).
--   3. Create new tables (WO_JobOrderTasks, WO_KB_Entries, WO_KB_Tickboxes, WO_Counters).
--   4. Backfill DisplayID + counters.
--   5. Verification SELECT.
--   6. Rollback comments at bottom.
-- ─────────────────────────────────────────────────────────────────────────────

SET XACT_ABORT ON;
SET NOCOUNT ON;
GO

USE [Machine DispensedDrink];
GO

-- ============================================================================
-- 0. Preserve V1 tables  →  rename to *_legacy_2026_06_03
-- ============================================================================

IF OBJECT_ID('dbo.WorkOrders', 'U') IS NOT NULL
   AND OBJECT_ID('dbo.WorkOrders_legacy_2026_06_03', 'U') IS NULL
    EXEC sp_rename 'dbo.WorkOrders', 'WorkOrders_legacy_2026_06_03';
GO

IF OBJECT_ID('dbo.WorkOrderActivity', 'U') IS NOT NULL
   AND OBJECT_ID('dbo.WorkOrderActivity_legacy_2026_06_03', 'U') IS NULL
    EXEC sp_rename 'dbo.WorkOrderActivity', 'WorkOrderActivity_legacy_2026_06_03';
GO

IF OBJECT_ID('dbo.WorkOrderImages', 'U') IS NOT NULL
   AND OBJECT_ID('dbo.WorkOrderImages_legacy_2026_06_03', 'U') IS NULL
    EXEC sp_rename 'dbo.WorkOrderImages', 'WorkOrderImages_legacy_2026_06_03';
GO


-- ============================================================================
-- 1. Base V2 tables  (mirrors init_workorders_db() in workorders.py)
-- ============================================================================

IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = 'WO_Complaints')
    CREATE TABLE dbo.WO_Complaints (
        ComplaintID       INT IDENTITY(1,1) PRIMARY KEY,
        Description       NVARCHAR(MAX)  NOT NULL,
        Source            NVARCHAR(20)   NOT NULL DEFAULT 'self',
        ImpactDescription NVARCHAR(MAX)  NULL,
        ImpactAmount      DECIMAL(18,2)  NULL,
        MachineName       NVARCHAR(255)  NULL,
        MachineCode       NVARCHAR(50)   NULL,
        Status            NVARCHAR(20)   NOT NULL DEFAULT 'open',
        JobOrderID        INT            NULL,
        SubmitterEmail    NVARCHAR(255)  NOT NULL,
        SubmittedAt       DATETIME2      NOT NULL DEFAULT SYSUTCDATETIME()
    );
GO

IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = 'WO_JobOrders')
    CREATE TABLE dbo.WO_JobOrders (
        JobOrderID        INT IDENTITY(1,1) PRIMARY KEY,
        ComplaintID       INT            NULL,
        MachineName       NVARCHAR(255)  NOT NULL,
        MachineCode       NVARCHAR(50)   NULL,
        Notes             NVARCHAR(MAX)  NULL,
        AssignedTo        NVARCHAR(255)  NULL,
        Priority          NVARCHAR(10)   NOT NULL DEFAULT 'normal',
        Status            NVARCHAR(20)   NOT NULL DEFAULT 'open',
        Report            NVARCHAR(MAX)  NULL,
        RootCause         NVARCHAR(MAX)  NULL,
        CorrectiveAction  NVARCHAR(MAX)  NULL,
        PreventiveAction  NVARCHAR(MAX)  NULL,
        CreatedBy         NVARCHAR(255)  NOT NULL,
        CreatedAt         DATETIME2      NOT NULL DEFAULT SYSUTCDATETIME(),
        CompletedBy       NVARCHAR(255)  NULL,
        CompletedAt       DATETIME2      NULL
    );
GO

IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = 'WO_DeliveryOrders')
    CREATE TABLE dbo.WO_DeliveryOrders (
        DeliveryOrderID   INT IDENTITY(1,1) PRIMARY KEY,
        MachineName       NVARCHAR(255)  NOT NULL,
        MachineCode       NVARCHAR(50)   NULL,
        Notes             NVARCHAR(MAX)  NULL,
        AssignedTo        NVARCHAR(255)  NULL,
        Priority          NVARCHAR(10)   NOT NULL DEFAULT 'normal',
        Status            NVARCHAR(20)   NOT NULL DEFAULT 'open',
        Item1Qty          INT            NOT NULL DEFAULT 0,
        Item2Qty          INT            NOT NULL DEFAULT 0,
        Item3Qty          INT            NOT NULL DEFAULT 0,
        Item4Qty          INT            NOT NULL DEFAULT 0,
        Item5Qty          INT            NOT NULL DEFAULT 0,
        Item6Qty          INT            NOT NULL DEFAULT 0,
        Item7Qty          INT            NOT NULL DEFAULT 0,
        Item8Qty          INT            NOT NULL DEFAULT 0,
        RecipientName     NVARCHAR(255)  NULL,
        CreatedBy         NVARCHAR(255)  NOT NULL,
        CreatedAt         DATETIME2      NOT NULL DEFAULT SYSUTCDATETIME(),
        CompletedBy       NVARCHAR(255)  NULL,
        CompletedAt       DATETIME2      NULL
    );
GO

IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = 'WO_Images')
    CREATE TABLE dbo.WO_Images (
        ImageID      INT IDENTITY(1,1) PRIMARY KEY,
        ParentType   NVARCHAR(20)   NOT NULL,
        ParentID     INT            NOT NULL,
        Stage        NVARCHAR(20)   NOT NULL,
        ImageData    VARBINARY(MAX) NULL,         -- was NOT NULL in init_workorders_db; we keep it nullable for SP-only rows
        ContentType  NVARCHAR(100)  NOT NULL,
        FileName     NVARCHAR(255)  NULL,
        UploadedBy   NVARCHAR(255)  NOT NULL,
        UploadedAt   DATETIME2      NOT NULL DEFAULT SYSUTCDATETIME()
    );
GO

IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = 'WO_Activity')
    CREATE TABLE dbo.WO_Activity (
        ActivityID   INT IDENTITY(1,1) PRIMARY KEY,
        ParentType   NVARCHAR(20)   NOT NULL,
        ParentID     INT            NOT NULL,
        Action       NVARCHAR(50)   NOT NULL,
        Detail       NVARCHAR(MAX)  NULL,
        ByUser       NVARCHAR(255)  NOT NULL,
        AtTime       DATETIME2      NOT NULL DEFAULT SYSUTCDATETIME()
    );
GO

-- Base indexes (mirror init_workorders_db)
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_WOImg_Parent')
    CREATE INDEX IX_WOImg_Parent ON dbo.WO_Images (ParentType, ParentID);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_WOAct_Parent')
    CREATE INDEX IX_WOAct_Parent ON dbo.WO_Activity (ParentType, ParentID);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_WOJO_Assigned')
    CREATE INDEX IX_WOJO_Assigned ON dbo.WO_JobOrders (AssignedTo, Status);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_WODO_Assigned')
    CREATE INDEX IX_WODO_Assigned ON dbo.WO_DeliveryOrders (AssignedTo, Status);
GO


-- ============================================================================
-- 2. WO_Complaints  — additive columns + Status → StatusCode (TINYINT)
-- ============================================================================

IF COL_LENGTH('dbo.WO_Complaints', 'FirstReportedAt') IS NULL
    ALTER TABLE dbo.WO_Complaints ADD FirstReportedAt DATETIME2 NULL;
GO

IF COL_LENGTH('dbo.WO_Complaints', 'ReportedBy') IS NULL
    ALTER TABLE dbo.WO_Complaints ADD ReportedBy NVARCHAR(255) NULL;
GO

IF COL_LENGTH('dbo.WO_Complaints', 'EventCode') IS NULL
    ALTER TABLE dbo.WO_Complaints ADD EventCode INT NULL;
GO

IF COL_LENGTH('dbo.WO_Complaints', 'PerceivedUrgency') IS NULL
    ALTER TABLE dbo.WO_Complaints ADD PerceivedUrgency TINYINT NOT NULL
        CONSTRAINT DF_WOC_PerceivedUrgency DEFAULT 1;
GO

IF COL_LENGTH('dbo.WO_Complaints', 'DisplayID') IS NULL
    ALTER TABLE dbo.WO_Complaints ADD DisplayID NVARCHAR(30) NULL;
GO

IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = 'CK_WOC_EventCode_Range')
    ALTER TABLE dbo.WO_Complaints
        ADD CONSTRAINT CK_WOC_EventCode_Range
        CHECK (EventCode IS NULL OR (EventCode BETWEEN 600000 AND 699999));
GO

IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = 'CK_WOC_PerceivedUrgency_Range')
    ALTER TABLE dbo.WO_Complaints
        ADD CONSTRAINT CK_WOC_PerceivedUrgency_Range
        CHECK (PerceivedUrgency BETWEEN 0 AND 2);
GO

-- StatusCode (parallel TINYINT)
IF COL_LENGTH('dbo.WO_Complaints', 'StatusCode') IS NULL
    ALTER TABLE dbo.WO_Complaints ADD StatusCode TINYINT NULL;
GO

UPDATE dbo.WO_Complaints
SET StatusCode = CASE
    WHEN LOWER(LTRIM(RTRIM(Status))) = 'open'        THEN 0
    WHEN LOWER(LTRIM(RTRIM(Status))) = 'in_progress' THEN 1
    WHEN LOWER(LTRIM(RTRIM(Status))) IN ('closed','completed') THEN 2
    ELSE 0
END
WHERE StatusCode IS NULL;
GO

IF EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('dbo.WO_Complaints')
      AND name = 'StatusCode' AND is_nullable = 1
)
BEGIN
    ALTER TABLE dbo.WO_Complaints ALTER COLUMN StatusCode TINYINT NOT NULL;
    ALTER TABLE dbo.WO_Complaints
        ADD CONSTRAINT DF_WOC_StatusCode DEFAULT 0 FOR StatusCode;
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = 'CK_WOC_StatusCode_Range')
    ALTER TABLE dbo.WO_Complaints
        ADD CONSTRAINT CK_WOC_StatusCode_Range
        CHECK (StatusCode BETWEEN 0 AND 2);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_WOC_StatusCode')
    CREATE INDEX IX_WOC_StatusCode ON dbo.WO_Complaints (StatusCode, SubmittedAt DESC);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_WOC_EventCode')
    CREATE INDEX IX_WOC_EventCode ON dbo.WO_Complaints (EventCode);
GO

-- Backfill DisplayID (no-op when table is empty; safe when populated)
;WITH numbered AS (
    SELECT
        ComplaintID,
        SubmittedAt,
        FORMAT(SubmittedAt, 'yyMM') AS YYMM,
        ROW_NUMBER() OVER (PARTITION BY FORMAT(SubmittedAt, 'yyMM')
                           ORDER BY SubmittedAt, ComplaintID) AS Seq
    FROM dbo.WO_Complaints
    WHERE DisplayID IS NULL
)
UPDATE c
SET DisplayID = CONCAT('KNM-CMP-', RIGHT('0000' + CAST(numbered.Seq AS VARCHAR(10)), 4), '-', numbered.YYMM)
FROM dbo.WO_Complaints c
JOIN numbered ON c.ComplaintID = numbered.ComplaintID;
GO

-- (DisplayID was created NVARCHAR(30) NULL above; no ALTER COLUMN needed.
--  The earlier ALTER caused a re-run failure because the unique index below
--  depends on this column.)

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'UX_WOC_DisplayID')
    CREATE UNIQUE INDEX UX_WOC_DisplayID ON dbo.WO_Complaints (DisplayID) WHERE DisplayID IS NOT NULL;
GO


-- ============================================================================
-- 3. WO_JobOrders  — additive columns + Status / Priority → TINYINT
-- ============================================================================

IF COL_LENGTH('dbo.WO_JobOrders', 'EventCode') IS NULL
    ALTER TABLE dbo.WO_JobOrders ADD EventCode INT NULL;
GO

IF COL_LENGTH('dbo.WO_JobOrders', 'Diagnosis') IS NULL
    ALTER TABLE dbo.WO_JobOrders ADD Diagnosis NVARCHAR(MAX) NULL;
GO

IF COL_LENGTH('dbo.WO_JobOrders', 'ProposedFix') IS NULL
    ALTER TABLE dbo.WO_JobOrders ADD ProposedFix NVARCHAR(MAX) NULL;
GO

IF COL_LENGTH('dbo.WO_JobOrders', 'LastBlockReason') IS NULL
    ALTER TABLE dbo.WO_JobOrders ADD LastBlockReason NVARCHAR(MAX) NULL;
GO

IF COL_LENGTH('dbo.WO_JobOrders', 'DisplayID') IS NULL
    ALTER TABLE dbo.WO_JobOrders ADD DisplayID NVARCHAR(30) NULL;
GO

IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = 'CK_WOJO_EventCode_Range')
    ALTER TABLE dbo.WO_JobOrders
        ADD CONSTRAINT CK_WOJO_EventCode_Range
        CHECK (EventCode IS NULL OR (EventCode BETWEEN 800000 AND 899999));
GO

-- StatusCode
IF COL_LENGTH('dbo.WO_JobOrders', 'StatusCode') IS NULL
    ALTER TABLE dbo.WO_JobOrders ADD StatusCode TINYINT NULL;
GO

UPDATE dbo.WO_JobOrders
SET StatusCode = CASE
    WHEN LOWER(LTRIM(RTRIM(Status))) IN ('open','in_progress') THEN 0
    WHEN LOWER(LTRIM(RTRIM(Status))) = 'needs_assistance'      THEN 1
    WHEN LOWER(LTRIM(RTRIM(Status))) IN ('completed','closed') THEN 2
    ELSE 0
END
WHERE StatusCode IS NULL;
GO

IF EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('dbo.WO_JobOrders')
      AND name = 'StatusCode' AND is_nullable = 1
)
BEGIN
    ALTER TABLE dbo.WO_JobOrders ALTER COLUMN StatusCode TINYINT NOT NULL;
    ALTER TABLE dbo.WO_JobOrders
        ADD CONSTRAINT DF_WOJO_StatusCode DEFAULT 0 FOR StatusCode;
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = 'CK_WOJO_StatusCode_Range')
    ALTER TABLE dbo.WO_JobOrders
        ADD CONSTRAINT CK_WOJO_StatusCode_Range
        CHECK (StatusCode BETWEEN 0 AND 2);
GO

-- PriorityCode
IF COL_LENGTH('dbo.WO_JobOrders', 'PriorityCode') IS NULL
    ALTER TABLE dbo.WO_JobOrders ADD PriorityCode TINYINT NULL;
GO

UPDATE dbo.WO_JobOrders
SET PriorityCode = CASE
    WHEN LOWER(LTRIM(RTRIM(Priority))) = 'low'    THEN 0
    WHEN LOWER(LTRIM(RTRIM(Priority))) = 'normal' THEN 1
    WHEN LOWER(LTRIM(RTRIM(Priority))) = 'high'   THEN 2
    ELSE 1
END
WHERE PriorityCode IS NULL;
GO

IF EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('dbo.WO_JobOrders')
      AND name = 'PriorityCode' AND is_nullable = 1
)
BEGIN
    ALTER TABLE dbo.WO_JobOrders ALTER COLUMN PriorityCode TINYINT NOT NULL;
    ALTER TABLE dbo.WO_JobOrders
        ADD CONSTRAINT DF_WOJO_PriorityCode DEFAULT 1 FOR PriorityCode;
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = 'CK_WOJO_PriorityCode_Range')
    ALTER TABLE dbo.WO_JobOrders
        ADD CONSTRAINT CK_WOJO_PriorityCode_Range
        CHECK (PriorityCode BETWEEN 0 AND 2);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_WOJO_StatusCode')
    CREATE INDEX IX_WOJO_StatusCode ON dbo.WO_JobOrders (StatusCode, CreatedAt DESC);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_WOJO_EventCode')
    CREATE INDEX IX_WOJO_EventCode ON dbo.WO_JobOrders (EventCode);
GO

;WITH numbered AS (
    SELECT
        JobOrderID,
        CreatedAt,
        FORMAT(CreatedAt, 'yyMM') AS YYMM,
        ROW_NUMBER() OVER (PARTITION BY FORMAT(CreatedAt, 'yyMM')
                           ORDER BY CreatedAt, JobOrderID) AS Seq
    FROM dbo.WO_JobOrders
    WHERE DisplayID IS NULL
)
UPDATE j
SET DisplayID = CONCAT('KNM-WkO-', RIGHT('0000' + CAST(numbered.Seq AS VARCHAR(10)), 4), '-', numbered.YYMM)
FROM dbo.WO_JobOrders j
JOIN numbered ON j.JobOrderID = numbered.JobOrderID;
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'UX_WOJO_DisplayID')
    CREATE UNIQUE INDEX UX_WOJO_DisplayID ON dbo.WO_JobOrders (DisplayID) WHERE DisplayID IS NOT NULL;
GO


-- ============================================================================
-- 4. WO_Images  — SharePoint columns
-- ============================================================================

IF COL_LENGTH('dbo.WO_Images', 'SPItemID') IS NULL
    ALTER TABLE dbo.WO_Images ADD SPItemID NVARCHAR(255) NULL;
GO

IF COL_LENGTH('dbo.WO_Images', 'SPWebURL') IS NULL
    ALTER TABLE dbo.WO_Images ADD SPWebURL NVARCHAR(1024) NULL;
GO

-- Ensure ImageData is nullable (was already created nullable in section 1 if fresh; this handles re-runs over an older definition)
IF EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('dbo.WO_Images') AND name = 'ImageData' AND is_nullable = 0
)
    ALTER TABLE dbo.WO_Images ALTER COLUMN ImageData VARBINARY(MAX) NULL;
GO


-- ============================================================================
-- 5. WO_JobOrderTasks  — NEW (per-WO tickbox checklist)
-- ============================================================================

IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = 'WO_JobOrderTasks')
    CREATE TABLE dbo.WO_JobOrderTasks (
        TaskID        INT IDENTITY(1,1) PRIMARY KEY,
        JobOrderID    INT             NOT NULL,
        SeqNum        INT             NOT NULL,
        Label         NVARCHAR(500)   NOT NULL,
        Done          BIT             NOT NULL DEFAULT 0,
        BlockedNote   NVARCHAR(MAX)   NULL,
        CompletedBy   NVARCHAR(255)   NULL,
        CompletedAt   DATETIME2       NULL,
        CONSTRAINT FK_WOJOTask_JobOrder
            FOREIGN KEY (JobOrderID) REFERENCES dbo.WO_JobOrders(JobOrderID)
    );
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_WOJOTask_JobOrder')
    CREATE INDEX IX_WOJOTask_JobOrder ON dbo.WO_JobOrderTasks (JobOrderID, SeqNum);
GO


-- ============================================================================
-- 6. WO_KB_Entries  +  WO_KB_Tickboxes  — NEW (knowledge base)
-- ============================================================================

IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = 'WO_KB_Entries')
    CREATE TABLE dbo.WO_KB_Entries (
        KBID          INT IDENTITY(1,1) PRIMARY KEY,
        EventCode     INT             NULL,
        Title         NVARCHAR(255)   NOT NULL,
        Diagnosis     NVARCHAR(MAX)   NULL,
        SuggestedFix  NVARCHAR(MAX)   NULL,
        UseCount      INT             NOT NULL DEFAULT 0,
        CreatedBy     NVARCHAR(255)   NOT NULL,
        CreatedAt     DATETIME2       NOT NULL DEFAULT SYSUTCDATETIME(),
        UpdatedBy     NVARCHAR(255)   NULL,
        UpdatedAt     DATETIME2       NULL,
        CONSTRAINT CK_WOKB_EventCode_Range
            CHECK (EventCode IS NULL
                OR EventCode BETWEEN 600000 AND 699999
                OR EventCode BETWEEN 800000 AND 899999)
    );
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_WOKB_EventCode')
    CREATE INDEX IX_WOKB_EventCode ON dbo.WO_KB_Entries (EventCode, UseCount DESC);
GO

IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = 'WO_KB_Tickboxes')
    CREATE TABLE dbo.WO_KB_Tickboxes (
        TBID    INT IDENTITY(1,1) PRIMARY KEY,
        KBID    INT             NOT NULL,
        SeqNum  INT             NOT NULL,
        Label   NVARCHAR(500)   NOT NULL,
        CONSTRAINT FK_WOKBTB_KB
            FOREIGN KEY (KBID) REFERENCES dbo.WO_KB_Entries(KBID) ON DELETE CASCADE
    );
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_WOKBTB_KB')
    CREATE INDEX IX_WOKBTB_KB ON dbo.WO_KB_Tickboxes (KBID, SeqNum);
GO


-- ============================================================================
-- 7. WO_Counters  — NEW (per-month DisplayID allocator)
-- ============================================================================

IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = 'WO_Counters')
    CREATE TABLE dbo.WO_Counters (
        Kind      NVARCHAR(10)  NOT NULL,   -- 'CMP' or 'WkO'
        YYMM      CHAR(4)       NOT NULL,   -- e.g. '2606'
        NextSeq   INT           NOT NULL,
        CONSTRAINT PK_WO_Counters PRIMARY KEY (Kind, YYMM)
    );
GO

-- Seed from any backfilled DisplayIDs so first new insert doesn't collide.
;WITH cmp_max AS (
    SELECT RIGHT(DisplayID, 4) AS YYMM,
           MAX(CAST(SUBSTRING(DisplayID, 9, 4) AS INT)) AS MaxSeq
    FROM dbo.WO_Complaints
    WHERE DisplayID LIKE 'KNM-CMP-____-____'
    GROUP BY RIGHT(DisplayID, 4)
)
MERGE dbo.WO_Counters AS tgt
USING (SELECT 'CMP' AS Kind, YYMM, MaxSeq + 1 AS NextSeq FROM cmp_max) AS src
   ON tgt.Kind = src.Kind AND tgt.YYMM = src.YYMM
WHEN NOT MATCHED THEN
    INSERT (Kind, YYMM, NextSeq) VALUES (src.Kind, src.YYMM, src.NextSeq);
GO

;WITH wko_max AS (
    SELECT RIGHT(DisplayID, 4) AS YYMM,
           MAX(CAST(SUBSTRING(DisplayID, 9, 4) AS INT)) AS MaxSeq
    FROM dbo.WO_JobOrders
    WHERE DisplayID LIKE 'KNM-WkO-____-____'
    GROUP BY RIGHT(DisplayID, 4)
)
MERGE dbo.WO_Counters AS tgt
USING (SELECT 'WkO' AS Kind, YYMM, MaxSeq + 1 AS NextSeq FROM wko_max) AS src
   ON tgt.Kind = src.Kind AND tgt.YYMM = src.YYMM
WHEN NOT MATCHED THEN
    INSERT (Kind, YYMM, NextSeq) VALUES (src.Kind, src.YYMM, src.NextSeq);
GO


-- ============================================================================
-- 8. VERIFICATION
-- ============================================================================

SELECT name AS TableName, create_date, modify_date
FROM sys.tables
WHERE name LIKE 'WO[_]%' OR name LIKE 'WorkOrder%legacy%'
ORDER BY name;
GO

SELECT
    'WO_Complaints'      AS TableName, COUNT(*) AS [Rows] FROM dbo.WO_Complaints
UNION ALL
SELECT 'WO_JobOrders',          COUNT(*) FROM dbo.WO_JobOrders
UNION ALL
SELECT 'WO_DeliveryOrders',     COUNT(*) FROM dbo.WO_DeliveryOrders
UNION ALL
SELECT 'WO_Images',             COUNT(*) FROM dbo.WO_Images
UNION ALL
SELECT 'WO_Activity',           COUNT(*) FROM dbo.WO_Activity
UNION ALL
SELECT 'WO_JobOrderTasks',      COUNT(*) FROM dbo.WO_JobOrderTasks
UNION ALL
SELECT 'WO_KB_Entries',         COUNT(*) FROM dbo.WO_KB_Entries
UNION ALL
SELECT 'WO_KB_Tickboxes',       COUNT(*) FROM dbo.WO_KB_Tickboxes
UNION ALL
SELECT 'WO_Counters',           COUNT(*) FROM dbo.WO_Counters
UNION ALL
SELECT 'WorkOrders_legacy_2026_06_03',         COUNT(*) FROM dbo.WorkOrders_legacy_2026_06_03
UNION ALL
SELECT 'WorkOrderActivity_legacy_2026_06_03',  COUNT(*) FROM dbo.WorkOrderActivity_legacy_2026_06_03
UNION ALL
SELECT 'WorkOrderImages_legacy_2026_06_03',    COUNT(*) FROM dbo.WorkOrderImages_legacy_2026_06_03;
GO


-- ============================================================================
-- ROLLBACK (manual — run only if you need to revert)
-- ============================================================================
/*
-- 1. Restore V1 legacy names:
EXEC sp_rename 'dbo.WorkOrders_legacy_2026_06_03',        'WorkOrders';
EXEC sp_rename 'dbo.WorkOrderActivity_legacy_2026_06_03', 'WorkOrderActivity';
EXEC sp_rename 'dbo.WorkOrderImages_legacy_2026_06_03',   'WorkOrderImages';

-- 2. Drop V2 tables (data loss — V2 has no real prod data yet at this point):
DROP TABLE IF EXISTS dbo.WO_KB_Tickboxes;
DROP TABLE IF EXISTS dbo.WO_KB_Entries;
DROP TABLE IF EXISTS dbo.WO_JobOrderTasks;
DROP TABLE IF EXISTS dbo.WO_Counters;
DROP TABLE IF EXISTS dbo.WO_Activity;
DROP TABLE IF EXISTS dbo.WO_Images;
DROP TABLE IF EXISTS dbo.WO_DeliveryOrders;
DROP TABLE IF EXISTS dbo.WO_JobOrders;
DROP TABLE IF EXISTS dbo.WO_Complaints;
*/
