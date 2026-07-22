"""
One-time script to seed / update MachineLookup with location names and coordinates.

Rules:
- Rows WITH a machine code  → UPSERT by machine code (INSERT if new, UPDATE if exists)
- Rows WITHOUT a machine code → INSERT only if no row with that exact name already exists
- Duplicate machine codes in the data → last row in the list wins
- Missing coordinates are stored as NULL
"""

import pymssql
import config

# (machine_code_or_None, location_name, longitude, latitude_or_None)
RAW_DATA = [
    ("32720359", "Ubi Tech Park A",                103.89633,   1.326781),
    ("32720370", "Singpost Center",                103.893995,  1.319107),
    ("32720372", "Prive Condo",                    103.904052,  1.401143),
    ("42920754", "Skyworks @ Bedok",               103.929152,  1.32086),
    ("42920755", "IMH Main Lobby",                 103.884719,  1.381952),
    ("42920756", "Sunshine Plaza",                 103.851104,  1.300522),
    ("42920757", "351 Braddell",                   103.845449,  1.343634),
    ("42920759", "IMH Annex",                      103.885335,  1.382854),
    ("42920761", "Ubi Tech Park C",                103.89633,   1.326781),
    ("42920762", "Paya Lebar Certis",              103.722805,  1.315294),
    ("43020632", "Ang Mo Kio Tech Point",          103.849955,  1.389623),
    ("43020633", "Oxley Bizhub 2",                 103.892079,  1.331771),
    ("44220851", "Home Team Academy",              103.722951,  1.374502),
    ("44520325", "Singapore Sailing Center",       103.962,     1.315934),
    ("44520629", "Kranji Camp II",                 103.742855,  1.399051),
    ("44520630", "GnC Plaza 8",                    103.9659,    1.33334),
    ("44520632", "AUPE",                           103.882271,  1.344073),
    ("44520633", "Welcia Orchard",                 103.83139,   1.30306),
    ("44520635", "GnC MBC",                        103.7995973, 1.2756422),
    ("45021768", "Bedok Police HQ",                103.937042,  1.3328),
    ("45021769", "CSC Holland",                    103.791865,  1.311371),
    ("45021770", "Gleneagles L1",                  103.819694,  1.308647),
    ("45021773", "Tanah Merah Ferry Terminal",     103.988507,  1.314537),
    ("45021774", "SP Kallang",                     103.872182,  1.326527),
    ("45021776", "Science Park Ascent",            103.785626,  1.290623),
    ("50220245", "PSA ITC",                        103.79042,   1.275732),
    ("50220246", "Delta House",                    103.825389,  1.2912),
    ("50220248", "PSA Alongside",                  103.79042,   1.275732),
    ("50220249", "TCF @ Jurong",                   103.737667,  1.332495),
    ("50420522", "Gleneagles L4",                  103.819694,  1.308647),
    ("50420523", "Maybank",                        103.849223,  1.387287),
    ("50420532", "RWS",                            103.821832,  1.255479),
    ("50420533", "Changi Airport Police",          103.981071,  1.343252),
    ("51321279", "10X Genomics",                   103.875756,  1.324564),
    ("51321280", "Lim Kim Hai Electric",           103.86844,   1.314087),
    ("51321286", "Parkway Lab",                    103.889214,  1.320228),
    ("51421679", "Skyworks",                       103.929152,  1.32086),
    ("51421681", "Collins Aerospace",              103.968736,  1.346705),
    ("51421682", "GnC IOI",                        103.852017,  1.280978),
    ("51421683", "CGH L9",                         103.94957,   1.340385),
    ("51421685", "ST Marine L2 Pantry",            None,        None),
    ("51421686", "Chinese Swimming Club",          103.900542,  1.30032),
    ("51421694", "ST Rifle Range",                 103.779406,  1.34369),
    ("51421696", "NYC HDB Hub",                    103.848552,  1.332773),
    ("51421698", "Mount Carmel BP West Coast",     103.765747,  1.303174),
    ("51421699", "SA Tours",                       103.842531,  1.284095),
    ("51421700", "GnC Marina One",                 103.852887,  1.278725),
    ("51421701", "Hundred Grains VivoCity",        103.823059,  1.265282),
    ("51421702", "GnC Geneos",                     103.785170,  1.292711),
    ("51421703", "GnC 1 Raffles Place",            103.85096,   1.284574),
    ("51421704", "Meta L27",                       103.8524,    1.277534),
    ("51421706", "Welcia Bedok Mall",              103.9302,    1.32482),
    ("52920213", "Welcia Raffles City",            103.8533,    1.29424),
    ("52920225", "Chasen Logistics",               103.727234,  1.316345),
    ("52920226", "CGH L5",                         103.94957,   1.340385),
    ("52920229", "Kaki Bukit Camp",                103.907542,  1.339464),
    ("53920763", "Kranji Camp 3",                  103.742764,  1.403701),
    ("53920765", "NV Residence",                   103.943952,  1.372053),
    ("53920767", "Medtronics",                     103.972083,  1.336573),
    ("53920769", "Affinity Serangoon",             103.873068,  1.366414),
    # No machine code — insert by name
    (None,       "10 Raeburn Park",                103.830957,  1.2748861),
    (None,       "54 Pandan Road",                 103.744892,  1.2998825),
    (None,       "American Club",                  103.8296255, 1.3084663),
    (None,       "Ascent",                         103.7830407, 1.2904462),
    (None,       "Best Bakes",                     103.7957429, 1.3018705),
    (None,       "Big Elephant Cafe Havelock 2",   103.8426068, 1.2871803),
    (None,       "CDPL Tuas Dormitory",            103.6346703, 1.2716146),
    ("55120035", "Changi Naval Base Cookhouse",    104.0139996, 1.3180665),
    (None,       "Cheers CMPB Gombak",             103.7613159, 1.3670568),
    (None,       "Commonwealth Towers",            103.8029461, 1.29587),
    (None,       "First Culinary Restaurant",      103.8378796, 1.3778131),
    (None,       "Foresque Residences",            103.7737331, 1.369308),
    (None,       "German Centre",                  103.7437705, 1.3249777),
    (None,       "Goldbell Tower",                 103.8344614, 1.3123062),
    (None,       "iNz Residences",                 103.737463,  1.3748321),
    (None,       "Japanese Association Singapore", 103.8132587, 1.3306242),
    ("45021777", "Mediacorp",                      103.7895933, 1.2955542),
    (None,       "Police Cantonment Complex",      103.8370915, 1.2785558),
    (None,       "Queens Peak Condominium",        103.804259,  1.2947572),
    (None,       "Rochester Commons",              103.7853008, 1.3047695),
    ("52920224", "Skyworks @ AMK",                 103.8465424, 1.3894852),
    (None,       "SP Pasir Panjang",               103.7959422, 1.2704721),
    (None,       "Thye Hong Centre",               103.8122588, 1.2910836),
    ("52821401", "Tuas Naval Base",                103.6627741, 1.2956892),
    ("52821398", "Changi Lv 1",                    103.9662336, 1.3516001),
    # Duplicates below — these override earlier entries for the same code
    ("43020632", "AMK Techpoint",                  103.8468548, 1.3894349),
    ("52920228", "Amran's Kitchen",                103.8795815, 1.3454914),
    ("52821394", "Fei Siong Group",                103.7044427, 1.3326885),
    ("53920761", "SIA Terminal 3 Control Center",  103.9839315, 1.356275),
    ("50420524", "Alice@medipolis",                103.7908503, 1.2938511),
    ("51421685", "St. Marine Benoi",               103.6770818, 1.3015955),
    ("51621681", "Collins Aerospace Changi",       103.9659583, 1.3457854),
    ("53920766", "Police Cantonment Complex",      103.8370915, 1.2785558),
    ("44520630", "MinDef Lunch Club",              103.6872948, 1.3716234),
    ("52920227", "SP Choa Chu Kang",               103.8735121, 1.3769613),
    ("52920230", "ST Marine Benoi (new)",          103.6770818, 1.3015955),
    ("44520635", "St Joseph (canteen)",            103.7050664, 1.3505482),
    ("51421682", "St Joseph (cafe)",               103.7050664, 1.3505482),
    ("44520326", "St Joseph (conference room)",    103.7050664, 1.3505482),
    ("45021776", "Ascent",                         103.7830407, 1.2904462),
    ("51421702", "Jamiyah",                        103.7729765, 1.2906672),
    ("51421702", "Grains & Co Geneos",             103.8484069, 1.2844078),
    ("51421679", "Little Splashes Paya Lebar",     103.8918856, 1.3189472),
    ("55120032", "Geylang NPC",                    103.8835589, 1.3109648),
    ("51421703", "Anguillia Mosque",               103.851667,  1.3104972),
    ("52821396", "ST Digital",                     103.8767108, 1.328872),
    ("52821395", "Dawn Shipping",                  103.6942719, 1.3313423),
    (None,       "ST Tuas",                        None,        None),
    ("54120170", "SFATC Toa Payoh",                103.8461766, 1.3370622),
]


def build_deduped():
    """Last entry per machine code wins; no-code rows are kept all."""
    by_code = {}
    no_code = []
    for code, name, lon, lat in RAW_DATA:
        if code:
            by_code[code] = (name, lon, lat)
        else:
            no_code.append((name, lon, lat))
    return by_code, no_code


def main():
    by_code, no_code = build_deduped()
    conn = pymssql.connect(
        server=config.DB_SERVER,
        database=config.DB_NAME,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        tds_version="7.4",
    )
    cursor = conn.cursor()

    inserted = updated = skipped = 0

    # ── Rows with a machine code: upsert ──────────────────────────────────────
    for code, (name, lon, lat) in by_code.items():
        cursor.execute(
            "SELECT COUNT(*) FROM MachineLookup WHERE MachineCode = %s", (code,)
        )
        exists = cursor.fetchone()[0]

        if exists:
            cursor.execute(
                "UPDATE MachineLookup SET MachineName=%s, Longitude=%s, Latitude=%s WHERE MachineCode=%s",
                (name, lon, lat, code),
            )
            print(f"  UPDATED  {code:12s}  {name}")
            updated += 1
        else:
            cursor.execute(
                "INSERT INTO MachineLookup (MachineCode, MachineName, Longitude, Latitude) VALUES (%s, %s, %s, %s)",
                (code, name, lon, lat),
            )
            print(f"  INSERTED {code:12s}  {name}")
            inserted += 1

    # ── Rows without a machine code: insert if name not already present ───────
    for name, lon, lat in no_code:
        cursor.execute(
            "SELECT COUNT(*) FROM MachineLookup WHERE MachineName = %s", (name,)
        )
        exists = cursor.fetchone()[0]

        if exists:
            print(f"  SKIPPED  (name exists)        {name}")
            skipped += 1
        else:
            cursor.execute(
                "INSERT INTO MachineLookup (MachineName, Longitude, Latitude) VALUES (%s, %s, %s)",
                (name, lon, lat),
            )
            print(f"  INSERTED (no code)             {name}")
            inserted += 1

    conn.commit()
    conn.close()
    print(f"\nDone — inserted: {inserted}, updated: {updated}, skipped: {skipped}")


if __name__ == "__main__":
    main()
