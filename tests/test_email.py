import sys, os, json, re
os.environ["SITE_LOCK"]="off"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import _boot
import index as m, kvfake
m._kv_cmd=kvfake.kv; m.cloud_enabled=lambda: True
scan=json.load(open(os.path.join(os.path.dirname(__file__),"scan.json"))); today=m._today_iso(); scan["date"]=today
kvfake.store[m._pk(f"scan:{today}")]=json.dumps(scan); kvfake.store[m._pk("scan:last")]=json.dumps(scan)
ok=bad=0
def chk(n,c,info=""):
    global ok,bad
    ok+=bool(c); bad+=(not c); print(("  OK   " if c else "  FAIL ")+n+(f"  [{info}]" if info and not c else ""))
EM="qa@x"
names=["AAPL","XOM","MU","TSLA","MSFT","SAP.DE","QQQ"]
m.kv_set_json(f"holdings:{EM}", {"holdings":[{"ticker":t,"qty":None,"name":t} for t in names], "risk":"balanced", "deposit_czk": 9000})
# den ≤ 10, aby se ukázal vklad
import datetime as _d
real=m.datetime
class FakeDT(real):
    @classmethod
    def now(cls, tz=None): return real(2026,10,5,6,0,tzinfo=tz or m.timezone.utc)
m.datetime=FakeDT
html, action = m._portfolio_email_html(EM)
m.datetime=real
txt=re.sub(r"<[^>]+>"," ",html); txt=re.sub(r"\s+"," ",txt)
print("AKCE:", action); print("TEXT:", txt[:900])
chk("nadpis Tvoje akcie", "Tvoje akcie" in html)
chk("hodnocení: silné/drž/slabší", "Silné" in html and "Drž" in html and "Slabší" in html)
chk("žádná výzva k doplnění kusů", "Počet kusů" not in html and "tužka" not in html and "neznám" not in html)
chk("vklad do koše tipů (nové tituly)", "Pravidelný vklad" in html and "9" in txt)
chk("věta co dnes (vklad na řadě)", action and "vklad" in action, str(action))
# uživatel s kusy a profilem maximum se nerozbije
m.kv_set_json(f"holdings:{EM}", {"holdings":[{"ticker":"AAPL","qty":3,"name":"Apple"}], "risk":"maximum"})
h2, a2 = m._portfolio_email_html(EM); chk("s uloženými kusy: kusy se ignorují (Tvoje akcie)", "Tvoje akcie" in h2 and a2)
print(f"\nVÝSLEDEK: {ok} OK, {bad} chyb")

sys.exit(1 if bad else 0)
