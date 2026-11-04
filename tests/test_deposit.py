"""Kam s penězi: doporučení aplikace, jen do mých akcií, jen nové tipy, vlastní výběr (jen názvy akcií, bez kusů)."""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import _boot
import index as m, kvfake, _fakemarket
m._kv_cmd = kvfake.kv; m.cloud_enabled = lambda: True
scan = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "scan.json"))); scan["date"] = m._today_iso()
_fakemarket.install(m, scan)
kvfake.store[m._pk(f"scan:{m._today_iso()}")] = json.dumps(scan); kvfake.store[m._pk("scan:last")] = json.dumps(scan)
ok = bad = 0
def chk(n, c, info=""):
    global ok, bad
    ok += bool(c); bad += (not c); print(("  OK   " if c else "  FAIL ") + n + (f"  [{info}]" if info and not c else ""))
EM = "qa@x"; m._auth_user = lambda: EM
c = m.app.test_client(); J = lambda r: r.get_json(silent=True) or {}
m._red_flags = lambda tk: {}

mix = scan["mix_rank_map"]
order = sorted(mix, key=lambda t: mix[t])
top = order[:60]                                        # nejsilnější v S&P 500 (některé jsou v koši)
bottom = order[-12:]                                    # nejslabší
def plan(holds, **kw):
    body = {"holdings": [{"ticker": t, "name": t} for t in holds], "risk": "maximum", "picks_n": 10}; body.update(kw)
    return J(c.post("/api/plan", json=body))

# portfolio: 14 akcií = 4 silné (z horních 60, mimo koš), 10 slabých
d0 = plan([])                                           # koš
basket = [p["ticker"] for p in d0["picks"]["basket"]]
strong_h = [t for t in top if t.replace("-", ".") not in basket and t not in basket][:6]
holds = strong_h + bottom
d = plan(holds, amount_czk=10000)
ev = {i["ticker"]: i for i in d["evaluation"]["items"]}
levels = {t: (ev.get(t) or {}).get("level") for t in holds}
n_strong = sum(1 for v in levels.values() if v == "strong")
chk("test má silné i slabé akcie", n_strong >= 2 and any(v == "weak" for v in levels.values()), str(levels))
dep = d["deposit"]
chk("výchozí = doporučení aplikace, součet = částka", dep["strategy"] == "app" and sum(l["amount_czk"] for l in dep["lines"]) == 10000, str(dep["amount_czk"]))
acts = {l["action"] for l in dep["lines"]}
chk("doporučení míchá dokup i nové tipy (portfolio 12 akcií)", acts == {"topup", "new"}, str(acts))
chk("dokup jen silných akcií, které držím", all(levels.get(l["ticker"]) == "strong" for l in dep["lines"] if l["action"] == "topup"))
chk("nové tipy nejsou akcie, které už držím", all(l["ticker"] not in holds for l in dep["lines"] if l["action"] == "new"))
chk("odvětví: z jednoho max 2 nové/dokup", max([sum(1 for l in dep["lines"] if l["sector"] == s) for s in {l["sector"] for l in dep["lines"]}] or [0]) <= 2, str([(l["ticker"], l["sector"]) for l in dep["lines"]]))
chk("žádná část pod minimem 500 Kč", all(l["amount_czk"] >= m.MIN_ORDER_CZK for l in dep["lines"]))
chk("vysvětlení od aplikace (advice)", len(dep["advice"]) >= 1 and "S&P 500" in " ".join(dep["advice"]))
chk("kandidáti pro vlastní výběr: moje i nové", {x["group"] for x in dep["candidates"]} == {"mine", "new"})

mine = plan(holds, amount_czk=10000, deposit_mode="mine", deposit_n=3)["deposit"]
chk("jen do mých: 3 dokupy, nic nového", len(mine["lines"]) == 3 and all(l["action"] == "topup" for l in mine["lines"]) and sum(l["amount_czk"] for l in mine["lines"]) == 10000)
new5 = plan(holds, amount_czk=10000, deposit_mode="new", deposit_n=5)["deposit"]
chk("jen nové: 5 tipů, nic z mých", len(new5["lines"]) == 5 and all(l["action"] == "new" for l in new5["lines"]))
new10 = plan(holds, amount_czk=10000, deposit_mode="new", deposit_n=10)["deposit"]
chk("jen nové: zadání 10, ale při 10 000 Kč max 6 pozic (min. 500 Kč, strop 6)", 1 <= len(new10["lines"]) <= 6, str(len(new10["lines"])))

weak_t = next(t for t, v in levels.items() if v == "weak"); strong_t = next(t for t, v in levels.items() if v == "strong")
nw = new5["lines"][0]["ticker"]
cu = plan(holds, amount_czk=9000, deposit_mode="custom", deposit_pick=[strong_t, nw, weak_t])["deposit"]
chk("vlastní výběr: 3 akcie rovnoměrně 3×3000", [l["amount_czk"] for l in cu["lines"]] == [3000, 3000, 3000], str([l["amount_czk"] for l in cu["lines"]]))
chk("slabá akcie dostane varování (jeho volba)", any("Slabší" in l["why"] for l in cu["lines"] if l["ticker"] == weak_t))
cu2 = plan(holds, amount_czk=9000, deposit_mode="custom", deposit_pick=[strong_t, nw], deposit_amounts={strong_t: 7000, nw: 2000})["deposit"]
chk("vlastní částky se respektují", {l["ticker"]: l["amount_czk"] for l in cu2["lines"]} == {strong_t: 7000, nw: 2000})
cu4 = plan(holds, amount_czk=9000, deposit_mode="custom", deposit_pick=[strong_t, nw, weak_t], deposit_amounts={nw: 2000})["deposit"]
chk("zadaná jen jedna částka: zbytek se rozdělí mezi ostatní (součet = zadáno)", {l["ticker"]: l["amount_czk"] for l in cu4["lines"]}[nw] == 2000 and sum(l["amount_czk"] for l in cu4["lines"]) == 9000, str([(l["ticker"], l["amount_czk"]) for l in cu4["lines"]]))
cu0 = plan(holds, amount_czk=9000, deposit_mode="custom", deposit_pick=[])["deposit"]
chk("vlastní výběr bez zaškrtnutí: žádné řádky, nápověda + kandidáti", cu0["lines"] == [] and cu0["advice"] and cu0["candidates"])
cu3 = plan(holds, amount_czk=9000, deposit_mode="custom", deposit_pick=["NEEXISTUJE", "<script>"])["deposit"]
chk("neznámé tickery se ignorují", cu3["lines"] == [])

# žádná silná akcie → vše do nových
dn = plan(bottom, amount_czk=6000)["deposit"]
chk("bez silných akcií: vše do nových tipů + vysvětlení", {l["action"] for l in dn["lines"]} == {"new"} and any("Silná" in a for a in dn["advice"]), str(dn["advice"]))
# malé portfolio → víc do nových
small = plan(strong_h[:2] + bottom[:2], amount_czk=10000)["deposit"]
nt = sum(1 for l in small["lines"] if l["action"] == "topup"); nn = sum(1 for l in small["lines"] if l["action"] == "new")
chk("malé portfolio (4 akcie): nových tipů je víc než dokupů", nn > nt, f"{nt} dokup / {nn} nových")
# malá částka
tiny = plan(holds, amount_czk=800)["deposit"]
chk("částka 800 Kč: jedna pozice, ne drobení", len(tiny["lines"]) == 1 and tiny["lines"][0]["amount_czk"] == 800, str(tiny["lines"]))
chk("špatný režim spadne na doporučení", plan(holds, amount_czk=5000, deposit_mode="blbost")["deposit"]["strategy"] == "app")
# e-mail používá stejné doporučení
m.kv_set_json(f"holdings:{EM}", {"holdings": [{"ticker": t, "qty": None, "name": t} for t in holds], "risk": "balanced", "deposit_czk": 10000, "deposit_month": "x"})
import datetime as _d
real = m.datetime
class FakeDT(real):
    @classmethod
    def now(cls, tz=None): return real(2026, 11, 3, 6, 0, tzinfo=tz or m.timezone.utc)
m.datetime = FakeDT
html, action = m._portfolio_email_html(EM)
m.datetime = real
chk("ranní e-mail: pravidelný vklad se rozdělí stejným doporučením", "Pravidelný vklad" in html and "10" in html, html[:200])
print(f"\nVÝSLEDEK: {ok} OK, {bad} chyb")
sys.exit(1 if bad else 0)
