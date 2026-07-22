# SharePoint integration — setup checklist

Status as of **2026-06-03**: App registration done, secret saved, Sites.Selected granted to AppDataBackEnd site. Below records what's been done and what's still pending before cutover.

## Captured values

These go into **Azure App Service → Configuration → Application settings** (NOT into `config.py`):

| Env var | Value |
|---|---|
| `MS_TENANT_ID` | `2108839d-ebf1-4e39-bbd5-2d5b8f2b4f1c` |
| `MS_CLIENT_ID` | `10427c4d-5b76-4c86-a96f-e8f2f0c73a32` |
| `MS_CLIENT_SECRET` | *Yash's password manager — never paste in chat/code* |
| `MS_SITE_ID` | `kopinearme.sharepoint.com,3e8a030f-d106-4995-a6f4-ec423d503026,f11ce5ec-98e2-4eec-b44a-a9b9e510eb83` |

## Permissions

- App registration: `KNM-VendingDashboard-SP-Backend`
- Microsoft Graph application permissions granted: **Sites.Selected**, **Files.ReadWrite.All** (admin-consented).
- Per-site `write` role granted on the AppDataBackEnd site via Graph `POST /sites/{site-id}/permissions` (done via Graph Explorer with temporary `Sites.FullControl.All` delegated, immediately revoked).

## Folder layout (auto-created on first upload)

```
Documents/
  ComplaintUploads/
    {YYYY}/{MM}/KNM-CMP-NNNN-YYMM/
      before-1.jpg
      …
  WorkOrderUploads/
    {YYYY}/{MM}/KNM-WkO-NNNN-YYMM/
      task-123-task_done.jpg
      task-123-task_blocked.jpg
      …
```

## Smoke test (post-deploy)

In an App Service SSH or local virtualenv with the env vars set, run:

```python
python -c "from sharepoint_helper import _selftest; print(_selftest())"
```

Expected output: `OK drive=… uploaded=ComplaintUploads/2026/06/SELFTEST/selftest.txt size=16 deleted=yes`

If you see:
- `MSAL token acquire failed` → check tenant/client IDs and that admin consent was granted.
- `403 Forbidden` on upload → site-permission grant didn't apply or wrong site. Re-run the Graph POST with the correct app client ID.
- `No document library found` → `MS_SITE_ID` is wrong, or the site has no default Documents library.

## Pending

- Set the 4 env vars in App Service Configuration.
- Bump `requirements.txt` to include `msal` and `requests`. See `requirements_additions.txt`.
- Run the smoke test once before pointing prod UI at the new endpoints.
- Calendar a secret renewal reminder for **2028-06** (24-month secret expiry).
