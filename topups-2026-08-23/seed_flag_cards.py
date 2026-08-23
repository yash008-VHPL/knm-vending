#!/usr/bin/env python3
"""
seed_flag_cards.py — put the physical top-up flag cards into dbo.NETS_FlagCard.
                                                                    2026-08-23
WHY THIS IS A SEPARATE SCRIPT AND NOT PART OF THE WEB APP
---------------------------------------------------------
Card_Hash is HMAC-SHA256(NETS_CARD_PEPPER, cardNo). The pepper is the entire
security boundary on the payment feed — the card-number keyspace is small
enough to enumerate for anyone holding it — so it stays a build-time secret.
Run this ONCE. Afterwards the Flask app joins on the stored digest and never
needs the pepper, never sees a card number, and never has to hash anything.

The card numbers are NOT in this file. knm-vending is a PUBLIC repository.
Put them in a local, git-ignored file, one per line:

    cards.txt
    ----------
    1000230006648074   Flag card 1
    1000230004290902   Flag card 2
    ...

Everything after the number on a line is an optional label. Blank lines and
lines starting with # are ignored.

USAGE
-----
    export NETS_CARD_PEPPER='...'            # same value the daily pull uses
    export NETS_DB_USER='...' NETS_DB_PASSWORD='...'
    python3 seed_flag_cards.py cards.txt              # seed over a DB connection
    python3 seed_flag_cards.py cards.txt --sql        # emit INSERTs, no DB needed
    python3 seed_flag_cards.py cards.txt --dry-run    # show what it would do
    python3 seed_flag_cards.py --list                 # what is seeded now

--sql is the path of least resistance. Only the HASHING needs the pepper; the
write is an ordinary INSERT. --sql prints ready-to-paste statements and never
opens a database connection, so you can run them in the Azure Query Editor with
the login you are already signed in as, and no database credential has to be
recovered from a write-only GitHub secret.

The digests are not secret-equivalent — they cannot be reversed to a card number
without the pepper — but do not commit them either: knm-vending is public and
they would let anyone confirm a guessed card number against the feed.

BEFORE YOU RUN IT — CHECK THE ASSUMPTION
----------------------------------------
This whole feature rests on Auresys returning the flag card's number unmasked.
auresys_pull.card_hash() stores NULL when cardNo is empty or all asterisks:

    if not raw or set(raw) <= {"*"}:
        return None

If the acquirer masks the PAN for the scheme these cards use, every flag tap
lands with Card_Hash = NULL and the cards are unidentifiable in principle, not
merely unseeded. Run probe_flag_card.py first — it takes one minute and it is
the difference between a working vend counter and a column of zeroes.
"""

import hashlib
import hmac
import os
import sys

DEFAULT_SERVER = "machineserver.database.windows.net"
DEFAULT_DB = "Machine DispensedDrink"
PEPPER_VER = 1                      # must match auresys_pull.PEPPER_VER


def card_hash(raw, pepper):
    """Byte-for-byte the same function as auresys_pull.card_hash.

    Any divergence here — a strip() dropped, hexdigest() instead of digest(),
    a different encoding — produces digests that will never match a single row,
    silently, forever. Do not 'tidy' it.
    """
    raw = (raw or "").strip()
    if not raw or set(raw) <= {"*"}:
        return None
    return hmac.new(pepper.encode(), raw.encode(), hashlib.sha256).digest()


def connect():
    import pymssql
    user = os.environ.get("NETS_DB_USER") or os.environ.get("DB_USER")
    pwd = os.environ.get("NETS_DB_PASSWORD") or os.environ.get("DB_PASSWORD")
    if not user or not pwd:
        sys.exit("Set NETS_DB_USER / NETS_DB_PASSWORD (or DB_USER / DB_PASSWORD).")
    return pymssql.connect(
        server=os.environ.get("NETS_DB_SERVER", DEFAULT_SERVER),
        database=os.environ.get("NETS_DB_NAME", DEFAULT_DB),
        user=user, password=pwd, tds_version="7.4", login_timeout=10)


def read_cards(path):
    out = []
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            num = parts[0].strip()
            label = (parts[1].strip() if len(parts) > 1 else "") or ("Flag card %d" % (len(out) + 1))
            if not num.isdigit():
                sys.exit("Line %d of %s: %r is not a card number." % (n, path, num))
            out.append((num, label))
    if not out:
        sys.exit("No cards found in %s." % path)
    return out


def do_list():
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT Label, Card_Last4, Card_Hash_Ver, IsActive, AddedBy, AddedAt "
                "FROM dbo.NETS_FlagCard ORDER BY Label")
    rows = cur.fetchall()
    if not rows:
        print("dbo.NETS_FlagCard is empty.")
    for r in rows:
        print("  %-28s ****%s  ver=%s  active=%s  by=%s  %s"
              % (r[0], r[1] or "????", r[2], "yes" if r[3] else "no", r[4] or "-", r[5]))
    print("\n%d card(s)." % len(rows))
    conn.close()


def emit_sql(rows, who):
    """Ready-to-paste INSERTs. Touches no database.

    MERGE-shaped rather than a bare INSERT so re-running it is safe: the primary
    key is the digest, and a second run of a plain INSERT would fail the whole
    batch on the first duplicate and leave you guessing which cards landed.
    """
    print("\n" + "-" * 72)
    print("-- Paste into the Azure Query Editor. Safe to re-run.")
    print("-- Generated by seed_flag_cards.py. Do NOT commit: knm-vending is public.")
    print("-" * 72)
    for h, ver, label, last4 in rows:
        lbl = label.replace("'", "''")
        print(
            "MERGE dbo.NETS_FlagCard AS t\n"
            "USING (SELECT 0x%s AS Card_Hash) AS s ON t.Card_Hash = s.Card_Hash\n"
            "WHEN MATCHED THEN UPDATE SET Label = N'%s', Card_Last4 = '%s',\n"
            "                             Card_Hash_Ver = %d, IsActive = 1\n"
            "WHEN NOT MATCHED THEN INSERT (Card_Hash, Card_Hash_Ver, Label, Card_Last4, AddedBy)\n"
            "     VALUES (0x%s, %d, N'%s', '%s', N'%s');"
            % (h.hex().upper(), lbl, last4, ver,
               h.hex().upper(), ver, lbl, last4, who.replace("'", "''")))
    print("\n-- Verify, then check the cards are actually visible in the feed:")
    print("SELECT Label, Card_Last4, Card_Hash_Ver, IsActive FROM dbo.NETS_FlagCard ORDER BY Label;")
    print("""
SELECT COUNT(DISTINCT Machine_Code) AS machines, COUNT(*) AS taps,
       MAX(Txn_DateTime) AS most_recent
FROM   dbo.NETS_Transaction
WHERE  Card_Hash IN (SELECT Card_Hash FROM dbo.NETS_FlagCard WHERE IsActive = 1);""")
    print("-" * 72)
    print("-- Expect roughly 120+ taps across ~25 machines. Zero means the pepper")
    print("-- used here is not the one the daily loader writes with.")


def main():
    args = [a for a in sys.argv[1:]]
    if "--list" in args:
        return do_list()
    sql_only = "--sql" in args
    dry = "--dry-run" in args
    args = [a for a in args if not a.startswith("--")]
    if not args:
        sys.exit(__doc__.strip().splitlines()[0] + "\n\nUsage: seed_flag_cards.py cards.txt "
                 "[--sql | --dry-run] | --list")
    path = args[0]

    pepper = os.environ.get("NETS_CARD_PEPPER")
    if not pepper:
        sys.exit("NETS_CARD_PEPPER not set. Refusing to run: a digest made with the "
                 "wrong pepper would silently never match a single transaction.")

    cards = read_cards(path)
    print("%d card(s) read from %s" % (len(cards), path))

    rows = []
    for num, label in cards:
        h = card_hash(num, pepper)
        if h is None:
            print("  SKIP  %s — masked or empty" % label)
            continue
        rows.append((h, PEPPER_VER, label[:60], num[-4:]))
        print("  %-28s ****%s  ->  %s…" % (label, num[-4:], h.hex()[:16]))

    who = os.environ.get("SEEDED_BY") or os.environ.get("USER") or "seed_flag_cards"

    if sql_only:
        return emit_sql(rows, who)
    if dry:
        print("\n--dry-run: nothing written.")
        return

    conn = connect()
    cur = conn.cursor()
    ins = upd = 0
    for h, ver, label, last4 in rows:
        cur.execute("SELECT COUNT(*) FROM dbo.NETS_FlagCard WHERE Card_Hash = %s", (h,))
        if cur.fetchone()[0]:
            cur.execute("UPDATE dbo.NETS_FlagCard SET Label=%s, Card_Last4=%s, "
                        "IsActive=1, Card_Hash_Ver=%s WHERE Card_Hash=%s",
                        (label, last4, ver, h))
            upd += 1
        else:
            cur.execute("INSERT INTO dbo.NETS_FlagCard "
                        "(Card_Hash, Card_Hash_Ver, Label, Card_Last4, AddedBy) "
                        "VALUES (%s,%s,%s,%s,%s)", (h, ver, label, last4, who))
            ins += 1
    conn.commit()
    print("\n%d inserted, %d updated." % (ins, upd))

    # Does any of this actually appear in the feed? A seeded card that has never
    # been seen is not an error, but it IS the single most likely reason the
    # vend counter comes up empty, so say it here rather than let the screen
    # imply the machines were never topped up.
    cur.execute("""
        SELECT COUNT(DISTINCT Machine_Code), COUNT(*), MAX(Txn_DateTime)
        FROM dbo.NETS_Transaction
        WHERE Card_Hash IN (SELECT Card_Hash FROM dbo.NETS_FlagCard WHERE IsActive = 1)
          AND Txn_Date >= DATEADD(day, -180, CAST(GETDATE() AS DATE))""")
    m, n, last = cur.fetchone()
    if not n:
        print("\n  ⚠  No transaction in the last 180 days matches any of these digests.\n"
              "     Either the cards have not been tapped since the feed started, or\n"
              "     Auresys is masking the card number (Card_Hash would be NULL) or\n"
              "     the pepper does not match the one the daily pull uses.\n"
              "     Run probe_flag_card.py before assuming the cards are wrong.")
    else:
        print("\n  ✓  %d taps across %d machines; most recent %s." % (n, m, last))
    conn.close()


if __name__ == "__main__":
    main()
