/* ============================================================================
   Franchisee accounts — schema migration                     2026-09-03
   Target: Azure SQL, database [Machine DispensedDrink]
   ----------------------------------------------------------------------------
   ADDITIVE ONLY. No DROP of any column or table. Safe to re-run.
   NO "GO" ANYWHERE IN THIS FILE — the portal Query Editor runs the whole pane
   as one batch and ignores GO (see migration_topups_2026-08-23.sql header).

   RUN EACH NUMBERED BLOCK ON ITS OWN. Read the output before the next one.

   What this is for: auresys_pull.py now logs in to the KNM Main account AND
   each franchisee account (AUVION, COFFEERUSH). Every transaction row records
   which account it came from, and the Top-ups vend counter stripes a machine
   with its franchisee's colour. NULL Account_Key = KNM Main (rows written
   before this migration are never re-stamped).

   Order of operations across the whole change:
     1. BLOCK 0..2 here (columns + index).
     2. Deploy auresys_pull.py / nets_mapping.py — the loader REFUSES to run
        until Account_Key exists, so nothing can be written half-tagged.
     3. Add the GitHub secrets, run the workflow with roster=true, paste the
        printed rows into BLOCK 3 and nets_mapping.py, review the names,
        run BLOCK 3.
     4. Deploy topups_api.py / _topups_tab.html.
   ========================================================================== */


/* ═══ BLOCK 0 — INSPECT ONLY ═══════════════════════════════════════════════
   MachineLookup.MachineCode must be a string column holding numeric-looking
   values: two live queries (app.py ~1698, alpha_preview.py ~206) join it to
   [MasterData Table].[Machine Code] WITHOUT a CAST, so a non-numeric code
   would make the whole machine list fail. The synthetic codes BLOCK 3 inserts
   are 9 digits (9 + terminal number), which fits INT. */
SELECT c.TABLE_NAME, c.COLUMN_NAME, c.DATA_TYPE, c.CHARACTER_MAXIMUM_LENGTH
FROM   INFORMATION_SCHEMA.COLUMNS c
WHERE  (c.TABLE_NAME = 'MachineLookup'      AND c.COLUMN_NAME IN ('MachineCode','MachineName','IsActive'))
    OR (c.TABLE_NAME = 'MasterData Table'   AND c.COLUMN_NAME = 'Machine Code')
    OR (c.TABLE_NAME = 'NETS_Transaction'   AND c.COLUMN_NAME = 'Account_Key')
    OR (c.TABLE_NAME = 'NETS_Unmapped_Row'  AND c.COLUMN_NAME = 'Account_Key');
-- Expect: MachineCode nvarchar, [Machine Code] int or nvarchar, no Account_Key rows yet.


/* ═══ BLOCK 1 — Account_Key columns ════════════════════════════════════════
   NVARCHAR(16) matches auresys_pull.ACCOUNT_KEY_RE (^[A-Z0-9_]{1,16}$).
   Nullable, no default: NULL means "KNM Main, written before 2026-09-03" and
   the loader writes 'MAIN' explicitly from now on. Metadata-only ALTER. */
IF COL_LENGTH('dbo.NETS_Transaction', 'Account_Key') IS NULL
    ALTER TABLE dbo.NETS_Transaction ADD Account_Key NVARCHAR(16) NULL;

IF COL_LENGTH('dbo.NETS_Unmapped_Row', 'Account_Key') IS NULL
    ALTER TABLE dbo.NETS_Unmapped_Row ADD Account_Key NVARCHAR(16) NULL;

SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, IS_NULLABLE
FROM   INFORMATION_SCHEMA.COLUMNS
WHERE  COLUMN_NAME = 'Account_Key';
-- Expect two rows.


/* ═══ BLOCK 2 — Index: carry Account_Key on the vend-counter index ═════════
   /api/topups/vendcounter now reads "Account_Key of the latest row per
   Machine_Code". Adding it to the INCLUDE keeps that a seek on the existing
   filtered index instead of a lookup per machine. DROP_EXISTING = ON, never
   DROP-then-CREATE (migration_topups_2026-08-23.sql explains why). Filtered
   index => the SET options below are required for the CREATE itself. */
SET ARITHABORT ON; SET NUMERIC_ROUNDABORT OFF; SET ANSI_NULLS ON;
SET ANSI_PADDING ON; SET ANSI_WARNINGS ON; SET CONCAT_NULL_YIELDS_NULL ON;
SET QUOTED_IDENTIFIER ON;

IF EXISTS (SELECT 1 FROM sys.indexes
           WHERE name = 'IX_NETS_Txn_MachineTime'
             AND object_id = OBJECT_ID('dbo.NETS_Transaction'))
    CREATE NONCLUSTERED INDEX IX_NETS_Txn_MachineTime
        ON dbo.NETS_Transaction (Machine_Code, Txn_DateTime)
        INCLUDE (Txn_Status_Code, Account_Key)
        WHERE Machine_Code IS NOT NULL
        WITH (DROP_EXISTING = ON);
ELSE
    CREATE NONCLUSTERED INDEX IX_NETS_Txn_MachineTime
        ON dbo.NETS_Transaction (Machine_Code, Txn_DateTime)
        INCLUDE (Txn_Status_Code, Account_Key)
        WHERE Machine_Code IS NOT NULL;

SELECT i.name, c.name AS col, ic.is_included_column
FROM   sys.indexes i
JOIN   sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
JOIN   sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
WHERE  i.object_id = OBJECT_ID('dbo.NETS_Transaction') AND i.name = 'IX_NETS_Txn_MachineTime';
-- Expect Account_Key with is_included_column = 1.

/* Any view over NETS_Transaction written as SELECT * needs its metadata
   refreshed or it will not see the new column. Harmless if none is. */
DECLARE @v NVARCHAR(300);
DECLARE vc CURSOR LOCAL FAST_FORWARD FOR
    SELECT QUOTENAME(s.name) + '.' + QUOTENAME(v.name)
    FROM   sys.views v JOIN sys.schemas s ON s.schema_id = v.schema_id
    WHERE  v.name LIKE 'vw_NETS%';
OPEN vc; FETCH NEXT FROM vc INTO @v;
WHILE @@FETCH_STATUS = 0
BEGIN
    BEGIN TRY EXEC sp_refreshview @v; PRINT 'refreshed ' + @v; END TRY
    BEGIN CATCH PRINT 'could not refresh ' + @v + ': ' + ERROR_MESSAGE(); END CATCH
    FETCH NEXT FROM vc INTO @v;
END
CLOSE vc; DEALLOCATE vc;


/* ═══ BLOCK 3 — MachineLookup rows for franchisee machines ═════════════════
   DO NOT RUN AS-IS. The VALUES list below is EMPTY on purpose: the machine
   codes and names come from `python auresys_pull.py --roster` (or the
   workflow with roster=true), which prints one line per franchisee terminal
   in exactly this shape, using the outlet name the Auresys portal shows.
   Paste them in, read every name, THEN run.

   MachineCode is synthetic — 9 + the terminal number zero-padded to 8 digits
   (SGKN_M0080 -> '900000080') — because these machines have no KNM telemetry
   id. If a franchisee machine DOES report telemetry, use its real
   [Machine Code] instead so the alpha dashboard heartbeat joins to it.

   Insert-only: a code already present is left untouched (same rule as
   app.seed_locations). UX_MachineLookup_MachineCode enforces uniqueness.
   Plain batch, no sp_executesql: nothing here references a column added
   earlier in this file, and the --roster stubs must paste without quote
   doubling. */
DECLARE @seed TABLE (MachineCode NVARCHAR(50), MachineName NVARCHAR(200));
INSERT INTO @seed (MachineCode, MachineName) VALUES
    -- paste --roster output here VERBATIM (plain single quotes), e.g.
    -- ('900000080', N'SOME OUTLET NAME'),   -- SGKN_M0080
    (NULL, NULL);
DELETE FROM @seed WHERE MachineCode IS NULL;
INSERT INTO dbo.MachineLookup (MachineCode, MachineName)
SELECT s.MachineCode, LEFT(s.MachineName, 100)     -- MachineName is NVARCHAR(100)
FROM   @seed s
WHERE  NOT EXISTS (SELECT 1 FROM dbo.MachineLookup m WHERE m.MachineCode = s.MachineCode);
SELECT @@ROWCOUNT AS inserted_rows;
SELECT MachineCode, MachineName, ISNULL(IsActive,1) AS IsActive
FROM   dbo.MachineLookup WHERE MachineCode LIKE '9%' AND LEN(MachineCode) = 9;


/* ═══ ROLLBACK (manual, only if the whole change is being backed out) ═══════
   Deploy the previous auresys_pull.py FIRST — the new one refuses to run
   without Account_Key, the old one never references it.
-- ALTER TABLE dbo.NETS_Transaction  DROP COLUMN Account_Key;   -- rebuild the
--   index without the INCLUDE first (DROP_EXISTING = ON) or this is refused
-- ALTER TABLE dbo.NETS_Unmapped_Row DROP COLUMN Account_Key;
-- DELETE FROM dbo.MachineLookup WHERE MachineCode LIKE '9%' AND LEN(MachineCode)=9
--   AND MachineCode IN (<the codes BLOCK 3 inserted>);
   ========================================================================== */
