/* ============================================================================
   KNM — Sales schedule (2026-08-11)
   ----------------------------------------------------------------------------
   OPTIONAL. init_workorders_db() adds these same three columns idempotently on
   every app start, so a normal deploy applies them by itself. Run this only if
   you want them in place before the deploy, or if a startup ALTER was swallowed
   (each one is caught independently, so the schema can end up partial).

   HOW TO RUN — the Azure portal Query Editor executes the WHOLE PANE as one
   batch and ignores GO. A column added in the same batch as an index that
   references it fails to compile ("Invalid column name 'SeriesID'") and takes
   the ALTERs down with it — the same trap that broke rev 1 of
   migration_AZURE_2026-07-01.sql.

       >>> HIGHLIGHT AND RUN **BLOCK 1** ON ITS OWN.
       >>> THEN HIGHLIGHT AND RUN **BLOCK 2**.
       >>> THEN RUN **BLOCK 3** TO VERIFY.

   Every statement is guarded, so re-running any block is harmless. Nothing is
   read, written or moved — the script only adds nullable columns and indexes.
   ============================================================================ */


/* ══ BLOCK 1 — columns ══════════════════════════════════════════════════════ */

/* Groups the materialised occurrences of one repeat, so "cancel the rest of
   the series" can find them. NULL on every one-off stop. */
IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
               WHERE TABLE_NAME = 'WO_DeliveryOrders' AND COLUMN_NAME = 'SeriesID')
    ALTER TABLE WO_DeliveryOrders ADD [SeriesID] NVARCHAR(40) NULL;

/* 'weekly' | 'fortnightly' | 'fourweekly' | 'none' — the exact set in
   SCHEDULE_STEP_DAYS (workorders.py). Every stride is a whole number of weeks,
   so a stop never drifts off the weekday sales picked; there is no 'monthly'.
   Display only — the occurrences are real rows, so nothing computes dates from
   this column. */
IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
               WHERE TABLE_NAME = 'WO_DeliveryOrders' AND COLUMN_NAME = 'SeriesRule')
    ALTER TABLE WO_DeliveryOrders ADD [SeriesRule] NVARCHAR(24) NULL;

/* Who asked for the stop, kept separate from CreatedBy so it survives if a
   dispatcher later edits the row. */
IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
               WHERE TABLE_NAME = 'WO_DeliveryOrders' AND COLUMN_NAME = 'RequestedBy')
    ALTER TABLE WO_DeliveryOrders ADD [RequestedBy] NVARCHAR(256) NULL;


/* ══ BLOCK 2 — indexes ══════════════════════════════════════════════════════ */
/* Built through sp_executesql so the column references compile at EXEC time,
   not when the batch is parsed. That keeps this block safe even if it is
   pasted together with BLOCK 1 by accident, and safe on a database where the
   ScheduledDate ALTER was swallowed at startup — it no-ops instead of erroring.
   ScheduledDate itself is added by init_workorders_db, not by this script. */

/* The calendar and the dispatch rail both filter open orders by ScheduledDate;
   without this they scan the whole table on every page load. */
IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
           WHERE TABLE_NAME = 'WO_DeliveryOrders' AND COLUMN_NAME = 'ScheduledDate')
   AND NOT EXISTS (SELECT 1 FROM sys.indexes
                   WHERE name = 'IX_WODO_ScheduledDate'
                     AND object_id = OBJECT_ID('WO_DeliveryOrders'))
    EXEC sp_executesql N'CREATE INDEX IX_WODO_ScheduledDate
                         ON WO_DeliveryOrders (ScheduledDate, Status)';

/* Filtered — only series rows are ever looked up by SeriesID, and most rows
   have SeriesID NULL, so filtering keeps this index off every ordinary
   delivery-order write.

   A filtered index does make SQL Server require the seven standard SET options
   on later writes to the table. That is already proven safe on this database
   from this client: UX_WOC_DisplayID (migration_2026-06-03.sql:255),
   UX_WOJO_DisplayID (:380) and UX_MLH_OpenInterval
   (migration_AZURE_2026-07-01.sql:40) are filtered indexes on tables the app
   INSERTs into through the same FreeTDS/pymssql connection every day. If the
   options were wrong, complaint and job-order creation would already be dead. */
IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
           WHERE TABLE_NAME = 'WO_DeliveryOrders' AND COLUMN_NAME = 'SeriesID')
   AND NOT EXISTS (SELECT 1 FROM sys.indexes
                   WHERE name = 'IX_WODO_Series'
                     AND object_id = OBJECT_ID('WO_DeliveryOrders'))
    EXEC sp_executesql N'CREATE INDEX IX_WODO_Series
                         ON WO_DeliveryOrders (SeriesID) WHERE SeriesID IS NOT NULL';


/* ══ BLOCK 3 — verify ═══════════════════════════════════════════════════════ */
/* Expect 7 column rows and 2 index rows.

   BLOCK 1 only creates three of the seven — SeriesID, SeriesRule, RequestedBy.
   The other four (ScheduledDate, RouteSeq, NeedsService, ServiceNote) come from
   init_workorders_db() at app start and CANNOT be produced by re-running
   BLOCK 1. So:
     * SeriesID / SeriesRule / RequestedBy missing  → re-run BLOCK 1.
     * any of the other four missing               → restart the app and check
       the startup log; the ALTER was swallowed there, not here.
   has_filter should read 1 for IX_WODO_Series and 0 for IX_WODO_ScheduledDate.
   Both index guards match on NAME ONLY, so an index of the same name with a
   different definition is accepted silently — if has_filter disagrees with the
   above, DROP that index by hand and re-run BLOCK 2 rather than assuming a
   re-run will correct it. */

SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_NAME = 'WO_DeliveryOrders'
  AND COLUMN_NAME IN ('ScheduledDate','RouteSeq','NeedsService','ServiceNote',
                      'SeriesID','SeriesRule','RequestedBy')
ORDER BY COLUMN_NAME;

SELECT name, has_filter
FROM sys.indexes
WHERE object_id = OBJECT_ID('WO_DeliveryOrders')
  AND name IN ('IX_WODO_ScheduledDate','IX_WODO_Series');
