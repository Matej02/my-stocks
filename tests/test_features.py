"""Sloučené záložky, záznam rad aplikace ve Výsledcích, české titulky zpráv, varování „slabá rozvaha“ (SEC)."""
import sys, os, json, re, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import _boot
import index as m, kvfake, _fakemarket
m._kv_cmd = kvfake.kv; m.cloud_enabled = lambda: True
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
scan = json.load(open(os.path.join(HERE, "scan.json"))); scan["date"] = m._today_iso()
_fakemarket.install(m, scan)
kvfake.store[m._pk(f"scan:{m._today_iso()}")] = json.dumps(scan); kvfake.store[m._pk("scan:last")] = json.dumps(scan)
ok = bad = 0
def chk(n, c, info=""):
    global ok, bad
    ok += bool(c); bad += (not c); print(("  OK   " if c else "  FAIL ") + n + (f"  [{info}]" if info and not c else ""))
EM = "qa@x"; c = m.app.test_client(); J = lambda r: r.get_json(silent=True) or {}
txt = lambda h: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h)).strip()
m._red_flags = lambda tk: {}
html = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
js = "".join(open(os.path.join(ROOT, "assets", "js", f), encoding="utf-8").read() for f in sorted(os.listdir(os.path.join(ROOT, "assets", "js"))))

print("== sloučené záložky ==")
tabs = re.findall(r'id="dtab-([a-z]+)"', html)
chk("v navigaci 4 záložky (bez Doporučení)", tabs == ["overview", "portfolio", "opportunities", "results"], str(tabs))
chk("sekce Doporučení pryč, track record je ve Výsledcích", "section-ai" not in html and html.index('id="track-box"') > html.index('id="section-results"') and html.index('id="track-box"') < html.index("/section-results"))
chk("žádný kód nepřepíná na zrušenou záložku", "switchTab('ai')" not in html + js and "renderRecommendations" not in js)
chk("Moje portfolio ukazuje AI shrnutí (Elite) a Výsledky načítají track record i záznam rad", 'id="rec-ai"' in js and "loadTrackRecord();\n        loadAdviceRecord();" in js)

print("== záznam rad aplikace ==")
m.kv_set_json(f"user:{EM}", {"email": EM, "plan": "elite"})
rows = [{"ticker": "AAA", "price": 100.0, "currency": "USD"}, {"ticker": "BBB", "price": 50.0, "currency": "USD"}, {"ticker": "CCC", "price": 10.0, "currency": "EUR"}]
ev = {"items": [{"ticker": "AAA", "action": "buy"}, {"ticker": "BBB", "action": "risk"}, {"ticker": "CCC", "action": "hold"}]}
chk("zapíše podhodnocenou a problémovou, držené bez rady ne", m._log_advice(EM, ev, rows) == 2)
chk("stejná rada se do 30 dnů nezapíše znovu", m._log_advice(EM, ev, rows) == 0)
rec = m.kv_get_json(f"advice:{EM}")
for i in rec["items"]: i["d"] = (m.datetime.now(m.timezone.utc).date() - m.timedelta(days=45)).isoformat()
m.kv_set_json(f"advice:{EM}", rec)
chk("po 30 dnech se rada zapíše znovu (nová cena)", m._log_advice(EM, ev, rows) == 2 and len(m.kv_get_json(f"advice:{EM}")["items"]) == 4)
rec = m.kv_get_json(f"advice:{EM}")
m._auth_user = lambda: None
chk("záznam rad bez přihlášení → 401", c.get("/api/advice-record").status_code == 401)
m._auth_user = lambda: EM
for k in [k for k in kvfake.store if "advicerec" in str(k)]: del kvfake.store[k]
d = J(c.get("/api/advice-record"))
chk("výsledky rad: počet a skupiny buy/risk s průměrem a srovnáním se S&P", d.get("ok") and d["count"] >= 2 and {"buy", "risk"} <= set(d["groups"]) and all(k in d["groups"]["buy"] for k in ("n", "avg_ret", "avg_spy", "beat_pct", "avg_days")), str(d)[:300])
chk("položky mají cenu tehdy i dnes a výnos", all(x["price0"] and x["price1"] and x["ret"] is not None for x in d["items"]), str(d["items"][:1]))
rec = m.kv_get_json(f"advice:{EM}")
for i in rec["items"]: i["d"] = m._today_iso()
m.kv_set_json(f"advice:{EM}", rec)
for k in [k for k in kvfake.store if "advicerec" in str(k)]: del kvfake.store[k]
chk("čerstvé rady (do týdne) se do souhrnu nepočítají", J(c.get("/api/advice-record"))["groups"] == {})
m.kv_set_json(f"advice:{EM}", {"items": []})
for k in [k for k in kvfake.store if "advicerec" in str(k)]: del kvfake.store[k]
chk("bez rad: prázdný záznam, nespadne", J(c.get("/api/advice-record")).get("count") == 0)
# integrace: eval_changes opravdu loguje
m.kv_set_json(f"advice:{EM}", {"items": []})
m._eval_changes(EM, {"items": [{"ticker": "AAA", "level": "strong", "verdict": "x", "rank": 1, "of": 500, "action": "buy"}]}, rows=rows)
chk("_eval_changes(rows=…) zapíše radu", len(m.kv_get_json(f"advice:{EM}")["items"]) == 1)

print("== české titulky zpráv ==")
calls = []
def llm(prompt, *a, **k):
    calls.append(prompt)
    return {"t": {"1": "Micron roste díky čipům", "2": "Nvidia představila nový čip"}}
m.call_llm = llm; m.analysis_enabled = lambda: True
T = ["Micron jumps on chip demand", "Nvidia unveils new chip", "Česká akcie roste po výsledcích"]
r = J(c.post("/api/news-translate", json={"ticker": "MU", "titles": T}))
chk("anglické titulky přeloženy, české nechány", r["ok"] and r["t"].get(T[0]) == "Micron roste díky čipům" and T[2] not in r["t"], str(r))
chk("do AI šly jen anglické titulky", len(calls) == 1 and "Česká akcie" not in calls[0])
r2 = J(c.post("/api/news-translate", json={"ticker": "MU", "titles": T}))
chk("podruhé z uložených překladů (AI se nevolá)", len(calls) == 1 and r2["t"].get(T[1]) == "Nvidia představila nový čip")
m.call_llm = lambda p, *a, **k: {"t": {"1": "Apple said it will report results after the market closes"}}
r3 = J(c.post("/api/news-translate", json={"ticker": "AAPL", "titles": ["Apple earnings date"]}))
chk("anglická odpověď AI se zahodí", r3["ok"] and r3["t"] == {}, str(r3))
m.analysis_enabled = lambda: False
chk("bez AI: prázdné, ne chyba", J(c.post("/api/news-translate", json={"ticker": "X", "titles": ["Hello world news"]}))["t"] == {})
chk("prázdný vstup a nesmysl nespadnou", J(c.post("/api/news-translate", json={"titles": []}))["ok"] and c.post("/api/news-translate", data="x", content_type="text/plain").status_code == 200)
chk("frontend: seznam hned, překlad doplní a označí „přeloženo AI“", "translateNewsTitles(ticker, list)" in js and "přeloženo AI" in js and 'data-tr="1"' in js)

print("== slabá rozvaha (SEC) ==")
E = m._distress_eval
f, w = E({"ni": -50, "cfo": -100, "cash": 120, "eq": 300, "liab": 400, "assets": 700}); chk("pálí hotovost: vystačí na 14 měsíců", f and "14 měsíců" in " ".join(w), str(w))
f, w = E({"ni": -50, "cfo": -100, "cash": 400, "eq": 300}); chk("hotovosti dost (48 měsíců) = bez příznaku", not f)
f, w = E({"ni": -10, "eq": -5}); chk("záporný vlastní kapitál + ztráta", f and "záporný" in w[0])
f, w = E({"ni": 80, "eq": -500, "cfo": 200}); chk("zisková firma se záporným kapitálem (zpětné odkupy) NENÍ příznak", not f)
f, w = E({"ni": -5, "liab": 95, "assets": 100}, "Information Technology"); chk("dluhy 95 % majetku + ztráta", f and "95 %" in w[0])
f, w = E({"ni": -5, "liab": 95, "assets": 100}, "Financials"); chk("banka s dluhy 95 % majetku není příznak (pro ni normální)", not f)
f, w = E({"ni": -5, "liab": 95, "assets": 100}, None); chk("neznámé odvětví: pravidlo o dluhu se nepoužije", not f)
f, w = E({"cfo": -10, "cash": 1}); chk("bez čistého zisku (chybí data) = bez příznaku", not f)
class R:
    def __init__(s, j, code=200): s._j = j; s.status_code = code
    def json(s): return s._j
def units(val, end="2025-12-31", start="2025-01-01", instant=False):
    u = {"fp": "FY", "form": "10-K", "end": end, "val": val, "filed": "2026-02-20"}
    if not instant: u["start"] = start
    return {"units": {"USD": [{"fp": "Q1", "form": "10-Q", "end": "2025-03-31", "val": 1, "start": "2025-01-01"}, u]}}
SEC = {"NetIncomeLoss": units(-80), "NetCashProvidedByUsedInOperatingActivities": units(-120), "CashAndCashEquivalentsAtCarryingValue": units(100, instant=True),
       "StockholdersEquity": units(50, instant=True), "Liabilities": units(300, instant=True), "Assets": units(350, instant=True)}
def sec_get(url, **k):
    for con, j in SEC.items():
        if f"/{con}.json" in url: return R(j)
    return R({}, 404)
real_get = m.requests.get; m.requests.get = sec_get
m._cik_map = lambda: {"XYZ": 123}
d = m._distress_compute("XYZ", "Information Technology")
chk("SEC: z ročních výkazů (ne kvartálních) vyjde příznak s důvodem", d["flag"] and any("10 měsíců" in x for x in d["why"]), str(d))
SEC["NetIncomeLoss"] = units(-80, end="2022-12-31", start="2022-01-01")
chk("zastaralý výkaz (starší 20 měsíců) → bez dat, ne příznak", m._distress_compute("XYZ")["flag"] is False)
SEC["NetIncomeLoss"] = units(-80)
kvfake.store.pop(m._pk("distress:map"), None)
n = m._distress_warm(["XYZ", "XYZ"], budget_s=5, sectors={"XYZ": "Information Technology"})
chk("warm uloží do mapy (jednou, i když je ticker dvakrát)", n == 1 and m._distress_map()["XYZ"]["flag"])
chk("čerstvá data se znovu nestahují", m._distress_warm(["XYZ"], budget_s=5) == 0)
m.requests.get = real_get
r = {"ticker": "XYZ", "kind": "stock", "name": "XYZ"}; m._annotate_rows([r])
a, why = m._holding_action({"level": "strong", "mom_pct": 20, "pe": 20}, r, scan)
chk("držená akcie se slabou rozvahou = watch (ne dokup) s vysvětlením a poctivou poznámkou", a == "watch" and "Slabá rozvaha" in why and "7–12 %" in why and "prodej jen kvůli tomu data nepotvrzují" in why, why[:120])
chk("ve vkladu se slabá rozvaha nedokupuje", "watch" in open(os.path.join(ROOT, "api", "index.py"), encoding="utf-8").read().split("def mine_pool")[1][:600])

print("== vážný problém dorazí i do e-mailu (cesta bez _build_plan) ==")
mix = scan["mix_rank_map"]; order = sorted(mix, key=lambda t: mix[t]); holds = order[:5] + order[-5:]; bad_t = order[2]
m.kv_set_json(f"holdings:{EM}", {"holdings": [{"ticker": t, "qty": None, "name": t} for t in holds], "risk": "balanced"})
m._red_flags = lambda tk: {t: {"pause": True, "reasons": [{"reason": m.REDFLAG_PAUSE["1.03"]}]} for t in tk if m._spx_key(t) == bad_t}
ue = m._user_eval_saved(EM)
it = {i["ticker"]: i for i in ue["ev"]["items"]}
chk("_user_eval_saved doplní red flagy držených akcií → action risk", it[bad_t]["action"] == "risk", it[bad_t].get("action"))
m._distress_map = lambda: {m._spx_key(order[1]): {"flag": True, "why": ["záporný vlastní kapitál"], "ts": int(time.time())}}
mh = txt(m.build_morning_summary_html(EM, [], []))
chk("ranní e-mail: sekce Pozor i Slabá rozvaha", "Pozor – vážný problém" in mh and "Slabá rozvaha" in mh, mh[:300])
print(f"\nVÝSLEDEK: {ok} OK, {bad} chyb")
sys.exit(1 if bad else 0)
