import sys, os, json, time
os.environ["SITE_LOCK"]="off"; os.environ["CRON_SECRET"]="s3cret"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import _boot
import index as m, kvfake
m._kv_cmd=kvfake.kv; m.cloud_enabled=lambda: True
scan=json.load(open(os.path.join(os.path.dirname(__file__),"scan.json")))
import _fakemarket; _fakemarket.install(m, scan) if not _boot.LIVE else None
today=m._today_iso(); scan["date"]=today
ok=bad=0
def chk(n,c,info=""):
    global ok,bad
    ok+=bool(c); bad+=(not c); print(("  OK   " if c else "  FAIL ")+n+(f"  [{info}]" if info and not c else ""))
S=kvfake.store
c=m.app.test_client()

print("== sken ještě není hotový ==")
calls=[]
m._scan_step=lambda budget_s=None, local=None: (calls.append(budget_s), None)[1]
st=m._daily_auto()
chk("bez skenu: stage=scan, nic nezapsáno", st.get("stage")=="scan" and st.get("scan") is False and not S.get(m._pk("verdicts")), str(st))
chk("sken se zkusil dokončit", len(calls)==1)
print("== sken hotový → verdikty po dávkách ==")
S[m._pk(f"scan:{today}")]=json.dumps(scan); S[m._pk("scan:last")]=json.dumps(scan)
uni=m._auto_universe(m._ensure_scan_maps(dict(scan)))
chk("universum: signály + koš (bez duplicit)", len(uni)==len({t for t,_ in uni}) and 5<=len(uni)<=15, str(uni))
t0=time.time(); st=m._daily_auto(); dt=time.time()-t0
n1=st["added"]
chk(f"1. dávka: přidáno {n1} ≤ 8 za {dt:.0f}s", 1<=n1<=8, str(st)[:200])
vs=m.kv_lrange("verdicts",-50,-1)
chk("záznam má pole jako hloubková analýza", all(all(k in v for k in ("ticker","ts","verdict","price","target","stop","horizon_days","score","user","src")) for v in vs) and all(v["user"]=="auto" for v in vs), str(vs[:1]))
chk("verdikty jsou smysluplné (cíl > cena > stop u Koupit)", all((v["target"] or 1e9)>v["price"]>(v["stop"] or 0) for v in vs if v["verdict"]=="Koupit" and v["target"] and v["stop"]))
for _ in range(4):
    if st.get("done"): break
    st=m._daily_auto()
chk("po dalších dávkách done=True", st.get("done") is True, str({k:st.get(k) for k in ("added","total","skipped","done","stage")}))
chk("přidáno = universe (nic navíc)", st["added"]==len(m.kv_lrange("verdicts",-600,-1)) and st["added"]+st["skipped"]+len([t for t in st["fails"] if st["fails"][t]>=2])>=len(uni)-0, str(st["added"]))
before=len(m.kv_lrange("verdicts",-600,-1)); st2=m._daily_auto()
chk("po 'done' další volání nic nepřidá", st2.get("done") and len(m.kv_lrange("verdicts",-600,-1))==before)
chk("auto:last pro Admin", (m.kv_get_json("auto:last") or {}).get("done") is True)

print("== duplicity a zámek ==")
S.pop(m._pk(f"auto:day:{today}"),None)   # nový „den“, ale verdikty z posledních 10 dnů už existují
st=m._daily_auto()
chk("titul s verdiktem < 10 dnů se nezapíše znovu", st["added"]==0 and st["done"] is True and st["skipped"]>=len(uni)-2, str({k:st.get(k) for k in ("added","skipped","done")}))
S.pop(m._pk(f"auto:day:{today}"),None); S[m._pk(f"auto:lock:{today}")]=json.dumps(time.time())
st=m._daily_auto(); chk("zámek: souběžné spuštění vrátí busy", st.get("busy") is True)
S[m._pk(f"auto:lock:{today}")]=json.dumps(0)

print("== endpointy ==")
m._auth_admin=lambda: None
chk("/api/admin/daily bez admina → 403", c.post("/api/admin/daily").status_code==403)
m._auth_admin=lambda: "admin@x"
S.pop(m._pk(f"auto:day:{today}"),None)
# vyřadit verdikty, ať je co dělat: nová „historie“
S[m._pk("verdicts")]=[]
r=c.post("/api/admin/daily"); d=r.get_json()
chk("/api/admin/daily (admin) → ok + added", r.status_code==200 and d["ok"] and d["added"]>=1 and "done" in d, str(d)[:200])
chk("/api/cron/daily bez hesla → 401", c.get("/api/cron/daily").status_code==401)
r=c.get("/api/cron/daily?secret=s3cret"); chk("/api/cron/daily s heslem → ok", r.status_code==200 and r.get_json()["ok"], str(r.get_json()))
print("== track record vidí nové verdikty ==")
r=c.get("/api/track-record"); d=r.get_json()
chk("track-record: položky + src", d["ok"] and d["count"]>=1 and any(i.get("src") in ("signal","basket") for i in d["items"]), str(d)[:200])
chk("track-record: ceny/výnosy se dopočítaly", any(i.get("return_pct") is not None for i in d["items"]))
S2=c.get("/api/admin/email-status") if hasattr(m,"_auth_admin") else None
e=S2.get_json() if S2 is not None else {}
chk("admin stav: daily_last, scan_last, počty", e.get("ok") and e.get("daily_last") and e.get("scan_last") is not None and e.get("verdict_count")>=1, str({k:e.get(k) for k in ("daily_last","scan_last","verdict_count","paper_count")}))
print(f"\nVÝSLEDEK: {ok} OK, {bad} chyb")

sys.exit(1 if bad else 0)
