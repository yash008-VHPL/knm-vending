-- ─────────────────────────────────────────────────────────────────────────────
-- KNM Vending — Location-History fix  ·  PART A : INSPECT (read-only, NON-destructive)
-- Date   : 2026-06-29
-- Author : Yashodhan Bhawe
-- Target : Azure SQL — database "Machine DispensedDrink"
--
-- PURPOSE
--   Diagnose the "machine relocation has no sharp cutoff / vends double up under
--   the old location name" bug BEFORE changing anything. Run this first, read the
--   result sets, confirm they match expectations, THEN run PART B.
--
--   Root cause (to confirm below):
--     1. MachineLookup has >1 row for the same MachineCode (no UNIQUE constraint) →
--        the LEFT JOIN in /api/transactions fans out → identical-timestamp rows
--        appear under both the old and new location name.
--     2. MachineLookup stores only the CURRENT name → there is no per-vend history,
--        so a relocation re-labels ALL historical vends to the new location.
--
-- This script makes NO changes. Every statement is SELECT-only.
-- ─────────────────────────────────────────────────────────────────────────────

SET NOCOUNT ON;
USE [Machine DispensedDrink];
GO

PRINT '========== A1. Does MachineLookup have a PRIMARY KEY / UNIQUE on MachineCode? ==========';
SELECT
    i.name              AS IndexName,
    i.is_primary_key    AS IsPK,
    i.is_unique         AS IsUnique,
    STRING_AGG(c.name, ', ') WITHIN GROUP (ORDER BY ic.key_ordinal) AS KeyColumns
FROM sys.indexes i
JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
JOIN sys.columns c        ON c.object_id  = ic.object_id AND c.column_id = ic.column_id
WHERE i.object_id = OBJECT_ID('dbo.MachineLookup')
GROUP BY i.name, i.is_primary_key, i.is_unique;
GO

PRINT '========== A2. Column types — confirm MachineLookup.MachineCode vs [MasterData Table].[Machine Code] ==========';
SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, IS_NULLABLE
FROM INFORMATION_SCHEMA.COLUMNS
WHERE (TABLE_NAME = 'MachineLookup'        AND COLUMN_NAME IN ('MachineCode','MachineName','Latitude','Longitude','IsActive','LastTopupTimestamp','PreviousTopupTimestamp','CountBeforeLastTopup','DecommissionedAt','DecommissionReason'))
   OR (TABLE_NAME = 'MasterData Table'     AND COLUMN_NAME IN ('Machine Code','Date Time','Event Code'))
ORDER BY TABLE_NAME, COLUMN_NAME;
GO

PRINT '========== A3. DUPLICATE rows in MachineLookup (the fan-out source) ==========';
PRINT '-- Any MachineCode with cnt > 1 will DOUBLE in /api/transactions. Expect ST Marine Benoi machine here.';
SELECT CAST(MachineCode AS NVARCHAR(50)) AS MachineCode,
       COUNT(*) AS RowCnt,
       STRING_AGG(MachineName, '  |  ') AS NamesOnFile
FROM dbo.MachineLookup
GROUP BY CAST(MachineCode AS NVARCHAR(50))
HAVING COUNT(*) > 1
ORDER BY RowCnt DESC, MachineCode;
GO

PRINT '========== A3b. Full row detail for every duplicated MachineCode (review before de-dupe) ==========';
;WITH dups AS (
    SELECT CAST(MachineCode AS NVARCHAR(50)) AS mc
    FROM dbo.MachineLookup
    GROUP BY CAST(MachineCode AS NVARCHAR(50))
    HAVING COUNT(*) > 1
)
SELECT ml.*
FROM dbo.MachineLookup ml
JOIN dups ON dups.mc = CAST(ml.MachineCode AS NVARCHAR(50))
ORDER BY CAST(ml.MachineCode AS NVARCHAR(50)), ml.MachineName;
GO

PRINT '========== A4. Reproduce the doubling for a date range (edit the dates) ==========';
PRINT '-- This mirrors /api/transactions. If a MachineCode is duplicated, each vend appears once per duplicate row.';
DECLARE @startOle FLOAT = CAST(CONVERT(datetime, '2026-06-01 00:00:00') AS FLOAT) + 2.0;
DECLARE @endOle   FLOAT = CAST(CONVERT(datetime, '2026-06-29 23:59:59') AS FLOAT) + 2.0;
SELECT TOP 50
    CAST(mdt.[Machine Code] AS NVARCHAR(50)) AS MachineCode,
    CAST(mdt.[Date Time] AS FLOAT)           AS VendOle,
    ml.MachineName,
    COUNT(*) OVER (PARTITION BY CAST(mdt.[Machine Code] AS NVARCHAR(50)), CAST(mdt.[Date Time] AS FLOAT)) AS RowsPerVend
FROM [MasterData Table] mdt
LEFT JOIN dbo.MachineLookup ml
       ON CAST(mdt.[Machine Code] AS NVARCHAR(50)) = CAST(ml.MachineCode AS NVARCHAR(50))
WHERE CAST(mdt.[Date Time] AS FLOAT) >= @startOle
  AND CAST(mdt.[Date Time] AS FLOAT) <= @endOle
  AND LEN(CAST(mdt.[Event Code] AS NVARCHAR(20))) = 6
  AND CAST(mdt.[Event Code] AS NVARCHAR(20)) LIKE '1%'
ORDER BY RowsPerVend DESC, VendOle DESC;
GO

PRINT '========== A5. Movement-order coverage (what backfill CAN reconstruct) ==========';
PRINT '-- Completed movements give real cutoffs (CompletedAt, UTC). Codes NOT here will get a single open interval = current name.';
SELECT MachineCode,
       SUM(CASE WHEN StatusCode = 2 THEN 1 ELSE 0 END) AS CompletedMoves,
       MIN(CASE WHEN StatusCode = 2 THEN CompletedAt END) AS FirstCompleted,
       MAX(CASE WHEN StatusCode = 2 THEN CompletedAt END) AS LastCompleted
FROM dbo.WO_MovementOrders
GROUP BY MachineCode
ORDER BY CompletedMoves DESC;
GO

PRINT '========== A6. Machines with vends but NO MachineLookup row (would show as raw code) ==========';
SELECT DISTINCT TOP 100 CAST(mdt.[Machine Code] AS NVARCHAR(50)) AS OrphanMachineCode
FROM [MasterData Table] mdt
LEFT JOIN dbo.MachineLookup ml
       ON CAST(mdt.[Machine Code] AS NVARCHAR(50)) = CAST(ml.MachineCode AS NVARCHAR(50))
WHERE ml.MachineCode IS NULL;
GO

PRINT '========== A7. TZ sanity — newest vend wall-clock vs UTC now ==========';
PRINT '-- VendLocalGuess should read as Singapore wall-clock. SysUtc is UTC. They should differ by ~8h.';
SELECT TOP 1
    CAST(mdt.[Date Time] AS FLOAT)                         AS NewestVendOle,
    DATEADD(SECOND, CAST((CAST(mdt.[Date Time] AS FLOAT) - 2.0 - FLOOR(CAST(mdt.[Date Time] AS FLOAT) - 2.0)) * 86400 AS INT),
            CAST(FLOOR(CAST(mdt.[Date Time] AS FLOAT)) - 2.0 AS datetime)) AS VendLocalGuess,
    SYSUTCDATETIME()                                       AS SysUtc,
    SYSDATETIME()                                          AS SysServerLocal
FROM [MasterData Table] mdt
ORDER BY CAST(mdt.[Date Time] AS FLOAT) DESC;
GO

PRINT '========== INSPECT COMPLETE — review A1..A7, then run PART B. ==========';
GO
