# live-only: potřebuje živý Yahoo (hledání názvů akcií) – spouští se jen s MA_TESTS_LIVE=1
import sys, os, json
os.environ["SITE_LOCK"]="off"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import _boot
import index as m, kvfake
m._kv_cmd=kvfake.kv; m.cloud_enabled=lambda: True
scan=json.load(open(os.path.join(os.path.dirname(__file__),"scan.json")))
store=kvfake.store
store[m._pk(f"scan:{m._today_iso()}")]=json.dumps(scan); store[m._pk("scan:last")]=json.dumps(scan)
EM="qa@x"; m._auth_user=lambda: EM
c=m.app.test_client(); ok=bad=0
def chk(n,cond,info=""):
    global ok,bad
    ok+=bool(cond); bad+=(not cond); print(("  OK   " if cond else "  FAIL ")+n+(f"  [{info}]" if info and not cond else ""))
J=lambda r: r.get_json(silent=True) or {}
for u in ("/api/signals","/api/sectors","/api/market-news","/api/search?q=apple","/api/watchlist?tickers=AAPL,MSFT"):
    r=c.get(u); chk(u, r.status_code==200 and J(r).get("ok") is not False, f"{r.status_code} {J(r).get('error')}")
r=J(c.post("/api/holdings/import", json={"text":"Apple\nNVDA 12\nMicrosoft 3,5 ks\nExxon Mobil\nSXR8"}))
chk("import textem (živý Yahoo)", r.get("ok") and r["read"]==5 and all(p.get("ticker") for p in r["positions"]), str([p.get("ticker") for p in r.get("positions",[])]))
lots=[{"ticker":p["ticker"],"qty":p.get("qty"),"name":p["name"]} for p in r["positions"]]
chk("uložení", J(c.post("/api/holdings", json={"holdings":lots,"risk":"maximum"})).get("count")==len(lots))
for risk in ("maximum","conservative","balanced","aggressive"):
    q=J(c.post("/api/plan", json={"holdings":lots,"risk":risk,"amount_czk":12000,"picks_n":10}))
    chk(f"plán {risk}", q.get("ok") and q["deposit"]["amount_czk"]==12000 and sum(l["amount_czk"] for l in q["deposit"]["lines"])==12000, q.get("error"))
q=J(c.post("/api/plan", json={"holdings":lots,"risk":"maximum","amount_czk":12000,"picks_n":6}))
chk("koš 6 + hodnocení", len(q["picks"]["basket"])==6 and len(q["evaluation"]["items"])==len(lots))
nq=J(c.post("/api/plan", json={"holdings":[{"ticker":"AAPL","qty":None},{"ticker":"MSFT","qty":None}],"risk":"maximum","amount_czk":5000}))
chk("jen názvy", nq.get("ok") and len(nq["evaluation"]["items"])==2)
# Yahoo/FX mimo provoz → akcie se nezahodí
real_q, real_fx = m._quotes, m._fx_czk
m._quotes=lambda ts: {}
q=J(c.post("/api/plan", json={"holdings":lots,"risk":"maximum"}))
chk("Yahoo mimo provoz: pozice zůstanou", q.get("ok") and len(q["rows"])==len(lots), str(len(q.get("rows",[]))))
m._quotes=real_q
chk("AI endpointy bez klíče", J(c.post("/api/ai/chat", json={"q":"ahoj"})).get("error") is not None)
print(f"\nVÝSLEDEK: {ok} OK, {bad} chyb")

sys.exit(1 if bad else 0)
