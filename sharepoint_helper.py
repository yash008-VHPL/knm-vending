"""
SharePoint helper — Microsoft Graph API client for KNM Fault Report / Tech Support.

Stores fault-report and work-order attachments in the KNM SharePoint site:
  Site : https://kopinearme.sharepoint.com/sites/AppDataBackEnd
  Lib  : Documents
  Folders:
    ComplaintUploads/{YYYY}/{MM}/{DisplayID}/
    WorkOrderUploads/{YYYY}/{MM}/{DisplayID}/

Auth: App-only via MSAL ConfidentialClientApplication (client credentials).
Permission scope: Sites.Selected with site-specific 'write' role.

This module is import-only. Nothing executes at import time.
"""
from __future__ import annotations

import os
import time
import threading
import urllib.parse
from typing import Optional, Dict, Tuple

import requests
import msal

import config


# ── Module constants ──────────────────────────────────────────────────────────

GRAPH_BASE      = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE     = ["https://graph.microsoft.com/.default"]
TOKEN_EXPIRY_PAD = 60  # refresh 60s before actual expiry
USER_CACHE_TTL  = 600  # 10 min cache for role-member lookups

# Folder name → root under the default "Documents" drive.
FOLDER_COMPLAINT = "ComplaintUploads"
FOLDER_WORKORDER = "WorkOrderUploads"

# Module-level cache. Reset on import; populated on first call.
_token_cache: Dict[str, float | str | None] = {"value": None, "expires_at": 0.0}
_drive_cache: Dict[str, str | None]         = {"id": None}
_role_user_cache: Dict[str, Dict]           = {}   # role_id → {"data": [...], "expires_at": float}
_token_lock  = threading.Lock()


# ── Config accessors ──────────────────────────────────────────────────────────

def _cfg(name: str) -> str:
    """Read MS_* config — env var takes precedence over config.py attribute."""
    v = os.environ.get(name) or getattr(config, name, "")
    if not v:
        raise RuntimeError(
            f"{name} not set. Required for SharePoint auth. "
            f"Set in App Service environment or config.py."
        )
    return str(v).strip()


# ── Token management ──────────────────────────────────────────────────────────

def _acquire_token() -> str:
    """Acquire (or refresh) a Graph access token using client-credentials."""
    with _token_lock:
        now = time.time()
        cached = _token_cache.get("value")
        expires = float(_token_cache.get("expires_at") or 0)
        if cached and expires - TOKEN_EXPIRY_PAD > now:
            return str(cached)

        app = msal.ConfidentialClientApplication(
            client_id   = _cfg("MS_CLIENT_ID"),
            client_credential = _cfg("MS_CLIENT_SECRET"),
            authority   = f"https://login.microsoftonline.com/{_cfg('MS_TENANT_ID')}",
        )
        result = app.acquire_token_for_client(scopes=GRAPH_SCOPE)
        if "access_token" not in result:
            err = result.get("error_description") or result.get("error") or "unknown"
            raise RuntimeError(f"MSAL token acquire failed: {err}")

        _token_cache["value"] = result["access_token"]
        _token_cache["expires_at"] = now + int(result.get("expires_in", 3600))
        return str(result["access_token"])


def _auth_header() -> Dict[str, str]:
    return {"Authorization": f"Bearer {_acquire_token()}"}


# ── Drive (default Documents library) ─────────────────────────────────────────

def _drive_id() -> str:
    """Resolve and cache the default document library drive ID for the site."""
    if _drive_cache.get("id"):
        return str(_drive_cache["id"])

    site_id = _cfg("MS_SITE_ID")
    url = f"{GRAPH_BASE}/sites/{site_id}/drives"
    resp = requests.get(url, headers=_auth_header(), timeout=20)
    resp.raise_for_status()
    drives = resp.json().get("value", [])
    # Default library is named "Documents" (template "documentLibrary").
    for d in drives:
        if (d.get("name") or "").lower() == "documents":
            _drive_cache["id"] = d["id"]
            return str(d["id"])
    # Fallback: first documentLibrary
    for d in drives:
        if d.get("driveType") == "documentLibrary":
            _drive_cache["id"] = d["id"]
            return str(d["id"])
    raise RuntimeError("No document library found on the SharePoint site.")


# ── Path helpers ──────────────────────────────────────────────────────────────

def _folder_path(kind: str, year: int, month: int, display_id: str) -> str:
    """
    kind: 'complaint' or 'workorder'
    Returns server-relative path under the drive root, e.g.
        ComplaintUploads/2026/06/KNM-CMP-0001-2606
    """
    root = FOLDER_COMPLAINT if kind == "complaint" else FOLDER_WORKORDER
    if kind not in ("complaint", "workorder"):
        raise ValueError(f"Unknown kind: {kind!r}")
    safe_display = "".join(c for c in display_id if c.isalnum() or c in "-_")
    return f"{root}/{year:04d}/{month:02d}/{safe_display}"


def _encode_path(path: str) -> str:
    """URL-encode each path segment but keep / separators."""
    return "/".join(urllib.parse.quote(seg, safe="") for seg in path.split("/"))


# ── Public API ────────────────────────────────────────────────────────────────

def upload_bytes(
    kind: str,
    display_id: str,
    year: int,
    month: int,
    file_name: str,
    data: bytes,
    content_type: str = "application/octet-stream",
) -> Tuple[str, str, str]:
    """
    Upload raw bytes to SP. Auto-creates parent folders.

    Returns: (sp_item_id, web_url, server_relative_path)

    For files <= 4 MB uses simple PUT. For larger files uses upload session.
    """
    if not isinstance(data, (bytes, bytearray)) or len(data) == 0:
        raise ValueError("Empty file data.")
    if not file_name:
        raise ValueError("file_name required.")

    folder = _folder_path(kind, year, month, display_id)
    # Sanitize filename: keep only safe characters
    safe_name = "".join(c if (c.isalnum() or c in "._- ") else "_" for c in file_name).strip()
    if not safe_name:
        safe_name = "upload.bin"
    full_path = f"{folder}/{safe_name}"

    drive_id = _drive_id()
    url_path = _encode_path(full_path)

    if len(data) <= 4 * 1024 * 1024:
        # Simple upload (auto-creates parent folders via :/path:/ syntax)
        url = f"{GRAPH_BASE}/drives/{drive_id}/root:/{url_path}:/content"
        headers = _auth_header()
        headers["Content-Type"] = content_type
        r = requests.put(url, data=data, headers=headers, timeout=60)
        r.raise_for_status()
        item = r.json()
        return item["id"], item.get("webUrl", ""), full_path

    # Large file: create upload session
    sess_url = f"{GRAPH_BASE}/drives/{drive_id}/root:/{url_path}:/createUploadSession"
    sess_body = {
        "item": {
            "@microsoft.graph.conflictBehavior": "replace",
            "name": safe_name,
        }
    }
    r = requests.post(sess_url, json=sess_body, headers=_auth_header(), timeout=30)
    r.raise_for_status()
    upload_url = r.json()["uploadUrl"]

    chunk = 5 * 1024 * 1024  # 5 MB chunks
    total = len(data)
    offset = 0
    item = None
    while offset < total:
        end = min(offset + chunk, total) - 1
        body = data[offset:end + 1]
        headers = {
            "Content-Length": str(len(body)),
            "Content-Range": f"bytes {offset}-{end}/{total}",
        }
        r = requests.put(upload_url, data=body, headers=headers, timeout=120)
        if r.status_code in (200, 201):
            item = r.json()
            break
        if r.status_code == 202:
            offset = end + 1
            continue
        r.raise_for_status()
    if item is None:
        raise RuntimeError("Large upload session did not return final item.")
    return item["id"], item.get("webUrl", ""), full_path


def get_item_metadata(sp_item_id: str) -> Dict:
    """Return SP driveItem metadata (size, hashes, webUrl, name, etc.)."""
    drive_id = _drive_id()
    url = f"{GRAPH_BASE}/drives/{drive_id}/items/{sp_item_id}"
    r = requests.get(url, headers=_auth_header(), timeout=20)
    r.raise_for_status()
    return r.json()


def download_bytes(sp_item_id: str) -> Tuple[bytes, str]:
    """
    Download the file's raw bytes.
    Returns (bytes, content_type).

    Used by the Flask /api/wo/images/<id> proxy so Easy Auth fronts every read.
    """
    drive_id = _drive_id()
    # /content returns the file body (302 redirect to a download URL — let requests follow).
    url = f"{GRAPH_BASE}/drives/{drive_id}/items/{sp_item_id}/content"
    r = requests.get(url, headers=_auth_header(), timeout=60, allow_redirects=True)
    r.raise_for_status()
    ct = r.headers.get("Content-Type", "application/octet-stream")
    return r.content, ct


def delete_item(sp_item_id: str) -> None:
    """Hard-delete a file from SP."""
    drive_id = _drive_id()
    url = f"{GRAPH_BASE}/drives/{drive_id}/items/{sp_item_id}"
    r = requests.delete(url, headers=_auth_header(), timeout=20)
    if r.status_code not in (204, 404):
        r.raise_for_status()


# ── Directory: list users with a given app role ──────────────────────────────

def list_users_by_role(sp_object_id: str, app_role_id: str) -> list:
    """Return active users assigned the given app role on the given service principal.
    Each element: {email, display_name, principal_id}.

    Requires Directory.Read.All on the calling app. Results cached in-process
    for USER_CACHE_TTL seconds keyed by (sp_object_id, app_role_id).
    """
    cache_key = f"{sp_object_id}:{app_role_id}"
    now = time.time()
    cached = _role_user_cache.get(cache_key)
    if cached and cached.get("expires_at", 0) > now:
        return list(cached["data"])

    out = []
    url = f"{GRAPH_BASE}/servicePrincipals/{sp_object_id}/appRoleAssignedTo"
    while url:
        r = requests.get(url, headers=_auth_header(), timeout=20)
        r.raise_for_status()
        page = r.json()
        for a in page.get("value", []):
            if a.get("appRoleId") != app_role_id:
                continue
            if (a.get("principalType") or "") != "User":
                continue  # skip group assignments for now
            pid = a.get("principalId")
            display = a.get("principalDisplayName") or ""
            # Look up email
            email = ""
            try:
                ur = requests.get(
                    f"{GRAPH_BASE}/users/{pid}?$select=mail,userPrincipalName,displayName,accountEnabled",
                    headers=_auth_header(), timeout=15,
                )
                if ur.status_code == 200:
                    uj = ur.json()
                    if uj.get("accountEnabled") is False:
                        continue
                    email = (uj.get("mail") or uj.get("userPrincipalName") or "").strip()
                    if uj.get("displayName"):
                        display = uj["displayName"]
            except Exception:
                pass
            if email:
                out.append({
                    "email": email.lower(),
                    "display_name": display or email,
                    "principal_id": pid,
                })
        url = page.get("@odata.nextLink")

    out.sort(key=lambda u: (u.get("display_name") or "").lower())
    _role_user_cache[cache_key] = {"data": out, "expires_at": now + USER_CACHE_TTL}
    return list(out)


def clear_user_cache():
    """Force the next list_users_by_role call to re-fetch from Graph."""
    _role_user_cache.clear()


# ── Smoke test (call manually; do NOT run at import) ──────────────────────────

def _selftest() -> str:
    """
    Run a round-trip test: token → drive list → tiny upload → metadata → delete.
    Returns a one-line summary. Raises on failure.

    Usage:
        python -c "from sharepoint_helper import _selftest; print(_selftest())"
    """
    _ = _acquire_token()
    drive = _drive_id()
    test_bytes = b"KNM SP self-test"
    item_id, url, path = upload_bytes(
        kind="complaint",
        display_id="SELFTEST",
        year=2026, month=6,
        file_name="selftest.txt",
        data=test_bytes,
        content_type="text/plain",
    )
    meta = get_item_metadata(item_id)
    delete_item(item_id)
    return f"OK drive={drive[:8]}… uploaded={path} size={meta.get('size')} deleted=yes"
