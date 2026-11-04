"""Kontrola index.html bez prohlížeče: syntaxe JS, nezavěšené onclick, povolené cizí zdroje (CSP), odstraněný kód."""
import sys, os, re, json, shutil, subprocess, tempfile
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
html = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
ok = bad = 0
def chk(n, c, info=""):
    global ok, bad
    ok += bool(c); bad += (not c); print(("  OK   " if c else "  FAIL ") + n + (f"  [{info}]" if info and not c else ""))

import glob
inline = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
files = sorted(glob.glob(os.path.join(ROOT, "assets", "js", "*.js")))
srcs = re.findall(r'<script src="(/assets/[^"]+)"', html)
chk("index.html načítá všechny skripty z assets/js ve správném pořadí", srcs == ["/assets/js/" + os.path.basename(f) for f in files], f"{srcs} vs {[os.path.basename(f) for f in files]}")
chk("styly jsou v assets/app.css a odkazuje na ně index.html", os.path.exists(os.path.join(ROOT, "assets", "app.css")) and '<link rel="stylesheet" href="/assets/app.css">' in html)
js = "\n".join(open(f, encoding="utf-8").read() for f in files) + "\n".join(inline)
chk("JS je v souborech (a v index.html žádný velký inline skript)", len(js) > 100000 and sum(len(x) for x in inline) < 2000, f"{len(js)} / inline {sum(len(x) for x in inline)}")
if shutil.which("node"):
    for f in files:
        r = subprocess.run(["node", "--check", f], capture_output=True, text=True)
        chk(f"syntaxe JS: {os.path.basename(f)}", r.returncode == 0, r.stderr[:300])
else:
    print("  SKIP syntaxe JS (node není nainstalovaný)")

allsrc = html + "\n" + js
used = set(re.findall(r'on(?:click|change|input|focusout|focusin|keydown|keyup|submit|blur|focus)="\s*(?:event\.stopPropagation\(\);\s*)?([A-Za-z_$][\w$]*)\(', allsrc))
defined = set(re.findall(r"(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", allsrc)) | set(re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", allsrc)) | set(re.findall(r"window\.([A-Za-z_$][\w$]*)\s*=", allsrc))
builtin = {"if", "event", "alert", "confirm", "prompt", "history", "this", "document", "window", "localStorage", "navigator", "setTimeout", "return", "void"}
missing = sorted(used - defined - builtin)
chk(f"každý onclick/onchange volá existující funkci ({len(used)} různých)", not missing, str(missing))

vj = json.load(open(os.path.join(ROOT, "vercel.json")))
csp = ""
for rt in vj.get("routes", []):
    csp = (rt.get("headers") or {}).get("Content-Security-Policy") or csp
allowed = set(re.findall(r"https?://[^\s;]+", csp))
hosts = set(re.findall(r"<(?:script|link)[^>]+(?:src|href)=\"(https?://[^\"/]+)", html))
chk("cizí skripty/styly jen z povolených CSP zdrojů", all(any(h == a.rstrip("/") or h.startswith(a.rstrip("/")) for a in allowed) for h in hosts) if csp else False,
    f"hosts={hosts} csp={bool(csp)}")
chk("CSP nepovoluje unsafe-eval", "unsafe-eval" not in csp)

for name in ("loadExitSignals", "renderSimpleBuy", "exit-box", "simple-buy", "api/exit-signals"):
    chk(f"odstraněný box (Co zvážit k prodeji / zjednodušený nákup): {name}", name not in allsrc, "zůstal v kódu")
chk("Příležitosti: původní boxy (Sleva v trendu, Podhodnocené s momentem) jsou zpět", all(x in html for x in ('id="signals-box"', 'id="opp-box"')) and all(x in js for x in ("loadSignals", "loadOpportunities", "renderSignals", "renderOpportunities")))
chk("Příležitosti: koš tipů je navíc pod nimi", html.index('id="opp-box"') < html.index('id="opp-picks-root"'))
chk("koš tipů je v Příležitostech", 'id="opp-picks-root"' in html and "loadOppPicks" in js)
chk("živý záznam koše ve Výsledcích", 'id="tr-basket-body"' in html and "renderResultsBasket" in js)
vjs = open(os.path.join(ROOT, "vercel.json"), encoding="utf-8").read()
chk("vercel.json servíruje assets (build + route)", '"src": "assets/**"' in vjs and '"src": "/assets/(.*)"' in vjs)
print(f"\nVÝSLEDEK: {ok} OK, {bad} chyb")
sys.exit(1 if bad else 0)
