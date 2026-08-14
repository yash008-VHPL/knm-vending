"""
gcal_feed.py — read-only Google Calendar feed for the Alpha scheduler.

The sales team keeps the route plan in Google Calendar. A Google Apps Script
web app (running as the calendar owner) returns already-expanded, SGT-dated
events as JSON. This module polls that endpoint on a BACKGROUND THREAD,
resolves each event title to machine codes via dbo.GCalSiteAlias, and serves
everything from an in-memory cache.

WHY A THREAD: app.py runs under a single gunicorn sync worker (Procfile has no
--workers/--threads), so any outbound call made inside a request would block
every other user for its full timeout. The request path only reads the cache
and never touches the network.

Config (App Service -> Environment variables):
    GCAL_FEED_URL     the Apps Script /exec URL
    GCAL_FEED_SECRET  the shared secret ('k' in the script)
    GCAL_POLL_SECONDS optional, default 300, minimum 60
Feed is simply disabled when GCAL_FEED_URL is unset — nothing breaks.
"""

import os
import re
import threading
import time
from datetime import datetime, timedelta

import requests

# ── config ──────────────────────────────────────────────────────────────────

def _cfg(name, default=""):
    v = os.environ.get(name)
    if v:
        return v
    try:
        import config
        return getattr(config, name, default)
    except Exception:
        return default


FEED_URL     = _cfg("GCAL_FEED_URL")
FEED_SECRET  = _cfg("GCAL_FEED_SECRET")
BACK_DAYS    = 7
FORWARD_DAYS = 56
CONNECT_TIMEOUT = 10
READ_TIMEOUT    = 90

def _poll_seconds():
    try:
        return max(60, int(_cfg("GCAL_POLL_SECONDS", "300")))
    except (TypeError, ValueError):
        return 300


# ── title parsing ───────────────────────────────────────────────────────────
# Titles arrive like "CB Clementi", "CGH x3", "🆕 Pax Ocean x2", "St Joseph's x1".
# Leading decoration (the 🆕 emoji) is stripped; a trailing " xN" is the number
# of machines being serviced on THAT visit, which is not always the number of
# machines at the site (St Joseph's was x1 on 14 Aug and x3 on 21 Aug).

_LEAD_JUNK = re.compile(r"^[^0-9A-Za-z]+")
_TRAIL_QTY = re.compile(r"\s+[xX]\s*(\d+)\s*$")
_WS        = re.compile(r"\s+")


def parse_title(raw):
    """'🆕 Pax Ocean x2' -> ('Pax Ocean', 2, True). Count defaults to 1."""
    t = (raw or "").strip()
    flagged = bool(_LEAD_JUNK.match(t))
    t = _LEAD_JUNK.sub("", t)
    qty = 1
    m = _TRAIL_QTY.search(t)
    if m:
        try:
            qty = max(1, int(m.group(1)))
        except ValueError:
            qty = 1
        t = t[: m.start()]
    return _WS.sub(" ", t).strip(), qty, flagged


# ── alias resolution ────────────────────────────────────────────────────────

def load_aliases(cursor):
    """-> {lowercased CalendarText: {'codes': [...], 'known': True}}"""
    cursor.execute(
        "SELECT CalendarText, MachineCode FROM dbo.GCalSiteAlias"
    )
    out = {}
    for text, code in cursor.fetchall():
        key = (text or "").strip().lower()
        if not key:
            continue
        slot = out.setdefault(key, {"codes": [], "known": True})
        if code:
            slot["codes"].append(str(code).strip())
    return out


def resolve(raw_title, aliases):
    """Map one event title onto machine codes plus a status the UI can act on.

    ok        count matches the alias rows -> safe to create one stop each
    partial   fewer machines than the site has -> a human must pick which
    over      more requested than mapped -> alias table is incomplete
    unmapped  title is known but no codes recorded yet
    unknown   title has never been seen
    """
    base, qty, flagged = parse_title(raw_title)
    entry = aliases.get(base.lower())
    codes = list(entry["codes"]) if entry else []

    if entry is None:
        status = "unknown"
    elif not codes:
        status = "unmapped"
    elif qty == len(codes):
        status = "ok"
    elif qty < len(codes):
        status = "partial"
    else:
        status = "over"

    return {
        "site": base,
        "qty": qty,
        "flagged": flagged,
        "codes": codes,
        "status": status,
    }


# ── cache + poller ──────────────────────────────────────────────────────────

_lock = threading.Lock()
_cache = {
    "events": [],
    "fetched_at": None,
    "error": None,
    "ok": False,
    "generated_at": None,
}
_started = False


def enabled():
    return bool(FEED_URL and FEED_SECRET)


def snapshot(frm=None, to=None):
    """Cache read only — never blocks, never raises."""
    with _lock:
        evs = list(_cache["events"])
        meta = {
            "ok": _cache["ok"],
            "error": _cache["error"],
            "fetchedAt": _cache["fetched_at"],
            "generatedAt": _cache["generated_at"],
            "enabled": enabled(),
        }
    if frm:
        evs = [e for e in evs if e["date"] >= frm]
    if to:
        evs = [e for e in evs if e["date"] <= to]
    meta["count"] = len(evs)
    return {"stops": evs, **meta}


def _fetch(get_cursor):
    body = {"k": FEED_SECRET, "back": BACK_DAYS, "days": FORWARD_DAYS}
    r = requests.post(FEED_URL, json=body,
                      timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("error") or "feed returned ok=false")

    conn = cur = None
    try:
        conn, cur = get_cursor()
        aliases = load_aliases(cur)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    out = []
    for ev in data.get("events", []):
        res = resolve(ev.get("title"), aliases)
        out.append({
            "gcalId":   ev.get("id"),
            "title":    ev.get("title"),
            "date":     ev.get("date"),
            "allDay":   bool(ev.get("allDay")),
            "startTime": ev.get("startTime"),
            "colour":   ev.get("colour") or "",
            "note":     ev.get("desc") or "",
            "site":     res["site"],
            "qty":      res["qty"],
            "isNew":    res["flagged"],
            "codes":    res["codes"],
            "status":   res["status"],
        })
    return out, data.get("generatedAt")


def refresh_once(get_cursor):
    """Returns True on success. Keeps the previous cache on failure."""
    try:
        events, generated = _fetch(get_cursor)
    except Exception as e:
        with _lock:
            _cache["error"] = "%s: %s" % (type(e).__name__, e)
            _cache["ok"] = False
        print("[gcal_feed] refresh failed: %s" % e)
        return False
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    with _lock:
        _cache["events"] = events
        _cache["fetched_at"] = now
        _cache["generated_at"] = generated
        _cache["error"] = None
        _cache["ok"] = True
    return True


def _loop(get_cursor):
    interval = _poll_seconds()
    while True:
        refresh_once(get_cursor)
        time.sleep(interval)


def start(get_cursor):
    """Idempotent. Safe to call at import time; no-op when unconfigured."""
    global _started
    if _started or not enabled():
        return False
    _started = True
    t = threading.Thread(target=_loop, args=(get_cursor,),
                         name="gcal-feed", daemon=True)
    t.start()
    return True
