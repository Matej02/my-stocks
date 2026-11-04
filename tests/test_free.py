import sys, os, json, time, re
os.environ["SITE_LOCK"]="off"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import _boot
import index as m, kvfake, requests
m._kv_cmd=kvfake.kv; m.cloud_enabled=lambda: True
ok=bad=0
def chk(n,c,info=""):
    global ok,bad
    ok+=bool(c); bad+=(not c); print(("  OK   " if c else "  FAIL ")+n+(f"  [{info}]" if info and not c else ""))
class R:
    def __init__(s,code=200,body=None,text=None): s.status_code=code; s._b=body; s.text=text if text is not None else json.dumps(body); s.content=s.text.encode(); s.headers={}
    def json(s): return s._b
real_get=requests.get
def install(fn): m.requests.get=fn
today=m._today_iso()
print("== Yahoo → záloha Nasdaq ==")
rows=[{"date":f"10/{d:02d}/2026","close":f"${100+d}.5","volume":"1,000,000","open":f"${100+d}","high":f"${101+d}","low":f"${99+d}"} for d in (30,29,28,27)]
calls=[]
def get1(url,*a,**k):
    calls.append(url.split("/")[2]+("1" if "query1" in url else "2" if "query2" in url else ""))
    if "finance.yahoo.com" in url: raise requests.exceptions.ConnectionError("pad")
    if "api.nasdaq.com/api/quote" in url: return R(200,{"data":{"tradesTable":{"rows":rows}}})
    return R(404,{})
install(get1)
r=m.yahoo_chart("AAPL","1mo","1d")
chk("Yahoo padá → Nasdaq: tvar výsledku", r["meta"]["regularMarketPrice"]==130.5 and r["meta"]["chartPreviousClose"]==129.5 and r["timestamp"]==sorted(r["timestamp"]) and r["indicators"]["quote"][0]["close"][-1]==130.5, str(r["meta"]))
chk("zkusil oba hostitele Yahoo před zálohou", calls[:2]==["query.finance.yahoo.com1".replace("query.","query1.")[:0] or calls[0], calls[1]] and any("1" in c for c in calls) and any(c.endswith("2") for c in calls) and calls[-1]=="api.nasdaq.com", str(calls))
chk("poslední svíčka je nejnovější", r["timestamp"][-1]>r["timestamp"][0])
for t,rg,iv,exp in (("SAP.DE","1y","1d","evropský"),("AAPL","1y","1wk","týdenní"),("AAPL","max","1d","nepodporovaný rozsah")):
    try: m.yahoo_chart(t,rg,iv); chk(f"{exp} ticker bez zálohy → chyba", False)
    except Exception: chk(f"{exp} bez zálohy → chyba (Yahoo výsledek zachován)", True)
def get2(url,*a,**k):
    if "finance.yahoo.com" in url: return R(200,{"chart":{"result":None,"error":{"code":"Not Found","description":"x"}}})
    calls.append(url); return R(200,{"data":{"tradesTable":{"rows":rows}}})
install(get2); calls.clear()
try: m.yahoo_chart("ZZZZ","1y","1d"); chk("neexistující ticker", False)
except Exception: chk("překlep v tickeru: Nasdaq se nevolá a nespustí pauzu", not calls and m._nq_fail["ts"]==0)
def get3(url,*a,**k):
    if "finance.yahoo.com" in url: raise requests.exceptions.ConnectionError("pad")
    raise requests.exceptions.ConnectionError("pad2")
install(get3)
try: m.yahoo_chart("AAPL","1y","1d")
except Exception: pass
chk("síťová chyba Nasdaq → krátká pauza zálohy", m._nq_fail["ts"]>0)
m._nq_fail["ts"]=0
print("== ČNB kurzy ==")
CNB="03.11.2026 #211\nzemě|měna|množství|kód|kurz\nAustrálie|dolar|1|AUD|15,203\nEMU|euro|1|EUR|24,340\nJaponsko|jen|100|JPY|13,810\nUSA|dolar|1|USD|21,762\nVelká Británie|libra|1|GBP|29,205\nKanada|dolar|1|CAD|15,573\nDánsko|koruna|1|DKK|3,257\nŠvýcarsko|frank|1|CHF|26,565\nPolsko|zlotý|1|PLN|5,509\nŠvédsko|koruna|1|SEK|2,222\nNorsko|koruna|1|NOK|2,090\n"
n=[0]
def get4(url,*a,**k):
    if "cnb.cz" in url: n[0]+=1; return R(200,text=CNB)
    raise requests.exceptions.ConnectionError("Yahoo mimo")
install(get4); m._cnb_mem.update(ts=0,rates=None); kvfake.store.clear()
chk("USD/EUR z ČNB bez Yahoo", abs(m._fx_czk("USD")-21.762)<1e-9 and abs(m._fx_czk("EUR")-24.34)<1e-9)
chk("JPY (za 100) a pence", abs(m._fx_czk("JPY")-0.1381)<1e-9 and abs(m._fx_czk("GBp")-0.29205)<1e-9)
chk("nové měny (CAD, DKK, SEK) fungují", all(m._fx_czk(c) for c in ("CAD","DKK","SEK","NOK")))
chk("kurz se stahuje jednou (keš)", n[0]==1, str(n))
chk("neznámá měna → None", m._fx_czk("XXX") is None and m._fx_czk("CZK")==1.0)
m._cnb_mem.update(ts=0,rates=None); kvfake.store.clear()
install(lambda url,*a,**k: R(200,text="<html>chyba</html>") if "cnb.cz" in url else (_ for _ in ()).throw(requests.exceptions.ConnectionError("x")))
chk("rozbitá odpověď ČNB a Yahoo mimo → None", m._fx_czk("USD") is None)
print("== kalendář výsledků ==")
def cal(url,*a,**k):
    d=(k.get("params") or {}).get("date")
    if "calendar/earnings" in url:
        if d==(m.datetime.now(m.timezone.utc).date()+m.timedelta(days=2)).isoformat(): return R(200,{"data":{"rows":[{"symbol":"AAPL","time":"time-after-hours","epsForecast":"$1.85","noOfEsts":"22"},{"symbol":"BRK.B","time":"time-pre-market","epsForecast":"($0.50)","noOfEsts":"2"}]}})
        if d==(m.datetime.now(m.timezone.utc).date()+m.timedelta(days=12)).isoformat(): return R(200,{"data":{"rows":[{"symbol":"NVDA","time":"time-not-supplied","epsForecast":"$2.49","noOfEsts":"13"},{"symbol":"AAPL","time":"time-pre-market","epsForecast":"$9","noOfEsts":"1"}]}})
        return R(200,{"data":{"rows":[{"symbol":"ZZZ","time":"","epsForecast":""}] if d else []}})
    raise requests.exceptions.ConnectionError("x")
install(cal); kvfake.store.clear()
e=m._earnings_for(["AAPL","BRK-B","NVDA","SAP.DE","MSFT"])
chk("AAPL nejbližší výsledky za 2 dny po zavření, odhad 1,85", e["AAPL"]["days"]==2 and e["AAPL"]["when"]=="post" and e["AAPL"]["eps"]==1.85 and e["AAPL"]["n"]==22, str(e.get("AAPL")))
chk("BRK-B ↔ BRK.B, záporný odhad v závorkách", e["BRK-B"]["eps"]==-0.5 and e["BRK-B"]["when"]=="pre", str(e.get("BRK-B")))
chk("NVDA za 12 dní bez času; SAP.DE a MSFT nejsou", e["NVDA"]["days"]==12 and e["NVDA"]["when"] is None and "SAP.DE" not in e and "MSFT" not in e)
chk("kalendář kešovaný (druhé volání bez sítě)", (lambda: (install(lambda *a,**k:(_ for _ in ()).throw(AssertionError("síť"))), m._earnings_for(["AAPL"],fetch=False))[1])().get("AAPL") is not None)
kvfake.store.clear(); install(lambda *a,**k:(_ for _ in ()).throw(requests.exceptions.ConnectionError("x")))
chk("kalendář nedostupný → prázdno, nic nespadne", m._earnings_for(["AAPL"])=={} and not kvfake.store)
print("== /api/plan a e-mail s výsledky ==")
scan=json.load(open(os.path.join(os.path.dirname(__file__),"scan.json"))); scan["date"]=today
kvfake.store[m._pk(f"scan:{today}")]=json.dumps(scan); kvfake.store[m._pk("scan:last")]=json.dumps(scan)
install(cal)
m._quotes=lambda ts: {t:{"price":100.0,"currency":"USD","name":t,"type":"EQUITY","chg":0.1} for t in ts}; m._red_flags=lambda t:{}
EM="qa@x"; m._auth_user=lambda: EM
d=m.app.test_client().post("/api/plan",json={"holdings":[{"ticker":"AAPL"},{"ticker":"NVDA"},{"ticker":"MSFT"}],"risk":"maximum"}).get_json()
chk("/api/plan nese earnings pro držené", d["ok"] and d["earnings"].get("AAPL",{}).get("days")==2 and d["earnings"].get("NVDA",{}).get("days")==12, str(d.get("earnings")))
m.kv_set_json(f"holdings:{EM}",{"holdings":[{"ticker":"AAPL"},{"ticker":"NVDA"}],"risk":"maximum","deposit_month":m.datetime.now(m.timezone.utc).strftime("%Y-%m")})
html,_=m._portfolio_email_html(EM)
chk("e-mail: „Tento týden hlásí výsledky“ jen do 7 dní (AAPL ano, NVDA ne)", "Tento týden hlásí výsledky" in html and "AAPL" in html.split("Tento týden hlásí výsledky")[1][:200] and "NVDA" not in html.split("Tento týden hlásí výsledky")[1][:200])
print("== short interest (FINRA) ==")
hdr='"accountingYearMonthNumber"|"symbolCode"|"issueName"|"issuerServicesGroupExchangeCode"|"marketClassCode"|"currentShortPositionQuantity"|"previousShortPositionQuantity"|"stockSplitFlag"|"averageDailyVolumeQuantity"|"daysToCoverQuantity"|"revisionFlag"|"changePercent"|"changePreviousNumber"|"settlementDate"'
univ=list((json.load(open(os.path.join(os.path.dirname(__file__),"scan.json")))["name_map"]).keys())
lines=[hdr]
for i,t in enumerate(univ):
    dtc=1.0+ (i%100)/20.0          # 1.0 … 5.95 rovnoměrně
    lines.append(f'"20261015"|"{t.replace(".","")}"|"X"|"A"|"NYSE"|"1000"|"900"||"500000"|"{dtc:.2f}"||"1.0"|"10"|"2026-10-15"')
lines.append('"20261015"|"MEME"|"X"|"A"|"NYSE"|"1"|"1"||"900000"|"40.00"||"1"|"1"|"2026-10-15"')
lines.append('"20261015"|"THIN"|"X"|"A"|"NYSE"|"1"|"1"||"1000"|"90.00"||"1"|"1"|"2026-10-15"')
body="\n".join(lines)
def fin(url,*a,**k):
    if "cdn.finra.org" in url:
        return R(200,text=body) if url.endswith("shrt20261015.csv") else R(403,text="<xml/>")
    raise requests.exceptions.ConnectionError("x")
install(fin); kvfake.store.clear()
kvfake.store[m._pk(f"scan:{today}")]=json.dumps(scan)
si=m._si_latest()
chk("načten nejnovější soubor, pokrytí a hranice horní pětiny", si and si["settle"]=="2026-10-15" and len(si["map"])>400 and 4.5<=si["thr"]<=5.2, str(si and (si["settle"],len(si["map"]),si["thr"])))
fl=m._short_flags(["MEME","THIN","AAPL","MU"])
chk("MEME (40 dní) varování; THIN (nízký objem) a běžné ne", "MEME" in fl and "THIN" not in fl and "AAPL" not in fl and fl["MEME"]["dtc"]==40.0, str(fl))
install(lambda *a,**k:(_ for _ in ()).throw(AssertionError("síť")))
chk("z keše bez sítě (fetch=False)", "MEME" in m._short_flags(["MEME"], fetch=False))
kvfake.store.clear(); install(lambda *a,**k:(_ for _ in ()).throw(requests.exceptions.ConnectionError("x")))
chk("FINRA nedostupné → prázdno bez pádu", m._short_flags(["MEME"])=={} )
m.requests.get=real_get
print(f"\nVÝSLEDEK: {ok} OK, {bad} chyb")

sys.exit(1 if bad else 0)
