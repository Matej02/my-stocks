"""Falešná tržní data pro offline testy: deterministický „Yahoo“ (ceny z px_map skenu, jinak z hashe tickeru).

Nahrazuje jen `yahoo_chart` – všechno nad ním (ceny, výnosy, koš, verdikty, FX přes Yahoo) běží skutečným kódem."""
import hashlib, time

_DAYS = {"1d": 2, "5d": 5, "1mo": 22, "3mo": 66, "6mo": 130, "1y": 252, "2y": 504, "5y": 1260, "10y": 2520, "max": 2520}
_CCY = {"DE": "EUR", "AS": "EUR", "PA": "EUR", "MI": "EUR", "L": "GBp", "SW": "CHF"}


def _h(s):
    return int(hashlib.md5(s.encode()).hexdigest(), 16)


def install(m, scan=None):
    px = (scan or {}).get("px_map") or {}
    names = (scan or {}).get("name_map") or {}

    def chart(ticker, rng, interval):
        t = ticker.upper()
        if t.startswith("ZZZ") or "NEEXISTUJE" in t:
            raise ValueError("Not Found")
        key = t.replace(".", "-")
        p = px.get(key) or px.get(t)
        base = float(p[0] if isinstance(p, (list, tuple)) else p) if p else float(20 + _h(t) % 380)
        n = _DAYS.get(rng, 252)
        step = 86400
        if interval != "1d":
            n, step = 78, 300
        end = int(time.time()) // 86400 * 86400 + 20 * 3600 if interval == "1d" else int(time.time())
        closes, v = [], base
        for i in range(n):
            closes.append(round(v, 4))
            r = ((_h(f"{t}{i}") % 1000) / 1000 - 0.47) * 0.03      # mírný růst, deterministicky
            v = v / (1 + r)
        closes.reverse()
        ts = [end - (n - 1 - i) * step for i in range(n)]
        suffix = t.rsplit(".", 1)[1] if "." in t else ""
        return {"meta": {"regularMarketPrice": base, "currency": _CCY.get(suffix, "USD"), "shortName": names.get(key, t),
                         "longName": names.get(key, t), "instrumentType": "EQUITY", "chartPreviousClose": closes[0]},
                "timestamp": ts,
                "indicators": {"quote": [{"close": closes, "open": closes, "high": closes, "low": closes,
                                          "volume": [1_000_000] * n}]}}
    m.yahoo_chart = chart
    return chart
