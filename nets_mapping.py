"""
nets_mapping.py - Auresys terminal -> KNM machine.

Account configuration (ACCOUNTS, TERMINAL_ACCOUNT) lives here. The terminal ->
machine mapping does NOT: it is MachineLookup.NETS_Map, read by load_from_db().

Keyed on the Auresys terminal id ("Machine ID", e.g. SGKN_M0043), NOT on the
outlet name. Outlet names change when a machine moves; the terminal id is the
stable join key and moves are handled by updating MachineLookup.NETS_Map.

Roster captured from the Auresys Report/Transactions page on 2026-08-14.
Terminals get reassigned to new sites at roughly five a month, so when
vw_NETS_Terminal_Reassigned flags one, fix MachineLookup.NETS_Map.

A trading terminal with no machine is a hard alert, never a silent skip.
"""

# --------------------------------------------------------------------------- #
# Terminal -> machine lives in the DATABASE, not in this file (2026-09-22).
# MachineLookup.NETS_Map holds the Auresys terminal id for each machine:
#   'SGKN_M0044' = this machine's payment terminal
#   '0'          = the machine has no Auresys terminal
#   NULL         = not yet confirmed
# Only active machines count. A terminal claimed by two active machines is
# ambiguous: its rows load unattributed and the pull alerts.
# load_from_db() must run before resolve()/known_terminals(); resolve() refuses
# to answer otherwise, so a missed load can never stamp every row NULL.
# --------------------------------------------------------------------------- #
TERMINAL_TO_MACHINE = {}      # terminal_id -> (machine_code, machine_name, None)
AMBIGUOUS = {}                # terminal_id -> [machine_code, ...]
_LOADED = False

SQL_MAP = ("SELECT CAST(MachineCode AS NVARCHAR(50)), MachineName, "
           "LTRIM(RTRIM(NETS_Map)) FROM MachineLookup "
           "WHERE NETS_Map IS NOT NULL AND LTRIM(RTRIM(NETS_Map)) NOT IN ('', '0') "
           "AND ISNULL(IsActive, 1) = 1")


def load_from_db(conn):
    """Fill TERMINAL_TO_MACHINE from MachineLookup.NETS_Map. -> AMBIGUOUS."""
    cur = conn.cursor()
    cur.execute(SQL_MAP)
    return load_rows(cur.fetchall())


def load_rows(rows):
    """rows = [(machine_code, machine_name, terminal_id)]. -> AMBIGUOUS."""
    global _LOADED
    by_term = {}
    for code, name, term in rows:
        by_term.setdefault(str(term), []).append((str(code), name))
    TERMINAL_TO_MACHINE.clear()
    AMBIGUOUS.clear()
    for term, machines in by_term.items():
        if len(machines) == 1:
            TERMINAL_TO_MACHINE[term] = (machines[0][0], machines[0][1], None)
        else:
            TERMINAL_TO_MACHINE[term] = (None, None, None)
            AMBIGUOUS[term] = sorted(m[0] for m in machines)
    _LOADED = True
    return dict(AMBIGUOUS)


# --------------------------------------------------------------------------- #
# Accounts (2026-09-03). The pull logs in to every Auresys account listed in
# the AURESYS_FRANCHISEES env var, plus MAIN. Each key MUST exist here: the
# dashboard reads the label and colour from this dict, and the loader refuses
# a key it cannot label. Keys are ^[A-Z0-9_]{1,16}$ - NETS_Transaction.
# Account_Key is NVARCHAR(16). NULL in that column means MAIN (history
# predates the column).
# --------------------------------------------------------------------------- #
MAIN_ACCOUNT = "MAIN"
ACCOUNTS = {
    "MAIN":       {"label": "KNM Main",   "color": None},        # never striped
    "AUVION":     {"label": "Auvion",     "color": "#2563eb"},   # blue
    "COFFEERUSH": {"label": "CoffeeRush", "color": "#d97706"},   # amber
}

# terminal_id -> account key, for terminals NOT on the MAIN roster. Anything
# absent here is assumed MAIN. Populated from `auresys_pull.py --roster`; the
# loader alerts when a terminal shows up on a different account's roster than
# the one recorded here (a terminal moved between accounts - fix this file).
TERMINAL_ACCOUNT = {
    'SGEE_M0001': 'COFFEERUSH',
    'SGEE_M0002': 'COFFEERUSH',
    'SGEE_M0003': 'COFFEERUSH',
    'SGEE_M0004': 'COFFEERUSH',
    'SGEE_M0005': 'COFFEERUSH',
    'SGEE_M0006': 'COFFEERUSH',
    'SGEE_M0007': 'COFFEERUSH',
    'SGEE_M0008': 'COFFEERUSH',
    'SGEE_M0009': 'COFFEERUSH',
    'SGEE_M0010': 'COFFEERUSH',
    'SGEE_M0011': 'COFFEERUSH',
    'SGEE_M0012': 'COFFEERUSH',
    'SGEE_M0013': 'COFFEERUSH',
    'SGEE_M0014': 'COFFEERUSH',
    'SGEE_M0015': 'COFFEERUSH',
    'SGEE_M0016': 'COFFEERUSH',
    'SGEE_M0017': 'COFFEERUSH',
    'SGEE_M0018': 'COFFEERUSH',
    'SGEE_M0019': 'COFFEERUSH',
    'SGEE_M0020': 'COFFEERUSH',
}


def account_of(terminal_id):
    return TERMINAL_ACCOUNT.get(terminal_id, MAIN_ACCOUNT)


def resolve(terminal_id):
    """Return (machine_code, machine_name) or (None, None)."""
    if not _LOADED:
        raise RuntimeError("nets_mapping.load_from_db() has not run")
    e = TERMINAL_TO_MACHINE.get(terminal_id)
    return (e[0], e[1]) if e else (None, None)


def known_terminals():
    return set(TERMINAL_TO_MACHINE) | set(TERMINAL_ACCOUNT)


def unmapped_terminals():
    return {t for t, v in TERMINAL_TO_MACHINE.items() if v[0] is None}
