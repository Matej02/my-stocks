"""Spustí všechny testy (každý ve vlastním procesu, offline). Konec s kódem 1, když cokoli selže.

    python3 tests/run_all.py            # to, co běží i v GitHub Actions
    MA_TESTS_LIVE=1 python3 tests/run_all.py   # navíc testy, které potřebují živý internet (Yahoo)
"""
import os, sys, glob, subprocess, time, re

HERE = os.path.dirname(os.path.abspath(__file__))
live = os.environ.get("MA_TESTS_LIVE") == "1"
failed, total_ok, total_bad = [], 0, 0
for path in sorted(glob.glob(os.path.join(HERE, "test_*.py"))):
    name = os.path.basename(path)
    if open(path, encoding="utf-8").read(400).startswith("# live-only") and not live:
        print(f"SKIP  {name} (potřebuje živý internet, MA_TESTS_LIVE=1)")
        continue
    t = time.time()
    r = subprocess.run([sys.executable, "-W", "ignore", path], capture_output=True, text=True, cwd=os.path.dirname(HERE))
    out = r.stdout + r.stderr
    m = re.search(r"VÝSLEDEK: (\d+) OK, (\d+) chyb", out)
    ok, bad = (int(m.group(1)), int(m.group(2))) if m else (0, 1)
    total_ok += ok; total_bad += bad
    print(f"{'OK  ' if r.returncode == 0 and bad == 0 else 'FAIL'}  {name}: {ok} OK, {bad} chyb ({time.time() - t:.0f} s)")
    if r.returncode != 0 or bad:
        failed.append(name)
        print("\n".join("      " + l for l in out.splitlines() if "FAIL" in l or "Traceback" in l or "Error" in l)[:3000])
print(f"\nCELKEM: {total_ok} OK, {total_bad} chyb; selhalo souborů: {len(failed)}")
sys.exit(1 if failed else 0)
