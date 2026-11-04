"""Kontrola index.html bez prohlížeče: syntaxe JS, nezavěšené onclick, povolené cizí zdroje (CSP), odstraněný kód."""
import sys, os, re, json, shutil, subprocess, tempfile
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
html = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
ok = bad = 0
def chk(n, c, info=""):
    global ok, bad
    ok += bool(c); bad += (not c); print(("  OK   " if c else "  FAIL ") + n + (f"  [{info}]" if info and not c else ""))

scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
js = "\n".join(scripts)
chk("na stránce je vložený skript", len(js) > 10000, str(len(js)))
if shutil.which("node"):
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(js); p = f.name
    r = subprocess.run(["node", "--check", p], capture_output=True, text=True)
    os.unlink(p)
    chk("syntaxe JS (node --check)", r.returncode == 0, r.stderr[:300])
else:
    print("  SKIP syntaxe JS (node není nainstalovaný)")

used = set(re.findall(r'on(?:click|change|input|focusout|focusin|keydown|keyup|submit|blur|focus)="\s*(?:event\.stopPropagation\(\);\s*)?([A-Za-z_$][\w$]*)\(', html))
defined = set(re.findall(r"(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", html)) | set(re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", html)) | set(re.findall(r"window\.([A-Za-z_$][\w$]*)\s*=", html))
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

for name in ("loadSignals", "renderSignals", "loadOpportunities", "renderOpportunities", "loadExitSignals", "renderSimpleBuy", "oppData",
             "signalsLoaded", "signals-box", "opp-box", "exit-box", "api/signals", "api/opportunities", "api/exit-signals"):
    chk(f"odstraněný starý seznam „co koupit“: {name}", name not in html.replace(".exit-box", "").replace(".opp-box", ""), "zůstal v index.html")
chk("koš tipů je v Příležitostech", 'id="opp-picks-root"' in html and "loadOppPicks" in html)
chk("živý záznam koše ve Výsledcích", 'id="tr-basket-body"' in html and "renderResultsBasket" in html)
print(f"\nVÝSLEDEK: {ok} OK, {bad} chyb")
sys.exit(1 if bad else 0)
