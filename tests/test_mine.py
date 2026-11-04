import sys, os, json, time, copy
os.environ["SITE_LOCK"]="off"; os.environ["DIGEST_SEND"]="on"; os.environ["BREVO_API_KEY"]="x"; os.environ["SENDER_EMAIL"]="a@b.c"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import _boot
import index as m, kvfake
m._kv_cmd=kvfake.kv; m.cloud_enabled=lambda: True
S=kvfake.store
scan=json.load(open(os.path.join(os.path.dirname(__file__),"scan.json"))); today=m._today_iso(); scan["date"]=today
import _fakemarket; _fakemarket.install(m, scan) if not _boot.LIVE else None
S[m._pk(f"scan:{today}")]=json.dumps(scan); S[m._pk("scan:last")]=json.dumps(scan)
ok=bad=0
def chk(n,c,info=""):
    global ok,bad
    ok+=bool(c); bad+=(not c); print(("  OK   " if c else "  FAIL ")+n+(f"  [{info}]" if info and not c else ""))
# ceny pod kontrolou
PX={"SPY":500.0}
def fake_quotes(ts):
    out={}
    for t in ts:
        k=m._spx_key(t); p=PX.get(t) or (scan["px_map"].get(k) or [None])[0]
        if p: out[t]={"price":p,"currency":"USD","name":(scan.get("name_map") or {}).get(k,t),"type":"EQUITY","chg":0.5}
    return out
m._quotes=fake_quotes
m._red_flags=lambda t: {}
sent=[]; m.send_email=lambda to,sub,html: (sent.append((to,sub)), True)[1]

print("== koš měsíce ==")
sc=m._ensure_scan_maps(dict(scan))
b1=m._month_basket(sc); chk("koš se zmrazí (30 kandidátů, ceny, SPY)", b1 and len(b1["order"])==30 and b1["spy"]==500.0 and all(v for v in b1["px"].values()), str(b1)[:150])
sc2=copy.deepcopy(sc); sc2["mom_rank_map"]={k:i+1 for i,k in enumerate(reversed(list(sc["mom_rank_map"])))}
p1=m._picks(sc,10,set()); p2=m._picks(sc2,10,set())
chk("tipy během měsíce stejné, i když se pořadí změní", [x["ticker"] for x in p1["basket"]]==[x["ticker"] for x in p2["basket"]] and p1["since"]==today)
chk("6 tipů = prvních 6 z koše", [x["ticker"] for x in m._picks(sc,6,set())["basket"]]==[x["ticker"] for x in p1["basket"]][:6])
print("== výkon koše ==")
order=[k for k,_ in b1["order"][:10]]
paths={k.replace(".","-"):[] for k in order}; paths["SPY"]=[]
prices={k.replace(".","-"): b1["px"][k]*1.10 for k in order}; prices["SPY"]=510.0
m._price_paths=lambda tks, rng="1y": ({t:prices.get(t) for t in tks},{t:paths.get(t,[]) for t in tks})
bp=m._basket_perf(10); chk("koš +10 % vs SPY +2 %", bp and abs(bp["ret"]-10.0)<0.01 and abs(bp["spy"]-2.0)<0.01, str(bp))
print("== výkon mých akcií + velikost + sektory ==")
EM="qa@x"; m.kv_set_json(f"user:{EM}",{"email":EM,"plan":"elite","notif":{"alerts":True,"morning":False}})
S.setdefault(m._pk("users"), set()).add(EM)
rows=[{"ticker":"AAPL","price":100.0,"kind":"stock","sector":"Information Technology","size":"L"},
      {"ticker":"MSFT","price":200.0,"kind":"stock","sector":"Information Technology","size":None},
      {"ticker":"XOM","price":50.0,"kind":"stock","sector":"Energy","size":"S"}]
pf=m._holdings_perf(EM,rows); chk("první volání: start 0 %", pf["n"]==3 and pf["ret"]==0.0)
PX["SPY"]=505.0; S.pop(m._pk("spy:px"),None); rows[0]["price"]=110.0; rows[1]["price"]=200.0; rows[2]["price"]=45.0
pf=m._holdings_perf(EM,rows); exp=(4*10+2*0+1*(-10))/7
chk(f"vážený výnos podle velikostí ({exp:.2f} %) vs SPY +1 %", abs(pf["ret"]-exp)<0.01 and abs(pf["spy"]-1.0)<0.01 and pf["weighted"], str(pf))
aw,sized=m._approx_weights(rows); chk("odhad vah: velká 4/7", sized and abs(aw["AAPL"]-4/7)<1e-9)
sm=m._sector_mix(rows, sc); chk("sektory: technologie 6/7 → varování", sm["items"][0]["sector"]=="Information Technology" and sm["warn"] and "Technologie" in sm["warn"], str(sm))
print("== Evropa ==")
fake={t:{"price":10+i,"currency":"EUR","name":("SAP SE" if t=="SAP.DE" else t+" AG"),"mom":100-i*0.5,"chg":0.1} for i,t in enumerate(m.EU_UNIVERSE)}
m._eu_one=lambda t:(t,fake.get(t))
eu=m._eu_scan(budget_s=30); chk("žebříček Evropy", eu and eu["n"]==len(m.EU_UNIVERSE) and eu["rows"]["SAP.DE"]["rank"]==1)
k,r_=m._eu_match({"ticker":"SAP","name":"SAP SE"},eu); chk("SAP (NYSE) ↔ SAP.DE podle jména", k=="SAP.DE")
print("== /api/plan s uživatelem ==")
m._auth_user=lambda: EM
c=m.app.test_client()
hold=[{"ticker":"AAPL","qty":5,"name":"Apple","size":"L"},{"ticker":"AAPL","qty":2,"buy_month":"2024-01","name":"Apple"},{"ticker":"XOM","qty":None,"name":"Exxon"},{"ticker":"SAP","qty":None,"name":"SAP SE"},{"ticker":"TSLA","qty":None,"name":"Tesla"}]
d=c.post("/api/plan",json={"holdings":hold,"risk":"maximum","picks_n":10}).get_json()
chk("plan: ok, kusy ignorované, 1 řádek na titul", d["ok"] and d["total_czk"]==0 and sorted(r["ticker"] for r in d["rows"])==["AAPL","SAP","TSLA","XOM"], str([r["ticker"] for r in d.get("rows",[])]))
ev={e["ticker"]:e for e in d["evaluation"]["items"]}
chk("SAP hodnocená evropským žebříčkem", ev["SAP"].get("source")=="eu" and ev["SAP"]["level"]=="strong", str(ev["SAP"]))
chk("velikost se drží (AAPL velká)", ev["AAPL"]["size"]=="L" and ev["AAPL"]["approx_weight"]>=40, str(ev["AAPL"]))
chk("koncentrace varuje u velké pozice", any("Velká pozice" in w for w in ev["AAPL"]["warn"]))
chk("odpověď má perf, basket_perf, sectors, changes", all(k in d for k in ("perf","basket_perf","sectors","changes")) and d["picks"]["since"]==today)
print("== změny od včera ==")
hist=m.kv_get_json(f"evalhist:{EM}"); y="2000-01-01"
def snap(**kw): return {t:dict(v) for t,v in hist[today].items()}
prev=snap()
prev["TSLA"].update(l="strong",v="Silná – drž",r=10,b=0)          # dva stupně dolů → hlásit
prev["AAPL"].update(l="hold",v="Drž",r=None)                         # AAPL dnes? podle skutečného pořadí
hist={y:prev, today:hist[today]}; m.kv_set_json(f"evalhist:{EM}",hist)
ch=m._eval_changes(EM,d["evaluation"]); txt=[x["text"] for x in ch["items"]]
chk("TSLA silná → slabší nahlášena", any(x.startswith("TSLA:") for x in txt) and ch["since"]==y, str(txt))
# hystereze: změna drž→slabší těsně u hranice se nehlásí
n_=d["evaluation"]["items"][0]["of"]
evf={"items":[{"ticker":"ZZ","level":"weak","verdict":"Slabší – nedokupuj","rank":int(n_*0.51),"of":n_,"in_basket":False,"source":None}]}
m.kv_set_json(f"evalhist:{EM}",{y:{"ZZ":{"l":"hold","v":"Drž","r":int(n_*0.49),"of":n_,"b":0,"src":"spx"}}})
chk("šum u hranice (49 % → 51 %) se nehlásí", m._eval_changes(EM,evf,record=False)["items"]==[])
evf["items"][0]["rank"]=int(n_*0.70)
chk("skutečný pokles (49 % → 70 %) se hlásí", len(m._eval_changes(EM,evf,record=False)["items"])==1)
print("== upozornění e-mailem ==")
m.kv_set_json(f"holdings:{EM}",{"holdings":hold,"risk":"maximum"})
m.kv_set_json(f"evalhist:{EM}",{y:prev})
r_=m._user_daily(EM); chk("zhoršení → e-mail odeslán (upozornění zapnutá, ranní vypnutý)", r_["alert_sent"] and sent and "Změna hodnocení" in sent[-1][1], str(r_))
n0=len(sent); m.kv_set_json(f"evalhist:{EM}",{y:prev}); m._user_daily(EM); chk("podruhé ten den už ne", len(sent)==n0)
m.kv_set_json(f"user:{EM}",{"email":EM,"plan":"elite","notif":{"alerts":True,"morning":True}})
S.pop(m._pk(f"alert:sent:{EM}:{today}"),None); m.kv_set_json(f"evalhist:{EM}",{y:prev}); m._user_daily(EM)
chk("s ranním souhrnem se zvlášť neposílá", len(sent)==n0)
print("== ranní e-mail ==")
m.kv_set_json(f"holdings:{EM}",{"holdings":hold,"risk":"maximum","deposit_month":m.datetime.now(m.timezone.utc).strftime("%Y-%m")}); m.kv_set_json(f"evalhist:{EM}",{y:prev})
html,action=m._portfolio_email_html(EM)
chk("e-mail: Co se změnilo od včera + výkon", "Co se změnilo od včera" in html and "TSLA" in html and "Tvoje akcie od" in html, html[:200])
chk("věta dne mluví o zhoršení", action and "TSLA se zhoršila" in action, str(action))
print("== notifikace: alerts se nesmaže uložením ranního ==")
c.post("/api/notifications",json={"morning":False,"weekly":True})
chk("alerts zůstává", (m.kv_get_json(f"user:{EM}") or {})["notif"]["alerts"] is True)
print(f"\nVÝSLEDEK: {ok} OK, {bad} chyb")

sys.exit(1 if bad else 0)
