"""
nets_mapping.py - Auresys terminal -> KNM machine.

SINGLE SOURCE OF TRUTH. Both the daily pull (auresys_pull.py) and the
monthly reconciliation (nets_reconcile.py) import from here. Do not keep
a second copy anywhere.

Keyed on the Auresys terminal id ("Machine ID", e.g. SGKN_M0043), NOT on the
outlet name. Outlet names change when a machine moves; the terminal id is the
stable join key and moves are handled by updating this file.

Roster captured from the Auresys Report/Transactions page on 2026-08-14.
Terminals get reassigned to new sites at roughly five a month, so when
vw_NETS_Terminal_Reassigned flags one, fix it HERE and in MachineLookup.

None = no matching machine in MachineLookup. The loader treats an unmapped
terminal that is actually trading as a hard alert, never a silent skip.
"""

# terminal_id: (machine_code, machine_name, outlet_name_when_mapped)
TERMINAL_TO_MACHINE = {
    'SGKN_M0001'    : ('43020633', 'Oxley Bizhub 2', 'OXLEY BIZHUB 2'),
    'SGKN_M0002'    : ('42920759', 'IMH MPSH', 'IMH MPSH'),
    'SGKN_M0003'    : ('42920756', 'Sunshine Plaza', 'SUNSHINE PLAZA'),
    'SGKN_M0004'    : ('42920757', '351 Braddell', 'BRADDELL 351'),
    'SGKN_M0005'    : ('42920761', 'Ubi Tech Park C', 'UBI TECHPARK LOBBY C'),
    'SGKN_M0006'    : ('42920755', 'IMH Main Lobby', 'IMH MAIN LOBBY'),
    'SGKN_M0007'    : ('43020632', 'AMK Techpoint', 'AMK TECHPOINT'),
    'SGKN_M0008'    : ('42920762', 'Paya Lebar Certis', 'CERTIS PAYA LEBAR'),
    'SGKN_M0009'    : ('52821401', 'Tuas Naval Base', 'TUAS NAVAL BASE'),
    'SGKN_M0010'    : ('45021768', 'Bedok Police HQ', 'BEDOK POLICE HQ'),
    'SGKN_M0011'    : ('44520325', 'Singapore Sailing Center', 'SINGAPORE SAILING FEDERATION'),
    'SGKN_M0013'    : ('45021777', 'Harbourfront Cruise Centre Departure', 'HARBOURFRONT CRUISE CENTRE DEPARTURE'),
    'SGKN_M0014'    : ('45021774', 'SP Kallang', 'SP KALLANG'),
    'SGKN_M0015'    : ('45021773', 'Tanah Merah Ferry Terminal', 'TANAH MERAH FERRY TERMINAL'),
    'SGKN_M0016'    : ('45021769', 'CSC Holland', 'HOLLAND V CSCOLLEGE'),
    'SGKN_M0017'    : ('50420524', 'Alice@medipolis', 'ALICE AT MEDIAPOLIS'),
    'SGKN_M0018'    : ('50420523', 'AMK Maybank', 'AMK MAYBANK CENTRE'),
    'SGKN_M0019'    : ('53920766', 'Police Cantonment Complex', 'POLICE CANTONTMENT COMPLEX'),
    'SGKN_M0020'    : ('51621681', 'Collins Aerospace Changi', 'COLLINS AEROSPACE CHANGI'),
    'SGKN_M0021'    : ('51421694', 'ST Rifle Range', 'RIFLE RANGE ATREC'),
    'SGKN_M0022'    : ('51421698', 'Mount Carmel BP West Coast', 'MT CARMEL BP CHURCH'),
    'SGKN_M0023'    : ('45021770', 'Gleneagles L1', 'TANGLIN GLENEAGLES L1 UCC'),
    'SGKN_M0024'    : ('51421684', 'CPAS Pasir Ris', 'CPAS PASIR RIS'),
    'SGKN_M0025'    : ('51421688', 'Japanese Association Singapore', 'ADAM ROAD THE JAPANESE ASSOCIATION'),
    'SGKN_M0026'    : ('50420522', 'Gleneagles L4', 'TANGLINGLENEAGLESL4ICU'),
    'SGKN_M0027'    : ('44520628', 'The American Club', 'ORCHARD AMERICAN CLUB'),
    'SGKN_M0028'    : ('32720359', 'Ubi Tech Park A', 'UBI TECHPARK LOBBY A'),
    'SGKN_M0029'    : ('50420533', 'Changi Airport Police', 'CHANGI AIRPORT POLICE HQ'),
    'SGKN_M0030'    : ('51421699', 'SA Tours', 'CHINATOWN SA TOURS'),
    'SGKN_M0031'    : ('42920754', 'Skyworks @ Bedok', 'BEDOK SKYWORKS TABLETOP'),
    'SGKN_M0032'    : ('32720370', 'Singpost Center', 'SINGPOST CENTRE'),
    'SGKN_M0034'    : ('44520629', 'Kranji Camp II', 'KRANJI CAMP II'),
    'SGKN_M0036'    : ('55120029', 'Burlington Square', 'BURLINGTON SQUARE'),
    'SGKN_M0037'    : ('51421696', 'NYC HDB Hub', 'HDB HUB NATIONAL YOUTH COUNCIL'),
    'SGKN_M0038'    : ('51321286', 'Parkway Lab', 'ALJUNIED PARKWAY LAB'),
    'SGKN_M0040'    : ('45021771', "St Andrew's Nursing Home", 'ALJUNIED ST ANDREW NURSING'),
    'SGKN_M0041'    : ('55120035', 'Changi Naval Base Cookhouse', 'CHANGI NAVAL BASE COOKHOUSE'),
    'SGKN_M0042'    : ('52920224', 'Skyworks @ AMK', 'AMK SKYWORKS'),
    'SGKN_M0043'    : ('53920761', 'SIA Terminal 3 Control Center', 'T3 SIA CONTROL CENTRE'),
    'SGKN_M0045'    : ('94210726', 'SGH L9', 'SGH Blk 6 L9'),
    'SGKN_M0046'    : ('52920227', 'SP Choa Chu Kang', 'SP CHOA CHU KANG'),
    'SGKN_M0047'    : ('55120032', 'Geylang NPC', 'GEYLANG NPC'),
    'SGKN_M0048'    : ('55120028', 'Sinwa Global', 'JOO KOON SINWA GLOBAL'),
    'SGKN_M0049'    : ('55120037', 'Link @ AMK', 'LINK@AMK'),
    'SGKN_M0050'    : ('55120044', 'Tee Yih Jia Building Carpark', 'TEE YIH JIA BUILDING CARPARK'),
    'SGKN_M0052'    : ('55120026', 'Changi Naval Base Blk119', 'CHANGI NAVAL BASE BLK 119'),
    'SGKN_M0054'    : ('55120043', 'Harbourfront Cruise Centre', 'HARBOURFRONT CRUISE CENTRE TICKETING'),
    'SGKN_M0055'    : ('55120025', 'CGH Lobby B', 'CHANGI GENERAL HOSPITAL LOBBY B'),
    'SGKN_M0056'    : ('54120170', 'SFATC Toa Payoh', 'SFATC Toa Payoh'),
    'SGKN_M0057'    : ('55120533', 'Admirax', 'ADMIRAX'),
    'SGKN_M0058'    : ('55120040', 'Verve @ The Octagon', 'VERVE OFFICES @ THE OCTAGON'),
    'SGKN_M0059'    : ('54120166', 'St Andrews Autism Centre', 'ST ANDREW AUTISM CENTRE'),
    'SGKN_M0061'    : ('55120033', 'Dieppe Barracks', 'DIEPPE BARRACKS'),
    'SGKN_M0062'    : ('55120030', 'Certis Training Academy', 'CERTIS TRAINING ACADEMY'),
    'SGKN_M0063'    : ('55120031', 'Inzy Group', 'INZY WOODSQUARE TOWER 2'),
    'SGKN_M0064'    : ('51421685', 'Givaudan Pioneer Rd', 'GIVUADAN PIONEER'),
    'SGKN_M0065'    : ('54120153', 'Givaudan Woodlands', 'GIVUADAN WOODLANDS'),
    'SGKN_M0066'    : ('55120532', 'SATS Seletar Camp', 'SELETAR CAMP'),
    'SGKN_M0067'    : ('54020504', 'CNB CC2C', 'CHANGI NAVAL BASE CC2C'),

    # ---- unresolved: trading, but no machine assigned yet ----
    'SGKN_M0012'    : (None, None, 'SP PASIR PANJANG'),   # NO MATCH
    'SGKN_M0033'    : (None, None, 'KAKI BUKIT CAMP'),   # NO MATCH
    'SGKN_M0035'    : (None, None, '28 AYER RAJAH CRESENT'),   # NO MATCH
    'SGKN_M0039'    : (None, None, 'KRANJI CAMP 604'),   # AMBIGUOUS
    'SGKN_M0044'    : (None, None, 'CHANGI GENERAL HOSPITAL A&E'),   # AMBIGUOUS
    'SGKN_M0051'    : (None, None, 'KRANJI CAMP BLK 808'),   # AMBIGUOUS
    'SGKN_M0053'    : (None, None, 'CHANGI NAVAL BASE BLK 225'),   # NO MATCH
    'SGKN_M0060'    : (None, None, 'CHANGI GENERAL HOSPITAL INTEGRATED BUILDING L1'),   # AMBIGUOUS
    'SGKN_M0068'    : (None, None, '--'),   # NOT CONFIGURED
    'SGKN_M0069'    : (None, None, 'MANDAI HILL CAMP'),   # NO MATCH
    'SGKN_M0070'    : (None, None, '--'),   # NOT CONFIGURED
    'SGKN_M0071'    : (None, None, 'HARBOURFRONT CRUISE CENTRE DEPARTURE 2'),   # NO MATCH
    'SGKN_M0072'    : (None, None, 'GEMINI@SIMS'),   # NO MATCH
    'SGKN_M0075'    : (None, None, '33 GREENWICH DRIVE DSV'),   # NO MATCH

    # ---- COFFEERUSH franchisee (roster 2026-09-03). MachineCode is synthetic:
    # 9 + terminal number, see auresys_pull.synthetic_machine_code. Names as
    # shown on the Auresys portal, URL-decoded. ----
    'SGEE_M0001'    : ('900000001', 'RWS Office', 'RWS Office'),
    'SGEE_M0002'    : ('900000002', 'Prive', 'Prive'),
    'SGEE_M0003'    : ('900000003', 'Medtronic', 'Medtronic'),
    'SGEE_M0004'    : ('900000004', 'Affinity', 'Affinity'),
    'SGEE_M0005'    : ('900000005', 'Rio Vista', 'Rio Vista'),
    'SGEE_M0006'    : ('900000006', 'Bishan Sheng Siong', 'Bishan Sheng Siong'),
    'SGEE_M0007'    : ('900000007', 'NV Residences', 'NV Residences'),
    'SGEE_M0008'    : ('900000008', 'SGEE_M0008', ''),   # NO NAME on portal 2026-09-03 - not in MachineLookup yet
    'SGEE_M0009'    : ('900000009', 'Genting Centre', 'Genting Centre'),
    'SGEE_M0010'    : ('900000010', 'SGEE_M0010', ''),   # NO NAME on portal 2026-09-03 - not in MachineLookup yet
    'SGEE_M0011'    : ('900000011', 'Mount Alvernia Main Lobby L4', 'Mount Alvernia Main Lobby L4'),
    'SGEE_M0012'    : ('900000012', 'Changi East Project Office Level 1 Pantry', 'Changi East Project Office Level 1 Pantry'),
    'SGEE_M0013'    : ('900000013', 'Bishan North Mall', 'Bishan North Mall'),
    'SGEE_M0014'    : ('900000014', 'Tresalveo', 'Tresalveo'),
    'SGEE_M0015'    : ('900000015', 'The Palette', 'The Palette'),
    'SGEE_M0016'    : ('900000016', 'SGEE_M0016', ''),   # NO NAME on portal 2026-09-03 - not in MachineLookup yet
    'SGEE_M0017'    : ('900000017', 'SGEE_M0017', ''),   # NO NAME on portal 2026-09-03 - not in MachineLookup yet
    'SGEE_M0018'    : ('900000018', 'SGEE_M0018', ''),   # NO NAME on portal 2026-09-03 - not in MachineLookup yet
    'SGEE_M0019'    : ('900000019', 'SGEE_M0019', ''),   # NO NAME on portal 2026-09-03 - not in MachineLookup yet
    'SGEE_M0020'    : ('900000020', 'SGEE_M0020', ''),   # NO NAME on portal 2026-09-03 - not in MachineLookup yet
}


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
    e = TERMINAL_TO_MACHINE.get(terminal_id)
    return (e[0], e[1]) if e else (None, None)


def known_terminals():
    return set(TERMINAL_TO_MACHINE) | set(TERMINAL_ACCOUNT)


def unmapped_terminals():
    return {t for t, v in TERMINAL_TO_MACHINE.items() if v[0] is None}
