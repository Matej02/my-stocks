"""Úpravy z kontroly aplikace: dlouhodobé hodnocení v detailu akcie, P/E a kapitalizace ze skenu, zprávy jen o firmě,
natažená akcie v koši bez „přikupuj“, čistší vyhledávání a větší dotykové plochy na mobilu."""
import sys, os, json, re
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import _boot
import index as m, kvfake, _fakemarket
m._kv_cmd = kvfake.kv; m.cloud_enabled = lambda: True
scan = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "scan.json"))); scan["date"] = m._today_iso()
_fakemarket.install(m, scan)
kvfake.store[m._pk(f"scan:{m._today_iso()}")] = json.dumps(scan); kvfake.store[m._pk("scan:last")] = json.dumps(scan)
ok = bad = 0
def chk(n, c, info=""):
    global ok, bad
    ok += bool(c); bad += (not c); print(("  OK   " if c else "  FAIL ")+ n + (f"  [{info}]" if info and not c else ""))
c = m.app.test_client(); J = lambda r: r.get_json(silent=True) or {}
m.fetch_fundamentals = lambda t: {"name": "Micron Technology", "pe_ratio": None, "market_cap": None}
m.gather_facts = lambda t: {"ticker": t, "price": 100.0}
m.compute_verdict_model = lambda f: {"verdict": "Držet", "score": 50}
m.compute_levels = lambda f, h: {}

print("== detail akcie ==")
d = J(c.get("/api/stock/MU?full=1"))
lt = d.get("longterm") or {}
chk("detail vrací dlouhodobé hodnocení (stejné jako v portfoliu)", lt.get("level") in ("strong", "hold", "weak") and lt.get("action") and lt.get("rank"), str(lt)[:200])
ev = m._evaluate_holdings([{"ticker": "MU", "kind": "stock", "name": "MU"}], m._ensure_scan_maps(dict(scan)),
                          {m._spx_key(k) for k, _s in (m._month_basket(m._ensure_scan_maps(dict(scan))) or {}).get("order", [])[:10]})["items"][0]
chk("…a shoduje se s hodnocením v portfoliu", (lt.get("action"), lt.get("verdict")) == (ev["action"], ev["verdict"]), f"{lt.get('action')} vs {ev['action']}")
chk("P/E ze skenu, když zdroj fundamentů mlčí", d.get("pe_ratio") == scan["pe_map"]["MU"], str(d.get("pe_ratio")))
chk("kapitalizace ze skenu (v dolarech)", abs(float(d.get("market_cap")) - scan["mcap_map"]["MU"] * 1e6) < 1, str(d.get("market_cap")))
d2 = J(c.get("/api/stock/MU"))
chk("při změně období se hodnocení nepočítá (rychlost)", d2.get("longterm") is None)
chk("titul mimo S&P 500 → bez dlouhodobého hodnocení, nespadne", J(c.get("/api/stock/SAP.DE?full=1")).get("ok") is not False)

print("== natažená akce v koši ==")
sc = m._ensure_scan_maps(dict(scan))
basket = {m._spx_key(k) for k, _s in (m._month_basket(sc) or {}).get("order", [])[:10]}
st = [t for t in basket if (sc["mom_map"].get(t) or 0) >= 100 and ((sc["pe_map"].get(t) or 0) >= 40 or not sc["pe_map"].get(t))]
if st:
    e = m._evaluate_holdings([{"ticker": st[0], "kind": "stock", "name": st[0]}], sc, basket)["items"][0]
    chk(f"{st[0]}: natažená v koši = „Silná – drž“, ne „přikupuj“", e["action"] == "stretched" and "přikupuj" not in e["verdict"], e["verdict"])
else:
    print("  (v testovacím skenu není natažená akcie v koši – přeskočeno)")

print("== zprávy v detailu jen o firmě ==")
class R:
    def __init__(s, j): s._j = j
    def json(s): return s._j
news = [{"title": "Micron jumps as chip stocks rebound", "link": "http://a", "publisher": "X", "providerPublishTime": 1},
        {"title": "5 Best Stocks To Buy According to a Billionaire", "link": "http://b", "publisher": "Y", "providerPublishTime": 2},
        {"title": "Sweetgreen Is Finally Growing Again", "link": "http://c", "publisher": "Z", "providerPublishTime": 3},
        {"title": "MU earnings preview: what to expect", "link": "http://d", "publisher": "Z", "providerPublishTime": 4}]
m.requests.get = lambda url, **k: R({"news": news}) if "finance/search" in url else (_ for _ in ()).throw(Exception("x"))
m._fetch_rss = lambda key: []
items = J(c.get("/api/news-feed?ticker=MU&limit=10")).get("items") or []
titles = [i["title"] for i in items]
chk("zůstanou jen zprávy o Micronu", titles and all("Micron" in t or "MU" in t for t in titles) and len(titles) == 2, str(titles))

print("== frontend ==")
root = os.path.dirname(_boot.HERE)
js = "".join(open(os.path.join(root, "assets", "js", f), encoding="utf-8").read() for f in sorted(os.listdir(os.path.join(root, "assets", "js"))))
css = open(os.path.join(root, "assets", "app.css"), encoding="utf-8").read()
html = open(os.path.join(root, "index.html"), encoding="utf-8").read()
chk("detail: blok „Dlouhodobě“ a krátkodobý pohled přejmenovaný", 'id="lt-box"' in html and "Krátkodobý pohled (~2 týdny)" in html and "function renderLongTerm" in js)
chk("detail: technika popisuje stav, ne pokyn (žádné „SILNĚ PRODÁVAT“ na obrazovce)", "Technicky velmi slabá" in js and "<span>Slabá</span><span>Neutrální</span><span>Silná</span>" in html)
chk("detail: prázdné ukazatele (N/A) se skrývají", "metric-box" in js and "N\\/A|—|--|NaN" in js)
chk("vyhledávání: bez termínových kontraktů na akcie, akcie nahoře", "[A-Z]{1,3}=F$" in js and "EQUITY: 0" in js)
chk("mobil: větší tlačítka velikosti, křížku a rychlých částek", all(x in css for x in (".mp-size button {min-height:36px", ".mp-stock-h .mp-icon-btn {width:38px", ".mp-quick button {min-height:38px")))
chk("mobil: záložky detailu drží pod tlačítkem Zpět", ".detail-tabs {position:sticky; top:60px" in css)
print(f"\nVÝSLEDEK: {ok} OK, {bad} chyb")
sys.exit(1 if bad else 0)
