# Cutover plan — V2 Fault Report + Tech Support

Date target: TBD.  Owner: Yash.  Status: drafted 2026-06-03.

## 0. Pre-flight (must be true before cutover)

- [x] DB migration `migration_2026-06-03.sql` run on `Machine DispensedDrink`. (Verified 2026-06-03 — 12 tables present.)
- [x] App registration `KNM-VendingDashboard-SP-Backend` created in `kopinearme.onmicrosoft.com`. Site permission granted on `AppDataBackEnd`.
- [ ] **App Service env vars set** in Azure Portal → App Service → Configuration → Application settings:
    - `MS_TENANT_ID = 2108839d-ebf1-4e39-bbd5-2d5b8f2b4f1c`
    - `MS_CLIENT_ID = 10427c4d-5b76-4c86-a96f-e8f2f0c73a32`
    - `MS_CLIENT_SECRET = <from password manager>`
    - `MS_SITE_ID = kopinearme.sharepoint.com,3e8a030f-d106-4995-a6f4-ec423d503026,f11ce5ec-98e2-4eec-b44a-a9b9e510eb83`
    - **Save** then **Continue** (restart prompt).
- [ ] **AAD app roles added** for the App Registration that fronts the Flask app (the one Easy Auth uses, *not* the SP-backend app):
    - `operator` — field staff who execute WOs.
    - `field_manager` — managers who triage / assign.
    - Existing `admin`, `dispatch`, `sales` stay as-is.
    - Assign 1 test user each role for smoke test.
- [ ] `requirements.txt` updated — append `msal>=1.28` and `requests>=2.32`.

## 1. Cutover commands (run from `/Users/yash008/Documents/Coding/Coding/KNM Apps/vending-dashboard`)

```bash
# Replace the live workorders.py with the V2 draft.
mv workorders.py            workorders.py.archived-2026-06-03
mv workorders.py.v2-draft   workorders.py

# Apply index.html patches (see INDEX_HTML_PATCH.md — 2 surgical edits).
# Do this by hand or with your editor.

# Append the new pip deps. Confirm no duplicates first.
cat requirements_additions.txt >> requirements.txt
sort -u -o requirements.txt requirements.txt

# Commit & push — Azure App Service auto-deploys on push to main.
git add workorders.py sharepoint_helper.py requirements.txt \
        templates/_fault_report_tab.html \
        templates/_tech_support_tab.html \
        templates/_kb_admin_tab.html \
        templates/index.html
git commit -m "V2 Fault Report + Tech Support: new tabs, SP image storage, KB"
git push origin main
```

Yash's convention from HANDSHAKE.md: push during off-hours (21:00 SGT).

## 2. Smoke tests (post-deploy, in order)

1. **App is up:** open the dashboard URL — old tabs (Sales, Heartbeat, Topups, etc.) still load.
2. **SP auth works:** SSH into App Service (or run locally with env vars set):
   ```
   python -c "from sharepoint_helper import _selftest; print(_selftest())"
   ```
   Expected: `OK drive=… uploaded=ComplaintUploads/2026/06/SELFTEST/selftest.txt size=16 deleted=yes`.
3. **Fault Report end-to-end:**
   - Sign in as a user with `admin` or any role.
   - Click **Fault Report** → **+ New Fault Report**.
   - Fill: machine, description, photo from phone camera. Submit.
   - Verify: row appears in list with `KNM-CMP-0001-2606`-style DisplayID. Click row → photo loads.
   - Verify in SP: open `https://kopinearme.sharepoint.com/sites/AppDataBackEnd/Documents/ComplaintUploads/2026/06/KNM-CMP-0001-2606/` — JPEG is there.
4. **Tech Support end-to-end:**
   - Sign in as a user with `field_manager` or `admin`.
   - Go to Fault Report → open the just-submitted complaint? *Cancel — feature deferred. Manager creates a WO via direct API for now, or wait for v3.* **Manual SQL** to create one test WO linked to the test complaint while WO-create UI is still pending.
   - Sign in as a user with `operator` role, mapped to that WO. Tap a tickbox → marks done. Tap "Can't complete" on a step → enter note → step shows blocked banner. Tap "Needs assistance" on WO → status flips to red badge.
5. **KB Admin:**
   - As `field_manager`, open **Manage KB** → add a test entry with event_code `800001`, 3 tickboxes. Save.
   - Reload. Edit the entry. Add a 4th tickbox. Save. Verify in DB: `SELECT * FROM WO_KB_Entries`, `SELECT * FROM WO_KB_Tickboxes`.
6. **No regressions on existing tabs:** Sales, Heartbeat, Topups, Locations, Dispatch — open each, verify normal.

## 3. Known gaps after cutover (not blocking)

- **Delivery Orders tab UI**: not yet split out; backend routes are ported but no new template. Existing flow worked via the old `_workorders_tab.html` which is no longer included after Patch 2. If delivery operators need access today, revert Patch 2 temporarily or hand-edit index.html to include both.
- **Heartbeat → fault auto-create**: deferred per 2026-06-03 directive.
- **Image backfill**: V1 had 0 `WorkOrderImages` rows; no migration needed. Future legacy ImageData blobs (if any) read fine via the fallback path in `/api/wo/images/<id>`.

## 3a. Future work flagged 2026-06-03

- **Event-code schema**: stays as bare `6xxxxx` / `8xxxxx` ranges for now. Detailed taxonomy and parser logic is a separate task — see `ERROR_CODE_TAXONOMY.md`. Revisit after ~30 days of V2 production data.
- **Topup tracker pivot**: the current `MachineLookup.LastTopupTimestamp` + Dispatch tab will migrate to a **delivery-WO-driven** model with per-machine consumption-pattern predictions. Existing Dispatch flow stays untouched until that redesign lands.

## 4. Rollback

If the deploy goes bad:

```bash
# Restore the previous workorders.py.
cd "/Users/yash008/Documents/Coding/Coding/KNM Apps/vending-dashboard"
mv workorders.py            workorders.py.failed-v2
mv workorders.py.archived-2026-06-03  workorders.py

# Revert the 2 index.html patches (manual).

git commit -am "Revert V2 cutover"
git push origin main
```

The V2 schema changes (new columns, new tables) stay in place — they're additive and don't break V1 code. The V1 `Status`/`Priority` text columns also stayed. So V1 code resumes reading from text columns; V2 TINYINT columns sit unused until next attempt.

## 5. Cleanup (after 2 weeks of stable operation)

- Drop the legacy text columns: `WO_Complaints.Status`, `WO_JobOrders.Status`, `WO_JobOrders.Priority`. Migration script `migration_2026-06-XX_text_status_dropt.sql` to be drafted at that time.
- Remove the legacy V1-table drop block from `init_workorders_db()` in `workorders.py`.
- Drop the renamed `*_legacy_2026_06_03` tables (after confirming nothing references them).
- Remove `workorders.py.bak-2026-06-03`, `*.bak-2026-06-03` template backups, and `migration_2026-06-03.sql.v1-upgrade-only`.
- Tighten SharePoint app permission from `Sites.ReadWrite.All` (if used) to `Sites.Selected` only. (Already done in this build — Sites.Selected is what's granted.)
