import datetime as dt, decimal, os, sys
sys.path.insert(0,"."); import auresys_pull as ap
os.environ.update(AURESYS_USER="u", AURESYS_PASSWORD="p", NETS_CARD_PEPPER="x")
def row(day,hh,status="Success",amt="1.40"):
    return {"vmsID":"T1","outletName":"Ubi","time":"%s %02d:00:00"%(day,hh),
            "dispenseStatus":status,"paymentType":"NETS","amount":amt,"cardNo":"****"}
ap.login=lambda *a,**k:None; ap.open_report_page=lambda *a,**k:("t",["T1"])
ap.nets_mapping.known_terminals=lambda:{"T1"}; ap.nets_mapping.resolve=lambda t:("M1","Ubi")
def scenario(label, rows_fn, total):
    def fake(se,to,te,day): return rows_fn(day), len(rows_fn(day)), decimal.Decimal(total)
    ap.fetch_day=fake; sys.argv=["x","--days","1","--dry-run"]
    print("\n--- %s"%label)
    try: ap.main()
    except SystemExit as e: print("exit:",e.code)
scenario("unmapped status, totalAmount EXCLUDES it",
         lambda d:[row(d,9),row(d,10),row(d,11,"Mystery","5.00")], "2.80")
scenario("unmapped status, totalAmount INCLUDES it",
         lambda d:[row(d,9),row(d,10),row(d,11,"Mystery","5.00")], "7.80")
scenario("genuine mismatch - neither fits",
         lambda d:[row(d,9),row(d,10),row(d,11,"Mystery","5.00")], "9.99")
scenario("unparseable amount - day cannot be reconciled",
         lambda d:[row(d,9),row(d,10,"Success","N/A")], "2.80")
