#!/usr/bin/env python3
"""
MY STOCKS - Backend pro Vercel (Serverless Function)
Flask app exportovaná jako `app` - Vercel ji automaticky spustí jako
WSGI handler pro všechny požadavky na /api/*

Obsahuje deterministický výpočetní engine (RSI, SMA, MACD, volatilita,
momentum) a Signal Score 0-100, který je nezávislý na AI.
"""

from flask import Flask, jsonify, request
from flask_cors import CORS
import yfinance as yf
import requests
import math
import time

app = Flask(__name__)
CORS(app)


# ---------------------------------------------------------------------------
# Pomocné funkce
# ---------------------------------------------------------------------------
def _clean(v, default=None):
    """Ošetří NaN/inf/None pro bezpečný JSON."""
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


# ---------------------------------------------------------------------------
# Technické indikátory (počítané z denní historie pomocí pandas)
# ---------------------------------------------------------------------------
def compute_indicators(df):
    """df = denní OHLCV DataFrame z yfinance. Vrací dict s indikátory."""
    out = {
        "rsi": None, "sma20": None, "sma50": None, "sma200": None,
        "macd": None, "macd_hist": None, "volatility": None,
        "mom_1m": None, "avg_volume": None, "last_volume": None,
    }
    try:
        close = df["Close"].dropna()
        if len(close) < 5:
            return out

        # RSI(14) – Wilderovo vyhlazení
        delta = close.diff()
        up = delta.clip(lower=0)
        down = -delta.clip(upper=0)
        roll_up = up.ewm(alpha=1 / 14, adjust=False).mean()
        roll_down = down.ewm(alpha=1 / 14, adjust=False).mean()
        rs = roll_up / roll_down.replace(0, float("nan"))
        rsi = 100 - 100 / (1 + rs)
        out["rsi"] = _round(rsi.iloc[-1], 1)

        # Klouzavé průměry
        for w in (20, 50, 200):
            if len(close) >= w:
                out[f"sma{w}"] = _round(close.rolling(w).mean().iloc[-1], 3)

        # MACD (12/26/9)
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd = ema12 - ema26
        signal = macd.ewm(span=9, adjust=False).mean()
        out["macd"] = _round(macd.iloc[-1], 4)
        out["macd_hist"] = _round((macd - signal).iloc[-1], 4)

        # Volatilita (anualizovaná, %)
        ret = close.pct_change().dropna()
        if len(ret) > 2:
            out["volatility"] = _round(ret.std() * (252 ** 0.5) * 100, 1)

        # Momentum za ~1 měsíc (21 obchodních dní)
        if len(close) > 22:
            out["mom_1m"] = _round((close.iloc[-1] / close.iloc[-22] - 1) * 100, 1)

        # Objem
        if "Volume" in df:
            vol = df["Volume"].dropna()
            if len(vol) > 0:
                out["last_volume"] = _clean(int(vol.iloc[-1]), None)
            if len(vol) >= 5:
                out["avg_volume"] = _clean(int(vol.tail(20).mean()), None)
    except Exception:
        pass
    return out


def signal_score(price, ind, high52, low52):
    """Deterministický technický scoring 0-100 + rozpad na faktory."""
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
    """Rolling SMA přes pole close hodnot. Nedostatek dat -> None (mezera v grafu)."""
    out = []
    n = len(closes)
    for i in range(n):
        if i + 1 < window:
            out.append(None)
        else:
            out.append(round(sum(closes[i + 1 - window:i + 1]) / window, 4))
    return out


# ---------------------------------------------------------------------------
# Endpointy
# ---------------------------------------------------------------------------
@app.route("/api/search")
def search_ticker():
    query = request.args.get("q", "")
    if len(query) < 1:
        return jsonify({"ok": True, "results": []})

    try:
        url = f"https://query2.finance.yahoo.com/v1/finance/search?q={query}&quotesCount=8&newsCount=0"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        r = requests.get(url, headers=headers, timeout=8)
        data = r.json()

        results = []
        for quote in data.get('quotes', []):
            if quote.get('quoteType') in ['EQUITY', 'ETF', 'CRYPTOCURRENCY', 'INDEX']:
                results.append({
                    "ticker": quote.get('symbol'),
                    "name": quote.get('shortname', quote.get('longname', 'Neznámý název')),
                    "exchange": quote.get('exchDisp', 'Trh'),
                    "type": quote.get('quoteType')
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
            stock = yf.Ticker(ticker)
            fi = stock.fast_info

            price = getattr(fi, 'last_price', None)
            if price is None:
                continue

            prev_close = getattr(fi, 'previous_close', price)
            change_pct = ((price - prev_close) / prev_close) * 100 if prev_close else 0

            results.append({
                "ticker": ticker,
                "name": ticker,
                "price": _round(price, 3),
                "prev_close": _round(prev_close, 3),
                "change_pct": _round(change_pct, 2, 0),
                "currency": getattr(fi, 'currency', 'USD')
            })
        except Exception:
            continue

    return jsonify({"ok": True, "results": results})


@app.route("/api/news/<path:ticker>")
def get_news(ticker):
    try:
        stock = yf.Ticker(ticker.upper())
        raw = getattr(stock, "news", []) or []
        items = []
        for n in raw[:8]:
            # yfinance vrací buď ploché dicty, nebo zanořené pod "content"
            content = n.get("content", n)
            title = content.get("title") or n.get("title")
            if not title:
                continue
            link = (
                n.get("link")
                or (content.get("clickThroughUrl") or {}).get("url")
                or (content.get("canonicalUrl") or {}).get("url")
                or ""
            )
            publisher = (
                n.get("publisher")
                or (content.get("provider") or {}).get("displayName")
                or "Zdroj"
            )
            ts = n.get("providerPublishTime")
            if not ts:
                pub = content.get("pubDate") or content.get("displayTime")
                ts = pub  # ISO string fallback
            items.append({
                "title": title,
                "link": link,
                "publisher": publisher,
                "time": ts
            })
        return jsonify({"ok": True, "results": items})
    except Exception as e:
        return jsonify({"ok": True, "results": [], "error": str(e)})


@app.route("/api/stock/<path:ticker>")
def get_stock_detail(ticker):
    period = request.args.get("period", "6mo")

    intraday_map = {
        "1d": ("1d", "5m"),
        "5d": ("5d", "15m"),
    }
    hist_period, hist_interval = intraday_map.get(period, (period, "1d"))
    is_intraday = period in ("1d", "5d")

    try:
        stock = yf.Ticker(ticker.upper())
        fi = stock.fast_info
        info = stock.info

        hist = stock.history(period=hist_period, interval=hist_interval)
        if hist.empty:
            return jsonify({"ok": False, "error": f"Nenalezena historická data pro {ticker}."})

        # Graf: OHLC + objem
        chart_data = []
        closes_for_sma = []
        for date, row in hist.iterrows():
            label = date.strftime("%d.%m %H:%M") if is_intraday else date.strftime("%Y-%m-%d")
            c = _round(row["Close"], 4)
            closes_for_sma.append(c if c is not None else 0)
            chart_data.append({
                "x": label,
                "o": _round(row["Open"], 4),
                "h": _round(row["High"], 4),
                "l": _round(row["Low"], 4),
                "c": c,
                "v": _clean(int(row["Volume"]), 0) if "Volume" in row else 0
            })

        # MA overlay jen pro denní grafy (u intraday nedává smysl)
        if not is_intraday:
            sma20 = sma_overlay(closes_for_sma, 20)
            sma50 = sma_overlay(closes_for_sma, 50)
            sma200 = sma_overlay(closes_for_sma, 200)
        else:
            sma20 = sma50 = sma200 = [None] * len(closes_for_sma)

        # Denní historie pro indikátory (vždy ~1 rok, nezávisle na zvoleném období)
        try:
            daily = stock.history(period="1y", interval="1d")
            indicators = compute_indicators(daily)
        except Exception:
            indicators = compute_indicators(hist)

        price = _round(getattr(fi, 'last_price', 0), 3) or 0
        prev_close = _round(getattr(fi, 'previous_close', price), 3) or price
        change_pct = ((price - prev_close) / prev_close) * 100 if prev_close else 0

        high52 = _round(getattr(fi, 'year_high', price), 3) or price
        low52 = _round(getattr(fi, 'year_low', price), 3) or price

        score, score_label, score_comps = signal_score(price, indicators, high52, low52)

        name = info.get("shortName") or info.get("longName") or ticker.upper()

        # Dividendový výnos: nejspolehlivěji z roční dividendy a ceny.
        # (info["dividendYield"] má napříč verzemi yfinance nejednotný formát.)
        div = None
        rate = _clean(info.get("trailingAnnualDividendRate"), None) or _clean(info.get("dividendRate"), None)
        if rate and price:
            div = round(rate / price * 100, 2)
        else:
            dy = _clean(info.get("dividendYield"), None)
            if dy is not None:
                # zlomek (0.0044 -> 0.44 %) vs. už procenta (0.44 -> 0.44 %)
                div = round(dy * 100 if dy < 1 else dy, 2)

        return jsonify({
            "ok": True,
            "ticker": ticker.upper(),
            "name": name,
            "price": price,
            "prev_close": prev_close,
            "change_pct": _round(change_pct, 2, 0),
            "currency": getattr(fi, 'currency', 'USD'),
            "sector": info.get("sector", "Trh"),
            "industry": info.get("industry", "Obecné"),
            "description": info.get("longBusinessSummary", "Popis není k dispozici."),
            "pe_ratio": _clean(info.get("trailingPE"), "N/A"),
            "forward_pe": _clean(info.get("forwardPE"), "N/A"),
            "market_cap": _clean(getattr(fi, 'market_cap', None), info.get("marketCap", "N/A")),
            "high52": high52,
            "low52": low52,
            "open": _round(getattr(fi, 'open', info.get("open")), 3),
            "day_high": _round(getattr(fi, 'day_high', info.get("dayHigh")), 3),
            "day_low": _round(getattr(fi, 'day_low', info.get("dayLow")), 3),
            "beta": _round(info.get("beta"), 2),
            "eps": _round(info.get("trailingEps"), 2),
            "dividend_yield": div,
            "earnings_ts": _clean(info.get("earningsTimestamp"), None),
            "target_mean": _round(info.get("targetMeanPrice"), 2),
            "recommendation": info.get("recommendationKey", None),
            # Technický engine
            "indicators": indicators,
            "score": score,
            "score_label": score_label,
            "score_components": score_comps,
            # Graf
            "chart": chart_data,
            "sma20": sma20,
            "sma50": sma50,
            "sma200": sma200,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": f"Nepodařilo se stáhnout detail: {e}"})


# Lokální testování: python api/index.py -> běží na http://127.0.0.1:5001
if __name__ == "__main__":
    app.run(debug=True, port=5001, host="127.0.0.1")
