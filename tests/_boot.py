"""Společný start testů: cesta k backendu + zákaz sítě (testy musí běžet offline a rychle).

Spuštění všech:  python3 tests/run_all.py     (nebo: pytest tests)
Povolení živé sítě pro ruční kontrolu (Yahoo apod.):  MA_TESTS_LIVE=1 python3 tests/test_plan.py
"""
import os, sys, socket

os.environ.setdefault("SITE_LOCK", "off")
os.environ.setdefault("CRON_SECRET", "s")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "api"))
sys.path.insert(0, HERE)
LIVE = os.environ.get("MA_TESTS_LIVE") == "1"

if not LIVE:
    def _no_net(*a, **k):
        raise OSError("síť je v testech zakázaná (MA_TESTS_LIVE=1 ji povolí)")
    socket.socket.connect = _no_net
    socket.getaddrinfo = _no_net
