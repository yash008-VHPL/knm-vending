-- 2026-08-28  Quarantine list for rows the pull cannot classify.
--
-- Additive only: creates one table, touches nothing that exists. Safe to run
-- twice. Rollback at the bottom.
--
-- Why: before this, one row with a status string absent from STATUS_MAP raised
-- ABORTED_PARSE inside parse_rows, which runs BEFORE load(). A single such row
-- on 2026-08-26 stopped every day of the rolling 10-day window from loading on
-- every run for two days. The classification guard was right; aborting the
-- whole window over one row was not. Unclassifiable rows now land here and the
-- rest of the pull proceeds.
--
-- Each run clears and rewrites only the dates it queried, so rows for the
-- current rolling window are always a live picture: a day stops appearing here
-- as soon as the status is mapped and the day reloads clean. Rows for a day
-- that has aged OUT of the window are never revisited and so are never removed
-- - deliberately, since that day is no longer fetched and this is the only
-- remaining evidence something was set aside. Clear an old range by re-running
-- the pull over it with --from/--to.
--
-- No card data is stored. These rows are NOT in NETS_Transaction and are
-- invisible to every dispense, amount and flag-card query by construction.

IF OBJECT_ID('dbo.NETS_Unmapped_Row', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.NETS_Unmapped_Row (
        Unmapped_Id       BIGINT IDENTITY(1,1) NOT NULL,
        Run_Seq           INT            NOT NULL,
        NETS_Terminal_No  NVARCHAR(50)   NOT NULL,
        Machine_Code      NVARCHAR(50)   NULL,
        Location_Name     NVARCHAR(200)  NULL,
        Txn_Date          DATE           NOT NULL,
        -- NULL whenever the timestamp was never parsed: an unparseable time,
        -- and every UNMAPPED_STATUS row (which is set aside before the parse).
        -- Raw_Time always keeps what the API actually sent.
        Txn_DateTime      DATETIME2(0)   NULL,
        Raw_Time          NVARCHAR(64)   NULL,
        Raw_Status        NVARCHAR(128)  NOT NULL,
        Raw_Payment_Type  NVARCHAR(64)   NULL,
        Raw_Amount        NVARCHAR(64)   NULL,
        Amount            DECIMAL(12,2)  NULL,
        -- UNMAPPED_STATUS | BAD_TIME | BAD_AMOUNT
        Reason            VARCHAR(32)    NOT NULL,
        Logged_At_UTC     DATETIME2(0)   NOT NULL
                          CONSTRAINT DF_NETS_Unmapped_Row_Logged
                          DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT PK_NETS_Unmapped_Row PRIMARY KEY CLUSTERED (Unmapped_Id)
    );

    -- The loader deletes by date before re-inserting; the dashboard reads
    -- newest-first. Both are covered by this one index.
    CREATE INDEX IX_NETS_Unmapped_Row_Date
        ON dbo.NETS_Unmapped_Row (Txn_Date DESC, NETS_Terminal_No);

    PRINT 'created dbo.NETS_Unmapped_Row';
END
ELSE
    PRINT 'dbo.NETS_Unmapped_Row already exists - nothing to do';

-- ROLLBACK (manual - run only if you need to revert)
-- DROP TABLE dbo.NETS_Unmapped_Row;
