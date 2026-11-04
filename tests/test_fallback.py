import sys, os, json
os.environ["SITE_LOCK"]="off"; os.environ["ANALYSIS_API_KEY"]="g"; os.environ["GEMINI_API_KEY"]="gem"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import _boot
import index as m, kvfake
m._kv_cmd=kvfake.kv; m.cloud_enabled=lambda: True
ok=bad=0
def chk(n,c,info=""):
    global ok,bad
    ok+=bool(c); bad+=(not c); print(("  OK   " if c else "  FAIL ")+n+(f"  [{info}]" if info and not c else ""))
class R:
    def __init__(s,code,body=None,headers=None): s.status_code=code; s._b=body; s.headers=headers or {}; s.text=json.dumps(body)
    def json(s): return s._b
def good(t): return R(200,{"choices":[{"message":{"content":t}}]})
def get(url, **k):
    if "groq" in url: return R(200,{"data":[{"id":"openai/gpt-oss-120b"},{"id":"qwen/qwen3.8-27b"}]})
    if "generativelanguage" in url: return R(200,{"data":[{"id":"models/gemini-2.5-flash"},{"id":"models/gemini-flash-latest"},{"id":"models/gemini-2.5-flash-lite"},{"id":"models/text-embedding-004"},{"id":"models/gemini-2.5-flash-image"}]})
    if "cerebras" in url: return R(200,{"data":[{"id":"llama3.1-8b"},{"id":"gpt-oss-120b"}]})
    return R(404,{})
m.requests.get=get
calls=[]
def setpost(fn):
    calls.clear(); m._llm_cache.update(ts=0,models=None); m._vision_cache.update(ts=0,models=None); m._fb_cache.clear()
    def post(url, headers=None, data=None, timeout=None):
        p=json.loads(data); calls.append((url.split("/")[2], p["model"])); return fn(url,p)
    m.requests.post=post
setpost(lambda u,p: R(429,{"e":1},{"retry-after":"30"}) if "groq" in u else good('{"x":1}'))
r=m.call_llm("json"); chk("Groq limit → Gemini odpoví", r=={"x":1} and m._llm_state["model"]=="gemini:gemini-flash-latest", str(m._llm_state)+str(calls))
chk("Gemini model z /models (flash-latest, ne image/embedding)", m._fb_models(dict(m.AI_FALLBACKS[0],key="gem"))[0]=="gemini-flash-latest" and not any("image" in x or "embed" in x for x in m._fb_models(dict(m.AI_FALLBACKS[0],key="gem"))))
setpost(lambda u,p: R(429,{"e":1}) )
r=m.call_llm("json"); chk("vše vyčerpané → None, kind=limit", r is None and m._llm_state["kind"]=="limit")
del os.environ["ANALYSIS_API_KEY"]; del os.environ["GEMINI_API_KEY"]; os.environ["CEREBRAS_API_KEY"]="cb"
setpost(lambda u,p: good('{"y":2}'))
r=m.call_llm("json"); chk("bez Groq, jen Cerebras → funguje (gpt-oss-120b)", r=={"y":2} and calls[0]==("api.cerebras.ai","gpt-oss-120b"), str(calls))
chk("analysis_enabled se zálohou", m.analysis_enabled())
del os.environ["CEREBRAS_API_KEY"]; os.environ["ANALYSIS_API_KEY"]="g"; os.environ["GEMINI_API_KEY"]="gem"
setpost(lambda u,p: R(429,{"e":1},{"retry-after":"40"}) if "groq" in u else good('{"positions":[{"name":"Apple"}]}'))
d,e=m.call_vision("data:image/jpeg;base64,AA"); chk("čtení fotky: Groq limit → Gemini", d and d["positions"][0]["name"]=="Apple" and any("generativelanguage" in c[0] for c in calls), str(e))
setpost(lambda u,p: R(504,{"e":1}) if "groq" in u else good("Odpověď"))
chk("text: Groq výpadek → Gemini", m.call_llm("ahoj", json_mode=False)=="Odpověď")
print("== AI komentář ==")
scan=json.load(open(os.path.join(os.path.dirname(__file__),"scan.json"))); today=m._today_iso(); scan["date"]=today
S=kvfake.store; S[m._pk(f"scan:{today}")]=json.dumps(scan); S[m._pk("scan:last")]=json.dumps(scan)
m._quotes=lambda ts: {t:{"price":100.0,"currency":"USD","name":t,"type":"EQUITY","chg":0.1} for t in ts}
m._red_flags=lambda t:{}
EM="adm@x"; m._auth_user=lambda: EM; m.admin_users=lambda:{EM}
m.kv_set_json(f"holdings:{EM}",{"holdings":[{"ticker":"AAPL","qty":None},{"ticker":"XOM","qty":None}]})
c=m.app.test_client()
setpost(lambda u,p: good("XOM je silná a patří do koše. AAPL drž. Nové peníze dávej do tipů."))
d=c.post("/api/ai/portfolio-comment").get_json(); chk("komentář vytvořen", d["ok"] and "XOM" in (d.get("comment") or ""), str(d))
d=c.post("/api/ai/portfolio-comment").get_json(); chk("podruhé z keše", d.get("cached") is True)
S.clear(); S[m._pk(f"scan:{today}")]=json.dumps(scan); S[m._pk("scan:last")]=json.dumps(scan); m.kv_set_json(f"holdings:{EM}",{"holdings":[{"ticker":"TSLA","qty":None}]})
setpost(lambda u,p: good("TSLA je slabší, doporučuji ji prodat."))
d=c.post("/api/ai/portfolio-comment").get_json(); chk("rada „prodat“ → komentář zahozen", d["ok"] and d.get("comment") is None and d.get("dropped"))
setpost(lambda u,p: good("TSLA je slabší – neprodávej ji, jen ji nedokupuj."))
S.clear(); S[m._pk(f"scan:{today}")]=json.dumps(scan); S[m._pk("scan:last")]=json.dumps(scan); m.kv_set_json(f"holdings:{EM}",{"holdings":[{"ticker":"TSLA","qty":None}]})
d=c.post("/api/ai/portfolio-comment").get_json(); chk("„neprodávej“ projde", d.get("comment") and "neprodávej" in d["comment"])
print("== admin test ==")
m._auth_admin=lambda: EM
setpost(lambda u,p: good('{"ok":true,"slovo":"ahoj"}'))
d=c.post("/api/admin/ai-test").get_json()
fb={f["id"]:f for f in d["fallbacks"]}
chk("admin test: Gemini nastavený a odpověděl, ostatní ne", fb["gemini"]["configured"] and fb["gemini"]["test"]["ok"] and not fb["cerebras"]["configured"], str(d["fallbacks"])[:200])
print(f"\nVÝSLEDEK: {ok} OK, {bad} chyb")

sys.exit(1 if bad else 0)
