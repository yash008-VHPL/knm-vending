-- ─────────────────────────────────────────────────────────────────────────────
-- KNM Vending — Location de-dup + dated move history   ·   AZURE QUERY EDITOR
-- DB     : Machine DispensedDrink
-- Date   : 2026-07-01  (rev 2 — split into 2 blocks; robust; re-throws on error)
--
-- RUN AS TWO SEPARATE EXECUTIONS (the portal editor runs the whole pane as one
-- batch, and creating + using a new table in one batch is what broke rev 1):
--   BLOCK 1 — highlight from "BLOCK 1" to the "END BLOCK 1" line, Run.
--   BLOCK 2 — then highlight from "BLOCK 2" to the "END BLOCK 2" line, Run.
--
-- BLOCK 1 just creates the history table (+ indexes). Idempotent, safe to re-run.
-- BLOCK 2 de-dups the 4 machines, back-fills history, adds UNIQUE(MachineCode),
--   verifies, and COMMITs — or rolls back AND re-throws the real error so you see
--   it (no more silent "success"). It does NOT touch [MasterData Table].
-- ─────────────────────────────────────────────────────────────────────────────


-- ═══════════════════════ BLOCK 1 — create table (run first) ═══════════════════
SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;

IF OBJECT_ID('dbo.MachineLocationHistory','U') IS NULL
    CREATE TABLE dbo.MachineLocationHistory (
        HistoryID       INT IDENTITY(1,1) PRIMARY KEY,
        MachineCode     NVARCHAR(50)   NOT NULL,
        LocationName    NVARCHAR(255)  NOT NULL,
        Latitude        FLOAT          NULL,
        Longitude       FLOAT          NULL,
        ValidFromOle    FLOAT          NOT NULL,   -- inclusive, SGT OLE float (epoch 1899-12-30)
        ValidToOle      FLOAT          NULL,       -- exclusive, NULL = current
        Source          NVARCHAR(30)   NOT NULL,
        MovementOrderID INT            NULL,
        CreatedAt       DATETIME2      NOT NULL DEFAULT SYSUTCDATETIME()
    );

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_MLH_Code_From')
    CREATE INDEX IX_MLH_Code_From ON dbo.MachineLocationHistory (MachineCode, ValidFromOle);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='UX_MLH_OpenInterval')
    CREATE UNIQUE INDEX UX_MLH_OpenInterval
        ON dbo.MachineLocationHistory (MachineCode) WHERE ValidToOle IS NULL;

SELECT 'BLOCK 1 OK — MachineLocationHistory ready. Now run BLOCK 2.' AS Result;
GO
-- ═══════════════════════ END BLOCK 1 ═════════════════════════════════════════



-- ═══════════════════════ BLOCK 2 — de-dup + backfill + unique (run second) ════
SET XACT_ABORT ON;
SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;
BEGIN TRY
    BEGIN TRAN;

    -- 1. Collapse the 3 exact-duplicate machines (identical rows → keep one).
    --    Partition on RAW MachineCode so this matches the UNIQUE index added below.
    ;WITH r AS (
        SELECT ROW_NUMBER() OVER (PARTITION BY MachineCode ORDER BY %%physloc%%) AS rn
        FROM dbo.MachineLookup
        WHERE MachineCode IN ('51421700','51421702','51421685')
    )
    DELETE FROM r WHERE rn > 1;

    -- 2. Collapse 55120031 → keep current 'Inzy Group', drop the old name.
    DELETE FROM dbo.MachineLookup
    WHERE MachineCode = '55120031' AND MachineName = 'Kranji Camp Blk 808';

    -- 3. Guard on RAW MachineCode (same key the UNIQUE index will use).
    IF EXISTS (SELECT 1 FROM dbo.MachineLookup
               GROUP BY MachineCode HAVING COUNT(*) > 1)
        THROW 50001, 'Duplicate MachineCode still present after de-dup — rolling back.', 1;

    -- 4. Back-fill one OPEN interval per machine = current location (only if empty).
    IF NOT EXISTS (SELECT 1 FROM dbo.MachineLocationHistory)
        INSERT INTO dbo.MachineLocationHistory
            (MachineCode, LocationName, Latitude, Longitude, ValidFromOle, ValidToOle, Source)
        SELECT MachineCode, MachineName, Latitude, Longitude, 0.0, NULL, 'backfill-current'
        FROM dbo.MachineLookup;

    -- 5. Uniqueness guard on the lookup — the doubling can never recur.
    IF NOT EXISTS (SELECT 1 FROM sys.indexes
                   WHERE object_id = OBJECT_ID('dbo.MachineLookup')
                     AND name = 'UX_MachineLookup_MachineCode')
        CREATE UNIQUE INDEX UX_MachineLookup_MachineCode ON dbo.MachineLookup (MachineCode);

    -- 6. Verify (both references are to pre-existing tables now — no dynamic SQL).
    DECLARE @dupCodes INT = (
        SELECT COUNT(*) FROM (
            SELECT 1 c FROM dbo.MachineLookup
            GROUP BY MachineCode HAVING COUNT(*) > 1) a);
    DECLARE @multiOpen INT = (
        SELECT COUNT(*) FROM (
            SELECT 1 c FROM dbo.MachineLocationHistory
            WHERE ValidToOle IS NULL
            GROUP BY MachineCode HAVING COUNT(*) > 1) b);

    IF @dupCodes <> 0 OR @multiOpen <> 0
        THROW 50002, 'Verification failed — rolling back.', 1;

    COMMIT TRAN;
    SELECT 'BLOCK 2 OK — 4 machines de-duped, history back-filled, UNIQUE added.' AS Result;
END TRY
BEGIN CATCH
    IF @@TRANCOUNT > 0 ROLLBACK TRAN;
    THROW;   -- re-throw the REAL error so it shows as a failure, not a fake success
END CATCH;
-- ═══════════════════════ END BLOCK 2 ═════════════════════════════════════════


-- ═══════════════════════ OPTIONAL — 55120031 history split (later) ════════════
-- Run only when you have the real move date. Splits its history so pre-date vends
-- read 'Kranji Camp Blk 808' and later vends read 'Inzy Group'. Or log it from the
-- app's Movements tab after the code deploy.
/*
SET XACT_ABORT ON; SET QUOTED_IDENTIFIER ON; SET ANSI_NULLS ON;
BEGIN TRY
    BEGIN TRAN;
    DECLARE @code NVARCHAR(50)='55120031', @oldLoc NVARCHAR(255)='Kranji Camp Blk 808';
    DECLARE @move DATETIME2 = '2026-04-15 00:00';                 -- <<< EDIT: real move date (SGT)
    DECLARE @cut FLOAT = CAST(CONVERT(datetime,@move) AS FLOAT) + 2.0;
    DECLARE @hid INT, @vfrom FLOAT;
    SELECT TOP 1 @hid=HistoryID, @vfrom=ValidFromOle FROM dbo.MachineLocationHistory
    WHERE MachineCode=@code AND ValidToOle IS NULL ORDER BY ValidFromOle DESC;
    IF @hid IS NULL   THROW 50010,'No open interval for 55120031.',1;
    IF @cut <= @vfrom THROW 50011,'Move date at/before interval start.',1;
    UPDATE dbo.MachineLocationHistory SET ValidFromOle=@cut WHERE HistoryID=@hid;
    INSERT INTO dbo.MachineLocationHistory (MachineCode,LocationName,ValidFromOle,ValidToOle,Source)
    VALUES (@code,@oldLoc,@vfrom,@cut,'corrective');
    COMMIT TRAN;
    SELECT 'OK — 55120031 split at ' + CONVERT(NVARCHAR(30),@move) AS Result;
END TRY
BEGIN CATCH
    IF @@TRANCOUNT>0 ROLLBACK TRAN;
    THROW;
END CATCH;
*/
