# ── Configuration ─────────────────────────────────────────────────────────────
# NO SECRETS IN THIS FILE. It is tracked in git and deployed as-is by
# .github/workflows/main_knmdispenseviewer.yml (actions/checkout ships only
# TRACKED files, so this module must stay tracked or `import config` fails).
# Real values come from App Service -> Environment variables, and from the
# environment in GitHub Actions.
import os

# ── Azure SQL connection ──────────────────────────────────────────────────────
DB_SERVER   = os.environ.get("DB_SERVER", "machineserver.database.windows.net")
DB_NAME     = os.environ.get("DB_NAME",   "Machine DispensedDrink")
DB_USER     = os.environ.get("DB_USER",     "")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")   # never a hardcoded fallback

# ── Internal API key ──────────────────────────────────────────────────────────
# Unset on purpose. Empty string makes /api/internal/vend-counts fail closed.
INTERNAL_API_KEY = os.environ.get("INTERNAL_API_KEY", "")

# ── Local development only ────────────────────────────────────────────────────
# DELIBERATE LITERALS — never read these from the environment. get_current_user()
# (app.py:31) and get_all_roles() (app.py:58) fall back to them when no Easy Auth
# principal is present, so DEV_ROLE="admin" as an app setting would make every
# unauthenticated request a full admin.
DEV_USER_EMAIL = ""
DEV_ROLE       = ""
