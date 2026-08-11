# KNM Vending — Session Handoff (2026-07-29)

Drag this into a new session to restore full context. Supersedes SESSION_HANDOFF_2026-07.md.

## Infra / access
- Flask + Azure SQL app: `Coding/KNM Apps/vending-dashboard`. Repo `github.com/yash008-VHPL/knm-vending`,
  deploy = `git push origin main` → Azure Web App KNMDispenseViewer auto-builds (~2-3 min).
- DB "Machine DispensedDrink" @ machineserver.database.windows.net (creds in config.py).
- **DB access from Claude: via GX-10** (`mcp__remote-devices__gx10-server__run_script`, python3+pymssql
  installed, scripts in `~/knm-diag/`). Sandbox/device-VM cannot reach the DB. GX-10 goes down when
  Tailscale drops — retry after RefreshMcpTools; wake the box if unreachable from the Mac too.
- **NEVER run git through the device bridge** — every git command leaves an undeletable `.git/index.lock`
  (bridge can't unlink; `mv` into `_to_delete/` clears it). Git + push = Yash only.
- Working rules: terse; ≤3 steps; ALWAYS fire a second verification agent before hard changes
  (DB writes, deploys); confirm before assuming; preflight then execute; idempotency guards on DB scripts.

## Root causes found & fixed this session (all deployed unless noted)
1. **Duplicate vend rows**: gateway replays write the same physical vend 2+× at the EXACT same OLE
   instant — sometimes under a DIFFERENT event code (hot/iced sibling codes). Fleet-wide, 100+ machines.
   Fix: dedup rule = one row per (machine, timestamp); ORDER BY [Event Code] keeps lowest (deterministic;
   per-SKU split ±0.5% ambiguous on dupe instants, totals exact). Applied to transactions/dispenses
   (inline dedup w/ date filter inside window), machine-list counts (single-scan GROUP BY join),
   single-machine counts (COUNT DISTINCT instant), nets_reconcile, alpha. Ingestion side NOT yet fixed
   (Azure Logic App parses machine emails, appends without dedup key, maps codes from drink-name text —
   need Logic App export or sample email to fix at source).
2. **seed_locations() clobber**: ran on every startup and UPDATE-reverted MachineLookup names/coords to a
   stale hardcoded list — the cause of "moved machines still show old location". Now INSERT-only.
   One-time repair applied 2026-07-28: 10 lookup names reconciled from open history intervals.
3. **Rename vs Move semantics**: admin location edit now REQUIRES name_change_mode: 'rename' (relabel open
   interval in place) or 'move' (cutover at user-supplied SGT time, validated). UI prompts. Movement tab
   and "Record a past move" unchanged. Location resolution: interval ValidFromOle <= vend < ValidToOle,
   fallback COALESCE(loc, ml.MachineName, raw code); filters match the COALESCE.
4. **Messages tab**: MasterCode join deduped (grouped) + replayed-event dedup (machine+instant+code;
   distinct codes at one instant deliberately kept — can be real simultaneous faults).
5. **Alpha app (/alpha) fully wired** to production /api/wo/* endpoints: report→complaint, assign
   (joborder/delivery; complaint→creates joborder), complete (job→pending_review, delivery→sign+topup sync,
   movement→location cutover), auto-plan, technicians from Graph, Add machine, per-machine
   vends-since-refill in bootstrap (3.6s native single-scan), 7d sales window fixed to SGT.
6. **DB additions**: index IX_MasterData_Machine_Time on [MasterData Table]([Machine Code],[Date Time])
   INCLUDE([Event Code]); reference view dbo.VendEvents (canonical dedup); both idempotent in init_db().
7. **NETS_TO_DB updated**: AMK MAYBANK CENTRE→'AMK Maybank'; KAKI BUKIT CAMP→None; ONE NORTH MEDIACORP→None.

## DB data changes applied (all second-agent verified, Source tags mark them)
- Warehouse convention: pre-deployment test vends attribute to closed interval "18 Kim Chuan (Warehouse)".
- 54120165 registered ST Marine Gul Yard (warehouse leg → cut 7/16 12:00).
- 54020502 registered UPS House (warehouse leg → cut 7/20 12:00).
- 52920251 registered UPS Alps (warehouse leg → cut 7/20 12:00).
- 54020501 = Beam Suntory (Monday's UPS Alps relabel reversed; full history from 0). **Silent since
  7/21 21:01 — machine-side failure (fleet ingest healthy), needs site visit.**
- 45021777 Mediacorp → Harbourfront Cruise Centre Departure @ 7/17 09:31 (spelling fixed).
- 54020497 Frontier CC1 → Mount E Royal Square @ 7/21 12:00 (44h gap verified).
- 51421700 GnC Marina One → Meta (Facebook) APAC 2 @ 6/24 (118d silence verified).
- 52920229→NAB, 54120150→Salad Crunch etc.: cuts exist from July admin edits at EDIT time, true move
  times pending worksheet (below).
- UPS House (54020502) dark 7/22-7/26 (zero events, data-side, self-recovered 7/27) — check NETS portal
  for offline sales those days.

## OPEN ITEMS (blocking on Yash/team)
1. 54020504 Frontier CC2: redeployed, destination unknown → then close Frontier @ cut + open new site.
2. `history_repair_worksheet.sql` (in repo): confirm RENAME vs MOVE for the July-16 batch + true move
   dates; run in Azure Query Editor. Supersessions already applied: HistoryID 138/139/140 backfills done.
3. Gul Road L2/L5: sheet says 54120168=L2, 51421144=L5; DB says the reverse. Physical check.
4. 51421685: history says Givaudan Pioneer Rd; tabletop sheet says ST Marine Benoi L2. Physical check.
5. 51421702 Grains & Co Geneos → Jamiyah Halfway House: real mismatch, move date unknown (~Jun 22-27
   inferred from weekend-vend pattern). Need date, then record.
6. Orphan machine codes vending but unregistered: 44520636, 45021771, 53920772, 55120034 (need names).
7. CGH 1 & 2 (51421683, 52920226): registered but ZERO events ever — gateway/SIM never provisioned.
8. Silent machines: 54020501 (7/21), 51421682 St Joseph Funhouse (7/6), 52821395 Dawn Shipping (7/10),
   52821397 E2i (6/16).
9. Ingestion root fix: get Logic App definition export or one sample machine email.
10. Known minor debt: stored-XSS pattern (complaint desc rendered unescaped in alpha), esc() apostrophe
    breaks Edit onclick in alpha for names with ', connection not closed on some app.py error paths.

## State markers
- Repo HEAD at handoff: 3027b5d (clean, pushed, deployed). Canonical file hashes verified disk==session.
- NETS reconciliation runs Aug 2 (GitHub Actions) — July numbers should be clean if item 1-2 land first.
- Beware: a `git restore`-type action on 7/29 silently reverted workorders.py + alpha_preview.html;
  restored from canonical. If files look stale, hash-compare before trusting.
