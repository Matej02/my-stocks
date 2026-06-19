#!/usr/bin/env python3
"""
MY STOCKS - Backend pro Vercel (Serverless Function)

Data se tahají PŘÍMO z veřejných Yahoo Finance HTTP endpointů
(v8/finance/chart, v1/finance/search, v10 quoteSummary) pomocí `requests`.
Žádné yfinance/pandas/numpy -> spolehlivé i na serverless (AWS) IP a rychlý cold start.

Indikátory (RSI, SMA, MACD, volatilita, momentum) a Signal Score 0-100
se počítají v čistém Pythonu a jsou nezávislé na AI.
"""

from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
import math
from datetime import datetime, timezone
from urllib.parse import quote

app = Flask(__name__)
CORS(app)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "application/json"}


# ---------------------------------------------------------------------------
# Pomocné funkce
# ---------------------------------------------------------------------------
def _clean(v, default=None):
    try:
        if v is None:
            return default
        if isinstance(v, (int, float)):
            if math.isnan(v) or math.isinf(v):
                return default
            return v
        return v
    except Exception:
        return default


def _round(v, n=3, default=None):
    v = _clean(v, None)
    if v is None:
        return default
    try:
        return round(float(v), n)
    except Exception:
        return default


def yahoo_chart(ticker, rng, interval):
    """Stáhne data z Yahoo chart endpointu. Vrací result dict (meta, timestamp, quote)."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(ticker, safe='')}"
    params = {"range": rng, "interval": interval, "includePrePost": "false"}
    r = requests.get(url, headers=HEADERS, params=params, timeout=10)
    j = r.json()
    res = (j.get("chart") or {}).get("result")
    if not res:
        err = (j.get("chart") or {}).get("error")
        raise ValueError(err or "Yahoo nevrátil data")
    return res[0]


# ---------------------------------------------------------------------------
# Technické indikátory (čistý Python)
# ---------------------------------------------------------------------------
def _sma(vals, w):
    if len(vals) < w:
        return None
    return sum(vals[-w:]) / w


def _ema_last(vals, span):
    if len(vals) < span:
        return None
    k = 2 / (span + 1)
    e = sum(vals[:span]) / span
    for v in vals[span:]:
        e = v * k + e * (1 - k)
    return e


def _ema_series(vals, span):
    if len(vals) < span:
        return []
    k = 2 / (span + 1)
    e = sum(vals[:span]) / span
    out = [e]
    for v in vals[span:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def compute_indicators(closes, volumes):
    out = {
        "rsi": None, "sma20": None, "sma50": None, "sma200": None,
        "macd": None, "macd_hist": None, "volatility": None,
        "mom_1m": None, "avg_volume": None, "last_volume": None,
    }
    closes = [c for c in closes if c is not None]
    if len(closes) < 5:
        return out

    # RSI(14) – Wilder
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    if len(deltas) >= 14:
        gains = [max(d, 0) for d in deltas]
        losses = [max(-d, 0) for d in deltas]
        avg_g = sum(gains[:14]) / 14
        avg_l = sum(losses[:14]) / 14
        for i in range(14, len(deltas)):
            avg_g = (avg_g * 13 + gains[i]) / 14
            avg_l = (avg_l * 13 + losses[i]) / 14
        if avg_l == 0:
            out["rsi"] = 100.0
        else:
            rs = avg_g / avg_l
            out["rsi"] = round(100 - 100 / (1 + rs), 1)

    out["sma20"] = _round(_sma(closes, 20), 3)
    out["sma50"] = _round(_sma(closes, 50), 3)
    out["sma200"] = _round(_sma(closes, 200), 3)

    # MACD (12/26/9)
    if len(closes) >= 26:
        e12 = _ema_last(closes, 12)
        e26 = _ema_last(closes, 26)
        macd_now = (e12 or 0) - (e26 or 0)
        out["macd"] = round(macd_now, 4)
        e12s = _ema_series(closes, 12)
        e26s = _ema_series(closes, 26)
        n = min(len(e12s), len(e26s))
        if n >= 9:
            macd_series = [e12s[-n + i] - e26s[-n + i] for i in range(n)]
            sig = _ema_last(macd_series, 9)
            if sig is not None:
                out["macd_hist"] = round(macd_now - sig, 4)

    # Volatilita (anualizovaná %)
    rets = [(closes[i] / closes[i - 1] - 1) for i in range(1, len(closes)) if closes[i - 1]]
    if len(rets) > 2:
        mean = sum(rets) / len(rets)
        var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
        out["volatility"] = round((var ** 0.5) * (252 ** 0.5) * 100, 1)

    # Momentum ~1 měsíc
    if len(closes) > 22:
        out["mom_1m"] = round((closes[-1] / closes[-22] - 1) * 100, 1)

    vols = [v for v in volumes if v is not None]
    if vols:
        out["last_volume"] = int(vols[-1])
        if len(vols) >= 5:
            tail = vols[-20:]
            out["avg_volume"] = int(sum(tail) / len(tail))
    return out


def signal_score(price, ind, high52, low52):
    score = 50.0
    comps = []

    def add(points, label, kind):
        nonlocal score
        score += points
        comps.append({"label": label, "kind": kind})

    if price and ind.get("sma50"):
        if price > ind["sma50"]:
            add(10, "Cena nad SMA50", "bull")
        else:
            add(-10, "Cena pod SMA50", "bear")

    if price and ind.get("sma200"):
        if price > ind["sma200"]:
            add(12, "Nad SMA200 – dlouhodobý býčí trend", "bull")
        else:
            add(-12, "Pod SMA200 – dlouhodobý medvědí trend", "bear")

    if ind.get("sma50") and ind.get("sma200"):
        if ind["sma50"] > ind["sma200"]:
            add(6, "Golden cross (SMA50 > SMA200)", "bull")
        else:
            add(-6, "Death cross (SMA50 < SMA200)", "bear")

    r = ind.get("rsi")
    if r is not None:
        if r < 30:
            add(12, f"RSI {r:.0f} – přeprodáno", "bull")
        elif r > 70:
            add(-12, f"RSI {r:.0f} – překoupeno", "bear")
        else:
            comps.append({"label": f"RSI {r:.0f} – neutrální zóna", "kind": "neutral"})

    mh = ind.get("macd_hist")
    if mh is not None:
        if mh > 0:
            add(8, "MACD nad signální linií", "bull")
        else:
            add(-8, "MACD pod signální linií", "bear")

    m1 = ind.get("mom_1m")
    if m1 is not None:
        if m1 > 0:
            add(6, f"Měsíční momentum +{m1:.1f}%", "bull")
        else:
            add(-6, f"Měsíční momentum {m1:.1f}%", "bear")

    try:
        if high52 and low52 and high52 > low52 and price:
            pos = (price - low52) / (high52 - low52) * 100
            if pos > 85:
                add(-4, "Blízko 52T maxima", "bear")
            elif pos < 15:
                add(4, "Blízko 52T minima", "bull")
    except Exception:
        pass

    score = max(0, min(100, score))
    if score >= 70:
        label = "SILNĚ NAKUPOVAT"
    elif score >= 58:
        label = "NAKUPOVAT"
    elif score >= 43:
        label = "NEUTRÁLNÍ"
    elif score >= 30:
        label = "PRODÁVAT"
    else:
        label = "SILNĚ PRODÁVAT"
    return round(score), label, comps


def sma_overlay(closes, window):
    out = []
    n = len(closes)
    for i in range(n):
        if i + 1 < window:
            out.append(None)
        else:
            seg = [c for c in closes[i + 1 - window:i + 1] if c is not None]
            out.append(round(sum(seg) / len(seg), 4) if len(seg) == window else None)
    return out


# ---------------------------------------------------------------------------
# Fundamenty (best-effort přes quoteSummary + crumb; když selže -> N/A)
# ---------------------------------------------------------------------------
def _raw(node, key, default=None):
    try:
        v = node.get(key)
        if isinstance(v, dict):
            return v.get("raw", default)
        return v if v is not None else default
    except Exception:
        return default


def fetch_fundamentals(ticker):
    out = {}
    try:
        s = requests.Session()
        s.headers.update(HEADERS)
        s.get("https://finance.yahoo.com/quote/" + quote(ticker, safe=''), timeout=4)
        crumb = s.get("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=4).text.strip()
        if not crumb or "<" in crumb or len(crumb) > 40:
            return out
        modules = "summaryDetail,defaultKeyStatistics,assetProfile,price,financialData"
        url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{quote(ticker, safe='')}"
        r = s.get(url, params={"modules": modules, "crumb": crumb}, timeout=6)
        result = (((r.json() or {}).get("quoteSummary") or {}).get("result") or [])
        if not result:
            return out
        d = result[0]
        sd = d.get("summaryDetail", {}) or {}
        ks = d.get("defaultKeyStatistics", {}) or {}
        ap = d.get("assetProfile", {}) or {}
        pr = d.get("price", {}) or {}
        fd = d.get("financialData", {}) or {}

        out["name"] = pr.get("longName") or pr.get("shortName")
        out["sector"] = ap.get("sector")
        out["industry"] = ap.get("industry")
        out["description"] = ap.get("longBusinessSummary")
        out["pe_ratio"] = _raw(sd, "trailingPE")
        out["forward_pe"] = _raw(sd, "forwardPE")
        out["market_cap"] = _raw(pr, "marketCap") or _raw(sd, "marketCap")
        out["beta"] = _raw(sd, "beta") or _raw(ks, "beta")
        out["eps"] = _raw(ks, "trailingEps")
        out["dividend_yield"] = _raw(sd, "dividendYield")
        out["target_mean"] = _raw(fd, "targetMeanPrice")
        out["recommendation"] = fd.get("recommendationKey")
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# Scanner příležitostí ("crazy opportunities")
# ---------------------------------------------------------------------------
def _qf(node, key, default=None):
    """Bezpečně vytáhne číslo ze screener quote (může být raw číslo nebo {'raw':...})."""
    v = node.get(key)
    if isinstance(v, dict):
        v = v.get("raw")
    return _clean(v, default)


def yahoo_screener(scr_id, count=30):
    url = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
    r = requests.get(url, headers=HEADERS,
                     params={"scrIds": scr_id, "count": count, "start": 0}, timeout=10)
    res = ((r.json() or {}).get("finance") or {}).get("result") or []
    if not res:
        return []
    return res[0].get("quotes", []) or []


def opportunity_score(q):
    """Skóre 'výbušnosti' 0-100 + odznaky. Odměňuje denní výbuch, objemový spike,
    roční momentum, malou kapitalizaci (prostor 10x) a blízkost průlomu."""
    day = _qf(q, "regularMarketChangePercent", 0) or 0
    price = _qf(q, "regularMarketPrice", 0) or 0
    vol = _qf(q, "regularMarketVolume", 0) or 0
    avg = _qf(q, "averageDailyVolume3Month", 0) or _qf(q, "averageDailyVolume10Day", 0) or 0
    yr = _qf(q, "fiftyTwoWeekChangePercent", 0) or 0
    mcap = _qf(q, "marketCap", 0) or 0
    hi52 = _qf(q, "fiftyTwoWeekHigh", 0) or 0
    lo52 = _qf(q, "fiftyTwoWeekLow", 0) or 0

    vol_ratio = (vol / avg) if avg else 0
    badges = []
    score = 0.0

    # 1) Denní výbuch (až 35 b.) – 100% za den = strop
    score += min(abs(day), 100) / 100 * 35
    if day >= 100:
        badges.append({"t": f"🚀 +{day:.0f}% DNES", "k": "extreme"})
    elif day >= 30:
        badges.append({"t": f"🚀 +{day:.0f}% dnes", "k": "hot"})
    elif day >= 10:
        badges.append({"t": f"📈 +{day:.0f}% dnes", "k": "up"})
    elif day <= -10:
        badges.append({"t": f"📉 {day:.0f}% dnes", "k": "down"})

    # 2) Objemový spike (až 25 b.) – kolikrát nad průměrem
    score += min(vol_ratio, 12) / 12 * 25
    if vol_ratio >= 5:
        badges.append({"t": f"🔥 Objem {vol_ratio:.0f}× průměr", "k": "hot"})
    elif vol_ratio >= 2:
        badges.append({"t": f"🔥 Objem {vol_ratio:.1f}× průměr", "k": "up"})

    # 3) Roční momentum (až 15 b.) – už běží
    score += max(min(yr, 1000), 0) / 1000 * 15
    if yr >= 500:
        badges.append({"t": f"💥 +{yr:.0f}% za rok", "k": "extreme"})
    elif yr >= 100:
        badges.append({"t": f"📈 +{yr:.0f}% za rok", "k": "up"})

    # 4) Malá kapitalizace = prostor pro 10x (až 15 b.)
    if mcap and mcap < 50e6:
        score += 15; badges.append({"t": "💎 Nano-cap", "k": "gem"})
    elif mcap and mcap < 300e6:
        score += 12; badges.append({"t": "💎 Micro-cap", "k": "gem"})
    elif mcap and mcap < 2e9:
        score += 8; badges.append({"t": "Small-cap", "k": "neutral"})
    elif mcap and mcap < 10e9:
        score += 4

    # 5) Blízkost průlomu / pozice v 52T pásmu (až 10 b.)
    if hi52 and lo52 and hi52 > lo52 and price:
        pos = (price - lo52) / (hi52 - lo52)
        if pos >= 0.9:
            score += 10; badges.append({"t": "⚡ Průlom k ATH", "k": "hot"})
        elif pos <= 0.15:
            score += 5; badges.append({"t": "🩹 U dna 52T", "k": "down"})

    # Moonshot heuristika: malá firma + objemový spike + denní výbuch
    moonshot = bool(mcap and mcap < 300e6 and vol_ratio >= 3 and day >= 15)
    if moonshot:
        badges.append({"t": "🌙 Moonshot potenciál", "k": "extreme"})

    score = max(0, min(100, round(score)))
    if score >= 80:
        tier = "EXTRÉMNÍ"
    elif score >= 65:
        tier = "VYSOKÁ"
    elif score >= 50:
        tier = "ZVÝŠENÁ"
    else:
        tier = "SLEDOVAT"

    return score, tier, badges, vol_ratio, moonshot


@app.route("/api/opportunities")
def get_opportunities():
    """Scanner šílených příležitostí napříč více Yahoo screenery."""
    mode = request.args.get("mode", "all")
    screen_map = {
        "gainers": ["day_gainers"],
        "small": ["small_cap_gainers", "aggressive_small_caps"],
        "active": ["most_actives"],
        "all": ["day_gainers", "small_cap_gainers", "aggressive_small_caps", "most_actives"],
    }
    scr_ids = screen_map.get(mode, screen_map["all"])

    seen = {}
    for scr in scr_ids:
        try:
            for q in yahoo_screener(scr, 40):
                sym = q.get("symbol")
                if not sym or sym in seen:
                    continue
                price = _qf(q, "regularMarketPrice", 0) or 0
                if price <= 0:
                    continue
                score, tier, badges, vol_ratio, moonshot = opportunity_score(q)
                seen[sym] = {
                    "ticker": sym,
                    "name": q.get("shortName") or q.get("displayName") or q.get("longName") or sym,
                    "price": _round(price, 4),
                    "change_pct": _round(_qf(q, "regularMarketChangePercent", 0), 2, 0),
                    "currency": q.get("currency", "USD"),
                    "market_cap": _qf(q, "marketCap", None),
                    "volume": int(_qf(q, "regularMarketVolume", 0) or 0),
                    "vol_ratio": _round(vol_ratio, 1, 0),
                    "year_pct": _round(_qf(q, "fiftyTwoWeekChangePercent", 0), 1, 0),
                    "high52": _round(_qf(q, "fiftyTwoWeekHigh", None), 4),
                    "low52": _round(_qf(q, "fiftyTwoWeekLow", None), 4),
                    "exchange": q.get("fullExchangeName") or q.get("exchange") or "",
                    "score": score,
                    "tier": tier,
                    "badges": badges,
                    "moonshot": moonshot,
                    "source": scr,
                }
        except Exception:
            continue

    items = sorted(seen.values(), key=lambda x: x["score"], reverse=True)
    return jsonify({
        "ok": True,
        "count": len(items),
        "updated": datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
        "results": items[:40],
    })


# ---------------------------------------------------------------------------
# Endpointy
# ---------------------------------------------------------------------------
@app.route("/api/search")
def search_ticker():
    query = request.args.get("q", "")
    if len(query) < 1:
        return jsonify({"ok": True, "results": []})
    try:
        url = f"https://query2.finance.yahoo.com/v1/finance/search?q={quote(query)}&quotesCount=8&newsCount=0"
        r = requests.get(url, headers=HEADERS, timeout=8)
        data = r.json()
        results = []
        for q in data.get('quotes', []):
            if q.get('quoteType') in ['EQUITY', 'ETF', 'CRYPTOCURRENCY', 'INDEX', 'FUTURE', 'CURRENCY']:
                results.append({
                    "ticker": q.get('symbol'),
                    "name": q.get('shortname', q.get('longname', 'Neznámý název')),
                    "exchange": q.get('exchDisp', 'Trh'),
                    "type": q.get('quoteType')
                })
        return jsonify({"ok": True, "results": results})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/watchlist")
def get_watchlist():
    tickers_param = request.args.get("tickers", "")
    if not tickers_param:
        return jsonify({"ok": True, "results": []})

    tickers = [t.strip().upper() for t in tickers_param.split(",") if t.strip()]
    results = []
    for ticker in tickers:
        try:
            res = yahoo_chart(ticker, "1d", "1d")
            meta = res.get("meta", {})
            price = meta.get("regularMarketPrice")
            if price is None:
                continue
            prev = meta.get("previousClose") or meta.get("chartPreviousClose") or price
            change_pct = ((price - prev) / prev) * 100 if prev else 0
            results.append({
                "ticker": ticker,
                "name": meta.get("shortName") or meta.get("longName") or ticker,
                "price": _round(price, 3),
                "prev_close": _round(prev, 3),
                "change_pct": _round(change_pct, 2, 0),
                "currency": meta.get("currency", "USD")
            })
        except Exception:
            continue
    return jsonify({"ok": True, "results": results})


@app.route("/api/news/<path:ticker>")
def get_news(ticker):
    try:
        url = f"https://query2.finance.yahoo.com/v1/finance/search?q={quote(ticker.upper())}&quotesCount=0&newsCount=8"
        r = requests.get(url, headers=HEADERS, timeout=8)
        data = r.json()
        items = []
        for n in data.get("news", [])[:8]:
            if not n.get("title"):
                continue
            items.append({
                "title": n.get("title"),
                "link": n.get("link", ""),
                "publisher": n.get("publisher", "Zdroj"),
                "time": n.get("providerPublishTime")
            })
        return jsonify({"ok": True, "results": items})
    except Exception as e:
        return jsonify({"ok": True, "results": [], "error": str(e)})


@app.route("/api/stock/<path:ticker>")
def get_stock_detail(ticker):
    ticker = ticker.upper()
    period = request.args.get("period", "6mo")

    range_map = {
        "1d": ("1d", "5m"), "5d": ("5d", "15m"), "1mo": ("1mo", "1d"),
        "3mo": ("3mo", "1d"), "6mo": ("6mo", "1d"), "1y": ("1y", "1d"),
        "max": ("max", "1wk"),
    }
    rng, interval = range_map.get(period, ("6mo", "1d"))
    is_intraday = period in ("1d", "5d")

    try:
        res = yahoo_chart(ticker, rng, interval)
        meta = res.get("meta", {})
        ts = res.get("timestamp") or []
        quote_node = ((res.get("indicators") or {}).get("quote") or [{}])[0]
        opens = quote_node.get("open") or []
        highs = quote_node.get("high") or []
        lows = quote_node.get("low") or []
        closes = quote_node.get("close") or []
        vols = quote_node.get("volume") or []

        gmt = meta.get("gmtoffset", 0) or 0
        chart_data = []
        closes_for_sma = []
        for i, t in enumerate(ts):
            c = closes[i] if i < len(closes) else None
            if c is None:
                continue
            dt = datetime.fromtimestamp(t + gmt, tz=timezone.utc)
            label = dt.strftime("%d.%m %H:%M") if is_intraday else dt.strftime("%Y-%m-%d")
            closes_for_sma.append(round(c, 4))
            chart_data.append({
                "x": label,
                "o": _round(opens[i] if i < len(opens) else c, 4),
                "h": _round(highs[i] if i < len(highs) else c, 4),
                "l": _round(lows[i] if i < len(lows) else c, 4),
                "c": round(c, 4),
                "v": int(vols[i]) if i < len(vols) and vols[i] is not None else 0
            })

        if not chart_data:
            return jsonify({"ok": False, "error": f"Nenalezena historická data pro {ticker}."})

        if not is_intraday:
            sma20 = sma_overlay(closes_for_sma, 20)
            sma50 = sma_overlay(closes_for_sma, 50)
            sma200 = sma_overlay(closes_for_sma, 200)
        else:
            sma20 = sma50 = sma200 = [None] * len(closes_for_sma)

        # Indikátory z 1 roku denních dat
        try:
            daily = yahoo_chart(ticker, "1y", "1d")
            dq = ((daily.get("indicators") or {}).get("quote") or [{}])[0]
            indicators = compute_indicators(dq.get("close") or [], dq.get("volume") or [])
        except Exception:
            indicators = compute_indicators(closes_for_sma, vols)

        price = _round(meta.get("regularMarketPrice", closes_for_sma[-1]), 3)
        prev = _round(meta.get("previousClose") or meta.get("chartPreviousClose") or price, 3)
        change_pct = ((price - prev) / prev) * 100 if prev else 0
        high52 = _round(meta.get("fiftyTwoWeekHigh", price), 3) or price
        low52 = _round(meta.get("fiftyTwoWeekLow", price), 3) or price

        score, score_label, score_comps = signal_score(price, indicators, high52, low52)

        # Fundamenty (best-effort)
        f = fetch_fundamentals(ticker)
        div = _clean(f.get("dividend_yield"), None)
        if div is not None:
            div = round(div * 100 if div < 1 else div, 2)

        return jsonify({
            "ok": True,
            "ticker": ticker,
            "name": f.get("name") or meta.get("shortName") or meta.get("longName") or ticker,
            "price": price,
            "prev_close": prev,
            "change_pct": _round(change_pct, 2, 0),
            "currency": meta.get("currency", "USD"),
            "sector": f.get("sector") or meta.get("exchangeName", "Trh"),
            "industry": f.get("industry") or meta.get("instrumentType", "Obecné"),
            "description": f.get("description") or "Popis není k dispozici.",
            "pe_ratio": _clean(f.get("pe_ratio"), "N/A"),
            "forward_pe": _clean(f.get("forward_pe"), "N/A"),
            "market_cap": _clean(f.get("market_cap"), "N/A"),
            "high52": high52,
            "low52": low52,
            "open": _round(opens[-1] if opens else None, 3),
            "day_high": _round(meta.get("regularMarketDayHigh"), 3),
            "day_low": _round(meta.get("regularMarketDayLow"), 3),
            "beta": _round(f.get("beta"), 2),
            "eps": _round(f.get("eps"), 2),
            "dividend_yield": div,
            "earnings_ts": None,
            "target_mean": _round(f.get("target_mean"), 2),
            "recommendation": f.get("recommendation"),
            "indicators": indicators,
            "score": score,
            "score_label": score_label,
            "score_components": score_comps,
            "chart": chart_data,
            "sma20": sma20,
            "sma50": sma50,
            "sma200": sma200,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": f"Nepodařilo se stáhnout detail: {e}"})


# Lokální test: python api/index.py -> http://127.0.0.1:5001
if __name__ == "__main__":
    app.run(debug=True, port=5001, host="127.0.0.1")
