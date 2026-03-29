# ── Azure SQL connection ──────────────────────────────────────────────────────
DB_SERVER   = "machineserver.database.windows.net"
DB_NAME     = "Machine DispensedDrink"
DB_USER     = "sqladmin"
DB_PASSWORD = "sqlKopi@311"

# ── Local development only ────────────────────────────────────────────────────
# When running locally (python app.py), Azure Easy Auth headers aren't present.
# Set these to simulate a logged-in user for testing.
# Leave both as "" before deploying to Azure.
DEV_USER_EMAIL = ""   # e.g. "ybhawe@kopinearme.com"
DEV_ROLE       = ""   # "admin" or "sales"
