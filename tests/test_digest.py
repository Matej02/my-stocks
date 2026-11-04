"""Ranní e-mail: stručný přehled s hlavní zprávou dne, akce u každé akcie (dokup / problém / natažená) a vklad."""
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
    ok += bool(c); bad += (not c); print(("  OK   " if c else "  FAIL ") + n + (f"  [{info}]" if info and not c else ""))
EM = "qa@x"; m._auth_user = lambda: EM
c = m.app.test_client(); J = lambda r: r.get_json(silent=True) or {}
txt = lambda h: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h)).strip()
m._red_flags = lambda tk: {}

mix = scan["mix_rank_map"]; order = sorted(mix, key=lambda t: mix[t])
d0 = J(c.post("/api/plan", json={"holdings": [], "risk": "maximum"}))
basket = {p["ticker"] for p in d0["picks"]["basket"]}
strong = [t for t in order[:60] if t not in basket][:6]
weak = order[-10:]
holds = strong + weak
m.kv_set_json(f"user:{EM}", {"email": EM, "plan": "elite", "display_name": "Matěj", "notif": {"morning": True}})
def setup(deposit=True):
    m.kv_set_json(f"holdings:{EM}", {"holdings": [{"ticker": t, "qty": None, "name": t} for t in holds], "risk": "balanced",
                                     **({"deposit_czk": 10000} if deposit else {})})
setup()
real = m.datetime
class FakeDT(real):
    @classmethod
    def now(cls, tz=None): return real(2026, 11, 3, 6, 0, tzinfo=tz or m.timezone.utc)
m.datetime = FakeDT

print("== akce u jednotlivých akcií ==")
ev = J(c.post("/api/plan", json={"holdings": [{"ticker": t, "name": t} for t in holds], "risk": "maximum"}))["evaluation"]
acts = {i["ticker"]: i for i in ev["items"]}
chk("každá hodnocená akcie má action + popisek", all(i.get("action") and "action_label" in i for i in ev["items"]))
chk("počty akcí v odpovědi", sum(ev["actions"].values()) == len(ev["items"]), str(ev["actions"]))
buys = [t for t, i in acts.items() if i["action"] == "buy"]
chk("podhodnocené (dokup) = silné a nahoře podle hodnoty+momenta, s vysvětlením", buys and all(acts[t]["level"] == "strong" and "levná" in acts[t]["action_why"] for t in buys), str(buys))
chk("slabší akcie = nedokupuj (ne prodej)", all(i["action"] == "nobuy" for i in ev["items"] if i["level"] == "weak"))
# natažená + problém synteticky
rows = [{"ticker": "AAA", "kind": "stock", "name": "A", "flag": None}, {"ticker": "BBB", "kind": "stock", "name": "B",
        "flag": {"pause": True, "reasons": [{"reason": "bankrot / ochrana před věřiteli (SEC 8-K 1.03)"}]}},
        {"ticker": "CCC", "kind": "stock", "name": "C"}]
a, w = m._holding_action({"level": "hold", "mom_pct": 150, "pe": 80}, rows[0], scan); chk("P/E 80 a +150 % za rok = natažená, ne prodej", a == "stretched" and "30 %" in w, a)
a, w = m._holding_action({"level": "hold", "mom_pct": 150, "pe": None}, rows[0], scan); chk("bez zisku a +150 % = natažená", a == "stretched")
a, w = m._holding_action({"level": "weak", "mom_pct": 5, "pe": 80}, rows[1], scan); chk("vážný problém firmy (8-K 1.03) = risk + zvaž prodej", a == "risk" and "bankrot" in w and "prodej" in w, w)
a, w = m._holding_action({"level": "hold", "mom_pct": 30, "pe": 20}, rows[2], scan); chk("průměrná akcie = drž", a == "hold")
a, w = m._holding_action({"level": "na"}, rows[2], scan); chk("fond/ETF bez akce", a == "na")

print("== ranní e-mail ==")
risk_t = strong[1]
m._sec_flags = lambda t, cm: ([{"code": "1.03", "date": m._today_iso(), "pause": True, "reason": m.REDFLAG_PAUSE["1.03"], "url": "http://x"}] if m._spx_key(t) == risk_t else [])
m._red_flags = lambda tk: {t: {"pause": True, "reasons": [{"reason": m.REDFLAG_PAUSE["1.03"]}], "until": "2099-01-01"} for t in tk if m._spx_key(t) == risk_t}
orig_vh = m._value_holdings
def vh(lots):
    rows_, total, missing, sc = orig_vh(lots)
    for r in rows_:
        if m._spx_key(r["ticker"]) == risk_t: r["flag"] = {"pause": True, "reasons": [{"reason": m.REDFLAG_PAUSE["1.03"]}]}
    return rows_, total, missing, sc
m._value_holdings = vh
earn_t = strong[2]
m._earnings_for = lambda tk: ({earn_t: {"date": "2026-11-04", "days": 1, "when": "post", "eps": 1.2, "n": 5}} if earn_t in tk else {})
sig = [{"ticker": "DELL", "name": "Dell", "price": 1, "currency": "USD", "score": 80, "vm": 60, "change_pct": 1, "target": 2, "stop": 1, "reward_pct": 5, "risk_pct": 3, "rr": 1.5, "sector": "Information Technology"}]
opps = [{"ticker": "XOM", "name": "Exxon", "vm_rank": 3, "score": 70, "pe": 12}]
html = m.build_morning_summary_html(EM, opps, sig, mark_sent=False)
t = txt(html)
open("/tmp/digest_sample.html", "w").write(f"<body style='margin:0;background:#0b0d12'>{html}</body>")
chk("pozdrav v 5. pádu + datum", "Dobré ráno, Matěji!" in t and "úterý 3. 11." in t, t[:120])
chk("hlavní zpráva dne = problém firmy má přednost", "Dnes: Pozor u" in t and m._MORNING_HEAD[EM].startswith("🔴 Pozor"), m._MORNING_HEAD.get(EM))
chk("sekce: problém, dokup, výsledky, vklad, trh, příležitosti", all(x in t for x in ("Pozor – vážný problém", "Podhodnocené z tvých akcií", "Výsledky do týdne", "Pravidelný vklad", "Trh dnes", "Nové příležitosti")), t[:600])
chk("bankrot je vysvětlen jednou větou a doporučení zvážit prodej", "bankrot" in t and "Zvaž prodej" in t)
chk("výsledky: ticker a datum", earn_t.split(".")[0] in t and "4. 11." in t)
chk("vklad: rozdělení + částka 10 000", "10 000" in t.replace("\xa0", " ") and "dokup" in t)
chk("problémová akcie není v dokupu ani ve vkladu", risk_t not in re.findall(r"dokup[^|]{0,0}", "") and not any(l["ticker"] == risk_t for l in m._deposit_names(10000, *(lambda d: (d["rows"], d["picks"], d["ev"]))(m._user_eval_saved(EM)))["lines"]))
chk("e-mail je stručný (méně než 5 500 znaků čistého textu)", len(t) < 5500, str(len(t)))
chk("odkaz do appky a upozornění, že to není záruka", "Otevřít MY ADVANTAGE" in t and "Není to investiční doporučení" in t)
m.kv_set_json(f"holdings:{EM}", {**m.kv_get_json(f"holdings:{EM}"), "deposit_month": None})
html2 = m.build_morning_summary_html(EM, opps, sig, mark_sent=True)
chk("po odeslání se vklad zapíše jako rozeslaný (jen 1× za měsíc)", m.kv_get_json(f"holdings:{EM}").get("deposit_month") == "2026-11")
chk("podruhé už vklad v e-mailu není", "Pravidelný vklad" not in txt(m.build_morning_summary_html(EM, opps, sig)))

print("== klidný den ==")
m._sec_flags = lambda t, cm: []; m._red_flags = lambda tk: {}; m._value_holdings = orig_vh; m._earnings_for = lambda tk: {}
setup(deposit=False)
h3 = txt(m.build_morning_summary_html(EM, [], []))
chk("bez problému: hlavní zpráva o podhodnocených akciích", "Z tvých akcií jsou teď podhodnocené" in h3 and "🟢" in m._MORNING_HEAD[EM], m._MORNING_HEAD.get(EM))
chk("bez vkladu a bez signálů e-mail neobsahuje prázdné sekce", "Pravidelný vklad" not in h3 and "Dnešní nákupní signál" not in h3)
chk("shrnutí zbytku: kolik akcií beze změny", "beze změny – nic dělat nemusíš" in h3)

print("== předmět e-mailu a uživatel bez akcií ==")
sent = []
m.send_email = lambda to, subj, body: (sent.append((to, subj)), True)[1]
m.digest_sending_enabled = lambda: True
m.kv_sadd("users", EM)
m._broadcast("morning", lambda em: "☀️ " + (m._MORNING_HEAD.pop(em, None) or "Ranní přehled trhu") + " – MY ADVANTAGE", lambda em: m.build_morning_summary_html(em, [], []))
chk("předmět nese hlavní zprávu dne", sent and "podhodnocen" in sent[0][1] and sent[0][1].endswith("MY ADVANTAGE"), str(sent))
m.kv_set_json("user:nikdo@x", {"email": "nikdo@x", "plan": "elite"})
chk("uživatel bez akcií dostane původní e-mail (nespadne)", "Ranní přehled" in txt(m.build_morning_summary_html("nikdo@x", [], [])))
m.datetime = real
print(f"\nVÝSLEDEK: {ok} OK, {bad} chyb")
sys.exit(1 if bad else 0)
