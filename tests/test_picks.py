"""Koš tipů = jediný seznam „co koupit“: /api/picks, e-maily, živý záznam koše, podpisový klíč, bezpečnostní příznaky."""
import sys, os, json, re
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import _boot
for k in ("APP_SECRET", "APP_PASSWORD", "SITE_PASSWORD"): os.environ.pop(k, None)
import index as m, kvfake, _fakemarket
m._kv_cmd = kvfake.kv; m.cloud_enabled = lambda: True
scan = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "scan.json"))); today = m._today_iso(); scan["date"] = today
_fakemarket.install(m, scan)
kvfake.store[m._pk(f"scan:{today}")] = json.dumps(scan); kvfake.store[m._pk("scan:last")] = json.dumps(scan)
ok = bad = 0
def chk(n, c, info=""):
    global ok, bad
    ok += bool(c); bad += (not c); print(("  OK   " if c else "  FAIL ") + n + (f"  [{info}]" if info and not c else ""))
EM = "qa@x"; m._auth_user = lambda: EM; m._auth_admin = lambda: None
c = m.app.test_client(); J = lambda r: r.get_json(silent=True) or {}
txt = lambda h: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h))

print("== /api/picks ==")
d = J(c.get("/api/picks?n=10"))
bk = [p["ticker"] for p in d["picks"]["basket"]]; more = [p["ticker"] for p in d["more"]]
chk("ok + koš 10 tipů", d.get("ok") and len(bk) == 10, str(d)[:200])
chk("dalších 15 kandidátů za košem, bez překryvu s košem", len(more) == 15 and not set(more) & set(bk), str(more))
chk("každý tip má název a zdůvodnění", all(p.get("why") and p.get("name") for p in d["picks"]["basket"] + d["more"]))
d6 = J(c.get("/api/picks?n=6"))
chk("n=6 → koš 6 tipů", len(d6["picks"]["basket"]) == 6 and [p["ticker"] for p in d6["picks"]["basket"]] == bk[:6], str([p["ticker"] for p in d6["picks"]["basket"]]))
chk("měsíc a statistiky ověření", d["picks"].get("month") == today[:7] and (d["picks"].get("stats") or {}).get("holdout"), str(d["picks"].get("month")))
saved = dict(kvfake.store)
for k in [k for k in kvfake.store if "scan:" in str(k)]: del kvfake.store[k]
real_ms = m.market_scan; m.market_scan = lambda allow_step=False: None
r = J(c.get("/api/picks")); chk("sken chybí → ok + scanning", r.get("ok") and r.get("scanning") and r.get("picks") is None, str(r)[:150])
m.market_scan = real_ms; kvfake.store.update(saved)
m._red_flags = lambda tk: {bk[0]: {"pause": True, "why": "test"}}
dd = J(c.get("/api/picks?n=10")); chk("pause: tip vypadne z koše a z kandidátů", bk[0] not in [p["ticker"] for p in dd["picks"]["basket"]] + [p["ticker"] for p in dd["more"]])
m._red_flags = lambda tk: {}

print("== živý záznam koše (řetězení měsíců) ==")
prev_month = "2026-10"
b0 = json.loads(json.dumps(kvfake.store.get(m._pk(f"basket:{today[:7]}")) and json.loads(kvfake.store[m._pk(f"basket:{today[:7]}")])))
chk("koš měsíce se zmrazil v KV", b0 and b0.get("order") and b0.get("px"), str(b0)[:100])
old = dict(b0); old["month"] = prev_month; old["date"] = "2026-10-05"
old["px"] = {k: v * 0.8 for k, v in b0["px"].items()}          # tehdejší ceny o 20 % nižší → koš v zisku
kvfake.store[m._pk(f"basket:{prev_month}")] = json.dumps(old)
kvfake.store[m._pk("basket:index")] = json.dumps([prev_month, today[:7]])
for k in [k for k in kvfake.store if "basketperf" in str(k)]: del kvfake.store[k]
bp = m._basket_perf_all()
chk("záznam pro 10 i 6 tipů", set(bp) == {10, 6}, str(list(bp)))
p10 = bp[10]; pr = p10["periods"]
chk("dva měsíce, první končí den před sestavením druhého", len(pr) == 2 and pr[0]["until"] and pr[1]["until"] is None, str(pr))
chain = ((1 + pr[0]["ret"] / 100) * (1 + pr[1]["ret"] / 100) - 1) * 100
chk("celkový výnos = řetězení měsíců", abs(chain - p10["ret"]) < 0.05, f"{chain} vs {p10['ret']}")
chk("koš 6 má 6 akcií v každém měsíci", all(x["n"] == 6 for x in bp[6]["periods"]), str(bp[6]["periods"]))
chk("1. měsíc ve zisku ≈ +25 % (tehdejší ceny 0,8×, konec den před dalším košem)", 15 < pr[0]["ret"] < 40, str(pr[0]))
tr = J(c.get("/api/track-record"))
chk("/api/track-record vrací koš (10 i 6)", set((tr.get("basket") or {})) == {"10", "6"}, str(tr.get("basket"))[:120])

print("== e-maily: jeden seznam „co koupit“ ==")
held = bk[0]
m.kv_set_json(f"holdings:{EM}", {"holdings": [{"ticker": held, "qty": None, "name": held}], "risk": "balanced"})
bh = m._basket_email_html(EM)
chk("blok „Koš tipů na <měsíc>“", "Koš tipů na" in bh and m._MONTH_CZ[int(today[5:7]) - 1] in bh, txt(bh)[:120])
chk("jen nové akcie (držená se nedoporučuje)", held not in txt(bh).replace("Koš tipů", ""), txt(bh)[:300])
chk("max 5 řádků + živý záznam koše", bh.count("border-top:1px solid #23262f") == 5 and "Živý záznam koše" in bh)
sig = [{"ticker": "DELL", "name": "Dell", "price": 1, "currency": "USD", "score": 80, "vm": 60, "change_pct": 1, "target": 2, "stop": 1, "reward_pct": 5, "risk_pct": 3, "rr": 1.5, "sector": "Information Technology"}]
opps = [{"ticker": "XOM", "name": "Exxon", "vm_rank": 3, "score": 70, "pe": 12}]
mh = m.build_morning_summary_html(EM, opps, sig)
chk("ranní e-mail: koš ano, staré seznamy ne", "Koš tipů na" in mh and "nákupní signály" not in mh and "Podhodnocené s momentem" not in mh, txt(mh)[:200])
wh = m.build_weekly_digest_html(EM, sig, opps)
chk("týdenní e-mail: koš ano, staré seznamy ne", "Koš tipů na" in wh and "Nákupní signály" not in wh and "Podhodnocené" not in wh)
m.market_scan = lambda allow_step=False: None
chk("blok koše bez skenu = prázdný řetězec", m._basket_email_html(EM) == "")
m.market_scan = real_ms
act = m._todays_action(EM, sig)
chk("„dnešní akce“ odkazuje na koš, ne na denní signál", act and "signál" not in act.lower(), str(act))

print("== podpisový klíč (_secret) ==")
m._secret_mem.clear(); kvfake.store.pop(m._pk("app:secret"), None)
s1 = m._secret()
chk("bez APP_SECRET: náhodný 64hex klíč, ne veřejný výchozí", len(s1) == 64 and re.fullmatch(r"[0-9a-f]{64}", s1) and s1 not in ("1410", "change-me"), s1[:6])
chk("uložený v KV (přežije restart)", json.loads(kvfake.store[m._pk("app:secret")])["v"] == s1)
m._secret_mem.clear(); chk("po restartu (prázdná paměť) stejný klíč", m._secret() == s1)
tok = m._gate_token(); chk("brána: token se podepsal a ověří", m._gate_token_ok(tok) and not m._gate_token_ok(tok[:-2] + "00"))
os.environ["APP_SECRET"] = "env-secret-123"; chk("APP_SECRET z prostředí má přednost", m._secret() == "env-secret-123")
del os.environ["APP_SECRET"]
m._secret_mem.clear()
real_get = m.kv_get_json; m.kv_get_json = lambda *a, **k: (_ for _ in ()).throw(Exception("kv down"))
s2 = m._secret(); chk("KV nedostupné → klíč v paměti, ne pád", len(s2) == 64, s2[:4])
chk("…a zůstává stejný po dobu běhu", m._secret() == s2)
m.kv_get_json = real_get; m._secret_mem.clear()

print("== bezpečnostní příznaky v Admin stavu ==")
m._auth_admin = lambda: "admin@x"; m._auth_user = lambda: "admin@x"
sc = J(c.get("/api/admin/email-status")).get("security") or {}
chk("bez SITE_PASSWORD/APP_SECRET: obojí False", sc.get("site_password") is False and sc.get("app_secret") is False and "site_lock" in sc, str(sc))
os.environ["SITE_PASSWORD"] = "dlouhe-heslo-xyz"; os.environ["APP_SECRET"] = "x" * 40
sc = J(c.get("/api/admin/email-status")).get("security") or {}
chk("po nastavení na Vercelu: obojí True", sc.get("site_password") is True and sc.get("app_secret") is True, str(sc))
chk("heslo ani klíč se v odpovědi nevrací", "dlouhe-heslo-xyz" not in c.get("/api/admin/email-status").get_data(as_text=True) and "x" * 40 not in c.get("/api/admin/email-status").get_data(as_text=True))
m._auth_admin = lambda: None
chk("bez admina 403", c.get("/api/admin/email-status").status_code in (401, 403))
print(f"\nVÝSLEDEK: {ok} OK, {bad} chyb")
sys.exit(1 if bad else 0)
