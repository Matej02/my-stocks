import sys, os, json, time
os.environ["SITE_LOCK"]="off"; os.environ["ANALYSIS_API_KEY"]="k"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import _boot
import index as m, kvfake
m._kv_cmd=kvfake.kv; m.cloud_enabled=lambda: True
scan=json.load(open(os.path.join(os.path.dirname(__file__),"scan.json")))
m.market_scan=lambda allow_step=False, budget_s=None: scan
m._yahoo_search=lambda q,n=8: []; m._quotes=lambda ts: {}
ok=bad=0
def chk(name, cond, info=""):
    global ok,bad
    if cond: ok+=1
    else: bad+=1
    print(("  OK   " if cond else "  FAIL ")+name+(f"  [{info}]" if info and not cond else ""))
class R:
    def __init__(s,code,body=None,headers=None,text=None): s.status_code=code; s._b=body; s.headers=headers or {}; s.text=text if text is not None else json.dumps(body)
    def json(s): return s._b
def good(txt): return R(200,{"choices":[{"message":{"content":txt}}]})
IDS=["openai/gpt-oss-120b","qwen/qwen3.8-27b","openai/gpt-oss-20b","whisper-large-v3","meta-models/muse-spark-open"]
m.requests.get=lambda url,**k: R(200,{"data":[{"id":i,"active":True} for i in IDS]})
calls=[]
def setpost(fn):
    m._llm_cache.update(ts=0,models=None); m._vision_cache.update(ts=0,models=None); calls.clear()
    def post(url, headers=None, data=None, timeout=None):
        p=json.loads(data); calls.append(p); return fn(p)
    m.requests.post=post
sleeps=[]; m.time.sleep=lambda s: sleeps.append(round(s,1))

print("== call_llm ==")
setpost(lambda p: good('{"a":1}'))
chk("gpt-oss + low effort", m.call_llm("json")=={"a":1} and calls[0]["model"]=="openai/gpt-oss-120b" and calls[0].get("reasoning_effort")=="low")
big="x"*14000
setpost(lambda p: good('{"a":1}')); m.call_llm(big)
chk("max_tokens se zmenší u dlouhého promptu (limit 8k tokenů/min)", calls[0]["max_tokens"]<=7400-int(14000/2.7) and calls[0]["max_tokens"]>=700, calls[0]["max_tokens"])
setpost(lambda p: good('{"a":1}')); m.call_llm("krátký")
chk("krátký prompt → max_tokens 3500", calls[0]["max_tokens"]==3500)
setpost(lambda p: R(429,{"error":"rate"},headers={"retry-after":"12"}, text='{"error":{"message":"Rate limit reached ... Please try again in 12s."}}'))
chk("429 všude → None, kind=limit", m.call_llm("json") is None and m._llm_state["kind"]=="limit" and m._llm_state["retry_after"]==12, str(m._llm_state))
chk("hláška: limit za minutu", "za minutu" in m._llm_fail_msg())
setpost(lambda p: R(429,{"e":1},text='{"error":{"message":"tokens per day (TPD) ... Please try again in 1h4m33.6s."}}'))
m.call_llm("json"); chk("denní limit → retry_after v hodinách", m._llm_state["retry_after"]>3600 and "Denní" in m._llm_fail_msg(), str(m._llm_state["retry_after"]))
setpost(lambda p: R(401,{"error":{"message":"Invalid API Key"}}))
chk("401 → kind=auth", m.call_llm("json") is None and m._llm_state["kind"]=="auth" and "klíč" in m._llm_fail_msg())
chk("admin vidí detail", "HTTP 401" in m._llm_fail_msg(admin=True) and "HTTP 401" not in m._llm_fail_msg())
setpost(lambda p: R(404,{"error":"gone"}) if p["model"].startswith("openai/gpt-oss-120b") else good('{"b":2}'))
chk("404 → další model (qwen, reasoning none)", m.call_llm("json")=={"b":2} and calls[1]["model"]=="qwen/qwen3.8-27b" and calls[1].get("reasoning_effort")=="none")
setpost(lambda p: R(404,{}) if p["model"]=="openai/gpt-oss-120b" else (R(429,{},headers={"retry-after":"5"}) if p["model"]=="qwen/qwen3.8-27b" else good('{"c":3}')))
chk("404 + 429 → třetí model", m.call_llm("json")=={"c":3} and len({c["model"] for c in calls})==3)
setpost(lambda p: good("<think>x</think>Odpověď"))
chk("text režim bez <think>", m.call_llm("ahoj", json_mode=False)=="Odpověď")
m.requests.get=lambda url,**k: (_ for _ in ()).throw(Exception("net"))
setpost(lambda p: good('{"d":4}')); chk("/models nedostupné → záloha", m.call_llm("json")=={"d":4})
m.requests.get=lambda url,**k: R(200,{"data":[{"id":i,"active":True} for i in IDS]})

print("== call_vision ==")
IMG="data:image/jpeg;base64,AAAA"
setpost(lambda p: good('{"positions":[{"name":"Apple","ticker":"AAPL"}]}'))
d,e=m.call_vision(IMG); chk("vision: qwen s reasoning none", d and calls[0]["model"]=="qwen/qwen3.8-27b" and calls[0].get("reasoning_effort")=="none" and calls[0]["max_tokens"]==1300 and calls[0]["temperature"]==0, str(e))
n={"c":0}
def lim_then_ok(p):
    n["c"]+=1
    return R(429,{},headers={"retry-after":"3"}, text='try again in 3s') if n["c"]==1 else good('{"positions":[{"name":"Tesla"}]}')
setpost(lim_then_ok); sleeps.clear(); n["c"]=0
d,e=m.call_vision(IMG); chk("vision: 429 s krátkým čekáním → počká a zopakuje", d is not None and sleeps and sleeps[0]<=4 and n["c"]==2, f"{d} {sleeps} {n}")
setpost(lambda p: R(429,{},headers={"retry-after":"40"}, text='try again in 40s')); sleeps.clear()
d,e=m.call_vision(IMG); chk("vision: dlouhé čekání → nečeká, kind=limit, retry_after=40", d is None and m._vision_state["kind"]=="limit" and m._vision_state["retry_after"]==40 and not sleeps, str(m._vision_state))
setpost(lambda p: R(404,{"error":"gone"})); d,e=m.call_vision(IMG)
chk("vision: vše mrtvé → kind=down", d is None and m._vision_state["kind"]=="down", str(m._vision_state))

print("== /api/holdings/import (fotka, Yahoo mimo provoz) ==")
c=m.app.test_client()
setpost(lambda p: good(json.dumps({"positions":[{"name":"Apple","ticker":"AAPL"},{"name":"Microsoft"},{"name":"Alphabet (Class A)","ticker":"GOOGL"},{"name":"Berkshire Hathaway B","ticker":"BRK.B"},{"name":"Exxon Mobil"}]})))
r=c.post("/api/holdings/import", json={"image": IMG}); d=r.get_json()
chk("import: 5 pozic přečteno a rozpoznáno ze skenu", d.get("ok") and d["read"]==5 and [p["ticker"] for p in d["positions"]]==["AAPL","MSFT","GOOGL","BRK-B","XOM"], str([p.get("ticker") for p in d.get("positions",[])]))
setpost(lambda p: R(429,{},headers={"retry-after":"30"}, text='try again in 30s'))
r=c.post("/api/holdings/import", json={"image": IMG}); d=r.get_json()
chk("import: limit → HTTP 429, kind=limit, retry_after", r.status_code==429 and d["kind"]=="limit" and d["retry_after"]==30, str(d))
setpost(lambda p: R(429,{},headers={"retry-after":"5400"}, text='try again in 1h30m0s'))
r=c.post("/api/holdings/import", json={"image": IMG}); d=r.get_json()
chk("import: denní limit → hláška o textu", "text" in d["error"] and d["retry_after"]==5400, d["error"])
r=c.post("/api/holdings/import", json={"text":"Apple\nNVDA 12\nBerkshire Hathaway"}); d=r.get_json()
chk("import textem funguje i bez AI", d.get("ok") and d["read"]==3 and all(p.get("ticker") for p in d["positions"]), str([p.get('ticker') for p in d.get('positions',[])]))

print("== admin ai-test ==")
m._auth_admin=lambda: "admin@x"
setpost(lambda p: R(404,{}) if p["model"]=="openai/gpt-oss-120b" else good('{"ok":true,"slovo":"ahoj"}'))
r=c.post("/api/admin/ai-test"); d=r.get_json()
chk("admin test: modely + dostupné + vision", d["ok"] and d["models"][0]["ok"] is False and d["models"][1]["ok"] is True and "openai/gpt-oss-120b" in d["available"] and d["vision"], str(d)[:200])
m._auth_admin=lambda: None
chk("admin test: bez admina 403", c.post("/api/admin/ai-test").status_code==403)
print(f"\nVÝSLEDEK: {ok} OK, {bad} chyb")

sys.exit(1 if bad else 0)
