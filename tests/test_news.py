"""Zprávy o tvých akcích jen česky + Přehled pro větší portfolio (historie výkonu, události) + přepínač koše."""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import _boot
import index as m, kvfake, _fakemarket
m._kv_cmd = kvfake.kv; m.cloud_enabled = lambda: True
scan = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "scan.json"))); scan["date"] = m._today_iso()
_fakemarket.install(m, scan)
ok = bad = 0
def chk(n, c, info=""):
    global ok, bad
    ok += bool(c); bad += (not c); print(("  OK   " if c else "  FAIL ") + n + (f"  [{info}]" if info and not c else ""))
c = m.app.test_client(); J = lambda r: r.get_json(silent=True) or {}

print("== detekce angličtiny ==")
chk("anglická věta", m._looks_english("Apple said it will report results after the market closes on Thursday"))
chk("česká věta (s diakritikou)", not m._looks_english("Apple oznámil výsledky, tržby vzrostly o deset procent"))
chk("česká věta bez diakritiky", not m._looks_english("Apple ma silne vysledky a trzby rostou"))
chk("krátký název/ticker", not m._looks_english("NVDA") and not m._looks_english("Apple Inc."))
chk("prázdné", not m._looks_english("") and not m._looks_english(None))

print("== _mine_compute: anglické shrnutí se nezobrazí ==")
class R:
    def __init__(s, j): s._j = j
    def json(s): return s._j
def news(url, **k):
    t = "NVDA"
    return R({"news": [{"title": "NVDA signs a big supply deal for AI chips", "publisher": "Test", "link": "http://x/1", "providerPublishTime": int(time.time()) - 3600}]})
m.requests.get = news
m.analysis_enabled = lambda: True
m._name_keys = lambda t, n: {"nvda", "nvidia"}
def llm(kind):
    def f(prompt, *a, **k):
        if kind == "en":
            return {"items": [{"ticker": "NVDA", "what": "NVIDIA said it will supply the chips to the customer after the deal", "why": "This is important for the company and its growth", "impact": "pozitivní", "severe": False, "sources": ["m1"]}]}
        if kind == "cs":
            return {"items": [{"ticker": "NVDA", "what": "NVIDIA uzavřela dodávku čipů pro AI.", "why": "Je to důležité pro tržby firmy.", "impact": "pozitivní", "severe": False, "sources": ["m1"]}]}
        return None
    return f
m.call_llm = llm("en"); r = m._mine_compute(["NVDA"], {})
chk("anglická odpověď AI: žádná položka a ai=False (UI nezobrazí angličtinu)", r["items"] == [] and r["ai"] is False, str(r)[:200])
m.call_llm = llm("cs"); r = m._mine_compute(["NVDA"], {})
chk("česká odpověď AI se zobrazí", len(r["items"]) == 1 and r["ai"] is True and "čipů" in r["items"][0]["what"], str(r)[:200])
m.call_llm = llm("none"); r = m._mine_compute(["NVDA"], {})
chk("AI nedostupná: ai=False, titulky zůstanou jen v datech", r["ai"] is False and len(r["headlines"]) == 1)
prompt_seen = []
m.call_llm = lambda p, *a, **k: (prompt_seen.append(p), None)[1]; m._mine_compute(["NVDA"], {})
chk("prompt vyžaduje češtinu", prompt_seen and "ČESKY" in prompt_seen[0])
src = open(os.path.join(os.path.dirname(_boot.HERE), "assets", "js", "10-briefing-kos-tipu.js"), encoding="utf-8").read()
mine = src[src.index("function dgMineHTML"):src.index("function renderDigestModal")]
chk("briefing: anglické titulky u „Tvoje akcie ve zprávách“ se už nevypisují", "h.title" not in mine and "dgRetryMine" in mine)

print("== Přehled s 35 akciemi ==")
tk = [t for t in list(scan["px_map"])[:35]]
d = J(c.get("/api/portfolio-history?days=30&tickers=" + ",".join(tk)))
chk("historie výkonu: všech 35 titulů (limit byl 30)", d.get("ok") and d.get("tickers_used") == 35 and d.get("series"), str({k: d.get(k) for k in ("ok", "tickers_used")}))
t0 = time.time(); ev = J(c.get("/api/alerts/events?days=10&tickers=" + ",".join(tk + ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"])))
chk("události: 40+ titulů nespadne", ev.get("ok") and isinstance(ev.get("events"), list), str(ev)[:150])
print(f"  ({time.time() - t0:.1f} s)")

print("== Přepínač koše: okamžitá odezva ==")
js = open(os.path.join(os.path.dirname(_boot.HERE), "assets", "js", "10-briefing-kos-tipu.js"), encoding="utf-8").read()
fn = js[js.index("function planSetPicks"):js.index("// ---------- koš tipů: jediný seznam")]
chk("planSetPicks hned ořízne koš a označí přepočet", "loading: true" in fn and "slice(0, planPicksN)" in fn)
chk("tlačítka ukazují stav přepočtu", "přepočítávám" in js)
print(f"\nVÝSLEDEK: {ok} OK, {bad} chyb")
sys.exit(1 if bad else 0)
