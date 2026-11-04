import sys, os, json
os.environ["SITE_LOCK"]="off"; os.environ["CRON_SECRET"]="s"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import _boot
import index as m, kvfake
m._kv_cmd=kvfake.kv; m.cloud_enabled=lambda: True
import _fakemarket; _fakemarket.install(m)     # e-mail sahá na ceny – ať nečeká na síť
ok=bad=0
def chk(n,c,info=""):
    global ok,bad
    ok+=bool(c); bad+=(not c); print(("  OK   " if c else "  FAIL ")+n+(f"  [{info}]" if info and not c else ""))
EM="korda050@gmail.com"
m.kv_set_json(f"user:{EM}",{"email":EM,"plan":"elite","notif":{"morning":True}})
c=m.app.test_client()
m._auth_user=lambda: None
chk("bez přihlášení → 401", c.post("/api/profile/name",json={"name":"Matěj"}).status_code==401)
m._auth_user=lambda: EM
chk("výchozí stav: /api/me bez jména (ne z e-mailu)", c.get("/api/me").get_json()["display_name"]=="")
r=c.post("/api/profile/name",json={"name":"  Matěj   Korda "}).get_json(); chk("uložení, mezery se sloučí", r["ok"] and r["name"]=="Matěj Korda", str(r))
chk("/api/me vrací jméno i jeho 5. pád (jen křestní jméno)", c.get("/api/me").get_json()["display_name"]=="Matěj Korda" and c.get("/api/me").get_json()["display_name_voc"]=="Matěji")
r=c.post("/api/profile/name",json={"name":"<script>alert(1)</script>Ahoj\x00\n"+"x"*60}).get_json()
chk("< > a řídicí znaky pryč, max 30 znaků", r["ok"] and "<" not in r["name"] and ">" not in r["name"] and len(r["name"])<=30 and "\x00" not in r["name"], r["name"])
chk("dlouhé jméno ořezané na 30", len(r["name"])==30)
c.post("/api/profile/name",json={"name":"Matěj"})
html=m.build_morning_summary_html(EM,[],[])
chk("ranní e-mail: 5. pád „Dobré ráno, Matěji!“", "Dobré ráno, Matěji!" in html, html[html.find("Dobré")-10:html.find("Dobré")+40])
m.kv_set_json(f"user:{EM}",{"email":EM,"plan":"elite","display_name":"<b>Hacker</b> & co"})
html=m.build_morning_summary_html(EM,[],[])
chk("e-mail escapuje HTML v jménu", "<b>Hacker</b>" not in html and "&lt;" in html or "Hacker" in html and "<b>Hacker</b>" not in html)
r=c.post("/api/profile/name",json={"name":""}).get_json(); chk("prázdné = bez oslovení", r["ok"] and r["name"]=="" and "display_name" not in m.kv_get_json(f"user:{EM}"))
html=m.build_morning_summary_html(EM,[],[]); chk("e-mail bez jména: jen „Dobré ráno!“ (ne z e-mailu)", "Dobré ráno!" in html and "korda050" not in html.split("Dobré ráno")[1][:60])
r=c.post("/api/profile/name",json={"name":123456}).get_json(); chk("číslo / špatný vstup nespadne", r["ok"])
r=c.post("/api/profile/name",data="nesmysl",content_type="text/plain"); chk("nesmyslné tělo → ok s prázdným jménem", r.status_code==200 and r.get_json()["name"]=="")
# přihlášení vrací jméno
import index as mm
salt,ph=m.hash_password("heslo12345"); m.kv_set_json(f"user:x@y.cz",{"email":"x@y.cz","salt":salt,"hash":ph,"plan":"elite","display_name":"Eva"}); m.kv_set_json("portfolio:x@y.cz",{"watchlist":[]})
m._rate_ok=lambda *a,**k: True
d=c.post("/api/login",json={"email":"x@y.cz","password":"heslo12345"}).get_json(); chk("login vrací jméno i 5. pád", d.get("ok") and d.get("display_name")=="Eva" and d.get("display_name_voc")=="Evo", str(d)[:150])
print("== skloňování (5. pád) ==")
CASES={"Matěj":"Matěji","matěj":"Matěji","MATĚJ":"Matěji","Petr":"Petře","Marek":"Marku","Zdeněk":"Zdeňku","Luděk":"Luďku","Tomáš":"Tomáši","Ondřej":"Ondřeji","Jan":"Jane","Jakub":"Jakube","David":"Davide","Daniel":"Danieli","Pavel":"Pavle","Karel":"Karle","Martin":"Martine","Vojtěch":"Vojtěchu","Patrik":"Patriku","Dominik":"Dominiku","Filip":"Filipe","Lukáš":"Lukáši","Jiří":"Jiří","Ivo":"Ivo","Josef":"Josefe","Michal":"Michale","Marcel":"Marcele","Jana":"Jano","Eva":"Evo","Lucie":"Lucie","Marie":"Marie","Kateřina":"Kateřino","Honza":"Honzo","Kuba":"Kubo","Alex":"Alexi","Alexandr":"Alexandře","Viktor":"Viktore","Vít":"Víte","Matty":"Matty","John":"John","Matthew":"Matthew","Ester":"Ester","Dagmar":"Dagmar","Matěj Korda":"Matěji","":"","Adam":"Adame","Roman":"Romane","Radek":"Radku","Jindřich":"Jindřichu","Štěpán":"Štěpáne"}
wrong=[(k,v,m._vocative(k)) for k,v in CASES.items() if m._vocative(k)!=v]
chk(f"{len(CASES)} jmen se skloňuje správně", not wrong, str(wrong))
chk("jen první slovo (Matěj Korda → Matěji)", m._vocative("Matěj Korda")=="Matěji")
chk("nesmysl nespadne; HTML se čistí ještě před uložením a v e-mailu/webu se escapuje", "<" not in m._vocative(m._clean_name("<b>x</b>")) and m._vocative("123")=="123" and m._vocative(None)=="")

print(f"\nVÝSLEDEK: {ok} OK, {bad} chyb")

sys.exit(1 if bad else 0)
