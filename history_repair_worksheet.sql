-- ============================================================================
-- KNM — MachineLocationHistory repair worksheet          (2026-07-20, Claude)
-- Run in Azure Query Editor AFTER confirming each classification.
-- Background: until today's fix, the admin Locations-tab edit recorded EVERY
-- name change as a cutover at the edit timestamp. For a RENAME (same site,
-- label corrected) that wrongly splits the site's history in two names. For a
-- real MOVE the cut exists but is stamped at the edit time, not the true move
-- time. Reviewed by secondary agent 2026-07-20 (findings applied: explicit
-- transactions, coord carry-over, CONVERT style 120).
--
-- RENAME fix = relabel + carry coords onto the old row, delete the new open
--              row, re-open the old row (delete-before-reopen respects the
--              one-open-interval filtered unique index; wrapped in a TRAN).
-- MOVE fix   = set both sides of the cut to the TRUE move time T:
--              DECLARE @T FLOAT = CAST(CONVERT(datetime,'YYYY-MM-DD HH:MM',120) AS FLOAT) + 2.0;
--              (style 120 = ISO, immune to DATEFORMAT; +2.0 converts SQL
--              datetime-float epoch 1900-01-01 to OLE epoch 1899-12-30)
--
-- My classification guesses are marked [GUESS]. Confirm before running.
-- ============================================================================

-- ── 1) Likely RENAMES (same site, label corrected) ──────────────────────────

-- 50420523  Maybank -> AMK Maybank   [GUESS: RENAME]
BEGIN TRAN;
UPDATE o SET o.LocationName = 'AMK Maybank',
             o.Latitude  = COALESCE(n.Latitude,  o.Latitude),
             o.Longitude = COALESCE(n.Longitude, o.Longitude)
FROM MachineLocationHistory o
JOIN MachineLocationHistory n ON n.HistoryID = 132
WHERE o.HistoryID = 40;
DELETE FROM MachineLocationHistory WHERE HistoryID = 132;
UPDATE MachineLocationHistory SET ValidToOle = NULL WHERE HistoryID = 40;
COMMIT;

-- 51421144  ST Marine Gul L2 -> ST Marine 55 Gul Rd L2   [GUESS: RENAME]
BEGIN TRAN;
UPDATE o SET o.LocationName = 'ST Marine 55 Gul Rd L2',
             o.Latitude  = COALESCE(n.Latitude,  o.Latitude),
             o.Longitude = COALESCE(n.Longitude, o.Longitude)
FROM MachineLocationHistory o
JOIN MachineLocationHistory n ON n.HistoryID = 135
WHERE o.HistoryID = 111;
DELETE FROM MachineLocationHistory WHERE HistoryID = 135;
UPDATE MachineLocationHistory SET ValidToOle = NULL WHERE HistoryID = 111;
COMMIT;

-- 54120168  ST Marine Gul L5 -> ST Marine 55 Gul Rd L5   [GUESS: RENAME]
BEGIN TRAN;
UPDATE o SET o.LocationName = 'ST Marine 55 Gul Rd L5',
             o.Latitude  = COALESCE(n.Latitude,  o.Latitude),
             o.Longitude = COALESCE(n.Longitude, o.Longitude)
FROM MachineLocationHistory o
JOIN MachineLocationHistory n ON n.HistoryID = 136
WHERE o.HistoryID = 71;
DELETE FROM MachineLocationHistory WHERE HistoryID = 136;
UPDATE MachineLocationHistory SET ValidToOle = NULL WHERE HistoryID = 71;
COMMIT;

-- 52821398  Changi Lv 1 -> CGH L1 EMERGENCY   [GUESS: RENAME]
BEGIN TRAN;
UPDATE o SET o.LocationName = 'CGH L1 EMERGENCY',
             o.Latitude  = COALESCE(n.Latitude,  o.Latitude),
             o.Longitude = COALESCE(n.Longitude, o.Longitude)
FROM MachineLocationHistory o
JOIN MachineLocationHistory n ON n.HistoryID = 133
WHERE o.HistoryID = 57;
DELETE FROM MachineLocationHistory WHERE HistoryID = 133;
UPDATE MachineLocationHistory SET ValidToOle = NULL WHERE HistoryID = 57;
COMMIT;

-- 51421682  St Joseph (cafe) -> St Joseph (Funhouse)   [GUESS: RENAME]
BEGIN TRAN;
UPDATE o SET o.LocationName = 'St Joseph (Funhouse)',
             o.Latitude  = COALESCE(n.Latitude,  o.Latitude),
             o.Longitude = COALESCE(n.Longitude, o.Longitude)
FROM MachineLocationHistory o
JOIN MachineLocationHistory n ON n.HistoryID = 134
WHERE o.HistoryID = 28;
DELETE FROM MachineLocationHistory WHERE HistoryID = 134;
UPDATE MachineLocationHistory SET ValidToOle = NULL WHERE HistoryID = 28;
COMMIT;

-- ── 2) Likely REAL MOVES — fill in the TRUE move time T, then run ───────────
-- Template (per machine):
--   BEGIN TRAN;
--   DECLARE @T FLOAT = CAST(CONVERT(datetime,'2026-07-16 09:00',120) AS FLOAT) + 2.0;
--   UPDATE MachineLocationHistory SET ValidToOle   = @T WHERE HistoryID = <old-row>;
--   UPDATE MachineLocationHistory SET ValidFromOle = @T WHERE HistoryID = <new-row>;
--   COMMIT;
-- (If the edit-time cut happens to BE the real move time, no action needed.)

-- 42920759  IMH Annex -> IMH MPSH                 [GUESS: MOVE within campus] old=1,   new=128, cut was 2026-07-16 13:05
-- 45021777  Mediacorp -> Harboufront Cruise Centre Depature  [MOVE]           old=105, new=141, cut was 2026-07-17 09:31
-- 51421685  St. Marine Benoi -> Givaudan Pioneer Rd          [MOVE]           old=43,  new=131, cut was 2026-07-16 13:22
-- 51421701  Hundred Grains VivoCity -> ST Marine Benoi Yard L2 [MOVE]         old=79,  new=137, cut was 2026-07-16 13:58
-- 52821394  Fei Siong Group -> ST Marine benoi L2            [MOVE]           old=81,  new=130, cut was 2026-07-16 13:21
-- 52920229  Kaki Bukit Camp -> NAB                           [MOVE?]          old=33,  new=125, cut was 2026-07-09 18:05
-- 54120150  Coffee Times 2 -> Salad Crunch          [MOVE? operator swap?]    old=110, new=126, cut was 2026-07-16 10:17
-- 54120154  Coffee Times 1 -> Sodexo                [MOVE? operator swap?]    old=80,  new=124, cut was 2026-07-09 18:05
-- 54120166  Shimizu Office -> St Andrews Autism Centre       [MOVE]           old=113, new=127, cut was 2026-07-16 10:19

-- ── 3) New installs registered late — extend the stay back to first vend ────
-- These machines have vends BEFORE their (admin-created) interval starts, so
-- early vends currently resolve only via the current-name fallback.
-- Each has exactly ONE history row (verified) — no overlap possible.

-- 54020501  Beam Suntory   (vends from 2026-07-10, interval from 2026-07-16)
UPDATE MachineLocationHistory SET ValidFromOle = 0 WHERE HistoryID = 138;
-- 54020497  Frontier CC1   (vends from 2026-07-13, interval from 2026-07-16)
UPDATE MachineLocationHistory SET ValidFromOle = 0 WHERE HistoryID = 139;
-- 54020504  Frontier CC2   (vends from 2026-07-13, interval from 2026-07-16)
UPDATE MachineLocationHistory SET ValidFromOle = 0 WHERE HistoryID = 140;

-- 55120043  Harbourfront Cruise Centre (vends from 2026-05-21, interval from 2026-07-07)
--   [CONFIRM it was at Harbourfront since May before running:]
-- UPDATE MachineLocationHistory SET ValidFromOle = 0 WHERE HistoryID = 123;

-- 51421703  IHH Level 5 (vends since 2025-09-17, interval only from 2026-07-16)
--   [NEEDS YOUR INPUT — where was it before Jul 16? Seed data suggests
--    'Anguillia Mosque' / 'GnC 1 Raffles Place'. Use the Machine History
--    card's "Record a past move", or tell me and I'll draft the rows.]

-- ── 4) Machines vending but ABSENT from MachineLookup (need names from you) ─
-- 44520636, 45021771, 52920251, 53920772, 54120165, 55120034, 55120531
