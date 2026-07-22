-- ─────────────────────────────────────────────────────────────────────────────
-- KNM Vending — Location-History fix  ·  PART B : APPLY
-- Date   : 2026-06-29
-- Author : Yashodhan Bhawe
-- Target : Azure SQL — database "Machine DispensedDrink"
--
-- RUN PART A FIRST and review its output. This script:
--   1. De-duplicates MachineLookup (preserves freshest top-up baselines) and adds
--      UNIQUE(MachineCode)  → kills the /api/transactions JOIN fan-out.
--   2. Creates dbo.MachineLocationHistory (effective-dated, per machine) — the
--      permanent record of every place a machine has been.
--   3. Back-fills history from completed WO_MovementOrders cutoffs + current
--      MachineLookup, so vends resolve to the location that was live AT VEND TIME
--      (sharp cutoff).
--
-- TIME UNITS (critical):
--   Vend [Date Time] is an OLE Automation float in SINGAPORE wall-clock (UTC+8),
--   epoch 1899-12-30.  WO_MovementOrders.CompletedAt is DATETIME2 in UTC.
--   Cutoff → vend-comparable OLE float:
--       CAST(CONVERT(datetime, DATEADD(HOUR, 8, CompletedAt)) AS FLOAT) + 2.0
--   ( +2.0 because SQL Server's float base is 1900-01-01 = OLE day 2.0 )
--
-- SAFETY:
--   Wrapped in a single transaction with SET XACT_ABORT ON. To dry-run, change the
--   final COMMIT to ROLLBACK, run, read the verification output, then re-run with COMMIT.
--   Idempotent: re-running does not create duplicate history or re-drop the index.
-- ─────────────────────────────────────────────────────────────────────────────

SET XACT_ABORT ON;
SET NOCOUNT ON;
USE [Machine DispensedDrink];
GO

BEGIN TRANSACTION;

-- ============================================================================
-- 1.  DE-DUPLICATE MachineLookup  (preserve freshest top-up baselines)
-- ============================================================================
-- 'Keeper' per MachineCode preference:
--   (a) row whose MachineName = the ToLocation of that machine's most recent
--       COMPLETED movement   (the true current location), else
--   (b) IsActive = 1, else
--   (c) freshest LastTopupTimestamp, else
--   (d) stable by MachineName.
-- Before deleting losers, copy the freshest top-up counters onto the keeper so
-- refill/heartbeat baselines are not lost.

;WITH last_move AS (
    SELECT MachineCode,
           ToLocation,
           ROW_NUMBER() OVER (PARTITION BY MachineCode ORDER BY CompletedAt DESC) rn
    FROM dbo.WO_MovementOrders
    WHERE StatusCode = 2 AND ToLocation IS NOT NULL
),
ranked AS (
    SELECT
        ml.*,
        ROW_NUMBER() OVER (
            PARTITION BY CAST(ml.MachineCode AS NVARCHAR(50))
            ORDER BY
                CASE WHEN lm.ToLocation IS NOT NULL AND ml.MachineName = lm.ToLocation THEN 0 ELSE 1 END,
                CASE WHEN ISNULL(ml.IsActive,1) = 1 THEN 0 ELSE 1 END,
                ISNULL(ml.LastTopupTimestamp, 0) DESC,
                ml.MachineName
        ) AS rn,
        MAX(ml.LastTopupTimestamp)     OVER (PARTITION BY CAST(ml.MachineCode AS NVARCHAR(50))) AS grp_last_topup,
        COUNT(*)                       OVER (PARTITION BY CAST(ml.MachineCode AS NVARCHAR(50))) AS grp_cnt
    FROM dbo.MachineLookup ml
    LEFT JOIN last_move lm ON lm.MachineCode = ml.MachineCode AND lm.rn = 1
)
-- 1a. Merge the freshest top-up baseline onto the keeper (only where duplicates exist)
UPDATE k
SET k.LastTopupTimestamp     = src.LastTopupTimestamp,
    k.PreviousTopupTimestamp = src.PreviousTopupTimestamp,
    k.CountBeforeLastTopup   = src.CountBeforeLastTopup
FROM dbo.MachineLookup k
JOIN ranked keeper
      ON keeper.rn = 1
     AND CAST(keeper.MachineCode AS NVARCHAR(50)) = CAST(k.MachineCode AS NVARCHAR(50))
     AND keeper.MachineName = k.MachineName
JOIN ranked src
      ON CAST(src.MachineCode AS NVARCHAR(50)) = CAST(keeper.MachineCode AS NVARCHAR(50))
     AND src.LastTopupTimestamp = src.grp_last_topup
WHERE keeper.grp_cnt > 1
  AND src.LastTopupTimestamp IS NOT NULL
  AND ISNULL(k.LastTopupTimestamp, -1) <> src.LastTopupTimestamp;

-- 1b. Delete the non-keeper duplicate rows
;WITH last_move AS (
    SELECT MachineCode, ToLocation,
           ROW_NUMBER() OVER (PARTITION BY MachineCode ORDER BY CompletedAt DESC) rn
    FROM dbo.WO_MovementOrders WHERE StatusCode = 2 AND ToLocation IS NOT NULL
),
ranked AS (
    SELECT ml.*,
        ROW_NUMBER() OVER (
            PARTITION BY CAST(ml.MachineCode AS NVARCHAR(50))
            ORDER BY
                CASE WHEN lm.ToLocation IS NOT NULL AND ml.MachineName = lm.ToLocation THEN 0 ELSE 1 END,
                CASE WHEN ISNULL(ml.IsActive,1) = 1 THEN 0 ELSE 1 END,
                ISNULL(ml.LastTopupTimestamp, 0) DESC,
                ml.MachineName
        ) AS rn
    FROM dbo.MachineLookup ml
    LEFT JOIN last_move lm ON lm.MachineCode = ml.MachineCode AND lm.rn = 1
)
DELETE FROM ranked WHERE rn > 1;
PRINT CONCAT('De-dup: removed ', @@ROWCOUNT, ' duplicate MachineLookup row(s).');

-- 1c. Enforce uniqueness so the fan-out can never recur.
-- Guard FIRST on the RAW MachineCode (same expression the UNIQUE index uses) so we
-- fail with a clear message rather than a cryptic index error. This also catches
-- multiple NULL codes (GROUP BY groups all NULLs together) and any whitespace-only
-- variants the CAST-based de-dup in 1a/1b may not have collapsed.
IF EXISTS (
    SELECT 1 FROM dbo.MachineLookup
    GROUP BY MachineCode HAVING COUNT(*) > 1
)
    THROW 50001, 'Duplicate (or multiple NULL) MachineCode remain after de-dup — aborting before UNIQUE index. Inspect MachineLookup manually.', 1;

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE object_id = OBJECT_ID('dbo.MachineLookup') AND name = 'UX_MachineLookup_MachineCode'
)
    CREATE UNIQUE INDEX UX_MachineLookup_MachineCode
        ON dbo.MachineLookup (MachineCode);
GO   -- (GO inside an open tran is fine in SSMS; if your client forbids it, remove and run as one batch)

-- ============================================================================
-- 2.  CREATE MachineLocationHistory  (effective-dated; SGT OLE floats)
-- ============================================================================
IF OBJECT_ID('dbo.MachineLocationHistory','U') IS NULL
    CREATE TABLE dbo.MachineLocationHistory (
        HistoryID       INT IDENTITY(1,1) PRIMARY KEY,
        MachineCode     NVARCHAR(50)   NOT NULL,
        LocationName    NVARCHAR(255)  NOT NULL,
        Latitude        FLOAT          NULL,
        Longitude       FLOAT          NULL,
        ValidFromOle    FLOAT          NOT NULL,   -- inclusive, SGT OLE float
        ValidToOle      FLOAT          NULL,       -- exclusive, NULL = still open
        Source          NVARCHAR(30)   NOT NULL,   -- backfill-seg0 | backfill-move | backfill-current | movement | admin
        MovementOrderID INT            NULL,
        CreatedAt       DATETIME2      NOT NULL DEFAULT SYSUTCDATETIME()
    );
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_MLH_Code_From')
    CREATE INDEX IX_MLH_Code_From ON dbo.MachineLocationHistory (MachineCode, ValidFromOle);
GO

-- At most one OPEN interval per machine (prevents future double-match)
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='UX_MLH_OpenInterval')
    CREATE UNIQUE INDEX UX_MLH_OpenInterval
        ON dbo.MachineLocationHistory (MachineCode)
        WHERE ValidToOle IS NULL;
GO

-- ============================================================================
-- 3.  BACK-FILL  (only when the table is empty — safe to re-run)
-- ============================================================================
-- To force a clean rebuild, uncomment the next line:
-- TRUNCATE TABLE dbo.MachineLocationHistory;

IF NOT EXISTS (SELECT 1 FROM dbo.MachineLocationHistory)
BEGIN
    DECLARE @SENTINEL FLOAT = 0.0;   -- covers all vends before the first known cutoff

    -- Completed movements as ordered cutoffs, converted to SGT OLE float
    ;WITH mv AS (
        SELECT
            CAST(MachineCode AS NVARCHAR(50)) AS MachineCode,
            MovementType,
            FromLocation, ToLocation, ToLat, ToLon,
            CAST(CONVERT(datetime, DATEADD(HOUR, 8, CompletedAt)) AS FLOAT) + 2.0 AS CutOle,
            MovementOrderID,
            ROW_NUMBER() OVER (PARTITION BY CAST(MachineCode AS NVARCHAR(50)) ORDER BY CompletedAt) AS rn,
            COUNT(*)     OVER (PARTITION BY CAST(MachineCode AS NVARCHAR(50)))                       AS n
        FROM dbo.WO_MovementOrders
        WHERE StatusCode = 2 AND CompletedAt IS NOT NULL
    ),
    mv2 AS (
        SELECT *,
               LEAD(CutOle) OVER (PARTITION BY MachineCode ORDER BY CutOle) AS NextCutOle
        FROM mv
    )
    -- 3a. Segment 0: from sentinel up to the first cutoff (name = first move's FromLocation, fallback current)
    INSERT INTO dbo.MachineLocationHistory (MachineCode, LocationName, Latitude, Longitude, ValidFromOle, ValidToOle, Source, MovementOrderID)
    SELECT mv2.MachineCode,
           COALESCE(NULLIF(mv2.FromLocation,''), cur.MachineName, mv2.MachineCode),
           cur.Latitude, cur.Longitude,
           @SENTINEL, mv2.CutOle, 'backfill-seg0', NULL
    FROM mv2
    LEFT JOIN dbo.MachineLookup cur ON CAST(cur.MachineCode AS NVARCHAR(50)) = mv2.MachineCode
    WHERE mv2.rn = 1;

    -- 3b. One interval per completed movement.
    --     Non-last move → name from ToLocation (retrieve = decommissioned).
    --     Last move      → open interval; name = current MachineLookup (live truth),
    --                      unless last move is a retrieve → decommissioned.
    INSERT INTO dbo.MachineLocationHistory (MachineCode, LocationName, Latitude, Longitude, ValidFromOle, ValidToOle, Source, MovementOrderID)
    SELECT
        mv2.MachineCode,
        CASE
            WHEN mv2.rn = mv2.n AND mv2.MovementType = 'retrieve' THEN '(decommissioned)'
            WHEN mv2.rn = mv2.n                                   THEN COALESCE(cur.MachineName, mv2.ToLocation, mv2.MachineCode)
            WHEN mv2.MovementType = 'retrieve'                    THEN '(decommissioned)'
            ELSE COALESCE(NULLIF(mv2.ToLocation,''), cur.MachineName, mv2.MachineCode)
        END,
        CASE WHEN mv2.rn = mv2.n THEN cur.Latitude  ELSE mv2.ToLat END,
        CASE WHEN mv2.rn = mv2.n THEN cur.Longitude ELSE mv2.ToLon END,
        mv2.CutOle,
        CASE WHEN mv2.rn = mv2.n THEN NULL ELSE mv2.NextCutOle END,
        CASE WHEN mv2.rn = mv2.n THEN 'backfill-current' ELSE 'backfill-move' END,
        mv2.MovementOrderID
    FROM mv2
    LEFT JOIN dbo.MachineLookup cur ON CAST(cur.MachineCode AS NVARCHAR(50)) = mv2.MachineCode;

    -- 3c. Machines with NO completed movements → single open interval = current name.
    INSERT INTO dbo.MachineLocationHistory (MachineCode, LocationName, Latitude, Longitude, ValidFromOle, ValidToOle, Source, MovementOrderID)
    SELECT CAST(ml.MachineCode AS NVARCHAR(50)),
           ml.MachineName, ml.Latitude, ml.Longitude,
           @SENTINEL, NULL, 'backfill-current', NULL
    FROM dbo.MachineLookup ml
    WHERE NOT EXISTS (
        SELECT 1 FROM dbo.MachineLocationHistory h
        WHERE h.MachineCode = CAST(ml.MachineCode AS NVARCHAR(50))
    );

    PRINT CONCAT('Back-fill: inserted ', (SELECT COUNT(*) FROM dbo.MachineLocationHistory), ' history interval(s).');
END
ELSE
    PRINT 'Back-fill skipped — MachineLocationHistory already populated.';
GO

-- ============================================================================
-- 4.  VERIFICATION  (read these before COMMIT)
-- ============================================================================
PRINT '--- V1. Any remaining duplicate MachineCode in MachineLookup? (expect 0 rows) ---';
SELECT CAST(MachineCode AS NVARCHAR(50)) MachineCode, COUNT(*) c
FROM dbo.MachineLookup GROUP BY CAST(MachineCode AS NVARCHAR(50)) HAVING COUNT(*) > 1;

PRINT '--- V2. Machines with >1 OPEN interval (expect 0 rows) ---';
SELECT MachineCode, COUNT(*) c
FROM dbo.MachineLocationHistory WHERE ValidToOle IS NULL
GROUP BY MachineCode HAVING COUNT(*) > 1;

PRINT '--- V3. Overlapping intervals per machine (expect 0 rows) ---';
SELECT a.MachineCode, a.HistoryID, b.HistoryID
FROM dbo.MachineLocationHistory a
JOIN dbo.MachineLocationHistory b
  ON a.MachineCode = b.MachineCode AND a.HistoryID < b.HistoryID
 AND a.ValidFromOle < ISNULL(b.ValidToOle, 1e9)
 AND b.ValidFromOle < ISNULL(a.ValidToOle, 1e9);

PRINT '--- V4. Full timeline for machines that have moved (visual check of the cutoff) ---';
SELECT h.MachineCode, h.LocationName, h.ValidFromOle, h.ValidToOle, h.Source
FROM dbo.MachineLocationHistory h
WHERE h.MachineCode IN (SELECT MachineCode FROM dbo.MachineLocationHistory GROUP BY MachineCode HAVING COUNT(*) > 1)
ORDER BY h.MachineCode, h.ValidFromOle;

PRINT '--- V5. Re-run the doubling check (RowsPerVend must now be 1 everywhere) ---';
DECLARE @s FLOAT = CAST(CONVERT(datetime,'2026-06-01 00:00:00') AS FLOAT)+2.0;
DECLARE @e FLOAT = CAST(CONVERT(datetime,'2026-06-29 23:59:59') AS FLOAT)+2.0;
SELECT TOP 50
    CAST(mdt.[Machine Code] AS NVARCHAR(50)) MachineCode,
    CAST(mdt.[Date Time] AS FLOAT) VendOle,
    loc.LocationName,
    COUNT(*) OVER (PARTITION BY CAST(mdt.[Machine Code] AS NVARCHAR(50)), CAST(mdt.[Date Time] AS FLOAT)) RowsPerVend
FROM [MasterData Table] mdt
OUTER APPLY (
    SELECT TOP 1 h.LocationName
    FROM dbo.MachineLocationHistory h
    WHERE h.MachineCode = CAST(mdt.[Machine Code] AS NVARCHAR(50))
      AND CAST(mdt.[Date Time] AS FLOAT) >= h.ValidFromOle
      AND (h.ValidToOle IS NULL OR CAST(mdt.[Date Time] AS FLOAT) < h.ValidToOle)
    ORDER BY h.ValidFromOle DESC
) loc
WHERE CAST(mdt.[Date Time] AS FLOAT) >= @s AND CAST(mdt.[Date Time] AS FLOAT) <= @e
  AND LEN(CAST(mdt.[Event Code] AS NVARCHAR(20))) = 6
  AND CAST(mdt.[Event Code] AS NVARCHAR(20)) LIKE '1%'
ORDER BY RowsPerVend DESC, VendOle DESC;
GO

-- ============================================================================
-- COMMIT or ROLLBACK
-- ============================================================================
-- Review V1..V5 above. If correct:
COMMIT TRANSACTION;
-- For a dry run instead, comment the COMMIT above and uncomment:
-- ROLLBACK TRANSACTION;
GO
