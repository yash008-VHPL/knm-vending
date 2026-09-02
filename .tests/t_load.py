import datetime as dt, decimal, sys, types
sys.path.insert(0, ".")
import auresys_pull as ap
ZERO = decimal.Decimal("0.00"); D1 = dt.date(2026, 9, 1)
STORED = [("SGKN_M0005", D1, 5, decimal.Decimal("7.00"))]
class Cur:
    def __init__(s): s.q=[]; s.deleted=[]; s._r=[]
    def execute(s, sql, params=None):
        s.q.append((sql, params)); u=" ".join(sql.split()).upper()
        if "OBJECT_ID" in u: s._r=[(1,)]
        elif "OUTPUT INSERTED.RUN_ID" in u: s._r=[("g",42)]
        elif u.startswith("SELECT NETS_TERMINAL_NO"): s._r=list(STORED)
        elif "DELETE FROM DBO.NETS_TRANSACTION" in u: s.deleted.append(params); s._r=[]
        else: s._r=[]
    def fetchone(s): return s._r[0] if s._r else None
    def fetchall(s): return s._r
CUR = Cur()
class Conn:
    def cursor(s): return CUR
args = types.SimpleNamespace(dry_run=False, force=False, include_today=True)
bad = [{"terminal":"SGKN_M0005","outlet":"X","date":D1,"ts":None,"raw_time":"t",
        "raw_status":"Weird","raw_scheme":None,"raw_amount":"0","amount":ZERO,
        "reason":"UNMAPPED_STATUS"}]
rows = [{"terminal":"SGKN_M0028","outlet":"Y","ts":dt.datetime(2026,9,1,10),"date":D1,
         "status":0,"scheme":"NETS","amount":decimal.Decimal("1.40"),"card_hash":None}]
rs, counts, un = ap.load(Conn(), rows, [D1], args, last_full=None, bad=bad,
                         full_window=[D1], degraded=True)
flat=[str(x) for p in CUR.deleted for x in (p or ())]
assert counts.get("PURGED_VANISHED",0)==0, "purge fired on a quarantined day"
assert counts.get("SKIPPED_SHRINK",0)==1, "expected SKIPPED_SHRINK"
assert "SGKN_M0005" not in flat, "quarantined machine-day was DELETEd"
upd=[q for q,_ in CUR.q if "UPDATE dbo.NETS_Pull_Run SET Status=" in q]
et=[p for q,p in CUR.q if "Error_Text=%s, Finished_At_UTC" in q and "Status='SUCCESS'" in q]
assert any("DEGRADED:" in str(p[0]) for p in et), "Error_Text not marked DEGRADED"
assert all("Status='SUCCESS'" in q for q in upd), "wrote a Status the CHECK rejects"
print("PASS t_load: no delete of quarantined day; Status=SUCCESS; Error_Text=DEGRADED")
