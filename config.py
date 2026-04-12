# ── Azure SQL connection ──────────────────────────────────────────────────────
DB_SERVER   = "machineserver.database.windows.net"
DB_NAME     = "Machine DispensedDrink"
DB_USER     = "sqladmin"
DB_PASSWORD = "sqlKopi@311"

# ── Internal API key (used by nets_reconcile.py / GitHub Actions) ─────────────
# Generate a strong random value and set it here AND as an Azure App Service
# environment variable named INTERNAL_API_KEY.
import os
INTERNAL_API_KEY = os.environ.get("INTERNAL_API_KEY", "change-me-to-a-strong-random-string")

# ── Local development only ────────────────────────────────────────────────────
# When running locally (python app.py), Azure Easy Auth headers aren't present.
# Set these to simulate a logged-in user for testing.
# Leave both as "" before deploying to Azure.
DEV_USER_EMAIL = ""   # e.g. "ybhawe@kopinearme.com"
DEV_ROLE       = ""   # "admin" or "sales"
