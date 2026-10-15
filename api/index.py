#!/usr/bin/env python3
"""
MY ADVANTAGE - Backend pro Vercel (Serverless Function)

Data se tahají PŘÍMO z veřejných Yahoo Finance HTTP endpointů
(v8/finance/chart, v1/finance/search, v10 quoteSummary) pomocí `requests`.
Žádné yfinance/pandas/numpy -> spolehlivé i na serverless (AWS) IP a rychlý cold start.

Indikátory (RSI, SMA, MACD, volatilita, momentum) a Signal Score 0-100
se počítají v čistém Pythonu a jsou nezávislé na AI.
"""

from flask import Flask, jsonify, request, make_response
from flask_cors import CORS
import requests
import math
import os
import json
import hmac
import hashlib
import base64
import secrets
import re
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

app = Flask(__name__)
CORS(app)


@app.after_request
def _security_headers(resp):
    """Bezpečnostní hlavičky na všech API odpovědích."""
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() if fwd else (request.remote_addr or "?"))


def _rate_ok(action, limit, window):
    """Jednoduchý rate-limit per IP (proti hrubé síle). Fail-open při chybě KV."""
    if not cloud_enabled():
        return True
    try:
        key = _pk(f"rl:{action}:{_client_ip()}:{int(time.time()) // window}")
        n = _kv_cmd("INCR", key)
        if n == 1:
            _kv_cmd("EXPIRE", key, window)
        return (n or 0) <= limit
    except Exception:
        return True

# ---------------------------------------------------------------------------
# VSTUPNÍ ZÁMEK (privátní režim)
# Celý web je zavřený na jedno heslo, dokud se nenastaví env SITE_LOCK=off.
# Heslo je v env SITE_PASSWORD (default 1410). Po ověření se nastaví podepsaná
# HttpOnly cookie, takže se heslo zadává jednou za GATE_DAYS dní.
# ---------------------------------------------------------------------------
GATE_COOKIE = "ma_gate"
GATE_DAYS = 30
# Cesty, které zámek NESMÍ zabouchnout – mají vlastní ověření (Stripe podepisuje
# webhook HMACem, crony chrání CRON_SECRET). Bez výjimky by zavření webu tiše
# rozbilo platby a ranní/týdenní e-maily.
GATE_EXEMPT = ("/api/gate", "/api/billing/webhook", "/api/cron/")


def site_lock_enabled():
    v = (os.environ.get("SITE_LOCK") or "on").strip().lower()
    return v not in ("off", "0", "false", "no")


def _site_password():
    return (os.environ.get("SITE_PASSWORD") or "1410").strip()


def _gate_token():
    """Podepsaný token s expirací. Heslo v něm NENÍ, jen čas platnosti."""
    exp = str(int(time.time()) + GATE_DAYS * 86400)
    sig = hmac.new(_secret().encode(), f"gate|{exp}".encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def _gate_token_ok(tok):
    try:
        exp, sig = (tok or "").split(".", 1)
        good = hmac.new(_secret().encode(), f"gate|{exp}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, good) and int(exp) > time.time()
    except Exception:
        return False


def gate_open():
    """True = návštěvník je za zámkem, nebo je zámek vypnutý."""
    return not site_lock_enabled() or _gate_token_ok(request.cookies.get(GATE_COOKIE))


LOCKED_HTML = ("<!doctype html><html lang=\"cs\"><meta charset=\"utf-8\">"
               "<meta name=\"robots\" content=\"noindex\">"
               "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
               "<title>MY ADVANTAGE</title>"
               "<body style=\"margin:0;display:grid;place-items:center;min-height:100vh;"
               "background:#0B0F17;color:#F4F6FA;font:16px system-ui,sans-serif\">"
               "<div style=\"text-align:center;padding:24px\">"
               "<p style=\"font-size:20px;margin:0 0 10px\">Aplikace je dočasně uzavřená.</p>"
               "<p style=\"opacity:.6;margin:0\"><a href=\"/\" style=\"color:#F0A030\">Zadat heslo</a></p>"
               "</div></body></html>")


@app.before_request
def _site_lock():
    """Fail-closed brána před vším, co obsluhuje Python (API + SEO stránky)."""
    if request.method == "OPTIONS" or gate_open():
        return None
    path = request.path or "/"
    # Kořen = statický shell aplikace (na Vercelu ho servíruje @vercel/static a
    # do Pythonu vůbec nedojde). Shell sám o sobě žádná data nenese – zobrazí
    # zamykací obrazovku a všechna /api/* volání pod ním jsou blokovaná níž.
    if path == "/" or path.startswith(GATE_EXEMPT):
        return None
    if path.startswith("/api/"):
        return jsonify({"ok": False, "locked": True,
                        "error": "Aplikace je dočasně uzavřená."}), 403
    return make_response(LOCKED_HTML, 403, {"Content-Type": "text/html; charset=utf-8"})


@app.route("/api/gate", methods=["GET"])
def gate_status():
    return jsonify({"ok": True, "locked": site_lock_enabled(), "open": gate_open()})


@app.route("/api/gate", methods=["POST"])
def gate_unlock():
    if not site_lock_enabled():
        return jsonify({"ok": True, "open": True})
    if not _rate_ok("gate", 10, 300):
        return jsonify({"ok": False, "error": "Příliš mnoho pokusů, zkuste to za chvíli."}), 429
    pw = str((request.get_json(silent=True) or {}).get("password") or "")
    if not hmac.compare_digest(pw, _site_password()):
        return jsonify({"ok": False, "error": "Nesprávné heslo."}), 401
    resp = make_response(jsonify({"ok": True, "open": True}))
    resp.set_cookie(GATE_COOKIE, _gate_token(), max_age=GATE_DAYS * 86400,
                    httponly=True, secure=(request.scheme == "https"),
                    samesite="Lax", path="/")
    return resp


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


def _pct(v, n=1):
    """Zlomek z Yahoo (0.23) -> procenta (23.0). None když nejde."""
    v = _clean(v, None)
    if v is None:
        return None
    try:
        return round(float(v) * 100, n)
    except Exception:
        return None


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


def _prev_close(closes, meta=None):
    """Včerejší zavírací cena z DENNÍCH svíček.

    Pozor na Yahoo: `previousClose` v chart datech vůbec nechodí a
    `chartPreviousClose` je cena PŘED začátkem grafu – u rozsahu 1y před rokem,
    u 5d před pěti dny. Appka tak vydávala vícedenní výnos za denní změnu
    (detail akcie, makro, sektorová heatmapa, skenery). Spolehlivá je jen
    předposlední denní svíčka; u rozsahu 1d (jediná svíčka) je chartPreviousClose
    opravdu včerejšek."""
    cl = [c for c in (closes or []) if c is not None]
    if len(cl) >= 2:
        return cl[-2]
    return (meta or {}).get("chartPreviousClose") or (cl[-1] if cl else None)


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
        "mom_1m": None, "avg_volume": None, "last_volume": None, "vol_ratio": None,
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
        # Poměr objemu: posledních ~5 dní vs posledních ~50 dní. <1 = pokles na
        # klidném objemu (nikdo nepanikaří), >1.15 = zvýšený prodejní tlak.
        # Nulové objemy (svátky/halty) z okna vyřazujeme = chybějící data.
        vlong = [v for v in vols[-50:] if v]
        vshort = [v for v in vols[-5:] if v]
        if len(vlong) >= 20 and vshort:
            base_v = sum(vlong) / len(vlong)
            if base_v:
                out["vol_ratio"] = round((sum(vshort) / len(vshort)) / base_v, 2)
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


# Prahy verdiktu – kalibrované backtestem (viz tech_setup_score).
VERDICT_BUY = 70
VERDICT_SELL = 45
VERDICT_TOP = 80  # konviktní „TOP signál" – přísnější výběr, vyšší doložená trefnost
# TOP tier navíc vyžaduje, aby se signál NEdělal na zvýšeném prodejním objemu
# (5denní průměr objemu vůči ~50dennímu). Backtest 5 let: silný setup s klidným
# objemem měl ~65 % trefnost (každý rok vč. 2022) a expectancy ~+2.3 %/obchod,
# vs. ~60 % široký signál. Práh kalibrován na basketu (viz /tmp/bt_improve3.py).
TOP_VOL_MAX = 1.15

# OBCHODNÍ PLÁN – kalibrovaný na VÝSLEDEK PORTFOLIA, ne na trefnost jednoho obchodu.
# Ladění 2018-2022, ověření na odděleném 2023-2026, 190 titulů vč. propadáků,
# simulace skutečného účtu (omezený počet souběžných pozic, denní přecenění).
#
# Proč cíl +15 % a ne +5 %: dřívější plán +5 %/-8 % měl vyšší trefnost jednotlivého
# obchodu (63 % vs 55 %), ale portfoliově byl NEJHORŠÍ z testovaných – vydělával
# +13,8 % ročně při propadu -19,4 %, zatímco +15 %/-10 % dal +22,2 % ročně při
# MĚLČÍM propadu -16,6 %. Vysoká trefnost se kupuje tím, že se uřízne každý velký
# zisk, zatímco ztráty zůstanou stejné. Poměr zisk:riziko je teď 1,5 (dřív 0,62).
#
# POZOR – měřeno a zamítnuto (nepřidávat zpět):
#   * vystoupit, když technické skóre spadne pod 45  -> -18,5 pb trefnosti
#     (prodává se tím do slabosti přesně na dně)
#   * pojistka na prudký jednodenní propad           -> -5,7 pb až 0,0 pb
#     (od -7 % ji stejně zachytí stop, takže je zbytečná)
#   * koncentrace do méně pozic bez indexového jádra -> horší výnos I hlubší propad
EXIT_TP = 0.15
EXIT_SL = 0.10
EXIT_MAX_DAYS = 30
# Portfolio: max tolik souběžných aktivních pozic, zbytek drží indexové jádro.
# Nezapojená hotovost byla největší jednotlivá ztráta – 71,5 % kapitálu leželo
# ladem a portfolio dělalo +3,8 % ročně místo +11,2 %.
PORTFOLIO_SLOTS = 5
# 30 obch. dní ≈ 6 kalendářních týdnů (většina obchodů skončí dřív na cíli/stopu).
EXIT_HORIZON_LABEL = "do 6 týdnů"


def tech_setup_score(price, ind):
    """Technické skóre 0-100 KALIBROVANÉ BACKTESTEM na 'nákup poklesu v uptrendu'.
    Vysoké skóre = cena nad SMA200 (dlouhodobý uptrend) + nízké RSI (krátkodobý
    pokles = sleva). V downtrendu se nízké RSI neodměňuje (padající nůž).
    Ověřeno na širokém US basketu (2 roky): skóre ≥70 mělo na 10-20 dní win-rate
    ~61-63 % vs baseline ~55-56 % a vyšší průměrný výnos. Vrací None bez dat."""
    rsi = ind.get("rsi")
    sma200 = ind.get("sma200")
    sma50 = ind.get("sma50")
    mom = ind.get("mom_1m")
    if rsi is None or sma200 is None or not price:
        return None
    if price > sma200:  # uptrend → odměň pokles (nízké RSI)
        s = 55 + max(-18, min(35, (50 - rsi) * 1.5)) + (5 if (sma50 and sma50 > sma200) else 0)
        if mom is not None and mom < -20:
            s -= 8
    else:               # downtrend → levné RSI je rizikové, nehoň nůž
        s = 40 + max(-8, min(6, (45 - rsi) * 0.2))
    return max(0, min(100, round(s)))


def is_top_signal(score, ind):
    """TOP (konviktní) signál = silný setup, který se NEděje na zvýšeném prodejním
    objemu. Backtestem doložená trefnost ~65 % (každý rok vč. 2022) vs ~60 % široký
    signál. Když objem neznáme, ber jen práh skóre (best-effort)."""
    if score is None or score < VERDICT_TOP:
        return False
    vr = (ind or {}).get("vol_ratio")
    return vr is None or vr < TOP_VOL_MAX


def _setup_note(score):
    if score is None:
        return ""
    if score >= VERDICT_BUY:
        return "Technicky v nákupní zóně"
    if score >= VERDICT_SELL:
        return "Technicky neutrální"
    return "Technicky slabé"


def _rating_label(buy, sell, neutral):
    total = buy + sell + neutral
    v = (buy - sell) / total if total else 0
    if v >= 0.5:
        return "Silně koupit"
    if v >= 0.15:
        return "Koupit"
    if v <= -0.5:
        return "Silně prodat"
    if v <= -0.15:
        return "Prodat"
    return "Neutrál"


def technical_rating(price, ind):
    """Vlastní souhrnné technické hodnocení (budík Koupit/Prodat) z indikátorů.
    Hlasy klouzavých průměrů + oscilátorů, žádný externí zdroj."""
    ma_buy = ma_sell = 0
    for key in ("sma20", "sma50", "sma200"):
        v = ind.get(key)
        if price and v:
            if price > v:
                ma_buy += 1
            elif price < v:
                ma_sell += 1
    if ind.get("sma50") and ind.get("sma200"):
        if ind["sma50"] > ind["sma200"]:
            ma_buy += 1
        else:
            ma_sell += 1

    osc_buy = osc_sell = osc_neu = 0
    r = ind.get("rsi")
    if r is not None:
        if r < 30:
            osc_buy += 1
        elif r > 70:
            osc_sell += 1
        else:
            osc_neu += 1
    mh = ind.get("macd_hist")
    if mh is not None:
        if mh > 0:
            osc_buy += 1
        elif mh < 0:
            osc_sell += 1
        else:
            osc_neu += 1
    m1 = ind.get("mom_1m")
    if m1 is not None:
        if m1 > 0:
            osc_buy += 1
        elif m1 < 0:
            osc_sell += 1
        else:
            osc_neu += 1

    buy = ma_buy + osc_buy
    sell = ma_sell + osc_sell
    neutral = osc_neu
    total = buy + sell + neutral
    value = (buy - sell) / total if total else 0
    return {
        "value": round(value, 3),
        "label": _rating_label(buy, sell, neutral),
        "buy": buy, "sell": sell, "neutral": neutral,
        "ma": {"buy": ma_buy, "sell": ma_sell,
               "label": _rating_label(ma_buy, ma_sell, 0)},
        "osc": {"buy": osc_buy, "sell": osc_sell, "neutral": osc_neu,
                "label": _rating_label(osc_buy, osc_sell, osc_neu)},
    }


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


def _quote_summary(ticker, modules):
    """Best-effort Yahoo quoteSummary -> result[0] dict (víc modulů najednou).
    Vrací {} při jakémkoli selhání (crumb, síť, prázdná data)."""
    try:
        s = requests.Session()
        s.headers.update(HEADERS)
        s.get("https://finance.yahoo.com/quote/" + quote(ticker, safe=''), timeout=4)
        crumb = s.get("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=4).text.strip()
        if not crumb or "<" in crumb or len(crumb) > 40:
            return {}
        url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{quote(ticker, safe='')}"
        r = s.get(url, params={"modules": modules, "crumb": crumb}, timeout=7)
        result = (((r.json() or {}).get("quoteSummary") or {}).get("result") or [])
        return result[0] if result else {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Ochrana heslem (volitelná – aktivuje se nastavením env APP_PASSWORD na Vercelu)
# ---------------------------------------------------------------------------
@app.route("/api/auth", methods=["GET"])
def auth_status():
    """Řekne frontendu, jestli je appka chráněná heslem."""
    return jsonify({"protected": bool(os.environ.get("APP_PASSWORD"))})


@app.route("/api/auth", methods=["POST"])
def auth_check():
    """Ověří heslo proti env APP_PASSWORD. Heslo není nikde v kódu."""
    real = os.environ.get("APP_PASSWORD")
    if not real:
        return jsonify({"ok": True, "protected": False})
    pw = (request.get_json(silent=True) or {}).get("password", "")
    if pw == real:
        return jsonify({"ok": True, "protected": True})
    return jsonify({"ok": False, "error": "Nesprávné heslo"}), 401


# ---------------------------------------------------------------------------
# Uživatelské účty + cloudová synchronizace portfolia (Vercel KV / Upstash Redis)
#
# Data se ukládají do Redis (REST API) – přístupy bere z env proměnných, které
# Vercel doplní sám po připojení úložiště (Storage). Když úložiště není
# nastavené, účty se prostě nenabídnou a appka jede lokálně (localStorage).
# ---------------------------------------------------------------------------
def _kv_creds():
    url = (os.environ.get("KV_REST_API_URL")
           or os.environ.get("UPSTASH_REDIS_REST_URL")
           or os.environ.get("REDIS_REST_API_URL"))
    tok = (os.environ.get("KV_REST_API_TOKEN")
           or os.environ.get("UPSTASH_REDIS_REST_TOKEN")
           or os.environ.get("REDIS_REST_API_TOKEN"))
    return url, tok


def cloud_enabled():
    u, t = _kv_creds()
    return bool(u and t)


def _kv_cmd(*args):
    """Spustí jeden Redis příkaz přes Upstash REST API. Vrací 'result' nebo None."""
    url, tok = _kv_creds()
    if not (url and tok):
        return None
    r = requests.post(url.rstrip("/"),
                      headers={"Authorization": f"Bearer {tok}",
                               "Content-Type": "application/json"},
                      data=json.dumps([str(a) for a in args]), timeout=8)
    j = r.json()
    if isinstance(j, dict) and "error" in j and j.get("error"):
        raise RuntimeError(j["error"])
    return (j or {}).get("result")


# Prefix všech klíčů – aby šlo bezpečně sdílet jednu Redis databázi s jiným
# projektem, aniž by se data potkala. Lze přepsat env proměnnou KV_PREFIX.
KV_PREFIX = os.environ.get("KV_PREFIX", "ms:")


def _pk(key):
    return f"{KV_PREFIX}{key}"


def kv_get_json(key):
    res = _kv_cmd("GET", _pk(key))
    if not res:
        return None
    try:
        return json.loads(res)
    except Exception:
        return None


def kv_set_json(key, value):
    return _kv_cmd("SET", _pk(key), json.dumps(value))


# ---- Hesla (pbkdf2, čistá stdlib) ----
def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120000)
    return salt, dk.hex()


def verify_password(password, salt, expected_hex):
    _, calc = hash_password(password, salt)
    return hmac.compare_digest(calc, expected_hex)


# ---- Tokeny (bezstavové, podepsané HMAC) ----
def _secret():
    return (os.environ.get("APP_SECRET")
            or os.environ.get("APP_PASSWORD")
            or "mystocks-default-secret-change-me")


def make_token(username):
    issued = str(int(time.time()))
    payload = base64.urlsafe_b64encode(f"{username}|{issued}".encode()).decode().rstrip("=")
    sig = hmac.new(_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_token(token):
    try:
        payload, sig = token.split(".", 1)
        exp = hmac.new(_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, exp):
            return None
        pad = "=" * (-len(payload) % 4)
        raw = base64.urlsafe_b64decode(payload + pad).decode()
        return raw.split("|", 1)[0]
    except Exception:
        return None


def _auth_user():
    """Vytáhne přihlášeného uživatele z hlavičky Authorization: Bearer <token>."""
    h = request.headers.get("Authorization", "")
    if not h.startswith("Bearer "):
        return None
    return verify_token(h[7:].strip())


USERNAME_RE = re.compile(r"^[a-z0-9_.-]{3,20}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _norm_user(u):
    return (u or "").strip().lower()


def _norm_email(e):
    return (e or "").strip().lower()


# ---- Odesílání e-mailů přes Brevo (volitelné; bez klíče se jen přeskočí) ----
def email_enabled():
    return bool(os.environ.get("BREVO_API_KEY") and os.environ.get("SENDER_EMAIL"))


# Poslední chyba odesílání e-mailu (pro admin diagnostiku). V serverless platí
# jen v rámci jednoho requestu – proto ji vracíme rovnou v odpovědi test endpointu.
_LAST_EMAIL_ERROR = {"msg": None}


def send_email(to_email, subject, html):
    """Pošle transakční e-mail přes Brevo. Vrací True/False. Nikdy nevyhodí výjimku.
    Důvod případného selhání ukládá do _LAST_EMAIL_ERROR."""
    if not email_enabled():
        _LAST_EMAIL_ERROR["msg"] = "E-maily nejsou nastavené (chybí BREVO_API_KEY nebo SENDER_EMAIL)."
        return False
    try:
        r = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"api-key": os.environ["BREVO_API_KEY"],
                     "Content-Type": "application/json", "accept": "application/json"},
            data=json.dumps({
                "sender": {"email": os.environ["SENDER_EMAIL"], "name": "MY ADVANTAGE"},
                "to": [{"email": to_email}],
                "subject": subject,
                "htmlContent": html,
            }), timeout=8)
        if r.status_code in (200, 201):
            _LAST_EMAIL_ERROR["msg"] = None
            return True
        _LAST_EMAIL_ERROR["msg"] = f"Brevo HTTP {r.status_code}: {r.text[:300]}"
        return False
    except Exception as e:
        _LAST_EMAIL_ERROR["msg"] = f"Výjimka při odesílání: {e}"
        return False


def _email_shell(title, body_html):
    return f"""<div style="font-family:Arial,sans-serif;background:#0b0d12;padding:32px;color:#e8eaf0">
      <div style="max-width:520px;margin:0 auto;background:#12141c;border:1px solid #23262f;border-radius:18px;padding:32px">
        <div style="font-size:24px;font-weight:800;margin-bottom:18px">MY <span style="color:#FF7A00">ADVANTAGE</span></div>
        <h2 style="font-size:20px;margin:0 0 14px">{title}</h2>
        {body_html}
        <p style="color:#9ba1b0;font-size:12px;margin-top:28px">Tento e-mail ti přišel z aplikace MY ADVANTAGE.</p>
      </div></div>"""


def send_welcome_email(email, eff="none"):
    if eff == "elite":
        body = ("<p style='line-height:1.6'>Tvůj účet má <b>plný přístup (Elite)</b> — máš odemčené vše: "
                "příležitosti s potenciálem, doporučení analytiků i hloubkovou analýzu.</p>")
    elif eff in ("pro", "start"):
        body = ("<p style='line-height:1.6'>Právě ti začalo <b>7 dní plánu Pro zdarma</b> — vyzkoušej "
                "příležitosti s potenciálem a doporučení analytiků. Po zkušební době máš slevu na předplatné.</p>")
    else:
        body = ("<p style='line-height:1.6'>Tvůj účet je připravený. Pro přístup k funkcím si "
                "vyber předplatné v profilu.</p>")
    html = _email_shell("Vítej v MY ADVANTAGE! 🎉",
                        body + "<p style='line-height:1.6'>Přejeme šťastnou ruku při investování!</p>")
    return send_email(email, "Vítej v MY ADVANTAGE 🎉", html)


def send_reset_email(email, link):
    html = _email_shell(
        "Obnovení hesla",
        f"<p style='line-height:1.6'>Klikni na tlačítko a nastav si nové heslo. "
        f"Odkaz platí 1 hodinu.</p>"
        f"<p style='margin:22px 0'><a href='{link}' style='background:#FF7A00;color:#000;"
        f"text-decoration:none;padding:12px 22px;border-radius:10px;font-weight:700'>Nastavit nové heslo</a></p>"
        f"<p style='color:#9ba1b0;font-size:12px'>Pokud jsi o reset nežádal, e-mail ignoruj.</p>")
    return send_email(email, "Obnovení hesla – MY ADVANTAGE", html)


# ---- Předplatné / plány ----
# Appka je plně placená. Bez přístupu = 'none'. Žebříček: start < pro < elite.
# trial (přes promo kód) = 7 dní Pro. comped (kód pro rodinu) = Elite natrvalo.
TRIAL_DAYS = 7
VALID_PLANS = ("start", "pro", "elite")
PLAN_RANK = {"none": -1, "start": 0, "pro": 1, "elite": 2}


def admin_users():
    return set(x.strip().lower() for x in (os.environ.get("ADMIN_USERS", "")).split(",") if x.strip())


def effective_plan(rec):
    """Skutečně aktivní plán. 'none' = bez přístupu (musí zaplatit nebo použít kód)."""
    rec = rec or {}
    plan = rec.get("plan", "none")
    now = int(time.time())
    if plan == "comped":
        return "elite"
    if plan == "trial":
        return (rec.get("trial_tier") or "pro") if now <= (rec.get("trial_ends") or 0) else "none"
    if plan in VALID_PLANS:
        pu = rec.get("plan_until")
        if pu and now > pu:
            return "none"
        return plan
    return "none"


def subscription_info(username, rec):
    rec = rec or {}
    now = int(time.time())
    eff = effective_plan(rec)
    info = {
        "plan": rec.get("plan", "none"),
        "effective": eff,
        "has_access": eff != "none",
        "trial_ends": rec.get("trial_ends"),
        "plan_until": rec.get("plan_until"),
        "discount_pct": rec.get("discount_pct"),
        "discount_until": rec.get("discount_until"),
        "is_admin": username in admin_users(),
    }
    if rec.get("plan") == "trial" and rec.get("trial_ends"):
        rem = rec["trial_ends"] - now
        info["days_left"] = max(0, rem // 86400 + (1 if rem % 86400 else 0))
        info["trial_active"] = now <= rec["trial_ends"]
    return info


def kv_sadd(key, member):
    return _kv_cmd("SADD", _pk(key), member)


def kv_smembers(key):
    res = _kv_cmd("SMEMBERS", _pk(key))
    return res if isinstance(res, list) else []


def kv_srem(key, member):
    return _kv_cmd("SREM", _pk(key), member)


def kv_del(key):
    return _kv_cmd("DEL", _pk(key))


def kv_rpush(key, value):
    return _kv_cmd("RPUSH", _pk(key), json.dumps(value))


def kv_lrange(key, start=0, stop=-1):
    res = _kv_cmd("LRANGE", _pk(key), start, stop)
    out = []
    for x in (res or []):
        try:
            out.append(json.loads(x))
        except Exception:
            pass
    return out


@app.route("/api/account/status")
def account_status():
    return jsonify({"cloud": cloud_enabled()})


@app.route("/api/register", methods=["POST"])
def register():
    if not cloud_enabled():
        return jsonify({"ok": False, "error": "Účty nejsou nastavené (chybí úložiště)."}), 400
    if not _rate_ok("register", 8, 600):
        return jsonify({"ok": False, "error": "Příliš mnoho pokusů. Zkus to za chvíli."}), 429
    body = request.get_json(silent=True) or {}
    email = _norm_email(body.get("email"))
    password = body.get("password") or ""
    if not EMAIL_RE.match(email):
        return jsonify({"ok": False, "error": "Zadej platný e-mail."}), 400
    if len(password) < 6:
        return jsonify({"ok": False, "error": "Heslo musí mít aspoň 6 znaků."}), 400
    invite = (body.get("invite") or "").strip().lower()
    ref_code = (body.get("ref_code") or "").strip().upper()
    try:
        if kv_get_json(f"user:{email}"):
            return jsonify({"ok": False, "error": "Účet s tímto e-mailem už existuje."}), 409

        now = int(time.time())
        # Bez kódu = žádný přístup, dokud si nezvolí placený plán
        rec = {"salt": None, "hash": None, "created": now, "plan": "none", "email": email}

        # Referral kód: pokud existuje a odkazuje na jiného uživatele, tag referrera
        if ref_code:
            referrer_email = _resolve_ref_code(ref_code)
            if referrer_email and referrer_email != email:
                rec["referred_by"] = referrer_email
                # Přidej referee do seznamu invited u referrera (idempotentně)
                try:
                    ref_rec = kv_get_json(f"user:{referrer_email}") or {}
                    invited = ref_rec.get("ref_invited") or []
                    if email not in invited:
                        invited.append(email)
                        ref_rec["ref_invited"] = invited
                        kv_set_json(f"user:{referrer_email}", ref_rec)
                except Exception:
                    pass

        if invite:
            inv = kv_get_json(f"invite:{invite}")
            if not inv:
                return jsonify({"ok": False, "error": "Neplatný kód."}), 400
            ctype = inv.get("type", "comped")
            if ctype == "comped":
                # Jednorázový kód → Elite natrvalo (rodina/kamarádi)
                if inv.get("used_by"):
                    return jsonify({"ok": False, "error": "Tento kód už byl použit."}), 409
                rec["plan"] = "comped"
                inv["used_by"] = email
                inv["used_at"] = now
                kv_set_json(f"invite:{invite}", inv)
            else:
                # Promo kód (sdílený, vícenásobný) → 7 dní Pro + sleva na 3 měsíce
                trial_days = int(inv.get("trial_days", TRIAL_DAYS))
                rec["plan"] = "trial"
                rec["trial_tier"] = inv.get("trial_tier", "pro")
                rec["trial_ends"] = now + trial_days * 86400
                rec["promo_code"] = invite
                disc = int(inv.get("discount_pct", 0) or 0)
                if disc:
                    rec["discount_pct"] = disc
                    rec["discount_until"] = now + int(inv.get("discount_months", 3)) * 30 * 86400
                # Referral tracking – přidej uživatele k seznamu kódu
                uses = inv.get("uses") or []
                uses.append({"email": email, "ts": now})
                inv["uses"] = uses
                kv_set_json(f"invite:{invite}", inv)

        salt, ph = hash_password(password)
        rec["salt"], rec["hash"] = salt, ph
        kv_set_json(f"user:{email}", rec)
        kv_set_json(f"portfolio:{email}", {"watchlist": [], "positions": {}, "alerts": []})
        kv_sadd("users", email)
        send_welcome_email(email, effective_plan(rec))
        return jsonify({"ok": True, "token": make_token(email), "user": email,
                        "subscription": subscription_info(email, rec)})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Chyba úložiště: {e}"}), 500


@app.route("/api/login", methods=["POST"])
def login():
    if not cloud_enabled():
        return jsonify({"ok": False, "error": "Účty nejsou nastavené (chybí úložiště)."}), 400
    if not _rate_ok("login", 15, 300):
        return jsonify({"ok": False, "error": "Příliš mnoho pokusů o přihlášení. Zkus to za pár minut."}), 429
    body = request.get_json(silent=True) or {}
    email = _norm_email(body.get("email"))
    password = body.get("password") or ""
    try:
        u = kv_get_json(f"user:{email}")
        if not u or not verify_password(password, u.get("salt", ""), u.get("hash", "")):
            return jsonify({"ok": False, "error": "Špatný e-mail nebo heslo."}), 401
        portfolio = kv_get_json(f"portfolio:{email}") or {"watchlist": [], "positions": {}, "alerts": []}
        return jsonify({"ok": True, "token": make_token(email), "user": email,
                        "portfolio": portfolio, "subscription": subscription_info(email, u)})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Chyba úložiště: {e}"}), 500


@app.route("/api/forgot", methods=["POST"])
def forgot_password():
    if not cloud_enabled():
        return jsonify({"ok": True})  # nic neprozrazujeme
    if not _rate_ok("forgot", 6, 600):
        return jsonify({"ok": True})  # tváříme se OK, ale nic nepošleme
    email = _norm_email((request.get_json(silent=True) or {}).get("email"))
    try:
        if EMAIL_RE.match(email) and kv_get_json(f"user:{email}"):
            token = secrets.token_urlsafe(24)
            kv_set_json(f"reset:{token}", {"email": email, "exp": int(time.time()) + 3600})
            base = os.environ.get("APP_URL") or request.host_url.rstrip("/")
            send_reset_email(email, f"{base}/?reset={token}")
    except Exception:
        pass
    # Vždy ok – neprozrazujeme, jestli e-mail existuje
    return jsonify({"ok": True})


@app.route("/api/reset", methods=["POST"])
def reset_password():
    if not cloud_enabled():
        return jsonify({"ok": False, "error": "Účty nejsou nastavené."}), 400
    body = request.get_json(silent=True) or {}
    token = (body.get("token") or "").strip()
    password = body.get("password") or ""
    if len(password) < 6:
        return jsonify({"ok": False, "error": "Heslo musí mít aspoň 6 znaků."}), 400
    try:
        data = kv_get_json(f"reset:{token}")
        if not data or data.get("exp", 0) < int(time.time()):
            return jsonify({"ok": False, "error": "Odkaz je neplatný nebo vypršel."}), 400
        email = data["email"]
        rec = kv_get_json(f"user:{email}")
        if not rec:
            return jsonify({"ok": False, "error": "Účet neexistuje."}), 404
        rec["salt"], rec["hash"] = hash_password(password)
        kv_set_json(f"user:{email}", rec)
        kv_set_json(f"reset:{token}", {"email": email, "exp": 0})  # zneplatnit
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Chyba úložiště: {e}"}), 500


@app.route("/api/me")
def me():
    user = _auth_user()
    if not user:
        return jsonify({"ok": False, "error": "Nepřihlášeno."}), 401
    try:
        rec = kv_get_json(f"user:{user}")
        if not rec:
            return jsonify({"ok": False, "error": "Účet neexistuje."}), 404
        return jsonify({"ok": True, "user": user, "subscription": subscription_info(user, rec),
                        "notif": rec.get("notif", {"morning": False})})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Chyba úložiště: {e}"}), 500


# ---------------------------------------------------------------------------
# Platby přes Stripe (Checkout + webhook). Aktivuje se až nastavením env klíčů
# (STRIPE_SECRET_KEY, STRIPE_PRICE_START/PRO/ELITE, STRIPE_WEBHOOK_SECRET).
# Žádná knihovna navíc – Stripe REST přes requests.
# ---------------------------------------------------------------------------
def _stripe_prices():
    return {"start": os.environ.get("STRIPE_PRICE_START"),
            "pro": os.environ.get("STRIPE_PRICE_PRO"),
            "elite": os.environ.get("STRIPE_PRICE_ELITE")}


def stripe_enabled():
    return bool(os.environ.get("STRIPE_SECRET_KEY") and any(_stripe_prices().values()))


def _set_user_plan(email, plan, sub_id=None, cust_id=None):
    rec = kv_get_json(f"user:{email}")
    if not rec:
        return
    prev_paid = rec.get("plan") in ("start", "pro", "elite")
    rec["plan"] = plan
    rec["plan_until"] = None  # předplatné běží, dokud ho Stripe neukončí
    if sub_id:
        rec["stripe_sub"] = sub_id
    if cust_id:
        rec["stripe_customer"] = cust_id
    kv_set_json(f"user:{email}", rec)
    # Referral bonus: první přechod na placený plán aktivuje +30 dní pro referrera i referee
    if plan in ("start", "pro", "elite") and not prev_paid:
        try:
            _activate_referral_bonus(email, rec)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# REFERRAL PROGRAM – „Přiveď kamaráda"
# ---------------------------------------------------------------------------
def _gen_ref_code(email):
    """Deterministický, přesto neuhodnutelný kód. Prefix z e-mailu + hash zbytek."""
    import hashlib
    prefix = "".join(c for c in (email or "").split("@")[0].upper() if c.isalnum())[:6] or "USER"
    h = hashlib.sha256((email + "::ref::" + os.environ.get("SECRET_KEY", "")).encode()).hexdigest()
    suffix = h[:4].upper()
    return f"{prefix}-{suffix}"


def _ensure_ref_code(email, rec):
    """Zajistí, že uživatel má ref_code + obrácený index."""
    if not rec.get("ref_code"):
        code = _gen_ref_code(email)
        rec["ref_code"] = code
        kv_set_json(f"user:{email}", rec)
        try:
            kv_set_json(f"refcode:{code}", {"owner": email})
        except Exception:
            pass
    return rec["ref_code"]


def _resolve_ref_code(code):
    """Kód → email vlastníka (nebo None)."""
    if not code:
        return None
    code = code.strip().upper()
    try:
        rec = kv_get_json(f"refcode:{code}") or {}
        return rec.get("owner")
    except Exception:
        return None


def _referral_stats(email):
    """Vrátí statistiky pro dashboard: invited, paid, bonus_days."""
    try:
        rec = kv_get_json(f"user:{email}") or {}
        invited = rec.get("ref_invited") or []  # list emailů
        paid = rec.get("ref_paid") or []        # list emailů, kteří převedli
        bonus = int(rec.get("ref_bonus_days") or 0)
        return {
            "code": rec.get("ref_code"),
            "invited_count": len(invited),
            "paid_count": len(paid),
            "bonus_days_pending": bonus,
        }
    except Exception:
        return {}


def _activate_referral_bonus(referee_email, referee_rec):
    """Přechod referee na placený plán → +30 dní pro referrera i referee."""
    referrer_email = referee_rec.get("referred_by")
    if not referrer_email:
        return
    ref_rec = kv_get_json(f"user:{referrer_email}")
    if not ref_rec:
        return
    # Zvýš referrera
    ref_rec["ref_bonus_days"] = int(ref_rec.get("ref_bonus_days") or 0) + 30
    paid_list = ref_rec.get("ref_paid") or []
    if referee_email not in paid_list:
        paid_list.append(referee_email)
    ref_rec["ref_paid"] = paid_list
    kv_set_json(f"user:{referrer_email}", ref_rec)
    # Zvýš referee
    referee_rec["ref_bonus_days"] = int(referee_rec.get("ref_bonus_days") or 0) + 30
    kv_set_json(f"user:{referee_email}", referee_rec)


@app.route("/api/notifications/unread")
def api_notifications_unread():
    """Vrátí počet nových událostí od posledního checkpointu uživatele.
    Klient uloží ts_last_seen do KV při otevření Přehledu."""
    user = _auth_user()
    if not user:
        return jsonify({"ok": True, "count": 0, "items": []})
    rec = kv_get_json(f"user:{user}") or {}
    last_seen = int(rec.get("events_seen_ts") or 0)
    pf = kv_get_json(f"portfolio:{user}") or {}
    tickers = (pf.get("watchlist") or [])[:20]
    if not tickers:
        return jsonify({"ok": True, "count": 0, "items": []})
    from datetime import date as _date
    today = datetime.now(timezone.utc).date()
    # Načti události za 7 dní a spočítej ty novější než last_seen
    events = []
    for t in tickers:
        try:
            events += _events_for_ticker(t, since_days=7)
        except Exception:
            continue
    fresh = []
    for e in events:
        try:
            d = _date.fromisoformat(e.get("date", ""))
            e_ts = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())
            if e_ts >= last_seen:
                fresh.append(e)
        except Exception:
            continue
    fresh.sort(key=lambda x: x.get("date") or "", reverse=True)
    return jsonify({"ok": True, "count": len(fresh), "items": fresh[:5],
                    "last_seen": last_seen,
                    "now": int(time.time())})


@app.route("/api/notifications/seen", methods=["POST"])
def api_notifications_seen():
    user = _auth_user()
    if not user:
        return jsonify({"ok": False, "error": "Nepřihlášeno."}), 401
    rec = kv_get_json(f"user:{user}")
    if not rec:
        return jsonify({"ok": False, "error": "Účet neexistuje."}), 404
    rec["events_seen_ts"] = int(time.time())
    kv_set_json(f"user:{user}", rec)
    return jsonify({"ok": True})


@app.route("/api/referral")
def api_referral():
    """Vrátí referral kód + statistiky pro přihlášeného uživatele."""
    user = _auth_user()
    if not user:
        return jsonify({"ok": False, "error": "Nepřihlášeno."}), 401
    rec = kv_get_json(f"user:{user}") or {}
    if not rec:
        return jsonify({"ok": False, "error": "Účet neexistuje."}), 404
    _ensure_ref_code(user, rec)
    stats = _referral_stats(user)
    stats["share_url"] = f"{APP_URL}/?ref={stats.get('code','')}"
    return jsonify({"ok": True, **stats})


@app.route("/api/billing/config")
def billing_config():
    prices = _stripe_prices()
    return jsonify({"ok": True, "enabled": stripe_enabled(),
                    "plans": [p for p in VALID_PLANS if prices.get(p)]})


@app.route("/api/billing/checkout", methods=["POST"])
def billing_checkout():
    user = _auth_user()
    if not user:
        return jsonify({"ok": False, "error": "Přihlas se."}), 401
    if not stripe_enabled():
        return jsonify({"ok": False, "error": "Platby zatím nejsou nastavené."}), 503
    if not _rate_ok("checkout", 20, 300):
        return jsonify({"ok": False, "error": "Příliš mnoho pokusů, zkus to za chvíli."}), 429
    plan = ((request.get_json(silent=True) or {}).get("plan") or "").lower()
    price = _stripe_prices().get(plan)
    if not price:
        return jsonify({"ok": False, "error": "Neplatný plán."}), 400
    base = os.environ.get("APP_URL") or request.host_url.rstrip("/")
    data = {
        "mode": "subscription",
        "line_items[0][price]": price,
        "line_items[0][quantity]": "1",
        "success_url": f"{base}/?paid=1",
        "cancel_url": f"{base}/?paid=0",
        "client_reference_id": user,
        "customer_email": user,
        "metadata[email]": user, "metadata[plan]": plan,
        "subscription_data[metadata][email]": user,
        "subscription_data[metadata][plan]": plan,
        "allow_promotion_codes": "true",
    }
    # 20% sleva z promo kódu (3 měsíce) → Stripe kupón, pokud je nastavený
    try:
        rec = kv_get_json(f"user:{user}") or {}
        coupon = os.environ.get("STRIPE_COUPON_20")
        if coupon and rec.get("discount_pct") and (rec.get("discount_until") or 0) > int(time.time()):
            data["discounts[0][coupon]"] = coupon
            data.pop("allow_promotion_codes", None)  # discounts a promo kódy nejdou spolu
    except Exception:
        pass
    try:
        r = requests.post("https://api.stripe.com/v1/checkout/sessions",
                          auth=(os.environ["STRIPE_SECRET_KEY"], ""), data=data, timeout=12)
        j = r.json()
        if r.status_code >= 300 or not j.get("url"):
            return jsonify({"ok": False, "error": (j.get("error") or {}).get("message") or "Stripe chyba."}), 502
        return jsonify({"ok": True, "url": j["url"]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _stripe_verify(payload, sig_header, secret):
    """Ověří podpis Stripe webhooku (HMAC-SHA256). payload = raw bytes."""
    try:
        parts = dict(p.split("=", 1) for p in sig_header.split(",") if "=" in p)
        t, v1 = parts.get("t"), parts.get("v1")
        if not t or not v1:
            return False
        signed = t.encode() + b"." + payload
        exp = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        return hmac.compare_digest(exp, v1)
    except Exception:
        return False


@app.route("/api/billing/webhook", methods=["POST"])
def billing_webhook():
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
    payload = request.get_data()
    if not secret or not _stripe_verify(payload, request.headers.get("Stripe-Signature", ""), secret):
        return jsonify({"ok": False}), 400
    try:
        event = json.loads(payload)
    except Exception:
        return jsonify({"ok": False}), 400
    typ = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}
    try:
        if typ == "checkout.session.completed":
            email = (obj.get("metadata") or {}).get("email") or obj.get("client_reference_id") or obj.get("customer_email")
            plan = (obj.get("metadata") or {}).get("plan")
            if email and plan in VALID_PLANS:
                _set_user_plan(email.lower(), plan, obj.get("subscription"), obj.get("customer"))
        elif typ == "customer.subscription.deleted" or (
                typ == "customer.subscription.updated" and obj.get("status") in ("canceled", "unpaid", "incomplete_expired")):
            email = (obj.get("metadata") or {}).get("email")
            if email:
                _set_user_plan(email.lower(), "none")
    except Exception:
        pass
    return jsonify({"ok": True})


@app.route("/api/notifications", methods=["GET", "POST"])
def notifications():
    user = _auth_user()
    if not user:
        return jsonify({"ok": False, "error": "Nepřihlášeno."}), 401
    try:
        rec = kv_get_json(f"user:{user}")
        if not rec:
            return jsonify({"ok": False, "error": "Účet neexistuje."}), 404
        if request.method == "POST":
            body = request.get_json(silent=True) or {}
            rec["notif"] = {
                "morning": bool(body.get("morning")),
                "weekly": bool(body.get("weekly")),
            }
            kv_set_json(f"user:{user}", rec)
        return jsonify({"ok": True, "notif": rec.get("notif", {"morning": False, "weekly": False})})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Chyba úložiště: {e}"}), 500


@app.route("/api/portfolio", methods=["GET"])
def get_portfolio():
    user = _auth_user()
    if not user:
        return jsonify({"ok": False, "error": "Nepřihlášeno."}), 401
    try:
        portfolio = kv_get_json(f"portfolio:{user}") or {"watchlist": [], "positions": {}, "alerts": []}
        return jsonify({"ok": True, "user": user, "portfolio": portfolio})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Chyba úložiště: {e}"}), 500


@app.route("/api/portfolio", methods=["POST"])
def save_portfolio():
    user = _auth_user()
    if not user:
        return jsonify({"ok": False, "error": "Nepřihlášeno."}), 401
    body = request.get_json(silent=True) or {}
    portfolio = {
        "watchlist": body.get("watchlist") if isinstance(body.get("watchlist"), list) else [],
        "positions": body.get("positions") if isinstance(body.get("positions"), dict) else {},
        "alerts": body.get("alerts") if isinstance(body.get("alerts"), list) else [],
        "updated": int(time.time()),
    }
    try:
        kv_set_json(f"portfolio:{user}", portfolio)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Chyba úložiště: {e}"}), 500


# ---------------------------------------------------------------------------
# Admin (pouze pro uživatele uvedené v env ADMIN_USERS)
# ---------------------------------------------------------------------------
def _auth_admin():
    u = _auth_user()
    return u if (u and u in admin_users()) else None


@app.route("/api/admin/users")
def admin_list_users():
    if not _auth_admin():
        return jsonify({"ok": False, "error": "Přístup jen pro admina."}), 403
    try:
        out = []
        for uname in kv_smembers("users"):
            rec = kv_get_json(f"user:{uname}") or {}
            pf = kv_get_json(f"portfolio:{uname}") or {}
            info = subscription_info(uname, rec)
            info.update({
                "username": uname,
                "created": rec.get("created"),
                "positions": len((pf.get("positions") or {})),
                "watchlist": len((pf.get("watchlist") or [])),
            })
            out.append(info)
        out.sort(key=lambda x: x.get("created") or 0, reverse=True)
        return jsonify({"ok": True, "users": out, "count": len(out)})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Chyba úložiště: {e}"}), 500


@app.route("/api/admin/set-plan", methods=["POST"])
def admin_set_plan():
    if not _auth_admin():
        return jsonify({"ok": False, "error": "Přístup jen pro admina."}), 403
    body = request.get_json(silent=True) or {}
    username = _norm_user(body.get("username"))
    plan = (body.get("plan") or "").strip().lower()
    days = body.get("days")
    if plan not in VALID_PLANS and plan not in ("comped", "trial", "none"):
        return jsonify({"ok": False, "error": "Neplatný plán."}), 400
    try:
        rec = kv_get_json(f"user:{username}")
        if not rec:
            return jsonify({"ok": False, "error": "Uživatel neexistuje."}), 404
        rec["plan"] = plan
        now = int(time.time())
        if plan in VALID_PLANS:
            rec["plan_until"] = (now + int(days) * 86400) if days else None
        elif plan == "trial":
            rec["trial_ends"] = now + int(days or TRIAL_DAYS) * 86400
            rec["trial_tier"] = "pro"
        elif plan == "comped":
            rec["plan_until"] = None
        kv_set_json(f"user:{username}", rec)
        return jsonify({"ok": True, "subscription": subscription_info(username, rec)})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Chyba úložiště: {e}"}), 500


@app.route("/api/admin/delete-user", methods=["POST"])
def admin_delete_user():
    admin = _auth_admin()
    if not admin:
        return jsonify({"ok": False, "error": "Přístup jen pro admina."}), 403
    email = _norm_email((request.get_json(silent=True) or {}).get("email"))
    if email in admin_users():
        return jsonify({"ok": False, "error": "Admin účet nelze smazat."}), 400
    try:
        kv_del(f"user:{email}")
        kv_del(f"portfolio:{email}")
        kv_srem("users", email)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Chyba úložiště: {e}"}), 500


@app.route("/api/admin/delete-invite", methods=["POST"])
def admin_delete_invite():
    if not _auth_admin():
        return jsonify({"ok": False, "error": "Přístup jen pro admina."}), 403
    code = (request.get_json(silent=True) or {}).get("code", "").strip().lower()
    if not code:
        return jsonify({"ok": False, "error": "Chybí kód."}), 400
    try:
        kv_del(f"invite:{code}")
        kv_srem("invites", code)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Chyba úložiště: {e}"}), 500


@app.route("/api/admin/email-test", methods=["POST"])
def admin_email_test():
    """Diagnostika e-mailů: zkusí poslat testovací e-mail a vrátí PŘESNÝ výsledek
    (status z Brevo / důvod selhání). Slouží k odhalení proč nechodí reset hesla."""
    admin = _auth_admin()
    if not admin:
        return jsonify({"ok": False, "error": "Přístup jen pro admina."}), 403
    to = _norm_email((request.get_json(silent=True) or {}).get("to")) or admin
    ok = send_email(to, "Test e-mailu – MY ADVANTAGE",
                    _email_shell("Test e-mailu ✅",
                                 "<p style='line-height:1.6'>Tohle je testovací e-mail z diagnostiky. "
                                 "Pokud ti dorazil, odesílání (a tím i reset hesla) funguje.</p>"))
    return jsonify({
        "ok": ok,
        "sent_to": to,
        "email_enabled": email_enabled(),
        "sender": os.environ.get("SENDER_EMAIL"),
        "has_brevo_key": bool(os.environ.get("BREVO_API_KEY")),
        "app_url": os.environ.get("APP_URL") or request.host_url.rstrip("/"),
        "error": _LAST_EMAIL_ERROR["msg"],
    })


@app.route("/api/admin/invites", methods=["GET", "POST"])
def admin_invites():
    if not _auth_admin():
        return jsonify({"ok": False, "error": "Přístup jen pro admina."}), 403
    try:
        if request.method == "POST":
            body = request.get_json(silent=True) or {}
            ctype = "promo" if (body.get("type") == "promo") else "comped"
            now = int(time.time())
            base = {"created": now, "type": ctype}
            if ctype == "comped":
                base["used_by"] = None
            else:
                base["uses"] = []
                base["trial_tier"] = "pro"
                base["trial_days"] = int(body.get("trial_days", TRIAL_DAYS))
                base["discount_pct"] = int(body.get("discount_pct", 20))
                base["discount_months"] = int(body.get("discount_months", 3))

            custom = (body.get("code") or "").strip().lower()
            if custom:
                if not re.match(r"^[a-z0-9_-]{2,24}$", custom):
                    return jsonify({"ok": False, "error": "Název kódu: 2–24 znaků (a–z, 0–9, _ -)."}), 400
                if kv_get_json(f"invite:{custom}"):
                    return jsonify({"ok": False, "error": "Takový kód už existuje."}), 409
                kv_set_json(f"invite:{custom}", base)
                kv_sadd("invites", custom)
                return jsonify({"ok": True, "created": [custom]})

            count = max(1, min(int(body.get("count", 1)), 20))
            created = []
            for _ in range(count):
                code = secrets.token_hex(4)
                kv_set_json(f"invite:{code}", dict(base))
                kv_sadd("invites", code)
                created.append(code)
            return jsonify({"ok": True, "created": created})

        # GET – seznam kódů + referral (kdo přišel a jaký má plán)
        out = []
        for code in kv_smembers("invites"):
            inv = kv_get_json(f"invite:{code}") or {}
            ctype = inv.get("type", "comped")
            item = {"code": code, "type": ctype, "created": inv.get("created"),
                    "discount_pct": inv.get("discount_pct")}
            if ctype == "comped":
                item["used_by"] = inv.get("used_by")
            else:
                referrals = []
                for u in (inv.get("uses") or []):
                    ur = kv_get_json(f"user:{u.get('email')}") or {}
                    referrals.append({"email": u.get("email"), "ts": u.get("ts"),
                                      "plan": effective_plan(ur)})
                item["referrals"] = referrals
                item["referral_count"] = len(referrals)
            out.append(item)
        out.sort(key=lambda x: x.get("created") or 0, reverse=True)
        return jsonify({"ok": True, "invites": out})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Chyba úložiště: {e}"}), 500


# ---------------------------------------------------------------------------
# Scanner příležitostí ("crazy opportunities")
# ---------------------------------------------------------------------------
def opp_theses(top_items):
    """Jedna věcná věta k top titulům podhodnocených s momentem, kešováno na den."""
    if not analysis_enabled() or not top_items:
        return {}
    date = _today()
    cache = kv_get_json(f"oppthesis:{date}") or {}
    missing = [it for it in top_items if it["ticker"] not in cache][:8]
    if missing:
        lines = [f'{it["ticker"]} ({it.get("name","")}, {it.get("sector") or "?"}): P/E {it.get("pe") or "—"}, '
                 f'cash flow {it.get("fcf_yield") or "—"} % ceny, momentum 12 měsíců {it.get("mom_pct") or "—"} %, '
                 f'skóre hodnota+momentum {it.get("score")}/100 (#{it.get("vm_rank")} v S&P 500)'
                 for it in missing]
        prompt = ("Jsi střízlivý akciový analytik. Pro každou akcii napiš JEDNU věcnou českou větu "
                  "(max 18 slov), co ji v datech dělá zajímavou – jen z uvedených čísel a obecně známého "
                  "byznysu firmy. ŽÁDNÉ předpovědi ceny, žádné „vyroste\", žádná procenta zisku. "
                  "Vrať POUZE validní JSON ve tvaru "
                  '{"TICKER": "věta", ...}.\n\nAKCIE:\n' + "\n".join(lines))
        res = call_llm(prompt)
        if isinstance(res, dict):
            for k, v in res.items():
                if isinstance(v, str):
                    cache[k.upper()] = v
            try:
                kv_set_json(f"oppthesis:{date}", cache)
            except Exception:
                pass
    return cache


@app.route("/api/opportunities")
def get_opportunities():
    """Podhodnocené s momentem: nejvyšší skóre HODNOTA & MOMENTUM v S&P 500.
    Ověřeno: nejlepších 20 titulů porazilo průměrnou akcii indexu v obou
    obdobích (+1,7 / +7,2 bodu ročně). Nahrazuje dřívější „Potenciál", který
    bodoval vzdálenost od ročního maxima a nic nepředpovídal."""
    s = market_scan(allow_step=False)
    if not s:
        return jsonify({"ok": True, "count": 0, "results": [], "scanning": True, "updated": ""})
    items = [dict(x) for x in (s.get("value") or [])]
    try:
        theses = opp_theses(items[:8])
        for it in items[:8]:
            if theses.get(it["ticker"]):
                it["why"] = theses[it["ticker"]]
    except Exception:
        pass
    return jsonify({"ok": True, "count": len(items), "results": items, "ai": analysis_enabled(),
                    "updated": s.get("updated"), "date": s.get("date"), "stale": bool(s.get("stale"))})


# ---------------------------------------------------------------------------
# Endpointy
# ---------------------------------------------------------------------------
@app.route("/api/search")
def search_ticker():
    query = request.args.get("q", "")
    if len(query) < 1:
        return jsonify({"ok": True, "results": []})
    if not _rate_ok("search", 120, 60):
        return jsonify({"ok": True, "results": []})  # tiše omezit nadměrné dotazy
    try:
        url = (f"https://query2.finance.yahoo.com/v1/finance/search?q={quote(query)}"
               f"&quotesCount=40&newsCount=0&enableFuzzyQuery=true&listsCount=0")
        r = requests.get(url, headers=HEADERS, timeout=8)
        data = r.json()
        # Povolené typy – širší (přidány MUTUALFUND, OPTION vynechán). Neznámé typy taky pustíme,
        # pokud mají symbol, aby se našlo víc titulů napříč burzami.
        allowed = {'EQUITY', 'ETF', 'CRYPTOCURRENCY', 'INDEX', 'FUTURE', 'CURRENCY', 'MUTUALFUND'}
        results, seen = [], set()
        for q in data.get('quotes', []):
            sym = q.get('symbol')
            if not sym or sym in seen:
                continue
            qt = q.get('quoteType')
            if qt and qt not in allowed:
                continue
            seen.add(sym)
            results.append({
                "ticker": sym,
                "name": q.get('shortname') or q.get('longname') or q.get('symbol'),
                "exchange": q.get('exchDisp', 'Trh'),
                "type": qt or 'EQUITY'
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
            prev = _prev_close([], meta) or price
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


def _market_status():
    """Stav americké burzy (přibližně, ignoruje svátky). Časy v SELČ/SEČ."""
    now = datetime.now(timezone.utc)
    summer = 3 <= now.month <= 10  # hrubá aproximace letního času
    open_utc, close_utc = (13.5, 20.0) if summer else (14.5, 21.0)
    prg = 2 if summer else 1
    tzname = "SELČ" if summer else "SEČ"
    hf = now.hour + now.minute / 60
    is_open = now.weekday() < 5 and open_utc <= hf < close_utc

    def hhmm(u):
        return f"{(int(u) + prg) % 24:02d}:{int(round((u % 1) * 60)):02d}"

    hours = f"{hhmm(open_utc)}–{hhmm(close_utc)} {tzname}"
    label = (f"Americká burza je OTEVŘENÁ · zavírá v {hhmm(close_utc)} {tzname}"
             if is_open else
             f"Americká burza je ZAVŘENÁ · obchodní hodiny {hours} (po–pá)")
    return {"open": is_open, "label": label, "hours": hours}


@app.route("/api/market-news")
def market_news():
    """Celkové dění na trhu (ne portfolio) + stav burzy. Pomáhá pochopit,
    proč je portfolio v plusu/mínusu, i když je burza zavřená."""
    items, seen = [], set()
    for q in ("^GSPC", "^IXIC", "stock market"):
        try:
            r = requests.get(f"https://query2.finance.yahoo.com/v1/finance/search?q={quote(q)}&quotesCount=0&newsCount=8",
                             headers=HEADERS, timeout=6).json()
            for n in (r.get("news") or []):
                t = n.get("title")
                if not t or t in seen:
                    continue
                seen.add(t)
                items.append({"title": t, "link": n.get("link", ""),
                              "publisher": n.get("publisher", "Zdroj"), "time": n.get("providerPublishTime")})
        except Exception:
            continue
    items.sort(key=lambda x: x.get("time") or 0, reverse=True)
    return jsonify({"ok": True, "results": items[:14], "market": _market_status()})


@app.route("/api/analyst/<path:ticker>")
def get_analyst(ticker):
    """Doporučení analytiků, cílové ceny a fundamenty z Finnhubu (env FINNHUB_KEY).
    Bez klíče vrací configured=False a frontend panely skryje."""
    key = os.environ.get("FINNHUB_KEY")
    if not key:
        return jsonify({"ok": True, "configured": False})

    sym = ticker.upper()
    base = "https://finnhub.io/api/v1"
    out = {"ok": True, "configured": True, "ticker": sym}

    def fh(path, params):
        params = dict(params or {})
        params["token"] = key
        r = requests.get(f"{base}/{path}", params=params, headers=HEADERS, timeout=8)
        if r.status_code != 200:
            return None
        return r.json()

    # Profil (kapitalizace, název, sektor)
    try:
        p = fh("stock/profile2", {"symbol": sym}) or {}
        if p.get("name"):
            out["name"] = p.get("name")
        out["industry"] = p.get("finnhubIndustry")
        mc = p.get("marketCapitalization")  # v milionech USD
        if mc:
            out["market_cap"] = int(float(mc) * 1e6)
        out["currency"] = p.get("currency")
    except Exception:
        pass

    # Fundamenty (P/E, EPS, beta, dividenda, short interest)
    try:
        m = (fh("stock/metric", {"symbol": sym, "metric": "all"}) or {}).get("metric", {}) or {}
        out["pe_ratio"] = _round(m.get("peTTM") or m.get("peBasicExclExtraTTM"), 2)
        out["eps"] = _round(m.get("epsTTM"), 2)
        out["beta"] = _round(m.get("beta"), 2)
        out["dividend_yield"] = _round(m.get("dividendYieldIndicatedAnnual"), 2)
        # Short interest – kolik % free-float je drženo na short pozicích
        out["short_percent_float"] = _round(m.get("shortInterestSharePercent") or m.get("shortInterestPercentageFloat"), 2)
        out["short_ratio"] = _round(m.get("shortRatio"), 2)  # dní k pokrytí shortů
    except Exception:
        pass

    # Doporučení analytiků (poslední období)
    try:
        recs = fh("stock/recommendation", {"symbol": sym}) or []
        if recs:
            r0 = recs[0]
            out["recs"] = {
                "strongBuy": int(r0.get("strongBuy", 0) or 0),
                "buy": int(r0.get("buy", 0) or 0),
                "hold": int(r0.get("hold", 0) or 0),
                "sell": int(r0.get("sell", 0) or 0),
                "strongSell": int(r0.get("strongSell", 0) or 0),
                "period": r0.get("period"),
            }
    except Exception:
        pass

    # Cílové ceny
    try:
        t = fh("stock/price-target", {"symbol": sym}) or {}
        if t.get("targetMean"):
            out["target"] = {
                "mean": _round(t.get("targetMean"), 2),
                "high": _round(t.get("targetHigh"), 2),
                "low": _round(t.get("targetLow"), 2),
            }
    except Exception:
        pass

    return jsonify(out)


# ---------------------------------------------------------------------------
# HLOUBKOVÁ ANALÝZA (server-side AI) + track record
# ---------------------------------------------------------------------------
# Denní limit hloubkových AI analýz podle plánu.
# Pro = "ochutnávka" (omezeně), Elite = plný limit. Admin jede na plný.
PRO_DAILY_LIMIT = 5
ELITE_DAILY_LIMIT = 30


def _llm_creds():
    return (os.environ.get("ANALYSIS_PROVIDER") or "groq").lower(), os.environ.get("ANALYSIS_API_KEY")


def analysis_enabled():
    return bool(os.environ.get("ANALYSIS_API_KEY"))


def call_llm(prompt, json_mode=True):
    prov, key = _llm_creds()
    if not key:
        return None
    if prov == "openai":
        url = "https://api.openai.com/v1/chat/completions"
        model = os.environ.get("ANALYSIS_MODEL", "gpt-4o-mini")
    else:
        url = "https://api.groq.com/openai/v1/chat/completions"
        model = os.environ.get("ANALYSIS_MODEL", "llama-3.3-70b-versatile")
    payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.4}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    try:
        r = requests.post(url, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                          data=json.dumps(payload), timeout=28)
        if r.status_code != 200:
            return None
        content = (((r.json() or {}).get("choices") or [{}])[0].get("message") or {}).get("content")
        if not json_mode:
            return content
        return json.loads(content)
    except Exception:
        return None


def _perf_windows(closes):
    """Procentní výkonnost přes několik oken (z denních close)."""
    out = {}
    if not closes:
        return out
    last = closes[-1]
    for label, n in (("1t", 5), ("1m", 22), ("3m", 66), ("6m", 132), ("1r", 252)):
        if len(closes) > n and closes[-1 - n]:
            out[label] = round((last / closes[-1 - n] - 1) * 100, 1)
    return out


def gather_facts(ticker):
    """Sesbírá co nejvíc TVRDÝCH dat o akcii pro analytický model: technika,
    výkonnost v čase, pozice v 52T pásmu, valuace, růst, ziskovost, rozvaha,
    cíle a nálada analytiků, blížící se výsledky, titulky. Vše best-effort –
    co se nepodaří stáhnout, se prostě vynechá (model dostane jen ověřená čísla)."""
    ticker = ticker.upper()
    daily = yahoo_chart(ticker, "1y", "1d")
    meta = daily.get("meta", {})
    dq = ((daily.get("indicators") or {}).get("quote") or [{}])[0]
    closes = [c for c in (dq.get("close") or []) if c is not None]
    ind = compute_indicators(closes, dq.get("volume") or [])
    price = meta.get("regularMarketPrice") or (closes[-1] if closes else None)
    high52 = meta.get("fiftyTwoWeekHigh")
    low52 = meta.get("fiftyTwoWeekLow")
    score, score_label, _ = signal_score(price, ind, high52, low52)
    setup = tech_setup_score(price, ind)
    tr = technical_rating(price, ind)
    f = {
        "ticker": ticker, "name": meta.get("shortName") or ticker,
        "currency": meta.get("currency", "USD"),
        "price": _round(price, 3), "high52": _round(high52, 3), "low52": _round(low52, 3),
        "rsi": ind.get("rsi"), "sma20": ind.get("sma20"), "sma50": ind.get("sma50"),
        "sma200": ind.get("sma200"), "macd_hist": ind.get("macd_hist"),
        "momentum_1m_pct": ind.get("mom_1m"), "volatility_pct": ind.get("volatility"),
        "avg_volume": ind.get("avg_volume"), "last_volume": ind.get("last_volume"),
        "vol_ratio": ind.get("vol_ratio"),
        "signal_score": score, "signal_label": score_label, "setup_score": setup,
        "setup_note": _setup_note(setup), "setup_top": is_top_signal(setup, ind),
        "vm_pct": _vm_for(ticker),
        "tech_rating": tr["label"], "tech_votes": {"buy": tr["buy"], "sell": tr["sell"], "neutral": tr["neutral"]},
    }
    # Výkonnost v čase + pozice v 52T pásmu + vzdálenost od klouzavých průměrů
    f["perf_pct"] = _perf_windows(closes)
    if high52 and low52 and high52 > low52 and price:
        f["range_position_pct"] = round((price - low52) / (high52 - low52) * 100, 1)
    if price and ind.get("sma50"):
        f["dist_sma50_pct"] = round((price / ind["sma50"] - 1) * 100, 1)
    if price and ind.get("sma200"):
        f["dist_sma200_pct"] = round((price / ind["sma200"] - 1) * 100, 1)

    # Bohaté fundamenty + analytici z Yahoo quoteSummary (best-effort, 1 session)
    qs = _quote_summary(ticker, "summaryDetail,defaultKeyStatistics,financialData,assetProfile,"
                                "calendarEvents,earningsTrend,upgradeDowngradeHistory")
    if qs:
        sd = qs.get("summaryDetail") or {}
        ks = qs.get("defaultKeyStatistics") or {}
        fd = qs.get("financialData") or {}
        ap = qs.get("assetProfile") or {}
        f["sector"] = ap.get("sector")
        f["industry"] = ap.get("industry")
        f["valuation"] = {k: v for k, v in {
            "pe": _raw(sd, "trailingPE"), "forward_pe": _raw(sd, "forwardPE") or _raw(ks, "forwardPE"),
            "peg": _raw(ks, "pegRatio"), "price_to_book": _raw(ks, "priceToBook"),
            "ev_ebitda": _raw(ks, "enterpriseToEbitda"), "market_cap": _raw(sd, "marketCap"),
        }.items() if v is not None}
        f["financials"] = {k: v for k, v in {
            "revenue_growth_pct": _pct(_raw(fd, "revenueGrowth")),
            "earnings_growth_pct": _pct(_raw(fd, "earningsGrowth")),
            "gross_margin_pct": _pct(_raw(fd, "grossMargins")),
            "operating_margin_pct": _pct(_raw(fd, "operatingMargins")),
            "profit_margin_pct": _pct(_raw(fd, "profitMargins")),
            "roe_pct": _pct(_raw(fd, "returnOnEquity")),
            "debt_to_equity": _raw(fd, "debtToEquity"),
            "current_ratio": _raw(fd, "currentRatio"),
            "free_cashflow": _raw(fd, "freeCashflow"),
            "total_cash": _raw(fd, "totalCash"), "total_debt": _raw(fd, "totalDebt"),
        }.items() if v is not None}
        analysts = {k: v for k, v in {
            "target_mean": _raw(fd, "targetMeanPrice"), "target_high": _raw(fd, "targetHighPrice"),
            "target_low": _raw(fd, "targetLowPrice"), "recommendation": fd.get("recommendationKey"),
            "num_opinions": _raw(fd, "numberOfAnalystOpinions"),
        }.items() if v is not None}
        tm = analysts.get("target_mean")
        if tm and price:
            analysts["target_upside_pct"] = round((tm / price - 1) * 100, 1)
        if analysts:
            f["analysts"] = analysts
        # Datum nejbližších výsledků (volatilita kolem earnings)
        try:
            ed = (((qs.get("calendarEvents") or {}).get("earnings") or {}).get("earningsDate") or [])
            ts0 = (ed[0].get("raw") if isinstance(ed[0], dict) else ed[0]) if ed else None
            if ts0:
                f["next_earnings"] = datetime.fromtimestamp(ts0, tz=timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            pass
        # Odhady růstu zisku (earningsTrend)
        try:
            growth = {}
            for t in (qs.get("earningsTrend") or {}).get("trend") or []:
                per, g = t.get("period"), _pct(_raw(t, "growth"))
                if per and g is not None:
                    growth[per] = g
            if growth:
                f["growth_estimates_pct"] = growth
        except Exception:
            pass
        # Poslední změny ratingu od bank
        try:
            recent = [{"firm": h.get("firm"), "action": h.get("action"),
                       "from": h.get("fromGrade"), "to": h.get("toGrade")}
                      for h in ((qs.get("upgradeDowngradeHistory") or {}).get("history") or [])[:5]
                      if h.get("firm")]
            if recent:
                f["recent_rating_changes"] = recent
        except Exception:
            pass

    # Fundamenty + analytici z Finnhubu (doplní/ověří, pokud je klíč)
    fk = os.environ.get("FINNHUB_KEY")
    if fk:
        try:
            base = "https://finnhub.io/api/v1"
            prof = requests.get(f"{base}/stock/profile2", params={"symbol": ticker, "token": fk}, timeout=6).json() or {}
            metric = (requests.get(f"{base}/stock/metric", params={"symbol": ticker, "metric": "all", "token": fk}, timeout=6).json() or {}).get("metric", {}) or {}
            recs = requests.get(f"{base}/stock/recommendation", params={"symbol": ticker, "token": fk}, timeout=6).json() or []
            f.setdefault("industry", prof.get("finnhubIndustry"))
            f["market_cap_musd"] = prof.get("marketCapitalization")
            f["pe"] = _round(metric.get("peTTM"), 2)
            f["eps"] = _round(metric.get("epsTTM"), 2)
            f["52w_metric"] = {"high": _round(metric.get("52WeekHigh"), 2), "low": _round(metric.get("52WeekLow"), 2)}
            if recs:
                f["analyst_recs"] = {k: recs[0].get(k) for k in ("strongBuy", "buy", "hold", "sell", "strongSell")}

            def _m(*keys):
                for k in keys:
                    v = metric.get(k)
                    if isinstance(v, (int, float)):
                        return v
                return None

            # Finnhub jako spolehlivý ZDROJ pilířů, když Yahoo quoteSummary selže.
            # Doplň jen to, co ještě nemáme (Yahoo má přednost, když projde).
            val = f.setdefault("valuation", {})
            for k, v in {"pe": _m("peTTM", "peBasicExclExtraTTM"),
                         "price_to_book": _m("pbQuarterly", "pbAnnual")}.items():
                if v is not None and k not in val:
                    val[k] = round(v, 2)
            fin = f.setdefault("financials", {})
            for k, v in {
                "revenue_growth_pct": _m("revenueGrowthTTMYoy", "revenueGrowthQuarterlyYoy"),
                "earnings_growth_pct": _m("epsGrowthTTMYoy", "epsGrowthQuarterlyYoy"),
                "gross_margin_pct": _m("grossMarginTTM", "grossMarginAnnual"),
                "operating_margin_pct": _m("operatingMarginTTM", "operatingMarginAnnual"),
                "profit_margin_pct": _m("netProfitMarginTTM", "netProfitMarginAnnual"),
                "roe_pct": _m("roeTTM", "roeRfy"),
                "current_ratio": _m("currentRatioQuarterly", "currentRatioAnnual"),
                "debt_to_equity": _m("totalDebt/totalEquityQuarterly", "totalDebt/totalEquityAnnual",
                                     "longTermDebt/equityQuarterly"),
            }.items():
                if v is not None and k not in fin:
                    fin[k] = round(v, 2)
            # Cílová cena → upside (když ji Yahoo nedodalo)
            try:
                pt = requests.get(f"{base}/stock/price-target", params={"symbol": ticker, "token": fk}, timeout=6).json() or {}
                tm = pt.get("targetMean")
                an = f.setdefault("analysts", {})
                if isinstance(tm, (int, float)) and tm and "target_mean" not in an:
                    an["target_mean"] = round(tm, 2)
                    if price:
                        an["target_upside_pct"] = round((tm / price - 1) * 100, 1)
            except Exception:
                pass
            # Datum nejbližších výsledků – Finnhub kalendář (když Yahoo crumb selhal).
            # Bez tohoto fallbacku by se varování na earnings na Vercelu skoro neukázalo.
            if "next_earnings" not in f:
                try:
                    today = datetime.now(timezone.utc).date()
                    to = (today + timedelta(days=90)).strftime("%Y-%m-%d")
                    cal = requests.get(f"{base}/calendar/earnings",
                                       params={"from": today.strftime("%Y-%m-%d"), "to": to,
                                               "symbol": ticker, "token": fk}, timeout=6).json() or {}
                    upcoming = sorted(e.get("date") for e in (cal.get("earningsCalendar") or [])
                                      if e.get("date") and e["date"] >= today.strftime("%Y-%m-%d"))
                    if upcoming:
                        f["next_earnings"] = upcoming[0]
                except Exception:
                    pass
            # Vyčisti prázdné dicty, ať model nepočítá s ničím
            for kk in ("valuation", "financials", "analysts"):
                if isinstance(f.get(kk), dict) and not f[kk]:
                    f.pop(kk, None)

            # ŽIVÉ SIGNÁLY (katalyzátory) – doplňkové, NEvstupují do backtestovaného
            # verdiktu (nejdou ověřit zpětně), jen informují: výsledky, revize, insideři.
            live = []
            try:  # překvapení ve výsledcích
                earn = requests.get(f"{base}/stock/earnings", params={"symbol": ticker, "limit": 1, "token": fk}, timeout=6).json() or []
                if earn and isinstance(earn, list):
                    sp = earn[0].get("surprisePercent")
                    if isinstance(sp, (int, float)):
                        if sp >= 2:
                            live.append({"t": f"Poslední výsledky překonaly odhad o {sp:.0f}%", "k": "bull"})
                        elif sp <= -2:
                            live.append({"t": f"Poslední výsledky pod odhadem o {abs(sp):.0f}%", "k": "bear"})
            except Exception:
                pass
            try:  # revize doporučení (trend nákupních hlasů)
                if recs and len(recs) >= 2:
                    cur = (recs[0].get("strongBuy", 0) or 0) + (recs[0].get("buy", 0) or 0)
                    prev = (recs[1].get("strongBuy", 0) or 0) + (recs[1].get("buy", 0) or 0)
                    if cur > prev:
                        live.append({"t": "Analytici zvyšují optimismus (přibývá nákupních doporučení)", "k": "bull"})
                    elif cur < prev:
                        live.append({"t": "Analytici snižují optimismus (ubývá nákupních doporučení)", "k": "bear"})
            except Exception:
                pass
            try:  # insider obchody (čistý směr)
                ins = requests.get(f"{base}/stock/insider-transactions", params={"symbol": ticker, "token": fk}, timeout=6).json() or {}
                rows = (ins.get("data") or [])[:40]
                net = sum((x.get("change") or 0) for x in rows if isinstance(x.get("change"), (int, float)))
                if rows and net > 0:
                    live.append({"t": "Insideři v poslední době spíše nakupovali", "k": "bull"})
                elif rows and net < 0:
                    live.append({"t": "Insideři v poslední době spíše prodávali", "k": "bear"})
            except Exception:
                pass
            if live:
                f["live_signals"] = live
        except Exception:
            pass
    # Pár titulků zpráv
    try:
        nr = requests.get(f"https://query2.finance.yahoo.com/v1/finance/search?q={quote(ticker)}&quotesCount=0&newsCount=6",
                          headers=HEADERS, timeout=6).json()
        f["headlines"] = [n.get("title") for n in (nr.get("news") or [])[:6] if n.get("title")]
    except Exception:
        f["headlines"] = []
    return f


def _grade(v, scale):
    """v -> skóre podle sestupných prahů [(min, score), ...]. None když v není číslo."""
    if not isinstance(v, (int, float)):
        return None
    for mn, sc in scale:
        if v >= mn:
            return sc
    return scale[-1][1]


EARNINGS_NEAR_DAYS = 7  # výsledky do tolika dní = zvýšené riziko → snížíme jistotu


# ─── PILÍŘ „NÁLADA & INSTITUCE" — pomocné výpočty ──────────────────────────────
# Tady BERE verdikt z dalších zdrojů: sentiment titulků, insider obchody, revize
# analytiků. Všechno transparentně z DAT, nic nevymyšleno. Pokud zdroj chybí,
# pilíř má snížené pokrytí (model si automaticky přerozdělí váhy).

_POS_KW = {"beat", "beats", "surge", "surges", "soar", "soars", "rally", "rallies",
           "jump", "jumps", "upgrade", "upgrades", "outperform", "raises", "raised",
           "boost", "boosts", "strong", "record", "growth", "rises", "wins",
           "bullish", "exceed", "exceeds", "approval", "approved", "launch",
           "expand", "deal", "partnership", "breakthrough", "profit", "profits"}
_NEG_KW = {"miss", "misses", "plunge", "plunges", "drop", "drops", "fall", "falls",
           "downgrade", "downgrades", "cut", "cuts", "warning", "warns", "weak",
           "loss", "losses", "decline", "declines", "concern", "concerns", "lawsuit",
           "investigation", "probe", "fraud", "delay", "delays", "resign", "resigns",
           "bearish", "recall", "recalls", "halt", "halts", "ban", "banned", "scandal"}


def _news_sentiment(headlines):
    """Skóre sentimentu 0-100 z titulků (>50 pozitivní, <50 negativní).
    Jednoduchá keyword analýza – transparentní a robustní. Vrací (score, pos, neg, n)."""
    if not headlines:
        return None
    pos = neg = 0
    for h in headlines:
        if not h:
            continue
        words = {w.strip(".,!?:;()[]\"'").lower() for w in h.split()}
        pos += len(words & _POS_KW)
        neg += len(words & _NEG_KW)
    total = pos + neg
    if total == 0:
        return (50, 0, 0, len(headlines))  # neutrální
    raw = pos / total  # 0..1
    score = round(20 + raw * 60)  # konzervativně 20..80, ne extrémy
    return (max(15, min(85, score)), pos, neg, len(headlines))


def _insider_net_score(live_signals):
    """Z `live_signals` zjistí směr insiderů. Vrací (score 0-100, text) nebo None."""
    if not live_signals:
        return None
    for ls in live_signals:
        t = (ls.get("t") or "").lower()
        if "insider" in t and "naku" in t:
            return (70, "Insideři spíše nakupují")
        if "insider" in t and "prod" in t:
            return (30, "Insideři spíše prodávají")
    return None


def _analyst_trend_score(live_signals):
    """Trend revizí doporučení (přibývá Buy?). Vrací (score 0-100, text) nebo None."""
    if not live_signals:
        return None
    for ls in live_signals:
        t = (ls.get("t") or "").lower()
        if "zvyšují optimismus" in t:
            return (72, "Analytici zvyšují optimismus")
        if "snižují optimismus" in t:
            return (32, "Analytici snižují optimismus")
    return None





def _days_to_earnings(facts):
    """Počet dní do nejbližších výsledků z facts['next_earnings'] ('YYYY-MM-DD').
    None když datum chybí. Záporné = už proběhly (stará data)."""
    ne = (facts or {}).get("next_earnings")
    if not ne:
        return None
    try:
        today = datetime.now(timezone.utc).date()
        ed = datetime.strptime(ne, "%Y-%m-%d").date()
        return (ed - today).days
    except Exception:
        return None


def _sec_pillar_score(ticker):
    """SEC pilíř: skóre 0-100 z insider transakcí (Form 4) + materiálních událostí (8-K).
    Vrací (score, note) nebo None, když nejsou data (neamerická akcie apod.).
    Logika:
      - Bázlínová hodnota 55 (mírně nad 50, protože samotná registrace v SEC = kvalita)
      - Insider nákupy v posledních 90 dnech: +6 bodů za kus, max +25
      - Kupičce (3+) 8-K v krátké době: -8 (turbulence)
      - Poslední 10-Q v posledních 45 dnech: +5 (čerstvá čísla)
      - Chybí 10-Q déle než 120 dnů: -8 (zpožděné výsledky)
    """
    if not ticker:
        return None
    try:
        items = _sec_recent_filings(ticker, limit=30)
    except Exception:
        return None
    if not items:
        return None
    from datetime import date as _date
    today = datetime.now(timezone.utc).date()
    def _age(d):
        try: return (today - _date.fromisoformat(d)).days
        except Exception: return 9999
    ins_90 = [i for i in items if i["kind"] == "insider" and _age(i["date"]) <= 90]
    mat_90 = [i for i in items if i["kind"] == "material" and _age(i["date"]) <= 90]
    latest_10q = next((i for i in items if i["form"] == "10-Q"), None)

    score = 55.0
    parts = []
    if ins_90:
        bonus = min(25, len(ins_90) * 6)
        score += bonus
        parts.append(f"insider {len(ins_90)}× (90d)")
    if len(mat_90) >= 3:
        score -= 8
        parts.append(f"8-K {len(mat_90)}× (turbulence)")
    elif mat_90:
        parts.append(f"8-K {len(mat_90)}×")
    if latest_10q:
        q_age = _age(latest_10q["date"])
        if q_age <= 45:
            score += 5
            parts.append("čerstvé 10-Q")
        elif q_age >= 120:
            score -= 8
            parts.append(f"10-Q staré {q_age} dnů")
    score = max(15, min(90, int(round(score))))
    note = " · ".join(parts) if parts else "žádné významné podání za 90 dnů"
    return (score, note)


def compute_verdict_model(facts):
    """DETERMINISTICKÝ pravidlový verdikt z tvrdých dat – ne odhad AI.
    Skóre 0-100 ze čtyř pilířů (technika, valuace, růst & ziskovost, analytici);
    váhy se přepočítají podle toho, co máme reálně za data. Vrací verdikt, skóre,
    jistotu (pokrytí daty + shoda pilířů + odstup od neutrálu) a transparentní rozpad.
    AI tenhle verdikt jen vysvětlí, nemění ho."""
    pillars = []

    # 1) TECHNIKA – backtestem kalibrované setup skóre (nákup poklesu v uptrendu);
    #    fallback na obecný Signal Score, kdyby setup_score chybělo.
    tech = facts.get("setup_score")
    tech_note = facts.get("setup_note") or ""
    if tech is None:
        tech = facts.get("signal_score")
        tech_note = facts.get("signal_label") or ""
    if tech is not None:
        pillars.append({"key": "technical", "label": "Technika", "weight": 0.30,
                        "score": int(round(tech)), "note": tech_note})

    # 2) HODNOTA & MOMENTUM – jediný fundamentální pilíř OVĚŘENÝ na historii
    #    (skóre 0-100 z denního skenu S&P 500; mimo index chybí a váhy se přepočtou).
    vm = facts.get("vm_pct")
    if isinstance(vm, (int, float)):
        pillars.append({"key": "vm", "label": "Hodnota & momentum", "weight": 0.25,
                        "score": int(vm), "note": f"skóre {int(vm)}/100 v rámci S&P 500"})

    # 2) VALUACE – preferuj PEG, pak forward P/E, pak trailing P/E
    val = facts.get("valuation") or {}
    peg, fpe, pe = val.get("peg"), val.get("forward_pe"), val.get("pe")
    vscore = vnote = None
    if isinstance(peg, (int, float)) and peg > 0:
        vscore = 90 if peg <= 1 else 75 if peg <= 1.5 else 60 if peg <= 2 else 40 if peg <= 3 else 22
        vnote = f"PEG {peg:.2f}"
    elif isinstance(fpe, (int, float)) and fpe > 0:
        vscore = 85 if fpe <= 12 else 70 if fpe <= 18 else 55 if fpe <= 25 else 40 if fpe <= 35 else 28 if fpe <= 50 else 18
        vnote = f"Forward P/E {fpe:.1f}"
    elif isinstance(pe, (int, float)) and pe > 0:
        vscore = 80 if pe <= 12 else 65 if pe <= 18 else 50 if pe <= 25 else 38 if pe <= 40 else 22
        vnote = f"P/E {pe:.1f}"
    if vscore is not None:
        pillars.append({"key": "valuation", "label": "Valuace", "weight": 0.10,
                        "score": int(vscore), "note": vnote})

    # 3) RŮST & ZISKOVOST – průměr dostupných ukazatelů
    fin = facts.get("financials") or {}
    subs = [s for s in (
        _grade(fin.get("revenue_growth_pct"), [(25, 90), (15, 75), (8, 60), (0, 45), (-1e9, 25)]),
        _grade(fin.get("earnings_growth_pct"), [(25, 92), (15, 76), (8, 60), (0, 45), (-1e9, 22)]),
        _grade(fin.get("profit_margin_pct"), [(20, 85), (10, 65), (3, 50), (0, 40), (-1e9, 20)]),
        _grade(fin.get("roe_pct"), [(20, 85), (12, 68), (5, 52), (0, 40), (-1e9, 22)]),
    ) if s is not None]
    if subs:
        bits = []
        if isinstance(fin.get("revenue_growth_pct"), (int, float)):
            bits.append(f"tržby {fin['revenue_growth_pct']:+.0f}%")
        if isinstance(fin.get("profit_margin_pct"), (int, float)):
            bits.append(f"marže {fin['profit_margin_pct']:.0f}%")
        if isinstance(fin.get("roe_pct"), (int, float)):
            bits.append(f"ROE {fin['roe_pct']:.0f}%")
        pillars.append({"key": "growth", "label": "Růst & ziskovost", "weight": 0.12,
                        "score": int(round(sum(subs) / len(subs))), "note": ", ".join(bits)})

    # 4) ANALYTICI – konsenzus (počty doporučení / rec key) + upside k cíli
    an = facts.get("analysts") or {}
    recs = facts.get("analyst_recs") or {}
    cscore = cnote = None
    tot = sum(v for v in recs.values() if isinstance(v, (int, float))) if recs else 0
    if tot:
        cscore = (recs.get("strongBuy", 0) * 100 + recs.get("buy", 0) * 78 + recs.get("hold", 0) * 50
                  + recs.get("sell", 0) * 25 + recs.get("strongSell", 0) * 5) / tot
        cnote = f"{tot} analytiků"
    else:
        rk = (an.get("recommendation") or "").lower().replace(" ", "_")
        rmap = {"strong_buy": 90, "strongbuy": 90, "buy": 75, "outperform": 72, "hold": 50,
                "neutral": 50, "underperform": 30, "sell": 25, "strong_sell": 10, "strongsell": 10}
        if rk in rmap:
            cscore, cnote = rmap[rk], f"doporučení: {rk.replace('_', ' ')}"
    up = an.get("target_upside_pct")
    ascore = None
    if isinstance(up, (int, float)):
        uscore = 95 if up >= 30 else 80 if up >= 15 else 62 if up >= 5 else 45 if up >= -5 else 25
        ascore = uscore if cscore is None else round(0.55 * cscore + 0.45 * uscore)
        cnote = (cnote + ", " if cnote else "") + f"cíl {up:+.0f}%"
    elif cscore is not None:
        ascore = round(cscore)
    if ascore is not None:
        pillars.append({"key": "analysts", "label": "Analytici", "weight": 0.12,
                        "score": int(ascore), "note": cnote or ""})

    # 5) NÁLADA & INSTITUCE – sentiment titulků + insider obchody + revize analytiků.
    #    Transparentně agreguje 3 nezávislé zdroje. Pilíř má smysl, jen když máme
    #    aspoň 1 z dílčích signálů; jinak se vůbec nepřidá a model přerozdělí váhy.
    sub_scores = []
    sub_notes = []
    ns = _news_sentiment(facts.get("headlines"))
    if ns:
        s_sc, pos, neg, n = ns
        sub_scores.append(s_sc)
        if pos or neg:
            sub_notes.append(f"titulky {pos}+/{neg}- ({n})")
        else:
            sub_notes.append(f"titulky neutrální ({n})")
    ins = _insider_net_score(facts.get("live_signals"))
    if ins:
        sub_scores.append(ins[0]); sub_notes.append(ins[1])
    ar = _analyst_trend_score(facts.get("live_signals"))
    if ar:
        sub_scores.append(ar[0]); sub_notes.append(ar[1])
    if sub_scores:
        sentiment_score = int(round(sum(sub_scores) / len(sub_scores)))
        pillars.append({"key": "sentiment", "label": "Nálada & instituce", "weight": 0.07,
                        "score": sentiment_score, "note": " · ".join(sub_notes)})

    # 6) SEC PODÁNÍ – oficiální americká data (Form 4 insider, 8-K materiální události).
    #    Nejsilnější first-hand signál, ale jen pro US akcie. Neamerické tituly ho nemají.
    sec_res = _sec_pillar_score(facts.get("ticker"))
    if sec_res:
        pillars.append({"key": "sec", "label": "Podání SEC", "weight": 0.04,
                        "score": sec_res[0], "note": sec_res[1]})

    # Váhy: ověřené pilíře (technika 0.30, hodnota&momentum 0.25) nesou víc než
    # polovinu; neověřené (valuace, růst, analytici, nálada, SEC) zbytek.
    # Chybějící pilíř se nepočítá – váhy se přepočtou na dostupná data.

    # Kompozitní skóre – váhy přepočítané podle dostupných pilířů
    wsum = sum(p["weight"] for p in pillars)
    composite = round(sum(p["score"] * p["weight"] for p in pillars) / wsum) if wsum else 50
    verdict = "Koupit" if composite >= VERDICT_BUY else ("Prodat" if composite < VERDICT_SELL else "Držet")

    # Ověřená pravidla mají přednost před vahami: splněný signál modelu
    # (TOP setup + skóre hodnota&momentum ≥ 65/100) = Koupit; pokles u drahého
    # titulu se slábnoucím momentem (VM < 30) dával v holdoutu -0,59 % na obchod,
    # takže nikdy Koupit.
    rule = None
    if isinstance(vm, (int, float)) and facts.get("setup_score") is not None:
        if facts.get("setup_top") and vm >= VM_MIN * 100:
            verdict, composite, rule = "Koupit", max(composite, VERDICT_BUY), "signal"
        elif verdict == "Koupit" and facts["setup_score"] >= VERDICT_BUY and vm < 30:
            verdict, composite, rule = "Držet", min(composite, VERDICT_BUY - 1), "low_vm"

    # Jistota: pokrytí daty (0.4) + shoda pilířů (0.35) + odstup od neutrálu (0.25)
    scores = [p["score"] for p in pillars]
    spread = (max(scores) - min(scores)) if len(scores) > 1 else 0
    agreement = 1 - min(spread, 70) / 70 * 0.6
    conviction = abs(composite - 50) / 50
    confidence = max(20, min(95, round((0.40 * wsum + 0.35 * agreement + 0.25 * conviction) * 100)))

    # RIZIKO VÝSLEDKŮ: před blížícími se earnings (binární událost = velký skok
    # ceny) snížíme jistotu a vrátíme příznak. NENÍ to win-% lever (nejde zpětně
    # ověřit), je to upozornění na zvýšenou volatilitu / načasování.
    dte = _days_to_earnings(facts)
    earnings_in = dte if (dte is not None and 0 <= dte <= 14) else None
    if earnings_in is not None and earnings_in <= EARNINGS_NEAR_DAYS:
        confidence = max(20, confidence - 18)

    return {
        "score": composite, "verdict": verdict, "confidence": confidence,
        "coverage_pct": round(wsum * 100), "earnings_in": earnings_in, "rule": rule,
        "pillars": [{**p, "weight": round(p["weight"], 2)} for p in pillars],
    }


def compute_levels(facts, horizon=10):
    """DETERMINISTICKÝ vstup/stop/cíl z volatility a horizontu (ne odhad AI).
    Šíře stop/cíl škáluje očekávaným pohybem za 'horizon' dní (sigma_H z roční
    volatility). Drží rozumné meze. Vrací i poměr zisk:riziko (R:R)."""
    price = facts.get("price")
    if not price:
        return None
    # Úrovně = PŘESNĚ to, co měří backtest (dřív se počítaly z volatility, ale
    # backtest měřil fixní horizont – uživatel tak viděl jiný plán, než jaký byl
    # doložený). Teď je plán a důkaz jedna a ta samá věc.
    stop_pct, tgt_pct = EXIT_SL, EXIT_TP
    horizon = EXIT_MAX_DAYS
    stop = round(price * (1 - stop_pct), 2)
    target = round(price * (1 + tgt_pct), 2)
    return {
        "entry": round(price, 2), "stop_loss": stop, "target_price": target,
        "horizon_days": horizon,
        "risk_pct": round(stop_pct * 100, 1), "reward_pct": round(tgt_pct * 100, 1),
        "rr": round(tgt_pct / stop_pct, 2) if stop_pct else None,
    }


def build_analysis_prompt(facts, model=None):
    model_block = ""
    if model:
        v = model.get("verdict", "")
        model_block = (
            "\n\nPRAVIDLOVÝ MODEL (spočítaný deterministicky z dat – je ZÁVAZNÝ):\n"
            + json.dumps(model, ensure_ascii=False)
            + f"\nVe výstupu pole 'verdict' a 'confidence' MUSÍ přesně odpovídat modelu (verdict='{v}'). "
            "Verdikt jen VYSVĚTLI čísly, neměň ho.\n"
            f"DŮLEŽITÉ – SOULAD: CELÝ text (headline, thesis, scenarios) musí ladit s verdiktem '{v}'. "
            "Je-li verdikt 'Držet', NEPIŠ to jako jasný nákup – piš vyváženě, proč spíš počkat "
            "(co chybí k nákupu). Je-li 'Prodat', piš opatrně/negativně. 'headline' musí odpovídat verdiktu. "
            "Pokud si pilíře protiřečí (např. dobří analytici vs. slabá ziskovost), napiš to otevřeně.\n"
            "HORIZONT je krátkodobý (do 6 týdnů, náš signál) – do pole 'horizon' napiš \"do 6 týdnů\" a scénáře "
            "piš k tomuto horizontu, ne k 3–6 měsícům."
        )
    return (
        "Jsi špičkový akciový analytik. Na základě DAT níže vytvoř hloubkovou, konkrétní a "
        "vyváženou analýzu v ČEŠTINĚ. Pracuj VÝHRADNĚ s čísly z dat – nic si nevymýšlej; "
        "co v datech není, neuváděj jako fakt.\n\n"
        "POSTUP (promysli, ale do výstupu dej jen výsledek):\n"
        "1) VALUACE: posuď P/E, forward P/E, PEG, P/B, EV/EBITDA vs. růst a ziskovost.\n"
        "2) RŮST & ZISKOVOST: revenue/earnings growth, marže, ROE, odhady růstu.\n"
        "3) ROZVAHA: dluh/equity, current ratio, cash vs. debt, free cashflow (riziko/odolnost).\n"
        "4) TECHNIKA: trend (vzdálenost od SMA50/200), RSI, MACD, momentum, pozice v 52T pásmu, výkonnost v čase.\n"
        "5) ANALYTICI & SENTIMENT: cílová cena a její upside, počet názorů, poslední změny ratingu, titulky.\n"
        "6) NAČASOVÁNÍ: pokud se blíží 'next_earnings', uveď to jako zdroj volatility/rizika.\n\n"
        "KALIBRACE 'confidence' (0-100): vysoká jen když se signály SHODUJÍ (technika + fundament + "
        "analytici míří stejným směrem). Když si protiřečí nebo data chybí, dej NÍŽE. Nebuď přehnaně optimistický.\n"
        "ÚROVNĚ: pole 'suggested_levels' v datech jsou ZÁVAZNÉ – do výstupu nastav entry/stop_loss/"
        "target_price PŘESNĚ na ně (spočítané z volatility). Nevymýšlej vlastní čísla.\n\n"
        "Vrať POUZE validní JSON přesně v tomto tvaru (bez markdownu):\n"
        "{\n"
        '  "verdict": "Koupit" | "Držet" | "Prodat",\n'
        '  "confidence": <0-100 celé číslo>,\n'
        '  "horizon": "<časový horizont, např. 3–6 měsíců>",\n'
        '  "target_price": <číslo>, "stop_loss": <číslo>, "entry": <číslo>,\n'
        '  "headline": "<jedna výstižná věta>",\n'
        '  "thesis": "<2-4 věty investiční teze opřené o konkrétní čísla>",\n'
        '  "fundamentals": "<rozbor: valuace vs. růst, ziskovost, rozvaha – s čísly>",\n'
        '  "technicals": "<rozbor technického obrazu – s čísly>",\n'
        '  "sentiment": "<analytici, cílová cena/upside, změny ratingu, zprávy>",\n'
        '  "scenarios": {"bull": "<býčí scénář + cíl>", "base": "<základní>", "bear": "<medvědí + riziko>"},\n'
        '  "risks": ["<riziko 1>", "<riziko 2>", "<riziko 3>"],\n'
        '  "catalysts": ["<katalyzátor 1>", "<katalyzátor 2>"]\n'
        "}\n\n"
        "DATA:\n" + json.dumps(facts, ensure_ascii=False)
    )


def _today():
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _today_iso():
    """Datum ve tvaru YYYY-MM-DD – pro archiv přehledů a pro URL. `_today()`
    (kompaktní tvar) se používá jako klíč u jiných dat, tak ho neměníme."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@app.route("/api/analysis/<path:ticker>", methods=["POST"])
def deep_analysis(ticker):
    user = _auth_user()
    if not user:
        return jsonify({"ok": False, "error": "Pro hloubkovou analýzu se přihlas."}), 401
    if not analysis_enabled():
        return jsonify({"ok": False, "error": "Analytický motor zatím není nastavený."}), 503
    try:
        rec = kv_get_json(f"user:{user}")
        is_admin = user in admin_users()
        eff = effective_plan(rec)
        if not is_admin and PLAN_RANK.get(eff, -1) < PLAN_RANK["pro"]:
            return jsonify({"ok": False, "error": "Hloubková analýza je součástí plánů Pro a Elite.", "upgrade": True}), 402

        # Denní limit podle plánu: Pro = ochutnávka, Elite (a admin) = plný limit
        is_elite = is_admin or PLAN_RANK.get(eff, -1) >= PLAN_RANK["elite"]
        daily_limit = ELITE_DAILY_LIMIT if is_elite else PRO_DAILY_LIMIT
        ukey = f"usage:{user}:{_today()}"
        used = kv_get_json(ukey) or 0
        if used >= daily_limit:
            msg = (f"Denní limit {daily_limit} analýz vyčerpán. Zkus to zítra."
                   if is_elite else
                   f"Vyčerpal jsi denní limit {daily_limit} analýz plánu Pro. "
                   f"Elite má {ELITE_DAILY_LIMIT}/den.")
            return jsonify({"ok": False, "error": msg, "upgrade": not is_elite}), 429

        facts = gather_facts(ticker)
        # Deterministický verdikt + úrovně z dat – AI je jen vysvětlí
        model = compute_verdict_model(facts)
        levels = compute_levels(facts, horizon=10)
        if levels:
            facts["suggested_levels"] = levels
        report = call_llm(build_analysis_prompt(facts, model))
        if not report:
            return jsonify({"ok": False, "error": "Analýzu se nepodařilo vygenerovat, zkus to znovu."}), 502

        # Verdikt, jistotu i úrovně bereme VŽDY z výpočtu (no guesses), ne z AI
        report["verdict"] = model["verdict"]
        report["confidence"] = model["confidence"]
        report["earnings_in"] = model.get("earnings_in")
        if levels:
            report["entry"] = levels["entry"]
            report["stop_loss"] = levels["stop_loss"]
            report["target_price"] = levels["target_price"]
            report["horizon"] = f"{EXIT_HORIZON_LABEL} (max {levels['horizon_days']} obch. dní)"

        kv_set_json(ukey, used + 1)

        # Ulož verdikt pro track record (snapshot ceny v čase)
        try:
            kv_rpush("verdicts", {
                "ticker": facts["ticker"], "ts": int(time.time()),
                "verdict": report.get("verdict"), "price": facts.get("price"),
                "target": report.get("target_price"), "currency": facts.get("currency"),
                "stop": (levels or {}).get("stop_loss"),
                "horizon_days": (levels or {}).get("horizon_days", 10),
                "score": model.get("score"), "user": user,
            })
        except Exception:
            pass

        return jsonify({"ok": True, "ticker": facts["ticker"], "name": facts.get("name"),
                        "currency": facts.get("currency"), "price": facts.get("price"),
                        "report": report, "facts": facts, "model": model, "levels": levels,
                        "used_today": used + 1, "daily_limit": daily_limit})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Chyba analýzy: {e}"}), 500


def _price_paths(tickers, rng="1y"):
    """Pro každý ticker vrátí (aktuální cena, dráha [(ts, close)]). Bereme CELOU
    dráhu, ne jen dnešní cenu – jinak by obchod, který zasáhl cíl a pak spadl,
    vyšel jako ztráta, tedy jinak, než co měří backtest a co podle plánu nastalo."""
    def one(t):
        try:
            res = yahoo_chart(t, rng, "1d")
            ts_ = res.get("timestamp") or []
            cl_ = ((res.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
            return t, (res.get("meta") or {}).get("regularMarketPrice"), \
                [(tt, c) for tt, c in zip(ts_, cl_) if c is not None]
        except Exception:
            return t, None, []
    from concurrent.futures import ThreadPoolExecutor
    prices, paths = {}, {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for t, pr, path in ex.map(one, [t for t in tickers if t]):
            prices[t], paths[t] = pr, path
    return prices, paths


def _agg_returns(rs):
    return {"count": len(rs), "avg_return": round(sum(rs) / len(rs), 2),
            "win_rate": round(sum(1 for r in rs if r > 0) / len(rs) * 100, 1)} if rs else None


def _model_record(limit=300):
    """Živý záznam modelu: všechny papírové obchody z denních signálů, přehrané
    proti skutečným cenám stejnou logikou jako pozice uživatele. Kešováno hodinu."""
    if not cloud_enabled():
        return {"count": 0}
    try:
        c = kv_get_json("paper:summary")
        if c and time.time() - c.get("ts", 0) < 3600:
            return c
        trades = kv_lrange("paper:trades", -limit, -1)
    except Exception:
        return {"count": 0}
    if not trades:
        return {"count": 0}
    prices, paths = _price_paths({t.get("ticker") for t in trades})
    items, closed, every = [], [], []
    for t in trades:
        ret, st, lbl = _replay_trade(t, paths.get(t.get("ticker")) or [], prices.get(t.get("ticker")))
        if ret is None:
            continue
        every.append(ret)
        if st != "open":
            closed.append(ret)
        items.append({"ticker": t.get("ticker"), "date": t.get("date"), "price": t.get("price"),
                      "vm": t.get("vm"), "return_pct": _round(ret, 2),
                      "exit_status": st, "exit_label": lbl})
    out = {"ts": int(time.time()), "count": len(items), "since": trades[0].get("date"),
           "closed": _agg_returns(closed), "all": _agg_returns(every), "items": items[-40:][::-1]}
    try:
        kv_set_json("paper:summary", out)
    except Exception:
        pass
    return out


def _replay_trade(v, path, cur, now=None):
    """Přehraje jeden obchod den po dni podle plánu uloženého u verdiktu.
    Vrací (výnos %, stav, popisek). JEDINÉ místo, kde tahle logika žije –
    používá ji track record, přehled otevřených pozic i ranní e-mail."""
    entry = v.get("price")
    if not entry:
        return None, None, None
    now = now or int(time.time())
    tgt, stp = v.get("target"), v.get("stop")
    t0 = v.get("ts") or 0
    for tt, c in path:
        if tt <= t0:
            continue
        if stp and c <= stp:
            return (stp - entry) / entry * 100, "stop", "🛑 Stop zasažen — vystup"
        if tgt and c >= tgt:
            return (tgt - entry) / entry * 100, "target", "✅ Cíl zasažen — realizuj zisk"
    if cur is None:
        return None, None, None
    horizon = v.get("horizon_days", EXIT_MAX_DAYS) or EXIT_MAX_DAYS
    ret_ = (cur - entry) / entry * 100
    if (now - t0) / 86400 > round(horizon * 7 / 5):
        return ret_, "expired", "⏳ Horizont vypršel — přehodnoť"
    return ret_, "open", "Drží se (do cíle/stopu/horizontu)"


def _positions_for(user, limit=12, risk=None):
    """Otevřené nákupní verdikty uživatele, přehrané proti ŽIVÉ ceně.
    Tohle je ta „živá" složka: plán se nastaví jednou, ale cíl/stop se
    kontrolují každý den – přesně to, co podle backtestu funguje
    (reagovat na zeslábnutí technického skóre naopak měřitelně škodí)."""
    risk = _risk(risk) if risk else _user_risk(user)
    try:
        mine = [v for v in (kv_lrange("verdicts", -200, -1) or [])
                if v.get("user") == user and v.get("verdict") == "Koupit"][-limit:]
    except Exception:
        return {"count": 0, "action_count": 0, "items": [], "allocation": _allocation(0, risk)}
    if not mine:
        return {"count": 0, "action_count": 0, "items": [], "allocation": _allocation(0, risk)}
    prices, paths = _price_paths({v.get("ticker") for v in mine if v.get("ticker")})
    now = int(time.time())
    items = []
    for v in mine:
        t = v.get("ticker")
        cur = prices.get(t)
        ret, status, label = _replay_trade(v, paths.get(t) or [], cur, now)
        if status is None:
            continue
        items.append({
            "ticker": t, "status": status, "label": label,
            "entry": _round(v.get("price"), 2), "current": _round(cur, 2),
            "target": v.get("target"), "stop": v.get("stop"),
            "return_pct": _round(ret, 2), "ts": v.get("ts"),
            "days_held": int((now - (v.get("ts") or now)) / 86400),
        })
    # Co vyžaduje akci (cíl/stop/expirace) nahoru, otevřené pod to.
    order = {"target": 0, "stop": 1, "expired": 2, "open": 3}
    items.sort(key=lambda x: (order.get(x["status"], 9), -(x["ts"] or 0)))
    return {"count": len(items), "items": items,
            "action_count": sum(1 for i in items if i["status"] != "open"),
            "allocation": _allocation(sum(1 for i in items if i["status"] == "open"), risk)}


def _allocation(open_positions, risk="balanced"):
    """Rozložení portfolia podle rizikového profilu – každá varianta ověřená
    na historii (viz VALIDATION["profiles"]):
      konzervativní = pozice po 10 % + cílení volatility 15 % (propad -17,5 %)
      vyvážený      = pozice po 10 %, satelit max 50 %, zbytek indexové jádro
      agresivní     = pozice po 20 %, satelit až 100 %
    Hotovost bez důvodu je nejdražší chyba (dřív ~70 % kapitálu leželo ladem);
    jediná výjimka je konzervativní profil v rozkolísaném trhu – tam je
    hotovost záměr, který propady na historii skoro půlil."""
    risk = _risk(risk)
    w = POSITION_WEIGHT[risk]
    used = max(0, min(open_positions, PORTFOLIO_SLOTS))
    expo, vol = 100, None
    if risk in VOL_TARGET:
        mr = _market_risk()
        vol = mr.get("spy_vol")
        expo = mr.get(f"exposure_{risk}") or 100
    per = round(w * expo, 1)
    active = round(used * per, 1)
    cash = round(100.0 - expo, 1)
    core = round(max(0.0, 100.0 - cash - active), 1)
    if cash > 0:
        note = (f"Trh je rozkolísaný (volatilita {vol} % ročně), konzervativní profil proto drží "
                f"{cash:.0f} % v hotovosti nebo krátkodobých dluhopisech. Až se trh uklidní, vrátí se do akcií.")
    else:
        note = ("Každý signál dostane stejný díl, zbytek drží široký indexový fond. "
                "Hotovost bez důvodu nevydělává nic.")
    return {"risk": risk, "slots_max": PORTFOLIO_SLOTS, "slots_used": used,
            "per_position_pct": per, "active_pct": active, "core_pct": core,
            "cash_pct": cash, "exposure_pct": expo, "spy_vol": vol, "note": note}


@app.route("/api/positions")
def my_positions():
    """Živý stav otevřených signálů přihlášeného uživatele proti jeho plánu."""
    user = _auth_user()
    if not user:
        return jsonify({"ok": False, "error": "Nepřihlášeno."}), 401
    if not _rate_ok("positions", 40, 60):
        return jsonify({"ok": True, "count": 0, "action_count": 0, "items": []})
    try:
        return jsonify({"ok": True, **_positions_for(user, risk=request.args.get("risk"))})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/track-record")
def track_record():
    """Agregovaná úspěšnost dosavadních verdiktů (re-fetch aktuálních cen)."""
    try:
        model = _model_record()
        verdicts = kv_lrange("verdicts", -200, -1)
        if not verdicts:
            return jsonify({"ok": True, "count": 0, "items": [], "stats": {}, "model": model})
        # Aktuální ceny (unikátní tickery)
        prices, paths = _price_paths({v.get("ticker") for v in verdicts if v.get("ticker")})

        def _replay(v, cur):
            return _replay_trade(v, paths.get(v.get("ticker")) or [], cur)
        items, buy_rets, buy_high_rets = [], [], []
        HIGH_CONF = 78  # práh „vysoká jistota" (skóre modelu)
        now = int(time.time())
        for v in verdicts:
            cur = prices.get(v.get("ticker"))
            exit_status, exit_label = None, None
            if v.get("verdict") == "Koupit":
                ret, exit_status, exit_label = _replay(v, cur)
            else:
                entry = v.get("price")
                ret = ((cur - entry) / entry * 100) if (cur and entry) else None
            if v.get("verdict") == "Koupit" and ret is not None:
                buy_rets.append(ret)
                if isinstance(v.get("score"), (int, float)) and v["score"] >= HIGH_CONF:
                    buy_high_rets.append(ret)
            items.append({**v, "current": _round(cur, 3), "return_pct": _round(ret, 2),
                          "exit_status": exit_status, "exit_label": exit_label})
        items = items[-60:][::-1]

        stats = {}
        if buy_rets:
            stats = {
                # ploché klíče kvůli zpětné kompatibilitě
                "buy_count": len(buy_rets),
                "buy_avg_return": round(sum(buy_rets) / len(buy_rets), 2),
                "buy_win_rate": round(sum(1 for r in buy_rets if r > 0) / len(buy_rets) * 100, 1),
                # rozpad podle jistoty
                "buy_all": _agg_returns(buy_rets),
                "buy_high": _agg_returns(buy_high_rets),
                "high_conf_threshold": HIGH_CONF,
            }
        return jsonify({"ok": True, "count": len(verdicts), "items": items, "stats": stats,
                        "model": model})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# STRATEGIE v2 (2026-10) – celé S&P 500 + HODNOTA & MOMENTUM + rizikové profily
#
# Ověřeno offline walk-forward testem (čísla ve VALIDATION níže): historické
# složení S&P 500 včetně vyřazených titulů (žádní „budoucí vítězové"), ceny
# včetně dividend, fundamenty z SEC až 120 dní po konci fiskálního roku,
# ladění 2015-2022, ověření na nedotčeném období 2023-2026.
#
# Co se změnilo proti v1 a proč – každý bod změřený, ne odhadnutý:
#  * universum 45 titulů → celé S&P 500. Ze 45 titulů vycházel signál zřídka
#    a většina kapitálu ležela ladem; S&P 500 dá ~10 kandidátů denně.
#  * nákupní signál = TOP setup (≥80 + klidný objem) A skóre hodnota&momentum
#    (VM) ≥ 65/100 (≈ nejlepší pětina S&P 500), řazeno podle VM. VM je PRŮMĚR
#    percentilů šesti složek, ne samo percentil – proto „65" ≠ „top 35 %".
#    Zisk na obchod +2,07 % / +2,13 %
#    (trénink/holdout) proti +2,03 % / +1,53 % u samotného TOP. Práh 65 je
#    střed stabilního plató 60-70 (0,50-0,55 výrazně horší) – ne vyladěná špička.
#  * pokles u DRAHÉHO titulu se SLÁBNOUCÍM momentem (VM < 30) dával v holdoutu
#    -0,59 % na obchod – takové poklesy model nekupuje.
#  * „podhodnocené" = nejvyšší VM. Dřívější „Potenciál" (45 % skóre tvořila
#    vzdálenost od ročního maxima) neměl žádnou předpovědní hodnotu
#    (Q5-Q1 +0,03 %/měsíc, t ≈ 0).
#  * konzervativní profil = cílení volatility 15 % (propad -17,5 % místo -33,7 %
#    u indexu). Trendový filtr „pod 200denním průměrem prodat" zamítnut:
#    v holdoutu stál 7 bodů ročně (prodal na dně celního propadu 2025).
#  * vstup o den později (signál ráno, nákup další den) stojí jen ~0,04 bodu
#    na obchod – strategie jde reálně obchodovat podle ranního přehledu.
#  * max 2 pozice na sektor (viz SECTOR_CAP) – diverzifikace skoro zadarmo.
# ---------------------------------------------------------------------------
VM_MIN = 0.65
POSITION_WEIGHT = {"conservative": 0.10, "balanced": 0.10, "aggressive": 0.20}
VOL_TARGET = {"conservative": 0.15}
RISK_PROFILES = ("conservative", "balanced", "aggressive")
# Max 2 pozice z jednoho sektoru. Na historii stojí ~0,2 bodu ročně (trénink
# -0,7, holdout +1,5) – prakticky zdarma pojištění proti šoku v jednom odvětví,
# který backtest nemusí zachytit (např. 5 custody bank = jedna sázka pětkrát).
SECTOR_CAP = 2
# Sken běží po dávkách a průběh ukládá do KV, takže doběhne při JAKÉMKOLI
# limitu běhu funkce (další volání pokračuje tam, kde předchozí skončilo).
SCAN_BUDGET_S = float(os.environ.get("SCAN_BUDGET_S") or 40)
SCAN_WORKERS = 10
SP500_CSV_URL = ("https://raw.githubusercontent.com/datasets/"
                 "s-and-p-500-companies/main/data/constituents.csv")

SP500_FALLBACK = (
    "MMM:66740 AOS:91142 ABT:1800 ABBV:1551152 ACN:1467373 ADBE:796343 AMD:2488 AES:874761 AFL:4977 "
    "A:1090872 APD:2969 ABNB:1559720 AKAM:1086222 ALB:915913 ARE:1035443 ALGN:1097149 ALLE:1579241 "
    "LNT:352541 ALL:899051 GOOGL:1652044 GOOG:1652044 MO:764180 AMZN:1018724 AMCR:1748790 AEE:1002910 "
    "AEP:4904 AXP:4962 AIG:5272 AMT:1053507 AWK:1410636 AMP:820027 AME:1037868 AMGN:318154 APH:820313 "
    "ADI:6281 AON:315293 APA:1841666 APO:1858681 AAPL:320193 AMAT:6951 APP:1751008 APTV:1521332 "
    "ACGL:947484 ADM:7084 ARES:1176948 ANET:1596532 AJG:354190 AIZ:1267238 T:732717 ATO:731802 "
    "ADSK:769397 ADP:8670 AZO:866787 AVY:8818 AXON:1069183 BKR:1701605 BALL:9389 BAC:70858 BAX:10456 "
    "BDX:10795 BRK.B:1067983 BBY:764478 TECH:842023 BIIB:875045 BLK:2012383 BX:1393818 XYZ:1512673 "
    "BE:1664703 BNY:1390777 BA:12927 BKNG:1075531 BSX:885725 BMY:14272 AVGO:1730168 BR:1383312 "
    "BRO:79282 BF.B:14693 BG:1996862 BXP:1037540 CHRW:1043277 CDNS:813672 CPT:906345 COF:927628 "
    "CAH:721371 CCL:815097 CARR:1783180 CVNA:1690820 CASY:726958 CAT:18230 CBOE:1374310 CBRE:1138118 "
    "CDW:1402057 COR:1140859 CNC:1071739 CNP:1130310 CF:1324404 CRL:1100682 SCHW:316709 CHTR:1091667 "
    "CVX:93410 CMG:1058090 CB:896159 CHD:313927 CIEN:936395 CI:1739940 CINF:20286 CTAS:723254 "
    "CSCO:858877 C:831001 CFG:759944 CLX:21076 CME:1156375 CMS:811156 KO:21344 CTSH:1058290 "
    "COHR:820318 COIN:1679788 CL:21665 CMCSA:1166691 FIX:1035983 COP:1163165 ED:1047862 STZ:16918 "
    "CEG:1868275 COO:711404 CPRT:900075 GLW:24741 CPAY:1175454 CSGP:1057352 COST:909832 CRH:849395 "
    "CRWD:1535527 CCI:1051470 CSX:277948 CMI:26172 CVS:64803 DHR:313616 DRI:940944 DDOG:1561550 "
    "DVA:927066 DECK:910521 DE:315189 DELL:1571996 DAL:27904 DVN:1090012 DXCM:1093557 FANG:1539838 "
    "DLR:1297996 DG:29534 DLTR:935703 D:715957 DPZ:1286681 DASH:1792789 DOV:29905 DOW:1751788 "
    "DHI:882184 DTE:936340 DUK:1326160 DD:1666700 ETN:1551182 EBAY:1065088 ECHO:1415404 ECL:31462 "
    "EIX:827052 EW:1099800 ELV:1156039 EME:105634 EMR:32604 ETR:65984 EOG:821189 EQT:33213 EFX:33185 "
    "EQIX:1101239 ERIE:922621 ESS:920522 EL:1001250 EG:1095073 EVRG:1711269 P:1474432 ES:72741 "
    "EXC:1109357 EXE:895126 EXPE:1324424 EXPD:746515 EXR:1289490 XOM:2115436 FFIV:1048695 FDS:1013237 "
    "FICO:814547 FAST:815556 FRT:34903 FDX:1048911 FDXF:2082247 FERG:2011641 FIS:1136893 FITB:35527 "
    "FSLR:1274494 FE:1031296 FISV:798354 FLEX:866374 F:37996 FTNT:1262039 FTV:1659166 FOXA:1754301 "
    "FOX:1754301 BEN:38777 FCX:831259 GRMN:1121788 IT:749251 GE:40545 GEHC:1932393 GEV:1996810 "
    "GEN:849399 GNRC:1474735 GD:40533 GIS:40704 GM:1467858 GPC:40987 GILD:882095 GPN:1123360 "
    "GL:320335 GDDY:1609711 GS:886982 HAL:45012 HIG:874766 HAS:46080 HCA:860730 DOC:765880 "
    "HSIC:1000228 HSY:47111 HPE:1645590 HLT:1585689 HD:354950 HONA:2089271 HON:773840 HRL:48465 "
    "HST:1070750 HWM:4281 HPQ:47217 HUBB:48898 HUM:49071 HBAN:49196 HII:1501585 IBM:51143 IEX:832101 "
    "IDXX:874716 ITW:49826 ILMN:1110803 INCY:879169 IR:1699150 PODD:1145197 INTC:50863 IBKR:1381197 "
    "ICE:1571949 IFF:51253 IP:51434 INTU:896878 ISRG:1035267 IVZ:914208 INVH:1687229 IQV:1478242 "
    "IRM:1020569 JBHT:728535 JBL:898293 JKHY:779152 J:52988 JNJ:200406 JCI:833444 JPM:19617 "
    "KVUE:1944048 KDP:1418135 KEY:91576 KEYS:1601046 KMB:55785 KIM:879101 KMI:1506307 KKR:1404912 "
    "KLAC:319201 KHC:1637459 KR:56873 LHX:202058 LH:920148 LRCX:707549 LVS:1300514 LDOS:1336920 "
    "LEN:920760 LII:1069202 LLY:59478 LIN:1707925 LYV:1335258 LMT:936468 L:60086 LOW:60667 "
    "LULU:1397187 LITE:1633978 LYB:1489393 MTB:36270 MPC:1510295 MAR:1048286 MRSH:62709 MLM:916076 "
    "MRVL:1835632 MAS:62996 MA:1141391 MKC:63754 MCD:63908 MCK:927653 MDT:1613103 MRK:310158 "
    "META:1326801 MET:1099219 MTD:1037646 MGM:789570 MCHP:827054 MU:723125 MSFT:789019 MAA:912595 "
    "MRNA:1682852 MDLZ:1103982 MPWR:1280452 MNST:865752 MCO:1059556 MS:895421 MOS:1285785 MSI:68505 "
    "MSCI:1408198 NDAQ:1120193 NTAP:1002047 NFLX:1065280 NEM:1164727 NWSA:1564708 NWS:1564708 "
    "NEE:753308 NKE:320187 NI:1111711 NDSN:72331 NSC:702165 NTRS:73124 NOC:1133421 NCLH:1513761 "
    "NRG:1013871 NUE:73309 NVDA:1045810 NVR:906163 NXPI:1413447 ORLY:898173 OXY:797468 ODFL:878927 "
    "OMC:29989 ON:1097864 OKE:1039684 ORCL:1341439 OTIS:1781335 PCAR:75362 PKG:75677 PLTR:1321655 "
    "PANW:1327567 PH:76334 PAYX:723531 PYPL:1633917 PNR:77360 PEP:77476 PFE:78003 PCG:1004980 "
    "PM:1413329 PSX:1534701 PNW:764622 PNC:713676 PPG:79879 PPL:922224 PFG:1126328 PG:80424 PGR:80661 "
    "PLD:1045609 PRU:1137774 PEG:788784 PTC:857005 PSA:1393311 PHM:822416 PWR:1050915 QCOM:804328 "
    "DGX:1022079 Q:2058873 RL:1037038 RJF:720005 RDDT:1713445 RTX:101829 O:726728 REG:910606 "
    "REGN:872589 RF:1281761 RSG:1060391 RMD:943819 RVTY:31791 HOOD:1783879 ROK:1024478 ROL:84839 "
    "ROP:882835 ROST:745732 RCL:884887 SPGI:64040 CRM:1108524 SNDK:2023554 SBAC:1034054 SLB:87347 "
    "STX:1137789 SRE:1032208 NOW:1373715 SHW:89800 SPG:1063761 SKYD:2041610 SWKS:4127 SJM:91419 "
    "SW:2005951 SNA:91440 SOLV:1964738 SO:92122 LUV:92380 SWK:93556 SBUX:829224 STT:93751 "
    "STLD:1022671 STE:1757898 SYK:310764 SMCI:1375365 SYF:1601712 SNPS:883241 SYY:96021 TMUS:1283699 "
    "TROW:1113169 TTWO:946581 TPR:1116132 TRGP:1389170 TGT:27419 TEL:1385157 TDY:1094285 TER:97210 "
    "TSLA:1318605 TXN:97476 TPL:1811074 TXT:217346 TMO:97745 TJX:109198 TKO:1973266 TSCO:916365 "
    "TT:1466258 TDG:1260221 TRV:86312 TRMB:864749 TFC:92230 TWLO:1447669 TYL:860731 TSN:100493 "
    "USB:36104 UBER:1543151 UDR:74208 ULTA:1403568 UNP:100885 UAL:100517 UPS:1090727 URI:1067701 "
    "UNH:731766 UHS:352915 VLO:1035002 VEEV:1393052 VTR:740260 VLTO:1967680 VRSN:1014473 VRSK:1442145 "
    "VZ:732712 VRTX:875320 VRT:1674101 VTRS:1792044 VICI:1705696 V:1403161 VST:1692819 VMRK:906107 "
    "VMC:1396009 VYLR:2128626 WRB:11544 GWW:277135 WAB:943452 WMT:104169 DIS:1744489 WM:823768 "
    "WAT:1000697 WEC:783325 WFC:72971 WELL:766704 WST:105770 WDC:106040 WY:106535 WSM:719955 "
    "WMB:107263 WTW:1140536 WDAY:1327811 WYNN:1174922 XEL:72903 XYL:1524472 YUM:1041061 ZBRA:877212 "
    "ZBH:1136869 ZTS:1555280 "
)

VALIDATION = {'as_of': '2026-10-14',
 'data': '2015-01 .. 2026-09, historické složení S&P 500 (633 titulů vč. 131 vyřazených), ceny vč. '
         'dividend, fundamenty z SEC s 120denním zpožděním',
 'rule': {'setup_min': 80,
          'vol_ratio_max': 1.15,
          'vm_min_pct': 65,
          'target_pct': 15,
          'stop_pct': 10,
          'max_days': 30,
          'slots': 5,
          'sector_cap': 2},
 'profiles': {'conservative': {'weight_pct': 10,
                               'vol_target_pct': 15,
                               'train': {'cagr': 10.7,
                                         'max_dd': -18.2,
                                         'sharpe': 0.83,
                                         'trades': 215,
                                         'win_rate': 62.8},
                               'holdout': {'cagr': 19.3,
                                           'max_dd': -14.8,
                                           'sharpe': 1.49,
                                           'trades': 128,
                                           'win_rate': 61.7},
                               'full': {'cagr': 13.2,
                                        'max_dd': -18.2,
                                        'sharpe': 1.01,
                                        'trades': 347,
                                        'win_rate': 62.5}},
              'balanced': {'weight_pct': 10,
                           'vol_target_pct': None,
                           'train': {'cagr': 12.9,
                                     'max_dd': -33.6,
                                     'sharpe': 0.74,
                                     'trades': 215,
                                     'win_rate': 62.8},
                           'holdout': {'cagr': 21.9,
                                       'max_dd': -19.0,
                                       'sharpe': 1.47,
                                       'trades': 128,
                                       'win_rate': 61.7},
                           'full': {'cagr': 15.6,
                                    'max_dd': -33.6,
                                    'sharpe': 0.91,
                                    'trades': 347,
                                    'win_rate': 62.5}},
              'aggressive': {'weight_pct': 20,
                             'vol_target_pct': None,
                             'train': {'cagr': 15.5,
                                       'max_dd': -34.8,
                                       'sharpe': 0.81,
                                       'trades': 215,
                                       'win_rate': 62.8},
                             'holdout': {'cagr': 21.2,
                                         'max_dd': -21.0,
                                         'sharpe': 1.28,
                                         'trades': 128,
                                         'win_rate': 61.7},
                             'full': {'cagr': 17.0,
                                      'max_dd': -34.8,
                                      'sharpe': 0.92,
                                      'trades': 347,
                                      'win_rate': 62.5}}},
 'benchmark': {'spy': {'train': {'cagr': 10.1, 'max_dd': -33.7, 'sharpe': 0.61},
                       'holdout': {'cagr': 22.3, 'max_dd': -18.8, 'sharpe': 1.43},
                       'full': {'cagr': 13.9, 'max_dd': -33.7, 'sharpe': 0.83}},
               'spy_voltarget': {'train': {'cagr': 8.6, 'max_dd': -18.7, 'sharpe': 0.7},
                                 'holdout': {'cagr': 19.6, 'max_dd': -14.7, 'sharpe': 1.46},
                                 'full': {'cagr': 11.9, 'max_dd': -18.7, 'sharpe': 0.94}}},
 'signals': {'train': {'model': {'count': 562, 'win_rate': 59.8, 'avg_return': 2.07, 'ci95': 0.61},
                       'old_top': {'count': 1648,
                                   'win_rate': 61.0,
                                   'avg_return': 2.03,
                                   'ci95': 0.37},
                       'all_dips': {'count': 18026,
                                    'win_rate': 57.2,
                                    'avg_return': 1.52,
                                    'ci95': 0.12},
                       'rejected_low_vm': {'count': 738,
                                           'win_rate': 54.6,
                                           'avg_return': 1.21,
                                           'ci95': 0.61}},
             'holdout': {'model': {'count': 379,
                                   'win_rate': 58.6,
                                   'avg_return': 2.13,
                                   'ci95': 0.82},
                         'old_top': {'count': 1138,
                                     'win_rate': 56.4,
                                     'avg_return': 1.53,
                                     'ci95': 0.47},
                         'all_dips': {'count': 10569,
                                      'win_rate': 53.9,
                                      'avg_return': 1.23,
                                      'ci95': 0.16},
                         'rejected_low_vm': {'count': 442,
                                             'win_rate': 44.1,
                                             'avg_return': -0.59,
                                             'ci95': 0.79}},
             'full': {'model': {'count': 941, 'win_rate': 59.3, 'avg_return': 2.09, 'ci95': 0.49},
                      'old_top': {'count': 2786,
                                  'win_rate': 59.2,
                                  'avg_return': 1.83,
                                  'ci95': 0.29},
                      'all_dips': {'count': 28595,
                                   'win_rate': 56.0,
                                   'avg_return': 1.41,
                                   'ci95': 0.09},
                      'rejected_low_vm': {'count': 1180,
                                          'win_rate': 50.7,
                                          'avg_return': 0.53,
                                          'ci95': 0.49}}}}



def _vm_for(ticker):
    """Percentil hodnota&momentum z posledního skenu S&P 500 (None mimo index)."""
    try:
        return ((kv_get_json("scan:last") or {}).get("vm_map") or {}).get((ticker or "").upper())
    except Exception:
        return None


def _risk(r):
    return r if r in RISK_PROFILES else "balanced"


def _user_risk(user):
    try:
        return _risk((kv_get_json(f"user:{user}") or {}).get("risk"))
    except Exception:
        return "balanced"


def _sp500_universe():
    """[(symbol, cik, sektor)] – živě z veřejného seznamu (kešováno týden),
    při výpadku vestavěná záloha. Složení se mění jen o pár titulů za kvartál."""
    try:
        c = kv_get_json("sp500:list")
        if c and time.time() - c.get("ts", 0) < 7 * 86400 and len(c.get("items") or []) > 400:
            return [tuple(x) for x in c["items"]]
    except Exception:
        pass
    items = []
    try:
        import csv as _csv
        import io as _io
        r = requests.get(SP500_CSV_URL, headers={"User-Agent": UA}, timeout=10)
        for row in _csv.DictReader(_io.StringIO(r.text)):
            sym = (row.get("Symbol") or "").strip().upper()
            cik = (row.get("CIK") or "").strip()
            if sym:
                items.append((sym, int(cik) if cik.isdigit() else None,
                              (row.get("GICS Sector") or "").strip()))
    except Exception:
        items = []
    if len(items) < 400:
        items = [(p.split(":")[0], int(p.split(":")[1]), "") for p in SP500_FALLBACK.split()]
    else:
        try:
            kv_set_json("sp500:list", {"ts": time.time(), "items": items})
        except Exception:
            pass
    return items


# (taxonomie, koncept, jednotka, okamžikový stav?, klíč)
_SEC_FUND_CONCEPTS = [
    ("us-gaap", "EarningsPerShareDiluted", "USD-per-shares", False, "eps"),
    ("us-gaap", "NetCashProvidedByUsedInOperatingActivities", "USD", False, "cfo"),
    ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding", "shares", False, "sh2"),
    ("us-gaap", "StockholdersEquity", "USD", True, "eq"),
    ("dei", "EntityCommonStockSharesOutstanding", "shares", True, "sh"),
]
SEC_LAG_DAYS = 120       # roční čísla jsou „známá" až 120 dní po konci FY
SEC_MAX_AGE_DAYS = 550   # starší čísla už nepoužívat


def _sec_fundamentals(ciks=None):
    """{cik: {eps, cfo, eq, sh}} – poslední ROČNÍ čísla, která už v daný den
    byla veřejně známá (konec FY + 120 dní). Stejné pravidlo jako ve výzkumu,
    takže živé skóre počítá přesně to, co bylo ověřeno. Zdroj: SEC XBRL
    frames (všechny firmy za období jedním dotazem). Kešováno 3 dny."""
    try:
        c = kv_get_json("sec:fund:v2")
        if c and time.time() - c.get("ts", 0) < 3 * 86400 and c.get("data"):
            return {int(k): v for k, v in c["data"].items()}
    except Exception:
        pass
    today = datetime.now(timezone.utc).date()
    jobs = []
    for y in (today.year, today.year - 1, today.year - 2):
        for tax, concept, unit, inst, key in _SEC_FUND_CONCEPTS:
            jobs.append((tax, concept, unit, f"CY{y}Q4I" if inst else f"CY{y}", key))

    def fetch(job):
        tax, concept, unit, per, key = job
        try:
            r = requests.get(f"https://data.sec.gov/api/xbrl/frames/{tax}/{concept}/{unit}/{per}.json",
                             headers=SEC_HEADERS, timeout=20)
            return key, (r.json().get("data") or []) if r.status_code == 200 else []
        except Exception:
            return key, []

    from concurrent.futures import ThreadPoolExecutor
    best = {}
    with ThreadPoolExecutor(max_workers=5) as ex:      # SEC povoluje max 10 dotazů/s
        for key, rows in ex.map(fetch, jobs):
            for d in rows:
                cik = d.get("cik")
                if ciks is not None and cik not in ciks:
                    continue
                try:
                    end = datetime.strptime(d["end"], "%Y-%m-%d").date()
                except Exception:
                    continue
                age = (today - end).days
                if age < SEC_LAG_DAYS or age > SEC_MAX_AGE_DAYS:
                    continue
                k = (int(cik), key)
                if k not in best or end > best[k][0]:
                    best[k] = (end, d.get("val"))
    out = {}
    for (cik, key), (_end, val) in best.items():
        out.setdefault(cik, {})[key] = val
    for v in out.values():
        if not v.get("sh") and v.get("sh2"):
            v["sh"] = v["sh2"]
        v.pop("sh2", None)
    if out:
        try:
            kv_set_json("sec:fund:v2", {"ts": time.time(),
                                        "data": {str(k): v for k, v in out.items()}})
        except Exception:
            pass
    return out


def _scan_one(item):
    """Denní metriky jednoho titulu ze 2 let dat. Vstupy identické s výzkumem:
    technické skóre z 400 posledních dní, momentum 12-1 z cen vč. dividend."""
    sym = item[0]
    d = None
    for attempt in range(2):
        try:
            d = yahoo_chart(sym.replace(".", "-"), "2y", "1d")
            break
        except Exception:
            time.sleep(0.8)
    if not d:
        return {"err": 1}
    meta = d.get("meta") or {}
    ind_ = d.get("indicators") or {}
    q = (ind_.get("quote") or [{}])[0]
    adjs = (ind_.get("adjclose") or [{}])[0].get("adjclose") or []
    cl_raw, vl_raw = q.get("close") or [], q.get("volume") or []
    closes, vols, adj = [], [], []
    for k, c in enumerate(cl_raw):
        if c is None:
            continue
        a = adjs[k] if k < len(adjs) and adjs[k] is not None else c
        closes.append(c)
        adj.append(a)
        v = vl_raw[k] if k < len(vl_raw) else None
        vols.append(v if v is not None else 0)
    if len(closes) < 260:
        return {"err": 1}
    ind = compute_indicators(closes[-400:], vols[-400:])
    price = meta.get("regularMarketPrice") or closes[-1]
    score = tech_setup_score(price, ind)
    prev = _prev_close(closes, meta)
    mom = (adj[-22] / adj[-253] - 1) if (len(adj) >= 253 and adj[-253]) else None
    return {
        "name": meta.get("shortName") or meta.get("longName") or sym,
        "price": round(price, 4), "close": round(closes[-1], 4),
        "chg": round((price - prev) / prev * 100, 2) if prev else 0,
        "cur": meta.get("currency", "USD"),
        "score": score, "top": bool(is_top_signal(score, ind)),
        "rsi": ind.get("rsi"), "vol": ind.get("volatility"), "vr": ind.get("vol_ratio"),
        "mom": round(mom, 4) if mom is not None else None,
        "y1": round((adj[-1] / adj[-253] - 1) * 100, 1) if len(adj) >= 253 else None,
    }


def _pct_ranks(vals):
    idx = sorted([i for i, v in enumerate(vals) if v is not None], key=lambda i: vals[i])
    out, n = [None] * len(vals), len(idx)
    for k, i in enumerate(idx):
        out[i] = k / (n - 1) if n > 1 else 0.5
    return out


def _vm_scores(recs):
    """HODNOTA & MOMENTUM = průměr percentilů E/P, B/P, CF/P a 3× momenta 12-1
    v rámci S&P 500 (momentum váží stejně jako celá hodnotová trojice). Hodnota
    a momentum jsou záporně korelované, proto je kombinace stabilní tam, kde
    každá složka zvlášť selhávala. Titul potřebuje aspoň 3 ze 6 složek."""
    keys = ["ep", "bp", "cfp", "mom", "mom", "mom"]
    ranks = [_pct_ranks([r.get(k) for r in recs]) for k in keys]
    out = []
    for i in range(len(recs)):
        v = [rk[i] for rk in ranks if rk[i] is not None]
        out.append(sum(v) / len(v) if len(v) >= 3 else None)
    return out


def _signal_item(r):
    lv = compute_levels({"price": r["price"]}) or {}
    ep = r.get("ep")
    return {
        "ticker": r["ticker"], "name": r.get("name") or r["ticker"],
        "price": _round(r["price"], 2), "change_pct": r.get("chg"), "currency": r.get("cur", "USD"),
        "score": r.get("score"), "rsi": _round(r.get("rsi"), 1), "note": _setup_note(r.get("score")),
        "conviction": "top", "vm": int(round((r.get("vm") or 0) * 100)), "vm_rank": r.get("vm_rank"),
        "mom_pct": round(r["mom"] * 100, 1) if r.get("mom") is not None else None,
        "pe": round(1 / ep, 1) if (ep and ep > 0) else None,
        "volatility_pct": r.get("vol"), "market_cap": r.get("mcap"), "sector": r.get("sector"),
        "target": lv.get("target_price"), "stop": lv.get("stop_loss"),
        "reward_pct": lv.get("reward_pct"), "risk_pct": lv.get("risk_pct"), "rr": lv.get("rr"),
    }


def _value_item(r):
    """Položka pro „Podhodnocené s momentem" – data, ne slogany."""
    vm = int(round(r["vm"] * 100))
    ep, bp, cfp, mom = r.get("ep"), r.get("bp"), r.get("cfp"), r.get("mom")
    pe = (1 / ep) if (ep and ep > 0) else None
    badges = []
    if pe and pe < 15:
        badges.append({"t": f"Levná · P/E {pe:.0f}", "k": "up"})
    if cfp and cfp >= 0.08:
        badges.append({"t": f"Cash flow {cfp * 100:.0f} % ceny", "k": "gem"})
    if mom is not None and mom >= 0.25:
        badges.append({"t": f"Momentum +{mom * 100:.0f} %", "k": "hot"})
    if r.get("top"):
        badges.append({"t": "Teď v nákupní zóně", "k": "extreme"})
    has_fund = any(r.get(k) is not None for k in ("ep", "bp", "cfp"))
    if not has_fund:
        badges.append({"t": "Jen momentum – chybí data SEC", "k": "down"})
    return {
        "ticker": r["ticker"], "name": r.get("name") or r["ticker"],
        "price": _round(r["price"], 2), "change_pct": r.get("chg"), "currency": r.get("cur", "USD"),
        "market_cap": r.get("mcap"), "sector": r.get("sector"), "volatility_pct": r.get("vol"),
        "score": vm, "vm_rank": r.get("vm_rank"),
        "tier": ("TOP 2 %" if r.get("vm_rank", 999) <= 10 else
                 "TOP 5 %" if r.get("vm_rank", 999) <= 25 else "TOP 10 %"),
        "badges": badges, "year_pct": r.get("y1"),
        "pe": round(pe, 1) if pe else None, "pb": round(1 / bp, 2) if (bp and bp > 0) else None,
        "fcf_yield": round(cfp * 100, 1) if cfp is not None else None,
        "mom_pct": round(mom * 100, 1) if mom is not None else None,
        "in_signal": bool(r.get("top") and r["vm"] >= VM_MIN), "has_fundamentals": has_fund,
    }


def _scan_finalize(date, uni, rows):
    ciks = {c for _s, c, _x in uni if c}
    fund = _sec_fundamentals(ciks)
    recs = []
    for sym, cik, sector in uni:
        r = rows.get(sym)
        if not r or r.get("err"):
            continue
        r = dict(r, ticker=sym, sector=sector)
        f = fund.get(cik) or {}
        px = r.get("close")
        mcap = (f["sh"] * px) if (f.get("sh") and px) else None
        r["mcap"] = mcap
        r["ep"] = (f["eps"] / px) if (f.get("eps") is not None and px) else None
        r["bp"] = (f["eq"] / mcap) if (f.get("eq") and mcap and f["eq"] > 0) else None
        r["cfp"] = (f["cfo"] / mcap) if (f.get("cfo") is not None and mcap) else None
        recs.append(r)
    for r, v in zip(recs, _vm_scores(recs)):
        r["vm"] = round(v, 4) if v is not None else None
    ranked = sorted([r for r in recs if r["vm"] is not None], key=lambda r: -r["vm"])
    for i, r in enumerate(ranked, 1):
        r["vm_rank"] = i
    n_ranked = len(ranked)
    sig = sorted([r for r in recs if r.get("top") and (r.get("vm") or 0) >= VM_MIN],
                 key=lambda r: -r["vm"])
    per_sector = {}
    for r in sig:
        per_sector[r.get("sector") or "?"] = per_sector.get(r.get("sector") or "?", 0) + 1
        r["sector_rank"] = per_sector[r.get("sector") or "?"]
    rejected = [r for r in recs if r.get("top") and r.get("vm") is not None and r["vm"] < VM_MIN]
    value = sorted([r for r in recs if r.get("vm") is not None], key=lambda r: -r["vm"])[:30]
    out = {
        "date": date, "updated": datetime.now(timezone.utc).strftime("%H:%M UTC"),
        "universe": len(uni), "scanned": len(recs), "ranked": n_ranked,
        "with_fundamentals": sum(1 for r in recs if r.get("ep") is not None),
        "signals": [dict(_signal_item(r), sector_rank=r["sector_rank"]) for r in sig],
        "rejected": [{"ticker": r["ticker"], "vm": int(round(r["vm"] * 100))} for r in rejected],
        "value": [_value_item(r) for r in value],
        "vm_map": {r["ticker"]: int(round(r["vm"] * 100)) for r in recs if r.get("vm") is not None},
        "sector_map": {r["ticker"]: r.get("sector") or "" for r in recs},
        "rank_map": {r["ticker"]: r["vm_rank"] for r in ranked},
        "name_map": {r["ticker"]: r.get("name") or r["ticker"] for r in recs},
        "mcap_map": {r["ticker"]: round(r["mcap"] / 1e6) for r in recs if r.get("mcap")},
        "rule": {"vm_min": int(VM_MIN * 100), "setup_min": VERDICT_TOP, "sector_cap": SECTOR_CAP},
    }
    try:
        kv_set_json(f"scan:{date}", out)
        kv_set_json("scan:last", out)
    except Exception:
        pass
    _paper_record(date, out["signals"])
    return out


def _scan_step(budget_s=None, local=None):
    """Jeden krok denního skenu S&P 500. Zpracovává dávky, dokud nevyprší časový
    rozpočet, a průběh průběžně ukládá do KV – další volání pokračuje tam, kde
    tohle skončilo. Vrací hotový sken, nebo None (ještě nedoběhl)."""
    date = _today_iso()
    try:
        done = kv_get_json(f"scan:{date}")
        if done:
            return done
    except Exception:
        pass
    budget = budget_s or SCAN_BUDGET_S
    uni = _sp500_universe()
    hkey = _pk(f"scanrows:{date}")
    have = {}
    if cloud_enabled():
        try:
            raw = _kv_cmd("HGETALL", hkey) or []
            for i in range(0, len(raw) - 1, 2):
                have[raw[i]] = json.loads(raw[i + 1])
        except Exception:
            pass
    if local is not None:
        have.update(local)
    todo = [u for u in uni if u[0] not in have]
    t0 = time.time()
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        while todo and time.time() - t0 < budget:
            batch, todo = todo[:50], todo[50:]
            args = []
            for (sym, _c, _s), row in zip(batch, ex.map(_scan_one, batch)):
                have[sym] = row
                args += [sym, json.dumps(row)]
                if local is not None:
                    local[sym] = row
            if cloud_enabled():
                try:
                    _kv_cmd("HSET", hkey, *args)
                    _kv_cmd("EXPIRE", hkey, 3 * 86400)
                except Exception:
                    pass
    if todo:
        return None
    return _scan_finalize(date, uni, have)


def market_scan(allow_step=True, budget_s=None):
    """Dnešní hotový sken; když ještě není, poslední hotový (a případně krok
    skenu). Uživatelský požadavek nikdy nečeká na celý sken, pokud existuje
    nějaký předchozí – ten těžký běh dělají ranní crony."""
    date = _today_iso()
    try:
        s = kv_get_json(f"scan:{date}")
        if s:
            return s
        last = kv_get_json("scan:last")
    except Exception:
        last = None
    if last and not allow_step:
        return dict(last, stale=True)
    s = _scan_step(budget_s)
    if s:
        return s
    return dict(last, stale=True) if last else None


def _market_risk():
    """Živé riziko trhu: realizovaná volatilita SPY za 20 dní a z ní doporučená
    expozice pro profily s cílením volatility. Kešováno hodinu."""
    try:
        c = kv_get_json("risk:spy")
        if c and time.time() - c.get("ts", 0) < 3600:
            return c
    except Exception:
        pass
    try:
        d = yahoo_chart("SPY", "3mo", "1d")
        cl = [c for c in (((d.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []) if c]
        rets = [cl[i] / cl[i - 1] - 1 for i in range(len(cl) - 20, len(cl))]
        mu = sum(rets) / len(rets)
        vol = math.sqrt(sum((x - mu) ** 2 for x in rets) / (len(rets) - 1)) * math.sqrt(252)
        out = {"ts": int(time.time()), "spy_vol": round(vol * 100, 1)}
        for prof, tgt in VOL_TARGET.items():
            out[f"exposure_{prof}"] = int(round(min(1.0, tgt / vol) * 100)) if vol else 100
        try:
            kv_set_json("risk:spy", out)
        except Exception:
            pass
        return out
    except Exception:
        return {"spy_vol": None}


def _paper_record(date, signals):
    """ŽIVÝ ZÁZNAM MODELU: každý denní signál se zapíše jako papírový obchod
    (vstup za tehdejší cenu, plán cíl/stop/max dní) a vyhodnotí se automaticky
    – nezávisle na tom, co kdo v appce rozklikl. Backtest je jen minulost;
    tohle je jediný poctivý důkaz do budoucna."""
    if not signals or not cloud_enabled():
        return
    try:
        if kv_get_json(f"paper:day:{date}"):
            return
        recent = kv_lrange("paper:trades", -400, -1)
        cutoff = time.time() - 7 * 86400      # stejný titul nejdřív po týdnu (jako ve výzkumu)
        seen = {t.get("ticker") for t in recent if (t.get("ts") or 0) > cutoff}
        for s in signals:
            if s["ticker"] in seen:
                continue
            kv_rpush("paper:trades", {
                "ticker": s["ticker"], "date": date, "ts": int(time.time()),
                "price": s["price"], "target": s["target"], "stop": s["stop"],
                "horizon_days": EXIT_MAX_DAYS, "vm": s["vm"], "score": s["score"],
                "verdict": "Koupit"})
        kv_set_json(f"paper:day:{date}", {"n": len(signals)})
    except Exception:
        pass


# ---------------------------------------------------------------------------
# DLOUHODOBÝ PLÁN (2026-10) – skutečné pozice, jádro + akcie, „Kam s penězi",
# rentgen portfolia, stres test, cesta k cíli a 3letý časový test.
#
# Pravidla ověřila simulace dlouhodobého investora, který jen přidává peníze
# (LONGTERM níže, 2015-2026, historické složení S&P 500, náklady 10 bps):
#  * jádro = S&P 500 UCITS ETF, satelit = akcie podle hodnoty & momenta
#  * nové peníze jdou na mezery vůči cíli (úměrně mezerám), nic se neprodává
#    kvůli vyvažování – měsíční přeskupování bylo o 5 bodů ročně horší
#  * nový titul vstoupí JEN do volného slotu, max 30 % slotů z jednoho sektoru;
#    držený titul zůstává v plánu, dokud je v top 3×N
#  * VÍTĚZE NECHAT BĚŽET: prodávat titul jen proto, že vypadl z plánu, stálo
#    3 body ročně (odřízlo NVDA, AVGO, KLAC…; platí i bez NVDA). Po 36 měsících
#    (v ČR bez daně) se jen ořízne pozice nad 3× cílové váhy (riziko jedné
#    firmy) a prodají se zakrnělé tituly mimo plán pod ½ cílové váhy (úklid).
#  * dluhopisy jako „konzervativní" složka zamítnuty: -2,5 b./rok výnosu za
#    -1,5 b. propadu (2022 padaly s akciemi).
# ---------------------------------------------------------------------------
LONGTERM = {'rule': {'core': 'S&P 500 UCITS ETF',
          'buffer_x': 3,
          'trim_x': 3,
          'cleanup_below_x': 0.5,
          'after_months': 36,
          'sector_cap_pct': 30,
          'cost_bps': 10},
 'profiles': {'conservative': {'stocks_pct': 20,
                               'slots': 10,
                               'train': {'cagr': 14.0,
                                         'max_dd': -25.7,
                                         'sharpe': 0.87,
                                         'holdings': 32,
                                         'trims_per_year': 0.5,
                                         'sells_per_year': 3.2,
                                         'new_per_year': 7.2,
                                         'max_weight': 7.5},
                               'holdout': {'cagr': 20.9,
                                           'max_dd': -7.7,
                                           'sharpe': 1.61,
                                           'holdings': 34,
                                           'trims_per_year': 0.0,
                                           'sells_per_year': 2.7,
                                           'new_per_year': 11.7,
                                           'max_weight': 7.1},
                               'full': {'cagr': 18.2,
                                        'max_dd': -25.7,
                                        'sharpe': 1.13,
                                        'holdings': 37,
                                        'trims_per_year': 2.5,
                                        'sells_per_year': 4.9,
                                        'new_per_year': 8.0,
                                        'max_weight': 7.9}},
              'balanced': {'stocks_pct': 40,
                           'slots': 20,
                           'train': {'cagr': 15.2,
                                     'max_dd': -26.5,
                                     'sharpe': 0.91,
                                     'holdings': 48,
                                     'trims_per_year': 1.6,
                                     'sells_per_year': 5.5,
                                     'new_per_year': 11.5,
                                     'max_weight': 10.7},
                           'holdout': {'cagr': 21.3,
                                       'max_dd': -6.9,
                                       'sharpe': 1.64,
                                       'holdings': 48,
                                       'trims_per_year': 0.0,
                                       'sells_per_year': 4.3,
                                       'new_per_year': 17.1,
                                       'max_weight': 5.3},
                           'full': {'cagr': 19.5,
                                    'max_dd': -26.5,
                                    'sharpe': 1.18,
                                    'holdings': 50,
                                    'trims_per_year': 3.0,
                                    'sells_per_year': 7.4,
                                    'new_per_year': 11.7,
                                    'max_weight': 10.7}},
              'aggressive': {'stocks_pct': 70,
                             'slots': 25,
                             'train': {'cagr': 16.6,
                                       'max_dd': -27.8,
                                       'sharpe': 0.96,
                                       'holdings': 62,
                                       'trims_per_year': 1.6,
                                       'sells_per_year': 6.2,
                                       'new_per_year': 14.0,
                                       'max_weight': 14.0},
                             'holdout': {'cagr': 20.6,
                                         'max_dd': -6.3,
                                         'sharpe': 1.58,
                                         'holdings': 56,
                                         'trims_per_year': 0.0,
                                         'sells_per_year': 5.1,
                                         'new_per_year': 20.0,
                                         'max_weight': 7.5},
                             'full': {'cagr': 22.6,
                                      'max_dd': -27.8,
                                      'sharpe': 1.24,
                                      'holdings': 59,
                                      'trims_per_year': 2.6,
                                      'sells_per_year': 8.9,
                                      'new_per_year': 14.0,
                                      'max_weight': 14.0}},
              'maximum': {'stocks_pct': 100,
                          'slots': 25,
                          'train': {'cagr': 18.7,
                                    'max_dd': -27.9,
                                    'sharpe': 1.02,
                                    'holdings': 57,
                                    'trims_per_year': 1.5,
                                    'sells_per_year': 6.9,
                                    'new_per_year': 14.0,
                                    'max_weight': 18.7},
                          'holdout': {'cagr': 19.7,
                                      'max_dd': -9.4,
                                      'sharpe': 1.41,
                                      'holdings': 55,
                                      'trims_per_year': 0.0,
                                      'sells_per_year': 5.3,
                                      'new_per_year': 20.0,
                                      'max_weight': 10.8},
                          'full': {'cagr': 25.8,
                                   'max_dd': -27.9,
                                   'sharpe': 1.3,
                                   'holdings': 60,
                                   'trims_per_year': 2.2,
                                   'sells_per_year': 9.0,
                                   'new_per_year': 14.1,
                                   'max_weight': 18.7}}},
 'benchmark': {'train': {'cagr': 11.3, 'max_dd': -23.9, 'sharpe': 0.75},
               'holdout': {'cagr': 20.4, 'max_dd': -8.3, 'sharpe': 1.58},
               'full': {'cagr': 14.1, 'max_dd': -23.9, 'sharpe': 0.97}},
 'alternatives': {'conservative': {'sell_out_of_plan_36m': {'train': 12.3,
                                                            'holdout': 21.0,
                                                            'full': 15.5},
                                   'monthly_rebalance': {'train': 12.3,
                                                         'holdout': 20.4,
                                                         'full': 14.9}},
                  'balanced': {'sell_out_of_plan_36m': {'train': 13.5,
                                                        'holdout': 21.9,
                                                        'full': 16.5},
                               'monthly_rebalance': {'train': 12.2, 'holdout': 19.8, 'full': 14.6}},
                  'aggressive': {'sell_out_of_plan_36m': {'train': 14.1,
                                                          'holdout': 21.4,
                                                          'full': 16.8},
                                 'monthly_rebalance': {'train': 13.2,
                                                       'holdout': 18.5,
                                                       'full': 14.9}}}}

LT_PROFILES = {"conservative": (0.20, 10), "balanced": (0.40, 20), "aggressive": (0.70, 25),
               # Maximum = 100 % akcie bez indexového jádra. Celé 2015-26 +25,8 %, ale v holdoutu
               # 2023-26 jen ≈ index (+19,7 vs +20,4) a velký rozdíl proti indexu oběma směry.
               "maximum": (1.00, 25)}


def _lt_risk(r):
    """Profil dlouhodobého plánu (má navíc „maximum"; krátkodobé signály ho nemají)."""
    return r if r in LT_PROFILES else _risk(r)
LT_BUFFER = 3
LT_TRIM_X = 3                # po 36 m oříznout pozici nad 3× cílové váhy
LT_CLEANUP_X = 0.5           # po 36 m prodat titul mimo plán menší než ½ cíle
LT_SECTOR_CAP = 0.30         # max podíl slotů z jednoho sektoru pro nové tituly
TAX_FREE_MONTHS = 36
MIN_ORDER_CZK = 500          # menší nákupy nedávají smysl (poplatky, drobení)
CORE_ETF = {"ticker": "SXR8.DE", "name": "iShares Core S&P 500 UCITS ETF (Acc)"}
# ETF, která appka bere jako indexové jádro. „world" ≈ 63 % USA (pro rentgen).
CORE_ETFS = {
    "SXR8.DE": "sp500", "CSPX.L": "sp500", "CSPX.AS": "sp500", "VUAA.DE": "sp500", "VUAA.L": "sp500",
    "VUSA.L": "sp500", "VUSA.AS": "sp500", "VUSA.DE": "sp500", "SPY5.L": "sp500", "IUSA.DE": "sp500",
    "SPXS.L": "sp500", "SPY": "sp500", "VOO": "sp500", "IVV": "sp500",
    "VWCE.DE": "world", "VWRA.L": "world", "VWRL.L": "world", "VWRL.AS": "world",
    "IWDA.AS": "world", "IWDA.L": "world", "SWDA.L": "world", "EUNL.DE": "world",
}
WORLD_US_SHARE = 0.63
DUAL_CLASS_SECONDARY = {"GOOG", "FOX", "NWS"}
# Čitelné názvy ETF jádra (Yahoo vrací zkratky typu „ISHARES VII PLC ISHRS CORE S&P")
CORE_NAMES = {"SXR8.DE": "iShares Core S&P 500", "CSPX.L": "iShares Core S&P 500", "CSPX.AS": "iShares Core S&P 500",
              "VUAA.DE": "Vanguard S&P 500 (Acc)", "VUAA.L": "Vanguard S&P 500 (Acc)", "VUSA.L": "Vanguard S&P 500",
              "VUSA.AS": "Vanguard S&P 500", "VUSA.DE": "Vanguard S&P 500", "IUSA.DE": "iShares S&P 500 (Dist)",
              "SPY5.L": "SPDR S&P 500", "VWCE.DE": "Vanguard FTSE All-World (Acc)", "VWRA.L": "Vanguard FTSE All-World (Acc)",
              "VWRL.L": "Vanguard FTSE All-World", "VWRL.AS": "Vanguard FTSE All-World", "IWDA.AS": "iShares Core MSCI World",
              "IWDA.L": "iShares Core MSCI World", "SWDA.L": "iShares Core MSCI World", "EUNL.DE": "iShares Core MSCI World"}
FX_SYMBOLS = {"USD": "CZK=X", "EUR": "EURCZK=X", "GBP": "GBPCZK=X", "CHF": "CHFCZK=X", "PLN": "PLNCZK=X"}
US_EXCHANGES = {"NMS", "NYQ", "NGM", "NCM", "ASE", "PCX", "BTS", "NYS", "NAS"}


SECTOR_CZ = {
    "Information Technology": "Technologie", "Health Care": "Zdravotnictví", "Financials": "Finance",
    "Consumer Discretionary": "Zbytná spotřeba", "Consumer Staples": "Základní spotřeba",
    "Communication Services": "Komunikace a média", "Industrials": "Průmysl", "Energy": "Energetika",
    "Materials": "Suroviny", "Utilities": "Energetické sítě", "Real Estate": "Nemovitosti",
}


def _sector_cz(s):
    return SECTOR_CZ.get(s, s or "")


def _spx_key(t):
    """Yahoo ticker → klíč ve skenu S&P 500 (BRK-B → BRK.B)."""
    return (t or "").upper().replace("-", ".")


def _fx_czk(cur):
    """Kurz měny → CZK (kešováno hodinu). Londýnské ceny v pencích (GBp) /100."""
    if not cur or cur.upper() == "CZK":
        return 1.0
    pence = cur in ("GBp", "GBX")
    c = "GBP" if pence else cur.upper()
    sym = FX_SYMBOLS.get(c)
    if not sym:
        return None
    rate = None
    try:
        cached = kv_get_json(f"fx:{c}")
        if cached and time.time() - cached.get("ts", 0) < 3600:
            rate = cached["rate"]
    except Exception:
        pass
    if rate is None:
        try:
            meta = yahoo_chart(sym, "5d", "1d").get("meta") or {}
            rate = float(meta.get("regularMarketPrice"))
            try:
                kv_set_json(f"fx:{c}", {"ts": time.time(), "rate": rate})
            except Exception:
                pass
        except Exception:
            return None
    return rate / 100 if pence else rate


def _quotes(tickers):
    """Aktuální cena, měna, název a denní změna pro seznam tickerů (paralelně)."""
    def one(t):
        try:
            d = yahoo_chart(t, "5d", "1d")
            meta = d.get("meta") or {}
            cl = [c for c in (((d.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []) if c]
            price = meta.get("regularMarketPrice") or (cl[-1] if cl else None)
            prev = _prev_close(cl, meta)
            return t, {"price": price, "currency": meta.get("currency") or "USD",
                       "name": meta.get("shortName") or meta.get("longName") or t,
                       "type": meta.get("instrumentType") or "",
                       "chg": round((price - prev) / prev * 100, 2) if (price and prev) else None}
        except Exception:
            return t, None
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as ex:
        return dict(ex.map(one, [t for t in tickers if t]))


def _num(v):
    """Číslo z AI/uživatele: „1 234,56", „1,234.56", „12.5 ks" → float."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^\d,.\-]", "", str(v).replace(" ", "").replace(" ", ""))
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".") if len(s.split(",")[-1]) != 3 else s.replace(",", "")
    try:
        return float(s)
    except Exception:
        return None


def _pc(x, d=1):
    """Procento česky: 19,0 (desetinná čárka)."""
    return f"{x:.{d}f}".replace(".", ",")


def _kc(x):
    """Částka česky: 12 345 Kč (nezlomitelná mezera)."""
    return f"{x:,.0f}".replace(",", "\u00a0") + "\u00a0Kč"


def _clean_holdings(raw):
    """Validace nákupů (lotů): [{ticker, qty, buy_month?, name?}]. Stejný ticker
    a měsíc se sloučí; různé měsíce zůstávají zvlášť – 3letý časový test platí
    pro každý nákup zvlášť, sloučení by mohlo mylně hlásit „bez daně"."""
    merged = {}
    for h in (raw or [])[:600]:
        if not isinstance(h, dict):
            continue
        t = (h.get("ticker") or "").strip().upper()
        if not re.match(r"^[A-Z0-9.\-=^]{1,15}$", t):
            continue
        qty = _num(h.get("qty"))
        if qty is None or not (0 < qty < 1e9):
            continue
        bm = (h.get("buy_month") or "").strip()
        bm = bm if re.match(r"^\d{4}-(0[1-9]|1[0-2])$", bm) else None
        k = (t, bm)
        if k in merged:
            merged[k]["qty"] = round(merged[k]["qty"] + qty, 6)
        else:
            merged[k] = {"ticker": t, "qty": round(qty, 6), "buy_month": bm, "name": str(h.get("name") or "")[:80]}
    return list(merged.values())[:400]


def _tax_free_from(ym):
    y, m = map(int, ym.split("-"))
    m2 = y * 12 + (m - 1) + TAX_FREE_MONTHS
    return f"{m2 // 12}-{m2 % 12 + 1:02d}"


def _scan_maps(scan):
    """Pořadí, sektory, názvy a kapitalizace z posledního skenu. Starší sken
    (před 2026-10) pořadí neukládal – dopočítá se z vm_map."""
    vms = scan.get("vm_map") or {}
    ranks = scan.get("rank_map")
    if not ranks:
        ranks = {t: i for i, t in enumerate(sorted(vms, key=lambda t: -vms[t]), 1)}
    return ranks, vms, scan.get("sector_map") or {}, scan.get("name_map") or {}, scan.get("mcap_map") or {}


def _months_between(ym, today=None):
    today = today or datetime.now(timezone.utc).date()
    y, m = map(int, ym.split("-"))
    return (today.year - y) * 12 + (today.month - m)


def _value_holdings(holdings):
    """Ocenění v CZK po titulech (loty sečtené) + zařazení (jádro / akcie
    S&P 500 / ostatní), data modelu a 3letý časový test po jednotlivých nákupech."""
    lots = _clean_holdings(holdings)
    by_t = {}
    for l in lots:
        by_t.setdefault(l["ticker"], []).append(l)
    q = _quotes(list(by_t))
    scan = market_scan(allow_step=False) or {}
    ranks, vms, smap, _n, _m = _scan_maps(scan)
    rows, missing = [], []
    for t, ls in by_t.items():
        qt = q.get(t)
        fx = _fx_czk(qt["currency"]) if qt else None
        if not qt or not qt.get("price") or not fx:
            missing.append(t)
            continue
        key = _spx_key(t)
        kind = "core" if t in CORE_ETFS else ("stock" if key in ranks else "other")
        qty = round(sum(l["qty"] for l in ls), 6)
        px_czk = qt["price"] * fx
        free_q = sum(l["qty"] for l in ls if l["buy_month"] and _months_between(l["buy_month"]) >= TAX_FREE_MONTHS)
        unknown_q = sum(l["qty"] for l in ls if not l["buy_month"])
        pending = sorted(_tax_free_from(l["buy_month"]) for l in ls
                         if l["buy_month"] and _months_between(l["buy_month"]) < TAX_FREE_MONTHS)
        known = sorted(l["buy_month"] for l in ls if l["buy_month"])
        row = {"ticker": t, "qty": qty, "name": CORE_NAMES.get(t) or qt["name"] or ls[0].get("name") or t,
               "lots": [{"qty": l["qty"], "buy_month": l["buy_month"]} for l in sorted(ls, key=lambda l: l["buy_month"] or "")],
               "price": round(qt["price"], 4), "currency": qt["currency"], "fx": round(fx, 4),
               "value_czk": round(qty * px_czk, 2), "chg": qt.get("chg"), "kind": kind,
               "vm": vms.get(key), "vm_rank": ranks.get(key),
               "sector": smap.get(key) or ("ETF" if kind == "core" else ""),
               "tax_free_qty": round(free_q, 6), "tax_free_value_czk": round(free_q * px_czk),
               "unknown_qty": round(unknown_q, 6),
               "tax_free": unknown_q == 0 and free_q >= qty - 1e-9,
               "tax_next": pending[0] if pending else None,
               "tax_free_from": (pending[-1] if pending else (known[0] and _tax_free_from(known[0]))) if known else None,
               "months_held": _months_between(known[0]) if known else None}
        rows.append(row)
    total = sum(r["value_czk"] for r in rows)
    for r in rows:
        r["weight"] = round(r["value_czk"] / total * 100, 2) if total else 0
    rows.sort(key=lambda r: -r["value_czk"])
    return rows, total, missing, scan


# ---------------------------------------------------------------------------
# PAUZA U VÁŽNÉHO PRŮŠVIHU. Nový nákup titulu se na 30 dní odloží (peníze jdou
# dalšímu v pořadí), NIC se neprodává. Zdroje: SEC 8-K kódy (spolehlivé) a AI
# ze zpráv (podvod, vyšetřování, hrozba bankrotu). POCTIVĚ: na S&P 500 2015-26
# akcie po těchto podáních v průměru dál NEZAOSTÁVALY (po přepočtu účetnictví
# medián +4 % za 3 měsíce) – trh zprávu zpracuje hned. Je to pojistka proti
# vzácné katastrofě, ne zdroj výnosu (research/lt9b_eval.py).
# ---------------------------------------------------------------------------
REDFLAG_DAYS = 30
REDFLAG_PAUSE = {"4.02": "přepočet účetnictví – dřívější výkazy neplatí (SEC 8-K 4.02)",
                 "1.03": "bankrot / ochrana před věřiteli (SEC 8-K 1.03)",
                 "3.01": "hrozba vyřazení z burzy (SEC 8-K 3.01)"}
REDFLAG_WARN = {"4.01": "výměna auditora (SEC 8-K 4.01)"}


def _cik_map():
    """Ticker (formát skenu, BRK.B) → CIK z univerza S&P 500 (bez stahování 1 MB seznamu SEC)."""
    try:
        return {s: c for s, c, _x in _sp500_universe() if c}
    except Exception:
        return {}


def _sec_flags(ticker, cmap):
    """Vážná 8-K za posledních 30 dní (kešováno den)."""
    key = _spx_key(ticker)
    day = _today_iso()
    ck = f"secflag:{key}:{day}"
    try:
        c = kv_get_json(ck)
        if c is not None:
            return c.get("flags") or []
    except Exception:
        pass
    cik = cmap.get(key)
    if not cik:
        try:
            cik = int(_sec_lookup_cik(ticker) or 0) or None
        except Exception:
            cik = None
    flags = []
    if cik:
        try:
            r = requests.get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json", headers=SEC_HEADERS, timeout=8)
            rec = ((r.json() or {}).get("filings") or {}).get("recent") or {}
            cutoff = (datetime.now(timezone.utc).date() - timedelta(days=REDFLAG_DAYS)).isoformat()
            for f, d, it, acc, doc in zip(rec.get("form", []), rec.get("filingDate", []), rec.get("items", []),
                                          rec.get("accessionNumber", []), rec.get("primaryDocument", [])):
                if f not in ("8-K", "8-K/A") or d < cutoff:
                    continue
                for code in (it or "").split(","):
                    code = code.strip()
                    if code in REDFLAG_PAUSE or code in REDFLAG_WARN:
                        flags.append({"code": code, "date": d, "pause": code in REDFLAG_PAUSE,
                                      "reason": REDFLAG_PAUSE.get(code) or REDFLAG_WARN[code],
                                      "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{doc}"})
        except Exception:
            return []          # výpadek SEC nekešovat
    try:
        kv_set_json(ck, {"flags": flags})
    except Exception:
        pass
    return flags


def _red_flags(tickers):
    """{ticker: {"pause": bool, "reasons": [...], "until": "YYYY-MM-DD"}} – jen tituly s nějakým nálezem."""
    tickers = sorted({t for t in tickers if t})[:60]
    if not tickers:
        return {}
    cmap = _cik_map()
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=5) as ex:      # SEC povoluje max 10 dotazů/s
        sec = dict(zip(tickers, ex.map(lambda t: _sec_flags(t, cmap), tickers)))
    out = {}
    now = time.time()
    for t in tickers:
        items = list(sec.get(t) or [])
        try:
            ai = kv_get_json(f"redflag:ai:{_spx_key(t)}")
            if ai and ai.get("until_ts", 0) > now:
                items.append({"code": "ai", "date": ai.get("date"), "pause": True, "reason": ai.get("reason"),
                              "url": ai.get("url")})
        except Exception:
            pass
        if not items:
            continue
        pause = any(i["pause"] for i in items)
        last = max((i.get("date") or "") for i in items)
        until = None
        if pause and last:
            until = (datetime.strptime(last, "%Y-%m-%d").date() + timedelta(days=REDFLAG_DAYS)).isoformat()
        out[t] = {"pause": pause, "until": until, "reasons": items[:3]}
    return out


def _build_plan(rows, risk, scan):
    """Cílové portfolio podle ověřených pravidel (sloty, limit sektoru, vítěze
    nechat běžet). Každé pozici doplní stav a případnou akci (ořez/prodej)."""
    risk = _lt_risk(risk)
    split, N = LT_PROFILES[risk]
    ranks, vms, smap, names, _m = _scan_maps(scan)
    by_t = {r["ticker"]: r for r in (scan.get("value") or [])}
    total = sum(r["value_czk"] for r in rows)
    slot = split / N * total
    stocks = [r for r in rows if r["kind"] == "stock"]
    keep = sorted([r for r in stocks if r["vm_rank"] and r["vm_rank"] <= LT_BUFFER * N],
                  key=lambda r: r["vm_rank"])[:N]
    keep_t = {r["ticker"] for r in keep}
    held = {_spx_key(r["ticker"]) for r in rows}
    cnt = {}
    for r in keep:
        cnt[r.get("sector")] = cnt.get(r.get("sector"), 0) + 1
    capn = math.ceil(LT_SECTOR_CAP * N)
    free, new, skipped = N - len(keep), [], []
    # kandidáti (2N nejlepších nedržených) + držené tituly → jedna kontrola průšvihů
    cand = [t for t, rk in sorted(ranks.items(), key=lambda x: x[1]) if rk <= 2 * N and t not in held]
    try:
        flags = _red_flags([c.replace(".", "-") for c in cand] + [r["ticker"] for r in rows if r["kind"] != "core"])
    except Exception:
        flags = {}
    paused = []
    for t, rk in sorted(ranks.items(), key=lambda x: x[1]):
        if free <= 0 or rk > 2 * N:
            break
        if t in held:
            continue
        fl = flags.get(t.replace(".", "-"))
        if fl and fl["pause"]:
            paused.append({"ticker": t.replace(".", "-"), "name": names.get(t) or t, "vm_rank": rk, **fl})
            continue
        sec = smap.get(t) or ""
        if sec and cnt.get(sec, 0) >= capn:
            skipped.append({"ticker": t, "vm_rank": rk, "sector": sec})
            continue
        v = by_t.get(t) or {}
        new.append({"ticker": t.replace(".", "-"), "name": names.get(t) or v.get("name") or t, "vm": vms.get(t),
                    "vm_rank": rk, "sector": sec, "price": v.get("price"), "currency": v.get("currency"),
                    "pe": v.get("pe"), "mom_pct": v.get("mom_pct")})
        cnt[sec] = cnt.get(sec, 0) + 1
        free -= 1
    actions = []
    trim_cap = LT_TRIM_X * slot
    for r in rows:
        r["action"] = None
        fl = flags.get(r["ticker"])
        if fl:
            r["flag"] = fl
            if fl["pause"] and r["kind"] != "core" and r["ticker"] in keep_t:
                paused.append({"ticker": r["ticker"], "name": r["name"], "vm_rank": r["vm_rank"], "held": True, **fl})
        if r["kind"] == "core":
            r["status"], r["status_label"] = "core", "Indexové jádro"
            continue
        if r["ticker"] in keep_t:
            r["status"], r["status_label"] = "plan", f"V plánu · #{r['vm_rank']} v S&P 500"
        elif r["kind"] == "stock":
            r["status"] = "hold"
            r["status_label"] = "Mimo plán · držet, nedokupovat"
        else:
            r["status"], r["status_label"] = "other", "Mimo S&P 500 · držet, nedokupovat"
        free_v = r["tax_free_value_czk"]
        if slot and r["value_czk"] > trim_cap:
            excess = r["value_czk"] - trim_cap
            cap_txt = f"strop je {_pc(LT_TRIM_X * split / N * 100)} % = 3× cílová váha"
            if free_v >= excess * 0.999:
                r["action"] = {"type": "trim", "amount_czk": round(excess),
                               "why": f"Pozice má {_pc(r['weight'])} %, {cap_txt}. Prodej kusů držených přes "
                                      "3 roky je bez daně; ořez snižuje riziko jedné firmy, peníze jdou do plánu."}
            elif free_v >= MIN_ORDER_CZK:
                r["action"] = {"type": "trim", "amount_czk": round(free_v), "partial": True,
                               "why": f"Pozice má {_pc(r['weight'])} %, {cap_txt}. Bez daně jde teď prodat jen "
                                      f"kusy starší 3 let (≈ {_kc(free_v)})"
                                      + (f", další od {r['tax_next']}." if r.get("tax_next") else ".")}
            elif r["unknown_qty"] > 0:
                r["action"] = {"type": "trim_check", "amount_czk": round(excess),
                               "why": f"Pozice má {_pc(r['weight'])} %, {cap_txt}. Doplň měsíc nákupu – ořez dává "
                                      "smysl až u kusů držených přes 3 roky, kdy je zisk v ČR bez daně."}
            else:
                r["action"] = {"type": "trim_wait", "amount_czk": round(excess),
                               "why": f"Pozice má {_pc(r['weight'])} %, {cap_txt}. Nejbližší kusy ale budou bez daně až "
                                      f"{r.get('tax_next')}. Do té doby jen nedokupovat."}
        elif r["status"] == "hold" and slot and r["value_czk"] < LT_CLEANUP_X * slot:
            if r["tax_free"]:
                r["action"] = {"type": "sell", "amount_czk": round(r["value_czk"]),
                               "why": "Mimo plán a po 3 letech pořád malá (pod ½ cílové váhy) – titul "
                                      "nevyšel. Prodej je bez daně, peníze jdou do plánu."}
            elif r["unknown_qty"] == 0 and r.get("tax_free_from"):
                r["action"] = {"type": "sell_wait",
                               "why": f"Malá pozice mimo plán. Úklid až od {r['tax_free_from']}, kdy je bez daně."}
        if r["action"] and r["action"]["type"] in ("trim", "sell"):
            actions.append({"ticker": r["ticker"], "name": r["name"], **r["action"]})
    p = LONGTERM["profiles"][risk]
    return {"risk": risk, "stocks_pct": int(split * 100), "core_pct": int(round((1 - split) * 100)),
            "slots": N, "slots_used": len(keep), "slot_pct": round(split / N * 100, 2), "new": new,
            "sector_skipped": skipped[:5], "actions": actions, "paused": paused,
            "validation": {k: p[k] for k in ("train", "holdout", "full")},
            "benchmark": LONGTERM["benchmark"], "rule": LONGTERM["rule"]}


def _allocate(amount, rows, total, plan):
    """„Kam s penězi": vklad se rozdělí úměrně mezerám vůči cíli (ověřené
    pravidlo); co mezery převýší, jde podle cílových vah. Drobné nákupy pod
    MIN_ORDER_CZK se nedělají – peníze určené akciím ale zůstávají akciím:
    soustředí se do titulů s největší mezerou (při shodě lepší pořadí) a
    ostatní přijdou na řadu příští měsíc. Jinak by malé měsíční vklady
    končily vždy jen v ETF a akciová část by nikdy nevznikla."""
    split, N = LT_PROFILES[plan["risk"]]
    V = total + amount
    core_rows = [r for r in rows if r["kind"] == "core"]
    core_val = sum(r["value_czk"] for r in core_rows)
    cv = (max(core_rows, key=lambda r: r["value_czk"]) if core_rows else
          {"ticker": CORE_ETF["ticker"], "name": CORE_ETF["name"]})
    core = {"ticker": cv["ticker"], "name": cv.get("name"), "action": "core", "target": (1 - split) * V,
            "cur": core_val, "vm_rank": 0, "price": cv.get("price"), "currency": cv.get("currency")}
    # Pozice mimo plán (starší akcie, jiné ETF) zabírají akciový rozpočet profilu –
    # jinak by vklady dál zvyšovaly podíl akcií nad zvolené riziko. Ověřeno: výnos
    # stejný (±0,5 b.), ale profil rizika platí i pro portfolio s historií.
    legacy = sum(r["value_czk"] for r in rows if r.get("status") not in ("core", "plan"))
    per_slot = max(0.0, split * V - legacy) / N
    stocks = []
    for r in rows:
        if r.get("status") == "plan" and not (r.get("flag") or {}).get("pause"):
            stocks.append({"ticker": r["ticker"], "name": r["name"], "action": "topup", "target": per_slot,
                           "cur": r["value_czk"], "vm_rank": r["vm_rank"], "sector": r.get("sector"),
                           "price": r["price"], "currency": r["currency"]})
    for n in plan["new"]:
        stocks.append({"ticker": n["ticker"], "name": n["name"], "action": "new", "target": per_slot,
                       "cur": 0.0, "vm_rank": n["vm_rank"], "sector": n.get("sector"),
                       "price": n.get("price"), "currency": n.get("currency"),
                       "pe": n.get("pe"), "mom_pct": n.get("mom_pct")})
    lines = [core] + stocks
    for l in lines:
        l["gap"] = max(0.0, l["target"] - l["cur"])
        l["amount"] = 0.0
    need = sum(l["gap"] for l in lines)
    f = min(1.0, amount / need) if need > 0 else 0.0
    for l in lines:
        l["amount"] = l["gap"] * f
    rest = amount - sum(l["amount"] for l in lines)
    if rest > 0.5:
        tw = sum(l["target"] for l in lines) or 1
        for l in lines:
            l["amount"] += rest * l["target"] / tw
    pool = sum(l["amount"] for l in stocks)
    if pool < MIN_ORDER_CZK:                    # na akcie letos moc málo → celé do jádra
        core["amount"] += pool
        for l in stocks:
            l["deferred"] = l["amount"] > 0
            l["amount"] = 0.0
    else:
        while True:
            pos = [l for l in stocks if l["amount"] > 0]
            small = [l for l in pos if l["amount"] < MIN_ORDER_CZK]
            if not small or len(pos) <= 1:
                break
            drop = min(pos, key=lambda l: (l["gap"], -l["vm_rank"]))
            moved, drop["amount"], drop["deferred"] = drop["amount"], 0.0, True
            keep = [l for l in pos if l is not drop]
            base = sum(l["gap"] for l in keep) or 1
            for l in keep:
                l["amount"] += moved * l["gap"] / base
    if 0 < core["amount"] < MIN_ORDER_CZK and any(l["amount"] > 0 for l in stocks):
        tgt = max(stocks, key=lambda l: l["amount"])
        tgt["amount"] += core["amount"]
        core["amount"] = 0.0
    need_q = [l["ticker"] for l in lines if l["amount"] > 0 and not l.get("price")]
    q = _quotes(need_q) if need_q else {}
    out = []
    for l in sorted(lines, key=lambda l: (-l["amount"], l["vm_rank"])):
        if l["amount"] <= 0:
            continue
        qt = q.get(l["ticker"]) or {}
        price = l.get("price") or qt.get("price")
        cur = l.get("currency") or qt.get("currency")
        if qt.get("name") and l["name"] == l["ticker"]:
            l["name"] = qt["name"]
        fx = _fx_czk(cur) if (price and cur) else None
        px_czk = price * fx if (price and fx) else None
        now_pct = l["cur"] / total * 100 if total else 0
        after_pct = l["cur"] / V * 100 if V else 0      # podíl po vkladu, kdyby se nedokoupilo
        if l["action"] == "core":
            why = (f"Indexové jádro (S&P 500): cíl {plan['core_pct']} % portfolia. Teď má {_pc(now_pct, 0)} %, "
                   f"s novým vkladem by bez dokoupení kleslo na {_pc(after_pct, 0)} %."
                   if core_rows else
                   f"Základ portfolia: 500 největších firem USA v jednom ETF, cíl {plan['core_pct']} %. "
                   "Irská UCITS verze – z ČR koupitelná, dividendy reinvestuje sama.")
        elif l["action"] == "topup":
            why = (f"Držíš a pořád je v plánu (#{l['vm_rank']} v S&P 500 podle hodnoty+momenta). "
                   f"S novým vkladem by měla {_pc(after_pct)} %, cíl je {_pc(per_slot / V * 100)} % – dokoupení ji vrací k cíli.")
        else:
            facts = ([f"P/E {_pc(l['pe'], 0)}"] if l.get("pe") else []) + \
                    ([f"za rok {'+' if l['mom_pct'] >= 0 else ''}{_pc(l['mom_pct'], 0)} %"] if l.get("mom_pct") is not None else [])
            why = (f"Nový titul do volného místa: #{l['vm_rank']} v S&P 500 podle hodnoty+momenta"
                   + (f" ({_sector_cz(l['sector'])})" if l.get("sector") else "")
                   + (" · " + " · ".join(facts) if facts else "") + ".")
        out.append({"ticker": l["ticker"], "name": l.get("name"), "action": l["action"],
                    "amount_czk": int(round(l["amount"])),
                    "shares": round(l["amount"] / px_czk, 4) if px_czk else None,
                    "price": round(price, 2) if price else None, "currency": cur, "why": why,
                    "sector": l.get("sector"), "vm_rank": l["vm_rank"] or None})
    diff = int(round(amount)) - sum(o["amount_czk"] for o in out)
    if out and diff:
        out[0]["amount_czk"] += diff
    deferred = [{"ticker": l["ticker"], "name": l.get("name"), "action": l["action"], "vm_rank": l["vm_rank"]}
                for l in stocks if l.get("deferred")]
    stock_now = sum(r["value_czk"] for r in rows if r.get("status") != "core")
    return {"amount_czk": int(round(amount)), "lines": out, "deferred": deferred, "min_order": MIN_ORDER_CZK,
            "stocks_full": per_slot <= 0, "stocks_now_pct": round(stock_now / total * 100, 1) if total else 0,
            "legacy_pct": round(legacy / total * 100, 1) if total else 0,
            "split": {"core": sum(o["amount_czk"] for o in out if o["action"] == "core"),
                      "topup": sum(o["amount_czk"] for o in out if o["action"] == "topup"),
                      "new": sum(o["amount_czk"] for o in out if o["action"] == "new")}}


def _xray(rows, total, scan):
    """Rentgen: co skutečně držíš včetně obsahu ETF. Váhy S&P 500 z tržních
    kapitalizací posledního skenu (SEC počty akcií × cena), celosvětový ETF ≈
    63 % USA. Přibližné, ale ukáže skrytou koncentraci (např. Apple napřímo i v ETF)."""
    if not total:
        return None
    _r, _v, smap, names, mcap = _scan_maps(scan)
    names = dict(names)
    # Dvě třídy akcií jedné firmy mají v SEC stejný počet akcií (celá firma) –
    # druhou třídu vynechat, jinak by se Alphabet & spol. započítaly dvakrát.
    mcap = {t: v for t, v in mcap.items() if t not in DUAL_CLASS_SECONDARY}
    tot_mcap = sum(v for v in mcap.values() if v) or 1
    sp_val = sum(r["value_czk"] for r in rows if CORE_ETFS.get(r["ticker"]) == "sp500")
    w_val = sum(r["value_czk"] for r in rows if CORE_ETFS.get(r["ticker"]) == "world")
    via_etf = sp_val + w_val * WORLD_US_SHARE
    eff, sect = {}, {}
    for t, mc in mcap.items():
        if not mc:
            continue
        v = via_etf * mc / tot_mcap
        eff[t] = eff.get(t, 0) + v
        sect[smap.get(t) or "?"] = sect.get(smap.get(t) or "?", 0) + v
    for r in rows:
        if r["kind"] == "core":
            continue
        k = _spx_key(r["ticker"])
        eff[k] = eff.get(k, 0) + r["value_czk"]
        s = r.get("sector") or "Ostatní"
        sect[s] = sect.get(s, 0) + r["value_czk"]
        names.setdefault(k, r["name"])
    non_us = w_val * (1 - WORLD_US_SHARE)
    if non_us:
        sect["Mimo USA (přes celosvětový ETF)"] = non_us
    # GOOG + GOOGL je jedna firma
    if "GOOG" in eff and "GOOGL" in eff:
        eff["GOOGL"] += eff.pop("GOOG")
    top = sorted(eff.items(), key=lambda x: -x[1])[:10]
    us = via_etf + sum(r["value_czk"] for r in rows if r["kind"] == "stock")
    sectors = [{"sector": _sector_cz(s), "pct": round(v / total * 100, 1)}
               for s, v in sorted(sect.items(), key=lambda x: -x[1]) if v / total >= 0.005]
    warnings = []
    for t, v in top[:3]:
        pct = v / total * 100
        if pct >= 10:
            direct = next((r["value_czk"] for r in rows if _spx_key(r["ticker"]) == t), 0)
            hidden = v - direct
            warnings.append(f"{names.get(t) or t} tvoří ve skutečnosti {pct:.0f} % portfolia"
                            + (f" (z toho {hidden / total * 100:.0f} % skrytě přes ETF)" if direct and hidden / total >= 0.01 else "")
                            + ". Jeden špatný rok této firmy by bolel celé portfolio.")
    for x in sectors[:2]:
        if x["pct"] >= 35 and x["sector"] not in ("Ostatní", "?"):
            warnings.append(f"Sektor {x['sector']} je {x['pct']:.0f} % portfolia (v S&P 500 bývá největší "
                            "sektor kolem 30 %). Nové vklady ho postupně ředí.")
    return {
        "top": [{"ticker": t, "name": names.get(t) or t, "value_czk": round(v),
                 "pct": round(v / total * 100, 2), "direct": any(_spx_key(r["ticker"]) == t for r in rows)}
                for t, v in top],
        "sectors": sectors, "warnings": warnings,
        "us_pct": round(us / total * 100, 1),
        "approx": bool(w_val),
    }


@app.route("/api/holdings", methods=["GET", "POST"])
def api_holdings():
    """Uložené pozice přihlášeného uživatele (samostatný klíč – ukládání
    watchlistu je nemůže přepsat)."""
    user = _auth_user()
    if not user:
        return jsonify({"ok": False, "error": "Nepřihlášeno."}), 401
    try:
        if request.method == "POST":
            b = request.get_json(silent=True) or {}
            hs = _clean_holdings(b.get("holdings"))
            rec = {"updated": int(time.time()), "holdings": hs}
            if b.get("risk") in LT_PROFILES:
                rec["risk"] = b["risk"]
            else:
                old = (kv_get_json(f"holdings:{user}") or {}).get("risk")
                if old:
                    rec["risk"] = old
            kv_set_json(f"holdings:{user}", rec)
            return jsonify({"ok": True, "count": len(hs)})
        rec = kv_get_json(f"holdings:{user}") or {}
        return jsonify({"ok": True, "holdings": rec.get("holdings") or [], "updated": rec.get("updated"),
                        "risk": rec.get("risk")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/plan", methods=["POST"])
def api_plan():
    """Celý dlouhodobý plán z pozic: ocenění, cíl, stavy pozic, rentgen a
    (když je zadaná částka) rozdělení vkladu. Bezstavové – pozice v těle."""
    if not _rate_ok("plan", 60, 60):
        return jsonify({"ok": False, "error": "Moc požadavků, zkus to za chvíli."}), 429
    body = request.get_json(silent=True) or {}
    holdings = body.get("holdings")
    user = _auth_user()
    if holdings is None and user:
        holdings = (kv_get_json(f"holdings:{user}") or {}).get("holdings") or []
    saved = (kv_get_json(f"holdings:{user}") or {}).get("risk") if user else None
    risk = _lt_risk(body.get("risk") or saved or (_user_risk(user) if user else None))
    try:
        rows, total, missing, scan = _value_holdings(holdings or [])
        plan = _build_plan(rows, risk, scan)
        out = {"ok": True, "rows": rows, "total_czk": round(total), "missing": missing, "plan": plan,
               "xray": _xray(rows, total, scan), "scan_date": scan.get("date")}
        amount = _num(body.get("amount_czk"))
        if amount and 0 < amount < 1e10:
            out["deposit"] = _allocate(amount, rows, total, plan)
        return jsonify(out)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# IMPORT POZIC Z FOTKY (Trading 212 screenshot). Obrázek přečte vizuální AI
# model, tickery se pak ověří proti burze: pro každého kandidáta se spočítá
# počet × cena × kurz a vybere se ten, který sedí na hodnotu ze screenshotu
# (rozliší třeba CSPX v Londýně v USD od SXR8 na Xetře v EUR). Fotka se
# nikam neukládá. Model: env VISION_MODEL (jinak výchozí podle poskytovatele).
# ---------------------------------------------------------------------------
VISION_MODELS = {"groq": ["meta-llama/llama-4-scout-17b-16e-instruct",
                          "meta-llama/llama-4-maverick-17b-128e-instruct"],
                 "openai": ["gpt-4o-mini"]}
IMPORT_PROMPT = """Na obrázku je přehled investičního portfolia (nejčastěji aplikace Trading 212, může být česky nebo anglicky).
Vypiš KAŽDOU pozici, která je na obrázku vidět. Nic si nedomýšlej – co nevidíš, dej null.
Pro každou pozici:
- "name": název tak, jak je napsaný (např. "Apple", "iShares Core S&P 500 (Acc)")
- "ticker": symbol, pokud je vidět (např. "AAPL", "CSPX", "SXR8"), jinak null
- "quantity": počet kusů/akcií jako číslo (zlomky jsou běžné, např. 12.3456), jinak null
- "value": aktuální hodnota pozice jako číslo, jinak null
- "currency": měna hodnoty ("CZK", "EUR", "USD", "GBP"), jinak null
- "avg_price": průměrná nákupní cena, pokud je vidět, jinak null
Čísla piš bez mezer a jednotek, desetinnou tečkou. Nezahrnuj celkový součet účtu, hotovost ani zisk/ztrátu jako pozici.
Odpověz JEN JSON: {"positions": [...], "account_currency": "CZK"}"""
ETF_SUFFIXES = (".DE", ".L", ".AS", ".MI", ".PA", ".SW")


def call_vision(image_data_url, prompt=IMPORT_PROMPT):
    """Vrací (dict | None, chyba). Zkusí modely poskytovatele po řadě."""
    prov = (os.environ.get("VISION_PROVIDER") or _llm_creds()[0]).lower()
    key = os.environ.get("VISION_API_KEY") or _llm_creds()[1]
    if not key:
        return None, "Chybí API klíč pro AI (ANALYSIS_API_KEY)."
    url = ("https://api.openai.com/v1/chat/completions" if prov == "openai"
           else "https://api.groq.com/openai/v1/chat/completions")
    models = ([os.environ["VISION_MODEL"]] if os.environ.get("VISION_MODEL")
              else VISION_MODELS.get(prov, VISION_MODELS["groq"]))
    err = None
    for model in models:
        payload = {"model": model, "temperature": 0, "response_format": {"type": "json_object"},
                   "messages": [{"role": "user", "content": [
                       {"type": "text", "text": prompt},
                       {"type": "image_url", "image_url": {"url": image_data_url}}]}]}
        try:
            r = requests.post(url, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                              data=json.dumps(payload), timeout=25)
            if r.status_code != 200:
                err = f"{model}: HTTP {r.status_code} {r.text[:160]}"
                continue
            content = (((r.json() or {}).get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            m = re.search(r"\{.*\}", content, re.S)
            return json.loads(m.group(0) if m else content), None
        except Exception as e:
            err = f"{model}: {e}"
    return None, err


def _yahoo_search(q, n=8):
    try:
        r = requests.get(f"https://query2.finance.yahoo.com/v1/finance/search?q={quote(q)}"
                         f"&quotesCount={n}&newsCount=0&listsCount=0", headers=HEADERS, timeout=8)
        return [{"ticker": x["symbol"], "name": x.get("shortname") or x.get("longname") or x["symbol"],
                 "exchange": x.get("exchDisp") or x.get("exchange") or "", "type": x.get("quoteType") or "",
                 "us": x.get("exchange") in US_EXCHANGES}
                for x in (r.json().get("quotes") or [])
                if x.get("symbol") and x.get("quoteType") in ("EQUITY", "ETF", "MUTUALFUND")]
    except Exception:
        return []


def _resolve_position(p):
    """Jedna pozice z AI → ověřený ticker + počet kusů. Vrací dict pro kontrolu."""
    name = (str(p.get("name") or "")).strip()[:80]
    raw_t = re.sub(r"[^A-Za-z0-9.\-]", "", str(p.get("ticker") or "")).upper()[:12]
    qty, value = _num(p.get("quantity")), _num(p.get("value"))
    vcur = (p.get("currency") or "CZK").upper()
    if vcur not in FX_SYMBOLS and vcur != "CZK":
        vcur = "CZK"
    value_czk = value * _fx_czk(vcur) if (value and _fx_czk(vcur)) else None
    cands, seen = [], set()

    def add(ticker, name="", exchange="", type="", us=False):
        t = (ticker or "").upper()
        if t and t not in seen and len(cands) < 10:
            seen.add(t)
            cands.append({"ticker": t, "name": name, "exchange": exchange, "type": type, "us": us})
    if raw_t:
        for x in _yahoo_search(raw_t):
            if x["ticker"].split(".")[0] == raw_t.split(".")[0]:
                add(**x)
        add(raw_t)
        if "." not in raw_t:
            for suf in ETF_SUFFIXES[:3]:
                add(raw_t + suf)
    if name and len(cands) < 6:
        for x in _yahoo_search(name, 6)[:5]:
            add(**x)
    q = _quotes([c["ticker"] for c in cands])
    scored = []
    for c in cands:
        qt = q.get(c["ticker"])
        if not qt or not qt.get("price"):
            continue
        fx = _fx_czk(qt["currency"])
        if not fx:
            continue
        px_czk = qt["price"] * fx
        c.update(price=round(qt["price"], 4), currency=qt["currency"], name=c["name"] or qt["name"],
                 px_czk=px_czk, type=c["type"] or qt.get("type") or "")
        if qty and value_czk:
            c["err"] = abs(qty * px_czk - value_czk) / value_czk
        scored.append(c)
    if not scored:
        return {"name": name or raw_t, "raw_ticker": raw_t, "ticker": None, "qty": qty,
                "value_czk": round(value_czk) if value_czk else None, "status": "unresolved", "candidates": []}
    base = raw_t.split(".")[0] if raw_t else ""

    def pref(c):
        # Tatáž akcie na víc burzách má v Kč skoro stejnou hodnotu → vezmi
        # „domovskou" kótaci: US (model zná S&P 500 pod US tickerem), přesný
        # ticker ze screenshotu, Xetra/Londýn, a až pak regionální burzy.
        t = c["ticker"]
        return (0 if (c["us"] and c["type"] == "EQUITY") else 1,
                0 if (base and t.split(".")[0] == base) else 1,
                0 if t in CORE_ETFS else 1,
                0 if ("." not in t or t.endswith((".DE", ".L"))) else 1)
    if qty and value_czk:
        top_err = min(c["err"] for c in scored)
        best = min([c for c in scored if c["err"] <= top_err + 0.03], key=pref)
        status = "ok" if best["err"] <= 0.15 else "check"
    else:
        exact = [c for c in scored if c["ticker"].split(".")[0] == base] if base else []
        best = min(exact or scored, key=pref)
        status = "ok" if exact else "check"
    if not qty and value_czk and best.get("px_czk"):
        qty = round(value_czk / best["px_czk"], 6)
        derived = True
    else:
        derived = False
    return {
        "name": name or best["name"], "raw_ticker": raw_t or None, "ticker": best["ticker"],
        "qty": qty, "qty_derived": derived, "value_czk": round(value_czk) if value_czk else None,
        "implied_czk": round(qty * best["px_czk"]) if qty else None, "status": status if qty else "check",
        "avg_price": _num(p.get("avg_price")),
        "candidates": [{"ticker": c["ticker"], "name": c["name"], "exchange": c["exchange"],
                        "price": c["price"], "currency": c["currency"],
                        "implied_czk": round(qty * c["px_czk"]) if qty else None}
                       for c in sorted(scored, key=lambda c: (c is not best, c.get("err", 9)))[:5]],
    }


@app.route("/api/holdings/import", methods=["POST"])
def api_holdings_import():
    """Jeden screenshot (data URL, klient ho zmenší) → pozice ke kontrole.
    Klient posílá každou fotku zvlášť a výsledky slučuje (překryv při
    scrollování řeší deduplikace podle tickeru)."""
    if not _rate_ok("import", 30, 3600):
        return jsonify({"ok": False, "error": "Moc importů za hodinu, zkus to později."}), 429
    img = (request.get_json(silent=True) or {}).get("image") or ""
    if not re.match(r"^data:image/(jpeg|png|webp);base64,", img) or len(img) > 4_000_000:
        return jsonify({"ok": False, "error": "Pošli obrázek JPG/PNG (max ~3 MB)."}), 400
    data, err = call_vision(img)
    if data is None:
        return jsonify({"ok": False, "error": "Fotku se nepodařilo přečíst.", "detail": err}), 502
    raw = [p for p in (data.get("positions") or []) if isinstance(p, dict)][:60]
    from concurrent.futures import ThreadPoolExecutor
    def safe(p):
        try:
            return _resolve_position(p)
        except Exception:
            return {"name": str(p.get("name") or p.get("ticker") or "?")[:80], "ticker": None,
                    "qty": _num(p.get("quantity")), "status": "unresolved", "candidates": []}
    with ThreadPoolExecutor(max_workers=6) as ex:
        res = list(ex.map(safe, raw))
    return jsonify({"ok": True, "positions": res, "read": len(raw),
                    "account_currency": data.get("account_currency")})


@app.route("/api/holdings/resolve")
def api_holdings_resolve():
    """Ruční oprava v kontrole importu: ticker/název → kandidáti s cenou."""
    q = (request.args.get("q") or "").strip()[:60]
    if not q:
        return jsonify({"ok": True, "candidates": []})
    res = _resolve_position({"ticker": q if re.match(r"^[A-Za-z0-9.\-]{1,12}$", q) else None, "name": q,
                             "quantity": request.args.get("qty"), "value": request.args.get("value"),
                             "currency": "CZK"})
    return jsonify({"ok": True, **res})


# ---------------------------------------------------------------------------
# STRES TEST V KČ a CESTA K CÍLI
# ---------------------------------------------------------------------------
STRESS_SCENARIOS = [
    {"id": "gfc", "label": "Finanční krize 2008", "start": "2007-10-09", "end": "2009-03-09"},
    {"id": "covid", "label": "Covid krach 2020", "start": "2020-02-19", "end": "2020-03-23"},
    {"id": "bear22", "label": "Inflace a sazby 2022", "start": "2022-01-03", "end": "2022-10-12"},
    {"id": "tariff25", "label": "Clový šok 2025", "start": "2025-02-19", "end": "2025-04-08"},
]
STRESS_PROXY = "^GSPC"


def _weekly(ticker):
    """{date_iso: close} týdně za 20 let (kešováno den)."""
    key = f"wk:{ticker}"
    try:
        c = kv_get_json(key)
        if c and time.time() - c.get("ts", 0) < 86400:
            return c["px"]
    except Exception:
        pass
    try:
        d = yahoo_chart(ticker, "20y", "1wk")
        cl = ((d.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        px = {datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d"): c
              for t, c in zip(d.get("timestamp") or [], cl) if c}
        try:
            kv_set_json(key, {"ts": time.time(), "px": px})
        except Exception:
            pass
        return px
    except Exception:
        return {}


def _px_at(px, day, before=True):
    """Cena nejblíž danému dni (týdenní svíčky): poslední ≤ den, jinak první ≥ den."""
    ds = sorted(px)
    if not ds or day < ds[0]:
        return None
    best = None
    for d in ds:
        if d <= day:
            best = d
        else:
            if best is None or not before:
                best = d
            break
    return px.get(best) if best else None


def _stress(rows):
    """Kolik by TVOJE dnešní portfolio ztratilo v Kč v minulých krizích.
    Pozice bez historie (např. novější ETF nebo firma) se počítají přes
    S&P 500 a jsou označené. Kurz koruny je započítaný – v krizích dolar
    obvykle posílí, což českému investorovi část ztráty vrátí."""
    tickers = sorted({r["ticker"] for r in rows} | {STRESS_PROXY, "CZK=X", "EURCZK=X", "GBPCZK=X"})
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as ex:
        hist = dict(zip(tickers, ex.map(_weekly, tickers)))
    fxsym = {"USD": "CZK=X", "EUR": "EURCZK=X", "GBP": "GBPCZK=X", "GBp": "GBPCZK=X", "GBX": "GBPCZK=X"}
    total = sum(r["value_czk"] for r in rows)
    out = []
    spx = hist.get(STRESS_PROXY) or {}
    for sc in STRESS_SCENARIOS:
        a, b = sc["start"], sc["end"]
        p0, p1 = _px_at(spx, a), _px_at(spx, b)
        spx_ret = (p1 / p0 - 1) if (p0 and p1) else None
        usd = hist.get("CZK=X") or {}
        u0, u1 = _px_at(usd, a), _px_at(usd, b)
        usd_ret = (u1 / u0 - 1) if (u0 and u1) else 0.0
        loss, proxied, worst = 0.0, [], []
        for r in rows:
            h = hist.get(r["ticker"]) or {}
            q0, q1 = _px_at(h, a), _px_at(h, b)
            if q0 and q1 and min(h) <= a:
                ret, cur = q1 / q0 - 1, r["currency"]
            else:                                   # bez historie → S&P 500 v USD
                ret, cur = (spx_ret if spx_ret is not None else 0.0), "USD"
                proxied.append(r["ticker"])
            fs = fxsym.get(cur)
            if fs:
                fh = hist.get(fs) or {}
                f0, f1 = _px_at(fh, a), _px_at(fh, b)
                fx_ret = (f1 / f0 - 1) if (f0 and f1) else (usd_ret if fs == "CZK=X" else 0.0)
            else:
                fx_ret = 0.0
            r_czk = (1 + ret) * (1 + fx_ret) - 1
            loss += r["value_czk"] * r_czk
            worst.append((r["ticker"], r_czk))
        # zotavení S&P 500: kdy index znovu překonal vrchol před krizí
        rec = None
        if p0:
            for d in sorted(spx):
                if d > b and spx[d] >= p0:
                    rec = d
                    break
        months = None
        if rec:
            y0, m0 = int(b[:4]), int(b[5:7])
            y1, m1 = int(rec[:4]), int(rec[5:7])
            months = (y1 - y0) * 12 + (m1 - m0)
        worst.sort(key=lambda x: x[1])
        out.append({**sc, "loss_czk": round(loss), "loss_pct": round(loss / total * 100, 1) if total else None,
                    "spx_pct": round(spx_ret * 100, 1) if spx_ret is not None else None,
                    "usd_czk_pct": round(usd_ret * 100, 1), "recovery_months": months,
                    "proxied": proxied, "proxied_pct": round(sum(r["value_czk"] for r in rows if r["ticker"] in proxied)
                                                             / total * 100) if total else 0,
                    "worst": [{"ticker": t, "pct": round(x * 100, 1)} for t, x in worst[:3]]})
    return out


@app.route("/api/plan/stress", methods=["POST"])
def api_plan_stress():
    if not _rate_ok("stress", 20, 60):
        return jsonify({"ok": False, "error": "Moc požadavků, zkus to za chvíli."}), 429
    body = request.get_json(silent=True) or {}
    holdings = body.get("holdings")
    user = _auth_user()
    if holdings is None and user:
        holdings = (kv_get_json(f"holdings:{user}") or {}).get("holdings") or []
    try:
        hs = _clean_holdings(holdings or [])
        q = _quotes([h["ticker"] for h in hs])
        rows = []
        for h in hs:
            qt = q.get(h["ticker"])
            fx = _fx_czk(qt["currency"]) if qt else None
            if qt and qt.get("price") and fx:
                rows.append({"ticker": h["ticker"], "currency": qt["currency"],
                             "value_czk": h["qty"] * qt["price"] * fx})
        if not rows:
            return jsonify({"ok": True, "scenarios": [], "total_czk": 0})
        return jsonify({"ok": True, "scenarios": _stress(rows),
                        "total_czk": round(sum(r["value_czk"] for r in rows))})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _sp500tr_monthly():
    """Měsíční výnosy S&P 500 vč. dividend od 1988 (kešováno týden)."""
    try:
        c = kv_get_json("hist:sp500tr")
        if c and time.time() - c.get("ts", 0) < 7 * 86400 and len(c.get("r") or []) > 300:
            return c["r"]
    except Exception:
        pass
    d = yahoo_chart("^SP500TR", "40y", "1mo")
    cl = [c for c in (((d.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []) if c]
    rets = [cl[i] / cl[i - 1] - 1 for i in range(1, len(cl))]
    try:
        kv_set_json("hist:sp500tr", {"ts": time.time(), "r": [round(x, 6) for x in rets]})
    except Exception:
        pass
    return rets


def _goal_sim(start, monthly, years, goal, n=3000, block=12, cost=0.002):
    """Cesta k cíli: blokový bootstrap měsíčních výnosů S&P 500 (12měsíční
    bloky drží krize pohromadě). Záměrně BEZ předpokladu, že model trh porazí.
    Konečná hodnota je lineární ve vkladu (V = start·G + měsíčně·A), takže
    z jedné simulace vyjde i to, kolik měsíčně je potřeba pro 80% šanci."""
    import random
    rets = _sp500tr_monthly()
    M = int(years * 12)
    rnd = random.Random(f"{start}|{monthly}|{years}|{goal}")
    fee = cost / 12
    Gs, As, fans = [], [], [[] for _ in range(int(years) + 1)]
    L = len(rets)
    for _ in range(n):
        G, A = 1.0, 0.0
        path = []
        while len(path) < M:
            i = rnd.randrange(0, L - block)
            path.extend(rets[i:i + block])
        for k in range(M):
            g = (1 + path[k]) * (1 - fee)
            A = (A + 1.0) * g
            G *= g
            if (k + 1) % 12 == 0:
                fans[(k + 1) // 12].append(start * G + monthly * A)
        Gs.append(G)
        As.append(A)
    finals = sorted(start * G + monthly * A for G, A in zip(Gs, As))

    def pct(arr, p):
        return arr[min(len(arr) - 1, int(p * len(arr)))]
    fan = [{"year": 0, "p10": start, "p50": start, "p90": start}]
    for y in range(1, int(years) + 1):
        f = sorted(fans[y])
        fan.append({"year": y, "p10": round(pct(f, .10)), "p50": round(pct(f, .50)), "p90": round(pct(f, .90))})
    out = {"p10": round(pct(finals, .10)), "p50": round(pct(finals, .50)), "p90": round(pct(finals, .90)),
           "contributed": round(start + monthly * M), "fan": fan, "paths": n,
           "history": f"S&P 500 vč. dividend, {L // 12} let historie"}
    if goal:
        out["prob"] = round(sum(1 for v in finals if v >= goal) / n * 100)
        need = sorted(max(0.0, (goal - start * G) / A) for G, A in zip(Gs, As))
        out["monthly_for_80"] = int(math.ceil(pct(need, .80) / 100) * 100)
        out["monthly_for_50"] = int(math.ceil(pct(need, .50) / 100) * 100)
    # 2,5% inflace: hodnota v dnešních penězích
    out["p50_real"] = round(out["p50"] / (1.025 ** years))
    return out


@app.route("/api/plan/goal", methods=["POST"])
def api_plan_goal():
    if not _rate_ok("goal", 30, 60):
        return jsonify({"ok": False, "error": "Moc požadavků, zkus to za chvíli."}), 429
    b = request.get_json(silent=True) or {}
    start = max(0.0, _num(b.get("start_czk")) or 0.0)
    monthly = max(0.0, _num(b.get("monthly_czk")) or 0.0)
    years = int(min(40, max(1, _num(b.get("years")) or 10)))
    goal = _num(b.get("goal_czk"))
    if start + monthly <= 0:
        return jsonify({"ok": False, "error": "Zadej hodnotu portfolia nebo měsíční vklad."}), 400
    try:
        return jsonify({"ok": True, "years": years, **_goal_sim(start, monthly, years, goal if goal and goal > 0 else None)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# DENNÍ BRIEFING – „Co se děje · proč · koho se to týká · co s tím".
# Místo seznamu odkazů na dlouhé články: AI shrne JEN to, co je v dodaných
# zprávách (každá událost musí citovat zdroj, jinak se zahodí). Čísla trhu
# počítá server sám, AI je nesmí vymýšlet. Rada „co s tím" NEPOCHÁZÍ z AI –
# odvozuje ji frontend z dlouhodobého plánu (zprávy plán nemění).
# Globální briefing se počítá max. jednou za 3 hodiny pro všechny (KV cache).
# ---------------------------------------------------------------------------
DIGEST_SLOT_H = 3
DIGEST_SECTORS = ["celý trh"] + list(SECTOR_CZ.values())
# Rady, listicles a osobní finance: pro „co se dnes stalo" čistý šum
NEWS_NOISE = ("could soar", "buy and hold", "millionaire", "retire", "my sister", "my brother", "my husband",
              "my wife", "my mother", "my father", "should you buy", "stocks to buy", "forever", "prediction:",
              "i'm ", "social security", "beaten-down", "no-brainer", "smartest", "too cheap to ignore",
              "here's why", "here's how", "dear ", "moneyist", "best ", "top 3", "top 5", "2 stocks", "3 stocks",
              "my 401", "credit card", "mortgage rates today", "reasons", "stock to buy", "better buy",
              "which stock", "did the market", "still buys", "dividend stocks", "is it time to", "should i ",
              "stocks that", "buy right now", "buy now", "vs.", "watchlist", "battleground", "behind the ticker",
              "buy, sell, or hold", "compelling bet", "should you be worried", "risk hiding", "what makes",
              "strategy adds", "strategy exits")
NEWS_NOISE_PUBLISHERS = ("stockstory", "insider monkey", "trefis")
DIGEST_FACTS = [("^GSPC", "S&P 500", ""), ("^IXIC", "Nasdaq", ""), ("^VIX", "VIX (strach)", ""),
                ("^TNX", "Výnos 10letých dluhopisů USA", "%"), ("CZK=X", "Dolar v korunách", " Kč"),
                ("CL=F", "Ropa WTI", " $")]
# Témata, podle kterých se dá poznat, co dnes trh hýbe (i bez AI)
NEWS_TOPICS = [
    ("AI a polovodiče", r"\bai\b|umělá inteligenc|umělou inteligenc|semicon|\bsemis\b|polovodi|\bchip|nvidia|nvda|"
                        r"micron|tsmc|asml|\bamd\b|broadcom|coreweave|anthropic|openai|datacent|datové centr"),
    ("Inflace a sazby", r"inflac|inflation|\bcpi\b|\bppi\b|\bfed\b|fomc|sazb|rate cut|rate hike|yield|výnos|"
                        r"treasur|dluhopis|powell"),
    ("Výsledky firem", r"earnings|výsledk|tržby|revenue|quarter|čtvrtlet|guidance|výhled|\beps\b|beats|misses|překon"),
    ("Ropa a geopolitika", r"\boil\b|\bropa|ropy|crude|opec|\biran|izrael|israel|ukraj|ukraine|\bwar\b|válk|"
                           r"tariff|\bclo\b|\bcla\b|sankc|sanction|china|čín"),
    ("Banky a finance", r"\bbank|goldman|jpmorgan|morgan stanley|wells fargo|citigroup|state street|blackrock"),
    ("Zlato a dolar", r"\bgold\b|zlato|zlata|dollar|dolar|\bdxy\b"),
]
MARKET_LEVEL_RX = (r"stock market today|wall street|futures|sell-?off|\brout\b|rally|indexy|indexes|indices|"
                   r"s&p 500|nasdaq|dow jones|\bdow\b|shrnutí|výprodej|seanc|premarket|pre-bell|pre-market|ranní|"
                   r"akciové trhy|akcie klesají|akcie rostou|stocks (fall|slip|rise|drop|climb|slide)|market (close|wrap)")
WRAP_SOURCES = ("Kurzy.cz — akcie svět", "Reuters", "CNBC", "MarketWatch", "Investor's Business Daily",
                "Yahoo Finance", "Roklen24")


def _news_pool(hours=36, limit=45):
    """Čerstvé tržní zprávy ze všech zdrojů, bez šumu a duplicit, seřazené podle
    toho, jak dobře vysvětlují DNEŠNÍ POHYB TRHU (ne jen podle času): shrnutí trhu
    a témata dne nahoře, reklamní rozbory jednotlivých titulů dole."""
    items, seen = [], set()
    now = time.time()
    for key, src in _RSS_SOURCES.items():
        try:
            rss = _fetch_rss(key) or []
        except Exception:
            continue
        for it in rss[:25]:
            title = _strip_html(it.get("title") or "")
            text = _strip_html(it.get("summary") or "")
            if text.lower().startswith(title.lower()[:30]):
                text = text[len(title):].strip(" .:-–")
            ts = _parse_rss_time(it.get("pub_date"))
            if not title or (ts and now - ts > hours * 3600):
                continue
            low = title.lower()
            if any(n in low for n in NEWS_NOISE) or not _is_market_news(title + " " + text[:200]):
                continue
            k = re.sub(r"[^a-z0-9á-ž]", "", low)[:50]
            if k in seen:
                continue
            seen.add(k)
            items.append({"src": src["label"], "title": title[:200], "text": text[:300],
                          "link": it.get("link") or "", "ts": ts or 0})
    try:
        for q in ("%5EGSPC", "%5EIXIC"):
            r = requests.get(f"https://query2.finance.yahoo.com/v1/finance/search?q={q}&quotesCount=0&newsCount=12",
                             headers=HEADERS, timeout=6).json()
            for n in (r.get("news") or []):
                t = (n.get("title") or "").strip()
                k = re.sub(r"[^a-z0-9á-ž]", "", t.lower())[:50]
                ts = n.get("providerPublishTime") or 0
                pub = (n.get("publisher") or "").lower()
                if (t and k not in seen and not any(x in t.lower() for x in NEWS_NOISE)
                        and not any(x in pub for x in NEWS_NOISE_PUBLISHERS)
                        and (not ts or now - ts < hours * 3600)):
                    seen.add(k)
                    items.append({"src": n.get("publisher") or "Yahoo Finance", "title": t[:200], "text": "",
                                  "link": n.get("link") or "", "ts": ts})
    except Exception:
        pass
    items = [p for p in items if not any(x in p["src"].lower() for x in NEWS_NOISE_PUBLISHERS)]

    def score(p):
        t = p["title"].lower()
        s = 0
        if re.search(MARKET_LEVEL_RX, t):
            s += 3
        if any(re.search(rx, t) for _l, rx in NEWS_TOPICS):
            s += 2
        if p["src"] in WRAP_SOURCES:
            s += 1
        age = (now - p["ts"]) / 3600 if p["ts"] else 24
        s += 2 if age < 6 else (1 if age < 18 else 0)
        if len(p["text"]) > 80:
            s += 1
        return s
    items.sort(key=lambda p: (-score(p), -(p["ts"] or 0)))
    items = items[:limit]
    for i, it in enumerate(items, 1):
        it["id"] = f"n{i}"
    return items


def _market_facts():
    """Tvrdá čísla dne (počítá server, ne AI)."""
    from concurrent.futures import ThreadPoolExecutor
    tks = [t for t, _l, _u in DIGEST_FACTS] + [t for t, _l, _i in _SECTOR_ETFS]
    with ThreadPoolExecutor(max_workers=8) as ex:
        got = dict(zip(tks, ex.map(_fetch_macro_one, tks)))
    facts = []
    for t, label, unit in DIGEST_FACTS:
        d = got.get(t)
        if d:
            facts.append({"key": t, "label": label, "unit": unit, "price": round(d["price"], 2), "chg": d["chg_pct"]})
    secs = sorted([{"label": l, "chg": got[t]["chg_pct"]} for t, l, _i in _SECTOR_ETFS if got.get(t)],
                  key=lambda x: x["chg"])
    return {"facts": facts, "best": secs[-1] if secs else None, "worst": secs[0] if secs else None}


def _sg(x, d=1):
    """Změna česky: +1,2 % / −0,6 %."""
    return ("+" if x >= 0 else "−") + _pc(abs(x), d) + " %"


def _market_story_base(mk):
    """Deterministický základ příběhu dne z tvrdých čísel (funguje i bez AI):
    nálada trhu (podle indexů, sektorů a VIX – ne jen VIX), co se stalo a proč
    je trh klidný/neklidný. AI k tomu jen doplní PŘÍČINY ze zpráv."""
    f = {x["key"]: x for x in mk.get("facts") or []}
    g = lambda k: (f.get(k) or {}).get("chg")
    spx, ndx, vixc = g("^GSPC"), g("^IXIC"), g("^VIX")
    vix = (f.get("^VIX") or {}).get("price")
    tnx, oil = f.get("^TNX"), f.get("CL=F")
    best, worst = mk.get("best"), mk.get("worst")
    spread = (best["chg"] - worst["chg"]) if (best and worst) else 0
    pts, why = 0, []
    if spx is not None:
        a = abs(spx)
        pts += 3 if a >= 1.5 else (2 if a >= 0.8 else (1 if a >= 0.4 else 0))
        if a >= 0.4:
            why.append(f"S&P 500 se pohnul o {_sg(spx)}")
    if ndx is not None and ndx <= -0.8:
        pts += 2 if ndx <= -1.5 else 1
        why.append(f"technologický Nasdaq {_sg(ndx)}")
    if vix is not None:
        pts += 3 if vix >= 30 else (2 if vix >= 22 else (1 if vix >= 18 else 0))
        if vixc is not None and vixc >= 10:
            pts += 1
            why.append(f"VIX (index strachu) skočil o {_sg(vixc, 0)}")
    if spread >= 4:
        pts += 1
        why.append(f"sektory se hodně rozešly ({worst['label']} {_sg(worst['chg'])}, {best['label']} {_sg(best['chg'])})"
                   " – investoři přesouvají peníze z jednoho odvětví do druhého")
    level, label = (("calm", "klidný") if pts <= 1 else ("mixed", "rozkolísaný") if pts <= 3
                    else ("stress", "neklidný") if pts <= 5 else ("panic", "velmi neklidný"))
    vlab = ""
    if vix is not None:
        vlab = ("nízký – trh se nebojí" if vix < 15 else "běžná hladina, žádná panika" if vix < 20
                else "zvýšený – investoři jsou ostražití" if vix < 30 else "vysoký – na trzích je strach")
    if why:
        mood_why = "Proč: " + "; ".join(why) + "."
    else:
        mood_why = "Indexy se skoro nehýbou, takže žádný důvod k nervozitě."
    if vlab:
        mood_why += f" VIX (index strachu) je {_pc(vix, 1)} – {vlab}."
    # co se stalo
    if spx is None:
        what = ""
    else:
        verb = ("klesají" if spx <= -0.3 else "rostou" if spx >= 0.3 else "se skoro nehýbou")
        what = f"Americké akcie dnes {verb}: S&P 500 {_sg(spx)}" + (f", Nasdaq {_sg(ndx)}" if ndx is not None else "") + "."
        if best and worst and spread >= 2:
            what += (f" Nejvíc klesá sektor {worst['label']} ({_sg(worst['chg'])}), nejvíc roste {best['label']} "
                     f"({_sg(best['chg'])}).")
        if oil and abs(oil["chg"]) >= 2:
            what += f" Ropa {_sg(oil['chg'])}."
        if tnx and abs(tnx["chg"]) >= 3:
            what += f" Výnos 10letých dluhopisů USA {_pc(tnx['price'], 2)} % ({_sg(tnx['chg'])})."
    return {"level": level, "label": label, "what": what, "why": mood_why, "points": pts}


def _topic_drivers(pool, n=3):
    """Bez AI: co dnes ve zprávách dominuje – témata podle počtu titulků, u každého
    dva nejvýstižnější titulky (nejdřív ty o pohybu trhu), bez opakování."""
    found = []
    for label, rx in NEWS_TOPICS:
        hits = [p for p in pool if re.search(rx, p["title"].lower())]
        if hits:
            hits.sort(key=lambda p: 0 if re.search(MARKET_LEVEL_RX, p["title"].lower()) else 1)
            found.append((len(hits), label, hits))
    found.sort(key=lambda x: -x[0])
    out, used = [], set()
    for cnt, label, hits in found:
        if len(out) >= n or (cnt < 2 and out):
            break
        fresh = [h for h in hits if h["id"] not in used] or hits
        top = fresh[:2]
        used.update(h["id"] for h in top)
        out.append({"title": label, "count": cnt,
                    "text": f"Téma dne ({cnt} zpráv): " + " · ".join(f"„{h['title']}“" for h in top),
                    "sources": [{"title": h["title"], "src": h["src"], "link": h["link"]} for h in top]})
    return out


def _wrap_note(pool):
    """Bez AI: nejlepší denní shrnutí trhu z českých/světových zdrojů jako přímá citace."""
    for p in pool:
        if p["src"] in WRAP_SOURCES and re.search(MARKET_LEVEL_RX, p["title"].lower()) and len(p["text"]) > 60:
            txt = p["text"][:260]
            if "." in txt[40:]:                      # perex v RSS bývá useknutý – ukončit celou větou
                txt = txt[:txt.rfind(".") + 1]
            return {"title": p["title"], "text": txt, "src": p["src"], "link": p["link"]}
    return None


DIGEST_PROMPT = """Jsi zkušený analytik, který každé ráno píše českému DLOUHODOBÉMU investorovi stručný briefing.
Investor není ekonom a nechce dlouhé články. Nejdůležitější otázka: CO SE DNES STALO S TRHEM A PROČ.

TVRDÁ ČÍSLA DNE (spočítaná, použij je přesně takto, nic nepřidávej):
{facts}

NÁLADA TRHU (spočítaná): {mood}

ZPRÁVY (id | zdroj | titulek | perex), seřazené od nejdůležitějších:
{items}

Úkol – vrať JSON s těmito poli:
1. "what": 2–3 věty prostou češtinou: co se dnes s trhem stalo. Použij čísla z tvrdých čísel (indexy, sektory),
   řekni, kdo táhne trh dolů nebo nahoru, a PROČ – ten důvod musí být ve zprávách.
2. "why": 1–2 věty, PROČ je trh dnes {mood_label}. Konkrétní příčiny ze zpráv (např. „investoři se bojí, že
   firmy kolem AI nezvládnou financování“), ne obecné fráze. Když zprávy důvod pohybu neuvádějí, napiš
   „Zprávy konkrétní důvod neuvádějí“ – nevymýšlej.
3. "drivers": 2–3 hlavní příčiny dne, každá {{"title": max 6 slov, "text": 1–2 věty co a PROČ to trh hýbe
   (mechanismus: např. vyšší inflace → sazby zůstanou vysoko → dražší úvěry → tlak na ocenění technologií),
   "sources": [id zpráv, povinné]}}.
4. "events": 0–3 DALŠÍ důležité události (firmy, výsledky, regulace), které nejsou už v "drivers":
   {{"title": max 8 slov, "what": co se stalo (jen fakta ze zpráv), "why": proč je to důležité,
   "impact": "pozitivní" | "negativní" | "smíšené", "sectors": POUZE z: {sectors},
   "tickers": tickery amerických firem, které zpráva přímo jmenuje, "sources": [id, povinné]}}.
5. "headline": jedna věta shrnující den. 6. "long_term": jedna věta, co to znamená pro dlouhodobého investora
   (střízlivě, bez paniky i bez nadšení).

Pravidla: čísla jen z tvrdých čísel nebo ze zpráv; žádné předpovědi cen; žádné „kupte/prodejte“; ignoruj rady,
žebříčky a reklamu; zkratky vysvětli (PPI = ceny výrobců, CPI = ceny pro spotřebitele). Odpověz JEN JSON:
{{"what": "...", "why": "...", "drivers": [], "events": [], "headline": "...", "long_term": "..."}}"""


def _pct_numbers(text):
    return [float(x.replace(",", ".")) for x in re.findall(r"(\d+(?:[.,]\d+)?)\s*(?:%|procent)", text or "")]


def _clean_digest(raw, pool, mk=None, base=None):
    """Validace výstupu AI: událost/příčina bez platného zdroje se zahodí, sektory
    jen ze seznamu, tickery jen jako tickery, texty zkrácené; procenta v textu
    musí pocházet z tvrdých čísel nebo ze zpráv, jinak se použije základ ze serveru."""
    if not isinstance(raw, dict):
        return None
    by_id = {p["id"]: p for p in pool}
    clip = lambda s, n: (str(s or "").strip()[:n])
    allowed = set()
    for fct in (mk or {}).get("facts") or []:
        allowed.add(abs(fct["chg"]))
    for k in ("best", "worst"):
        if (mk or {}).get(k):
            allowed.add(abs(mk[k]["chg"]))
    for p in pool:
        allowed.update(_pct_numbers(p["title"] + " " + p["text"]))
    if base:
        allowed.update(_pct_numbers(base.get("what", "") + " " + base.get("why", "")))
    ok_nums = lambda t: all(any(abs(v - a) < 0.06 for a in allowed) for v in _pct_numbers(t))
    events = []
    for e in (raw.get("events") or [])[:6]:
        if not isinstance(e, dict):
            continue
        srcs = [by_id[s] for s in (e.get("sources") or []) if isinstance(s, str) and s in by_id]
        if not srcs or not e.get("what") or not ok_nums(str(e.get("what")) + " " + str(e.get("why") or "")):
            continue
        imp = (e.get("impact") or "").lower()
        events.append({
            "title": clip(e.get("title"), 90), "what": clip(e.get("what"), 360), "why": clip(e.get("why"), 360),
            "impact": imp if imp in ("pozitivní", "negativní", "smíšené") else "smíšené",
            "sectors": [s for s in (e.get("sectors") or []) if s in DIGEST_SECTORS][:4],
            "tickers": [t.upper() for t in (e.get("tickers") or [])
                        if isinstance(t, str) and re.match(r"^[A-Za-z.\-]{1,6}$", t)][:6],
            "sources": [{"title": s["title"], "src": s["src"], "link": s["link"]} for s in srcs[:3]],
        })
    drivers = []
    for d in (raw.get("drivers") or [])[:4]:
        if not isinstance(d, dict):
            continue
        srcs = [by_id[s] for s in (d.get("sources") or []) if isinstance(s, str) and s in by_id]
        if not srcs or not d.get("text") or not ok_nums(str(d.get("text"))):
            continue
        drivers.append({"title": clip(d.get("title"), 60), "text": clip(d.get("text"), 320),
                        "sources": [{"title": s["title"], "src": s["src"], "link": s["link"]} for s in srcs[:3]]})
    what, why = clip(raw.get("what"), 520), clip(raw.get("why"), 420)
    if what and not ok_nums(what):
        what = ""
    if why and not ok_nums(why):
        why = ""
    if not (events or drivers or what):
        return None
    return {"what": what, "why": why, "drivers": drivers[:3], "events": events[:4],
            "headline": clip(raw.get("headline"), 240), "long_term": clip(raw.get("long_term"), 300)}


def _news_digest(allow_llm=True):
    """Globální briefing (kešováno na 3h slot). Vždy obsahuje „příběh dne" ze
    serveru (nálada, co se stalo, hlavní témata); AI k němu doplňuje příčiny ze zpráv."""
    now = datetime.now(timezone.utc)
    slot = f"{now.strftime('%Y-%m-%d')}:{now.hour // DIGEST_SLOT_H}"
    try:
        c = kv_get_json(f"news:digest2:{slot}")
        # verze bez AI (výpadek) platí jen 15 min, pak se AI zkusí znovu
        if c and (c.get("ai") or not analysis_enabled() or time.time() - c.get("generated", 0) < 900):
            return c
    except Exception:
        pass
    pool = _news_pool()
    mk = _market_facts()
    base = _market_story_base(mk)
    topics = _topic_drivers(pool)
    story = {"level": base["level"], "label": base["label"], "what": base["what"], "why": base["why"],
             "drivers": topics, "ai": False, "note": _wrap_note(pool)}
    out = {"ok": True, "slot": slot, "generated": int(time.time()), "market": mk, "ai": False, "ai_status": "no_key",
           "story": story,
           "headlines": [{"title": p["title"], "text": p["text"][:200], "src": p["src"], "link": p["link"], "ts": p["ts"]}
                         for p in pool[:14]]}
    if not analysis_enabled():
        out["ai_status"] = "no_key"
    elif not allow_llm or not pool:
        out["ai_status"] = "skipped"
    else:
        facts = "\n".join(f"- {f['label']}: {f['price']}{f['unit']} ({'+' if f['chg'] >= 0 else ''}{f['chg']} % za den)"
                          for f in mk["facts"])
        if mk.get("best") and mk.get("worst"):
            facts += (f"\n- Nejvíc rostoucí sektor: {mk['best']['label']} ({mk['best']['chg']:+.2f} %), "
                      f"nejvíc klesající: {mk['worst']['label']} ({mk['worst']['chg']:+.2f} %)")
        items = "\n".join(f"{p['id']} | {p['src']} | {p['title']} | {p['text']}" for p in pool)
        hint = "; ".join(f"{d['title']} ({d['count']} zpráv)" for d in topics)
        raw = call_llm(DIGEST_PROMPT.format(
            facts=facts, items=items, sectors=", ".join(DIGEST_SECTORS), mood_label=base["label"],
            mood=f"{base['label']} ({base['why']})" + (f" Nejčastější témata ve zprávách: {hint}." if hint else "")))
        if raw is None:
            out["ai_status"] = "llm_failed"
        else:
            dg = _clean_digest(raw, pool, mk, base)
            if not dg:
                out["ai_status"] = "no_valid"
            else:
                story["ai"] = True
                if dg["what"]:
                    story["what"] = dg["what"]
                if dg["why"]:
                    story["why"] = dg["why"]
                if dg["drivers"]:
                    story["drivers"] = dg["drivers"]
                    story["note"] = None
                out.update({"headline": dg["headline"], "long_term": dg["long_term"], "events": dg["events"]})
                out["ai"] = True
                out["ai_status"] = "ok"
    try:
        kv_set_json(f"news:digest2:{slot}", out)
        kv_set_json("news:digest:status", {"ts": int(time.time()), "ai": out["ai"], "ai_status": out["ai_status"],
                                           "pool": len(pool), "mood": base["label"]})
    except Exception:
        pass
    return out


@app.route("/api/news/digest")
def api_news_digest():
    if not _rate_ok("digest", 60, 60):
        return jsonify({"ok": False, "error": "Moc požadavků, zkus to za chvíli."}), 429
    try:
        return jsonify(_news_digest())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


MINE_PROMPT = """Jsi analytik a píšeš českému dlouhodobému investorovi, co je nového u akcií, které DRŽÍ.
Zprávy (id | ticker | zdroj | titulek | perex):
{items}

Pro každý ticker, u kterého je ve zprávách něco PODSTATNÉHO (výsledky, výhled, akvizice, soud, regulace,
změna vedení, velký pohyb ceny s uvedeným důvodem), napiš česky:
- "ticker", "what": 1–2 věty co se stalo (jen fakta ze zpráv), "why": 1 věta proč je to pro firmu důležité,
- "impact": "pozitivní" | "negativní" | "smíšené", "sources": id zpráv (povinné),
- "severe": true JEN pokud zprávy přímo uvádějí podvod nebo účetní skandál, vyšetřování úřady (SEC, DOJ)
  kvůli podvodu, přepočet dřívějších výsledků, bankrot nebo vážnou hrozbu bankrotu, vyřazení z burzy.
  Pokuta, prohraný soud, slabé výsledky nebo snížený výhled NEJSOU severe. Jinak false.
Tickery, kde jsou jen obecné články, rady nebo nic podstatného, VYNECH. Žádné rady kupovat/prodat, žádné předpovědi.
Odpověz JEN JSON: {{"items": [{{"ticker": "...", "what": "...", "why": "...", "impact": "...", "severe": false, "sources": []}}]}}"""


_NAME_SUFFIX = re.compile(r"\b(inc|incorporated|corp|corporation|co|company|holdings?|group|plc|n\.?v|s\.?a|ag|se|ltd|limited|the|class [a-c])\b\.?", re.I)


_COMMON_FIRST = {"state", "first", "general", "american", "united", "bank", "global", "national", "public",
                 "digital", "energy", "capital", "international", "target", "block", "match", "best", "dollar",
                 "southern", "western", "eastern", "northern", "texas", "universal", "applied", "advanced",
                 "consolidated", "regional", "realty", "insurance", "health", "booking", "progressive", "principal"}


def _name_keys(ticker, name):
    """Řetězce, podle kterých poznáme, že zpráva je opravdu o téhle firmě."""
    keys = {k.lower() for k in _ticker_synonyms(ticker) if len(k) >= 3 and not k.isupper()}
    clean = _NAME_SUFFIX.sub(" ", re.sub(r"[(),]", " ", name or "")).strip()
    words = [w.lower() for w in clean.split() if len(w) >= 3]
    if words:
        common = words[0] in _COMMON_FIRST or len(words[0]) < 5
        if len(words) > 1:
            keys.add(" ".join(words[:2]))
        if not common:
            keys.add(words[0])
    return keys


def _about(title, ticker, keys):
    base = ticker.split(".")[0].upper()
    if len(base) >= 2 and re.search(rf"(?<![A-Za-z]){re.escape(base)}(?![A-Za-z])", title):
        return True
    low = title.lower()
    return any(k in low for k in keys)


def _mine_digest(tickers, names=None, cap=20):
    """Co je nového u TVÝCH akcií / tipů (zprávy o firmě za 3 dny), kešováno na slot."""
    names = names or {}
    tickers = sorted({t.upper() for t in tickers if re.match(r"^[A-Z0-9.\-]{1,12}$", (t or "").upper())})[:cap]
    if not tickers:
        return {"ok": True, "items": [], "ai": False}
    now = datetime.now(timezone.utc)
    slot = f"{now.strftime('%Y-%m-%d')}:{now.hour // DIGEST_SLOT_H}"
    key = "news:mine:" + hashlib.sha1((slot + "|" + ",".join(tickers)).encode()).hexdigest()[:20]
    try:
        c = kv_get_json(key)
        if c and (c.get("ai") or not analysis_enabled() or time.time() - c.get("generated", 0) < 900):
            return c
    except Exception:
        pass

    def one(t):
        try:
            r = requests.get(f"https://query2.finance.yahoo.com/v1/finance/search?q={quote(t)}&quotesCount=0&newsCount=8",
                             headers=HEADERS, timeout=6).json()
            out, keys = [], _name_keys(t, names.get(t))
            for n in (r.get("news") or []):
                ts = n.get("providerPublishTime") or 0
                tt = (n.get("title") or "").strip()
                if (tt and (not ts or time.time() - ts < 72 * 3600) and not any(x in tt.lower() for x in NEWS_NOISE)
                        and _about(tt, t, keys)):
                    out.append({"ticker": t, "src": n.get("publisher") or "Yahoo", "title": tt[:200], "text": "",
                                "link": n.get("link") or "", "ts": ts})
            return out[:4]
        except Exception:
            return []
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=6) as ex:
        pool = [x for lst in ex.map(one, tickers) for x in lst]
    for i, p in enumerate(pool, 1):
        p["id"] = f"m{i}"
    res = {"ok": True, "ai": False, "items": [], "generated": int(time.time()),
           "headlines": [{"ticker": p["ticker"], "title": p["title"], "src": p["src"], "link": p["link"], "ts": p["ts"]}
                         for p in pool]}
    if pool and analysis_enabled():
        raw = call_llm(MINE_PROMPT.format(items="\n".join(
            f"{p['id']} | {p['ticker']} | {p['src']} | {p['title']} | {p['text']}" for p in pool)))
        by_id = {p["id"]: p for p in pool}
        for e in ((raw or {}).get("items") or [])[:10]:
            if not isinstance(e, dict):
                continue
            t = str(e.get("ticker") or "").upper()
            # zdroj musí být zpráva právě o tomhle tickeru
            srcs = [by_id[s] for s in (e.get("sources") or [])
                    if isinstance(s, str) and s in by_id and by_id[s]["ticker"] == t]
            if not srcs or t not in tickers or not e.get("what"):
                continue
            imp = (e.get("impact") or "").lower()
            severe = e.get("severe") is True
            res["items"].append({"ticker": t, "what": str(e["what"])[:360], "why": str(e.get("why") or "")[:300],
                                 "impact": imp if imp in ("pozitivní", "negativní", "smíšené") else "smíšené",
                                 "severe": severe,
                                 "sources": [{"title": s["title"], "src": s["src"], "link": s["link"]} for s in srcs[:2]]})
            if severe:
                try:
                    kv_set_json(f"redflag:ai:{_spx_key(t)}", {
                        "until_ts": time.time() + REDFLAG_DAYS * 86400, "date": _today_iso(),
                        "reason": "ze zpráv: " + str(e["what"])[:200], "url": srcs[0]["link"]})
                except Exception:
                    pass
        res["ai"] = raw is not None
    try:
        kv_set_json(key, res)
    except Exception:
        pass
    return res


@app.route("/api/news/mine", methods=["POST"])
def api_news_mine():
    if not _rate_ok("news_mine", 30, 60):
        return jsonify({"ok": False, "error": "Moc požadavků, zkus to za chvíli."}), 429
    try:
        b = request.get_json(silent=True) or {}
        names = b.get("names") if isinstance(b.get("names"), dict) else {}
        return jsonify(_mine_digest(b.get("tickers") or [], {str(k).upper(): str(v)[:80] for k, v in names.items()}))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/signals")
def get_signals():
    """Dnešní nákupní signály ověřeného modelu (celé S&P 500)."""
    s = market_scan(allow_step=False)
    if not s:
        return jsonify({"ok": True, "count": 0, "results": [], "scanning": True,
                        "updated": "", "universe": 0})
    sig = s.get("signals") or []
    return jsonify({"ok": True, "count": len(sig), "results": sig, "date": s.get("date"),
                    "updated": s.get("updated"), "stale": bool(s.get("stale")),
                    "universe": s.get("scanned"), "rejected": s.get("rejected") or [],
                    "rule": s.get("rule")})


@app.route("/api/backtest")
def get_backtest():
    """Ověřené výsledky strategie (offline walk-forward test, viz VALIDATION).
    Tvar zůstává kompatibilní s tím, co čte frontend (top/buy/baseline…);
    hlavní čísla jsou z NEDOTČENÉHO období 2023-2026."""
    v = VALIDATION
    h = v["signals"]["holdout"]
    return jsonify({"ok": True, "result": {
        "top": h["model"], "buy": h["model"], "baseline": h["all_dips"],
        "period": "2015–2026, ověřeno na 2023–2026", "sample_count": h["model"]["count"],
        "ticker_count": 633, "horizon_days": EXIT_MAX_DAYS, "plan": {
            "target_pct": v["rule"]["target_pct"], "stop_pct": v["rule"]["stop_pct"],
            "max_days": v["rule"]["max_days"]},
        "validation": v,
    }})


# ---------------------------------------------------------------------------
# MAKRO SNÍMEK – stav trhu na první pohled (VIX, US10Y, DXY, ropa, BTC)
# ---------------------------------------------------------------------------
_MACRO_TICKERS = [
    # (yahoo_ticker, label, unit, regime_low, regime_high, lower_is_calm)
    # lower_is_calm=True -> nízká hodnota = "klid" (zelená), vysoká = "stres" (červená)
    ("^VIX",      "VIX",     "",   15,  25,  True),   # strach na akciích
    ("^TNX",      "US10Y",   "%",  3.5, 4.7, True),   # 10letý vládní výnos
    ("DX-Y.NYB",  "DXY",     "",   100, 106, True),   # americký dolar
    ("CL=F",      "Ropa",    "$",  65,  85,  False),  # neutrální (kontext)
    ("BTC-USD",   "Bitcoin", "$",  60000, 100000, False),  # neutrální
]

def _fetch_macro_one(ticker):
    """Jeden makro instrument – cena + denní změna."""
    try:
        d = yahoo_chart(ticker, "5d", "1d")
        meta = d.get("meta", {}) or {}
        closes = [c for c in (((d.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []) if c is not None]
        if not closes:
            return None
        price = meta.get("regularMarketPrice") or closes[-1]
        prev = _prev_close(closes, meta)
        chg_pct = ((price - prev) / prev * 100.0) if prev else 0.0
        return {"price": float(price), "chg_pct": round(chg_pct, 2)}
    except Exception:
        return None


def _macro_regime(value, low, high, lower_is_calm):
    """Vrátí 'calm' / 'neutral' / 'stress' podle režimu."""
    if value is None:
        return "neutral"
    if lower_is_calm:
        if value <= low: return "calm"
        if value >= high: return "stress"
        return "neutral"
    else:
        # Pro „neutrální" instrumenty (ropa, BTC) jen značíme extrémy
        if value <= low: return "low"
        if value >= high: return "high"
        return "mid"


@app.route("/api/macro")
def get_macro():
    """Makro snímek – kešováno na 30 minut."""
    cached = kv_get_json("macro:snapshot")
    if cached and (time.time() - cached.get("ts", 0) < 1800):
        return jsonify({"ok": True, "cached": True, **cached})
    items = []
    for (tk, label, unit, lo, hi, low_calm) in _MACRO_TICKERS:
        d = _fetch_macro_one(tk)
        if not d:
            continue
        items.append({
            "ticker": tk, "label": label, "unit": unit,
            "price": d["price"], "chg_pct": d["chg_pct"],
            "regime": _macro_regime(d["price"], lo, hi, low_calm),
        })
    out = {"ts": int(time.time()), "items": items,
           "updated": datetime.now(timezone.utc).strftime("%H:%M UTC")}
    try:
        kv_set_json("macro:snapshot", out)
    except Exception:
        pass
    return jsonify({"ok": True, "cached": False, **out})


# ---------------------------------------------------------------------------
# KALENDÁŘ UDÁLOSTÍ – earnings z Finnhubu pro watchlist + makro presety
# ---------------------------------------------------------------------------
def _macro_events_window(days=10):
    """Statické přibližné US makro události (CPI/FOMC/NFP/PPI/Retail/Jobless).
    Vrací jen události spadající do okna [dnes, dnes+days].
    Posloupnost: měsíční CPI cca 12., PPI cca 13., Retail cca 16., FOMC 8×/rok,
    NFP první pátek v měsíci, Jobless každý čtvrtek."""
    today = datetime.now(timezone.utc).date()
    end = today + timedelta(days=days)
    out = []
    cur = today
    while cur <= end:
        # Jobless claims – každý čtvrtek
        if cur.weekday() == 3:
            out.append({"date": cur.strftime("%Y-%m-%d"), "name": "Jobless Claims (týdenní)", "kind": "macro", "weight": "low"})
        # NFP – první pátek v měsíci
        if cur.weekday() == 4 and cur.day <= 7:
            out.append({"date": cur.strftime("%Y-%m-%d"), "name": "NFP – zaměstnanost USA", "kind": "macro", "weight": "high"})
        # CPI cca 10.-13., PPI cca 11.-14., Retail Sales cca 15.-17.
        if cur.day == 12 and cur.weekday() < 5:
            out.append({"date": cur.strftime("%Y-%m-%d"), "name": "US CPI – inflace", "kind": "macro", "weight": "high"})
        if cur.day == 13 and cur.weekday() < 5:
            out.append({"date": cur.strftime("%Y-%m-%d"), "name": "US PPI – ceny výrobců", "kind": "macro", "weight": "mid"})
        if cur.day == 16 and cur.weekday() < 5:
            out.append({"date": cur.strftime("%Y-%m-%d"), "name": "Retail Sales", "kind": "macro", "weight": "mid"})
        cur += timedelta(days=1)
    return out


@app.route("/api/calendar")
def get_calendar():
    """Vrátí earnings z Finnhubu pro daný seznam tickerů + makro presety."""
    tickers_param = request.args.get("tickers", "").upper()
    days = max(1, min(int(request.args.get("days", "10") or 10), 30))
    tickers = [t.strip() for t in tickers_param.split(",") if t.strip()][:30]
    today = datetime.now(timezone.utc).date()
    end = today + timedelta(days=days)
    today_s = today.strftime("%Y-%m-%d")
    end_s = end.strftime("%Y-%m-%d")
    fk = os.environ.get("FINNHUB_KEY")
    items = []
    if fk and tickers:
        for t in tickers:
            try:
                cal = requests.get("https://finnhub.io/api/v1/calendar/earnings",
                                   params={"from": today_s, "to": end_s, "symbol": t, "token": fk},
                                   timeout=5).json() or {}
                for e in (cal.get("earningsCalendar") or [])[:1]:
                    d = e.get("date")
                    if not d or d < today_s or d > end_s:
                        continue
                    items.append({"date": d, "ticker": t, "name": t,
                                  "kind": "earnings", "hour": e.get("hour", "")})
            except Exception:
                continue
    items += _macro_events_window(days=days)
    items.sort(key=lambda x: (x["date"], x.get("kind") != "earnings"))
    return jsonify({"ok": True, "items": items[:20],
                    "updated": datetime.now(timezone.utc).strftime("%H:%M UTC")})


# ---------------------------------------------------------------------------
# SEKTOROVÁ HEATMAPA – 11 S&P 500 sektorů přes SPDR sektorové ETF
# ---------------------------------------------------------------------------
_SECTOR_ETFS = [
    ("XLK",  "Technologie",      "ti-cpu"),
    ("XLF",  "Finance",          "ti-building-bank"),
    ("XLV",  "Zdravotnictví",    "ti-stethoscope"),
    ("XLE",  "Energetika",       "ti-flame"),
    ("XLY",  "Spotřeba (luxus)", "ti-shopping-bag"),
    ("XLP",  "Spotřeba (denní)", "ti-shopping-cart"),
    ("XLI",  "Průmysl",          "ti-tools"),
    ("XLB",  "Materiály",        "ti-package"),
    ("XLU",  "Utility",          "ti-bolt"),
    ("XLRE", "Reality",          "ti-building"),
    ("XLC",  "Komunikace",       "ti-broadcast"),
]


@app.route("/api/sectors")
def get_sectors():
    """Sektorová heatmapa – cache 15 min."""
    cached = kv_get_json("sectors:snapshot")
    if cached and (time.time() - cached.get("ts", 0) < 900):
        return jsonify({"ok": True, "cached": True, **cached})
    items = []
    for (tk, label, icon) in _SECTOR_ETFS:
        d = _fetch_macro_one(tk)
        if not d:
            continue
        items.append({"ticker": tk, "label": label, "icon": icon,
                      "price": d["price"], "chg_pct": d["chg_pct"]})
    out = {"ts": int(time.time()), "items": items,
           "updated": datetime.now(timezone.utc).strftime("%H:%M UTC")}
    try:
        kv_set_json("sectors:snapshot", out)
    except Exception:
        pass
    return jsonify({"ok": True, "cached": False, **out})


# ---------------------------------------------------------------------------
# SROVNÁVAČ AKCIÍ – 2–3 tickery vedle sebe (rychlá metrika)
# ---------------------------------------------------------------------------
def _compare_one(ticker):
    """Rychlá metriky pro srovnání – Yahoo chart + technické skóre + Finnhub fundamenty."""
    try:
        daily = yahoo_chart(ticker, "1y", "1d")
        meta = daily.get("meta", {}) or {}
        q = ((daily.get("indicators") or {}).get("quote") or [{}])[0]
        raw_cl = q.get("close") or []
        raw_vl = q.get("volume") or []
        closes, vols = [], []
        for k, c in enumerate(raw_cl):
            if c is not None:
                closes.append(c)
                v = raw_vl[k] if k < len(raw_vl) else None
                vols.append(v if v is not None else 0)
        if not closes:
            return None
        price = meta.get("regularMarketPrice") or closes[-1]
        prev = _prev_close(closes, meta)
        chg = ((price - prev) / prev * 100.0) if prev else 0
        out = {
            "ticker": ticker,
            "name": meta.get("shortName") or meta.get("longName") or ticker,
            "currency": meta.get("currency", "USD"),
            "price": round(price, 2),
            "change_pct": round(chg, 2),
        }
        if len(closes) >= 210:
            try:
                ind = compute_indicators(closes, vols)
                sc = tech_setup_score(price, ind)
                if sc is not None:
                    out["ma_score"] = sc
                out["rsi"] = ind.get("rsi")
                out["volatility_pct"] = ind.get("volatility")
            except Exception:
                pass
        # Finnhub fundamenty (klíčové metriky)
        fk = os.environ.get("FINNHUB_KEY")
        if fk:
            try:
                base = "https://finnhub.io/api/v1"
                metric = (requests.get(f"{base}/stock/metric", params={"symbol": ticker, "metric": "all", "token": fk}, timeout=5).json() or {}).get("metric", {}) or {}
                prof = requests.get(f"{base}/stock/profile2", params={"symbol": ticker, "token": fk}, timeout=4).json() or {}
                pt = requests.get(f"{base}/stock/price-target", params={"symbol": ticker, "token": fk}, timeout=4).json() or {}
                out["market_cap_musd"] = prof.get("marketCapitalization")
                out["pe"] = metric.get("peTTM")
                out["revenue_growth_pct"] = metric.get("revenueGrowthTTMYoy")
                out["profit_margin_pct"] = metric.get("netProfitMarginTTM")
                out["roe_pct"] = metric.get("roeTTM")
                out["debt_to_equity"] = metric.get("totalDebt/totalEquityQuarterly")
                tm = pt.get("targetMean")
                if isinstance(tm, (int, float)) and tm and price:
                    out["target_upside_pct"] = round((tm / price - 1) * 100, 1)
            except Exception:
                pass
        return out
    except Exception:
        return None


@app.route("/api/compare")
def api_compare():
    tickers = [t.strip().upper() for t in request.args.get("tickers", "").split(",") if t.strip()][:3]
    if not tickers:
        return jsonify({"ok": True, "results": []})
    cached = kv_get_json("compare:" + ",".join(tickers))
    if cached and (time.time() - cached.get("ts", 0) < 600):
        return jsonify({"ok": True, "cached": True, **cached})
    res = [r for r in (_compare_one(t) for t in tickers) if r]
    out = {"ts": int(time.time()), "results": res}
    try:
        kv_set_json("compare:" + ",".join(tickers), out)
    except Exception:
        pass
    return jsonify({"ok": True, "cached": False, **out})


# ---------------------------------------------------------------------------
# SEC EDGAR – oficiální americké finanční regulátorská data (zdarma)
# Poskytuje: 10-K (roční), 10-Q (kvartální), 8-K (materiální události),
# Form 4 (insider transakce). SEC vyžaduje User-Agent s kontaktem.
# ---------------------------------------------------------------------------
SEC_UA = os.environ.get("SEC_UA") or "MY ADVANTAGE contact@myadvantage.site"
SEC_HEADERS = {"User-Agent": SEC_UA, "Accept": "application/json"}

# Popisky forem v češtině + kritičnost pro obchodní rozhodnutí
_SEC_FORMS = {
    "10-K":  ("Výroční zpráva",          "annual",   "Auditovaná roční data. Základ pro fundamenty."),
    "10-Q":  ("Kvartální zpráva",        "quarterly","Nejnovější tržby, marže, cash-flow."),
    "8-K":   ("Materiální událost",      "material", "Akvizice, změna CFO, ztráta kontraktu, právní spor."),
    "4":     ("Insider transakce",       "insider",  "Nákup/prodej člověka z vedení firmy."),
    "3":     ("Nový insider",            "insider",  "Nový člen vedení / >10% akcionář."),
    "SC 13G":("Institucionální podíl",   "insider",  "Fond koupil >5% akcií (pasivně)."),
    "SC 13D":("Institucionální podíl",   "insider",  "Fond koupil >5% akcií (aktivně, může tlačit na změny)."),
    "DEF 14A":("Pozvánka na valnou hromadu","material","Odměny CEO, dividendy, akvizice."),
    "S-1":   ("Registrace nových akcií",  "material","Nová emise = ředění stávajících akcionářů."),
}


def _sec_lookup_cik(ticker):
    """Přeloží ticker → CIK (Central Index Key). Kešováno permanentně."""
    ticker = (ticker or "").upper()
    if not ticker:
        return None
    ck = f"sec:cik:{ticker}"
    cached = kv_get_json(ck)
    if cached and cached.get("cik"):
        return cached["cik"]
    try:
        r = requests.get("https://www.sec.gov/files/company_tickers.json",
                         headers=SEC_HEADERS, timeout=8)
        data = r.json() or {}
        for _, row in data.items():
            if (row.get("ticker") or "").upper() == ticker:
                cik = str(row.get("cik_str") or "").zfill(10)
                try: kv_set_json(ck, {"cik": cik, "name": row.get("title")})
                except Exception: pass
                return cik
    except Exception:
        return None
    return None


def _sec_recent_filings(ticker, limit=15):
    """Vrátí posledních N podání pro daný ticker přes SEC submissions API."""
    cik = _sec_lookup_cik(ticker)
    if not cik:
        return []
    ck = f"sec:sub:{cik}"
    cached = kv_get_json(ck)
    if cached and (time.time() - cached.get("ts", 0) < 4 * 3600):
        return cached.get("items", [])[:limit]
    try:
        r = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json",
                         headers=SEC_HEADERS, timeout=8)
        d = r.json() or {}
        recent = (d.get("filings") or {}).get("recent") or {}
        forms = recent.get("form") or []
        dates = recent.get("filingDate") or []
        accs  = recent.get("accessionNumber") or []
        prims = recent.get("primaryDocument") or []
        descs = recent.get("primaryDocDescription") or []
        items = []
        for i, form in enumerate(forms[:80]):
            if form not in _SEC_FORMS:
                continue
            acc_raw = accs[i] if i < len(accs) else ""
            acc = acc_raw.replace("-", "") if acc_raw else ""
            primary = prims[i] if i < len(prims) else ""
            url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{primary}" if acc and primary else ""
            info = _SEC_FORMS[form]
            items.append({
                "form": form,
                "form_cz": info[0],
                "kind": info[1],
                "hint": info[2],
                "date": dates[i] if i < len(dates) else "",
                "description": (descs[i] or "") if i < len(descs) else "",
                "url": url,
            })
        try: kv_set_json(ck, {"ts": int(time.time()), "items": items})
        except Exception: pass
        return items[:limit]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# EXTERNÍ RSS – Patria (CZ) + Motley Fool (EN) jako doplňkové novinkové zdroje
# ---------------------------------------------------------------------------
_RSS_SOURCES = {
    # Ověřeno curl smoke-testem 3.7.2026: každý zdroj reálně vrací <item> položky.
    "kurzy": {
        "label": "Kurzy.cz — akcie ČR",
        "flag": "🇨🇿",
        "url": "https://www.kurzy.cz/zpravy/util/forext.dat?type=rss&col=wzAkcieCR",
        "fallback": "https://www.kurzy.cz/zpravy/util/forext.dat?type=rss&col=wzAkcieSvet",
    },
    "kurzy_svet": {
        "label": "Kurzy.cz — akcie svět",
        "flag": "🇨🇿",
        "url": "https://www.kurzy.cz/zpravy/util/forext.dat?type=rss&col=wzAkcieSvet",
        "fallback": "https://www.kurzy.cz/zpravy/util/forext.dat?type=rss&col=wzMakro",
    },
    "roklen": {
        "label": "Roklen24",
        "flag": "🇨🇿",
        "url": "https://roklen24.cz/feed/",
        "fallback": "https://roklen24.cz/feed/",
    },
    "e15": {
        "label": "E15",
        "flag": "🇨🇿",
        "url": "https://www.e15.cz/rss",
        "fallback": "https://www.e15.cz/rss",
    },
    "idnes": {
        "label": "iDnes Ekonomika",
        "flag": "🇨🇿",
        "url": "https://servis.idnes.cz/rss.aspx?c=ekonomikah",
        "fallback": "https://servis.idnes.cz/rss.aspx?c=ekonomikah",
    },
    "fool": {
        "label": "Motley Fool",
        "flag": "🇺🇸",
        "url": "https://www.fool.com/a/feeds/rss/main.aspx",
        "fallback": "https://www.fool.com/feeds/index.aspx",
    },
    "cnbc": {
        "label": "CNBC",
        "flag": "🇺🇸",
        "url": "https://www.cnbc.com/id/10001147/device/rss/rss.html",
        "fallback": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    },
    "seekingalpha": {
        "label": "Seeking Alpha",
        "flag": "🇺🇸",
        "url": "https://seekingalpha.com/market_currents.xml",
        "fallback": "https://seekingalpha.com/feed.xml",
    },
    "marketwatch": {
        "label": "MarketWatch",
        "flag": "🇺🇸",
        "url": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
        "fallback": "http://feeds.marketwatch.com/marketwatch/topstories/",
    },
}


def _parse_rss(xml_data):
    """Minimalistický RSS/Atom parser – vrací list { title, link, pub_date, summary }.
    Vstup MUSÍ být bytes: feedy s deklarací encoding (např. windows-1250 u Kurzy.cz)
    ET.fromstring odmítá jako str ('Unicode strings with encoding declaration…')."""
    import xml.etree.ElementTree as ET
    if isinstance(xml_data, str):
        xml_data = xml_data.encode("utf-8", errors="replace")
    try:
        root = ET.fromstring(xml_data)
    except Exception:
        return []
    items = []
    ns_atom = "{http://www.w3.org/2005/Atom}"
    # RSS 2.0
    for it in root.iter("item"):
        t = (it.findtext("title") or "").strip()
        l = (it.findtext("link") or "").strip()
        p = (it.findtext("pubDate") or "").strip()
        d = (it.findtext("description") or "").strip()
        if t:
            items.append({"title": t, "link": l, "pub_date": p, "summary": d[:400]})
    # Atom fallback
    if not items:
        for it in root.iter(ns_atom + "entry"):
            t = (it.findtext(ns_atom + "title") or "").strip()
            l_el = it.find(ns_atom + "link")
            l = (l_el.get("href") if l_el is not None else "").strip()
            p = (it.findtext(ns_atom + "updated") or it.findtext(ns_atom + "published") or "").strip()
            d = (it.findtext(ns_atom + "summary") or "").strip()
            if t:
                items.append({"title": t, "link": l, "pub_date": p, "summary": d[:400]})
    return items[:40]


def _fetch_rss(src_key):
    """Načte RSS z definovaného zdroje. Kešováno 30 min."""
    src = _RSS_SOURCES.get(src_key)
    if not src:
        return []
    ck = f"rss:{src_key}"
    cached = kv_get_json(ck)
    # Prázdný výsledek necachujeme (a nedůvěřujeme mu) – po opravě URL zdroje
    # by jinak stará prázdná cache blokovala funkční feed až 30 minut.
    if cached and cached.get("items") and (time.time() - cached.get("ts", 0) < 1800):
        return cached.get("items", [])
    items = []
    for u in [src["url"], src["fallback"]]:
        try:
            r = requests.get(u, headers=HEADERS, timeout=8)
            items = _parse_rss(r.content)
            if items:
                break
        except Exception:
            continue
    for it in items:
        it["source"] = src["label"]
        it["flag"] = src["flag"]
    if not items:
        return []
    try:
        kv_set_json(ck, {"ts": int(time.time()), "items": items})
    except Exception:
        pass
    return items


def _ticker_synonyms(ticker):
    """Ke tickeru vrátí seznam řetězců, které v CZ/EN textu vypadají jako zmínka firmy."""
    ticker = (ticker or "").upper()
    synonyms = {ticker}
    # Krátký název – když ho známe z KV/backendu
    static_map = {
        "NVDA":  ["nvidia"],
        "AAPL":  ["apple"],
        "MSFT":  ["microsoft"],
        "GOOGL": ["alphabet", "google"], "GOOG": ["alphabet", "google"],
        "META":  ["meta", "facebook"],
        "AMZN":  ["amazon"],
        "TSLA":  ["tesla"],
        "AMD":   ["amd", "advanced micro"],
        "INTC":  ["intel"],
        "MA":    ["mastercard"],
        "V":     ["visa"],
        "JPM":   ["jpmorgan", "jp morgan"],
        "BAC":   ["bank of america"],
        "XOM":   ["exxon"],
        "CVX":   ["chevron"],
        "JNJ":   ["johnson & johnson", "johnson and johnson"],
        "PG":    ["procter & gamble", "procter and gamble"],
        "KO":    ["coca-cola", "coca cola"],
        "PEP":   ["pepsi"],
        "DIS":   ["disney"],
        "NFLX":  ["netflix"],
        "CEZ.PR":   ["čez", "cez"],
        "KOMB.PR":  ["komerční banka", "komercni banka", "komerčka"],
        "MONET.PR": ["moneta", "moneta money bank"],
        "PHILIP.PR":["philip morris", "philip morris čr"],
        "ERSTE.PR": ["erste", "erste group"],
        "KOFOL.PR": ["kofola"],
        "COLT.PR":  ["colt cz", "colt cz group", "csg"],
        "CETV.PR":  ["cetv", "central european media"],
        "FORT.PR":  ["fortuna"],
        "PRIM.PR":  ["primoco", "primoco uav"],
        "GEVO.PR":  ["gevorkyan"],
        "PILULKA.PR":["pilulka", "pilulka.cz"],
        "BAAERO":   ["aero vodochody"],
        "AVAST.PR": ["avast"],
    }
    for s in static_map.get(ticker, []):
        synonyms.add(s.lower())
    return list(synonyms)


# ---------------------------------------------------------------------------
# SJEDNOCENÝ NEWS FEED – Yahoo + všechny RSS zdroje sloučené do jednoho seznamu
# Legální: každá položka má odkaz zpět na originál (fair use, jako Google News).
# ---------------------------------------------------------------------------
def _source_domain(url):
    """Vytáhne doménu z URL pro decentní atribuci ('patria.cz', 'fool.com'...)."""
    if not url: return ""
    try:
        from urllib.parse import urlparse
        h = urlparse(url).hostname or ""
        h = h.lower()
        if h.startswith("www."): h = h[4:]
        if h.startswith("feeds."): h = h[6:]
        return h
    except Exception:
        return ""


def _parse_rss_time(pub):
    """Best-effort čas pubDate → unix timestamp; None při neúspěchu."""
    if not pub: return None
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(pub)
        if dt is not None:
            return int(dt.timestamp())
    except Exception:
        pass
    # ISO8601
    try:
        s = pub.strip().replace("Z", "+00:00")
        return int(datetime.fromisoformat(s).timestamp())
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# PORTFOLIO PERFORMANCE – equal-weighted index z tvé watchlist vs SPY baseline
# ---------------------------------------------------------------------------
@app.route("/api/portfolio-history")
def api_portfolio_history():
    """Vrátí denní kumulativní výkon watchlist (equal-weighted) vs SPY.
    Base = 100 v den [today - N]. Data pro rychlé vykreslení line chartu."""
    tickers = [t.strip().upper() for t in (request.args.get("tickers") or "").split(",") if t.strip()][:30]
    days = max(7, min(365, int(request.args.get("days", "90") or 90)))
    if not tickers:
        return jsonify({"ok": True, "series": [], "baseline": [], "labels": []})
    ck = f"pfh:{','.join(sorted(tickers))}:{days}"
    cached = kv_get_json(ck)
    if cached and (time.time() - cached.get("ts", 0) < 3600):
        return jsonify({"ok": True, "cached": True, **cached})

    # Rozhoduju období: pro 30 dní stačí 1mo, pro 90 3mo, pro 1 rok 1y
    if days <= 45:
        rng = "3mo"
    elif days <= 200:
        rng = "6mo"
    else:
        rng = "1y"

    # Sesbírej denní close pro každý ticker (unifikované na 'YYYY-MM-DD')
    per_ticker = {}
    for t in tickers:
        try:
            d = yahoo_chart(t, rng, "1d")
            ts_list = d.get("timestamp") or []
            closes = ((d.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
            gmt = (d.get("meta") or {}).get("gmtoffset", 0) or 0
            series = {}
            for i, ts in enumerate(ts_list):
                c = closes[i] if i < len(closes) else None
                if c is None: continue
                dt = datetime.fromtimestamp(ts + gmt, tz=timezone.utc).strftime("%Y-%m-%d")
                series[dt] = c
            per_ticker[t] = series
        except Exception:
            continue

    # SPY baseline
    spy_series = {}
    try:
        d = yahoo_chart("SPY", rng, "1d")
        ts_list = d.get("timestamp") or []
        closes = ((d.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        gmt = (d.get("meta") or {}).get("gmtoffset", 0) or 0
        for i, ts in enumerate(ts_list):
            c = closes[i] if i < len(closes) else None
            if c is None: continue
            dt = datetime.fromtimestamp(ts + gmt, tz=timezone.utc).strftime("%Y-%m-%d")
            spy_series[dt] = c
    except Exception:
        pass

    # Průnik dat: brát jen ty dny, kdy máme cenu pro SPY i alespoň polovinu tickerů
    all_dates = sorted(spy_series.keys())[-days:]
    if not all_dates:
        return jsonify({"ok": True, "series": [], "baseline": [], "labels": []})

    # Základní ceny (první dostupný datum) pro každý ticker
    firsts = {t: None for t in tickers}
    for t in tickers:
        s = per_ticker.get(t) or {}
        for dt in all_dates:
            v = s.get(dt)
            if v is not None:
                firsts[t] = v
                break

    spy_first = None
    for dt in all_dates:
        if spy_series.get(dt) is not None:
            spy_first = spy_series[dt]; break

    labels, series, baseline = [], [], []
    for dt in all_dates:
        # Equal-weighted: průměr (cena_dnes / cena_prvního_dne * 100) přes tickery
        vals = []
        for t in tickers:
            f = firsts.get(t)
            cur = (per_ticker.get(t) or {}).get(dt)
            if f and cur:
                vals.append(cur / f * 100.0)
        if not vals or spy_first is None or spy_series.get(dt) is None:
            continue
        labels.append(dt)
        series.append(round(sum(vals) / len(vals), 2))
        baseline.append(round(spy_series[dt] / spy_first * 100.0, 2))

    out = {
        "ts": int(time.time()),
        "labels": labels,
        "series": series,
        "baseline": baseline,
        "days": days,
        "tickers_used": len([t for t in tickers if firsts[t] is not None]),
    }
    if series and baseline:
        out["final_change_pct"] = round(series[-1] - 100, 2)
        out["baseline_change_pct"] = round(baseline[-1] - 100, 2)
        out["alpha_pct"] = round(out["final_change_pct"] - out["baseline_change_pct"], 2)
    try:
        kv_set_json(ck, out)
    except Exception:
        pass
    return jsonify({"ok": True, "cached": False, **out})


@app.route("/api/news-feed")
def api_news_feed():
    """Sjednocený feed: Yahoo (per-ticker) + RSS zdroje. Filtr podle tickeru,
    když je zadaný. Vrací jednotný tvar {title, link, source, source_domain, ts}."""
    ticker = (request.args.get("ticker") or "").upper()
    limit = max(5, min(40, int(request.args.get("limit", "25") or 25)))
    items = []

    # 1) Yahoo News (jen když je ticker) – nejrelevantnější k dané akci.
    # U pražských tickerů (.PR) je Yahoo search nespolehlivý (vrací generické
    # tiskovky GlobeNewswire/PR Newswire) → prosej výsledky přes synonyma firmy.
    if ticker:
        strict_syn = _ticker_synonyms(ticker) if ticker.endswith(".PR") else None
        try:
            r = requests.get(f"https://query2.finance.yahoo.com/v1/finance/search?q={quote(ticker)}&quotesCount=0&newsCount=12",
                             headers=HEADERS, timeout=6)
            data = r.json() or {}
            for n in (data.get("news") or [])[:12]:
                if not n.get("title"):
                    continue
                if strict_syn is not None:
                    hay = (n.get("title") or "").lower()
                    if not any(s in hay for s in strict_syn):
                        continue
                items.append({
                    "title": n.get("title"),
                    "link": n.get("link") or "",
                    "source": n.get("publisher") or "Yahoo Finance",
                    "source_domain": _source_domain(n.get("link") or "") or "finance.yahoo.com",
                    "ts": n.get("providerPublishTime") or None,
                })
        except Exception:
            pass

    # 2) Všechny RSS zdroje (filtrované na ticker synonyma, když je zadaný)
    syn = _ticker_synonyms(ticker) if ticker else []
    for key in _RSS_SOURCES.keys():
        try:
            rss = _fetch_rss(key)
        except Exception:
            continue
        for n in rss:
            if syn:
                hay = ((n.get("title") or "") + " " + (n.get("summary") or "")).lower()
                if not any(s in hay for s in syn):
                    continue
            items.append({
                "title": n.get("title"),
                "link": n.get("link") or "",
                "source": n.get("source") or _RSS_SOURCES[key]["label"],
                "source_domain": _source_domain(n.get("link") or ""),
                "ts": _parse_rss_time(n.get("pub_date")),
            })

    # Deduplikace podle URL/titulku
    seen = set()
    dedup = []
    for it in items:
        key = (it.get("link") or "") + "|" + (it.get("title") or "")
        if key in seen:
            continue
        seen.add(key)
        dedup.append(it)

    # Řazení: nejnovější první; položky bez data na konec
    now = int(time.time())
    dedup.sort(key=lambda x: -(x.get("ts") or 0))
    return jsonify({
        "ok": True,
        "count": len(dedup),
        "items": dedup[:limit],
        "updated": datetime.now(timezone.utc).strftime("%H:%M UTC"),
    })


@app.route("/api/rss")
def api_rss():
    """Vrací seznam novinek z externích zdrojů. src=patria|fool|all, ticker= filtr."""
    src = request.args.get("src", "all")
    ticker = (request.args.get("ticker") or "").upper()
    keys = [src] if src in _RSS_SOURCES else list(_RSS_SOURCES.keys())
    items = []
    for k in keys:
        items += _fetch_rss(k)
    if ticker:
        syn = _ticker_synonyms(ticker)
        def _match(it):
            hay = (it["title"] + " " + it.get("summary", "")).lower()
            return any(s in hay for s in syn)
        items = [it for it in items if _match(it)]
    # Nejnovější první
    items.sort(key=lambda x: x.get("pub_date") or "", reverse=True)
    return jsonify({"ok": True, "count": len(items), "items": items[:20],
                    "updated": datetime.now(timezone.utc).strftime("%H:%M UTC")})


# ---------------------------------------------------------------------------
# EARNINGS SURPRISE HISTORY – kolikrát firma překonala konsenzus (Finnhub free)
# ---------------------------------------------------------------------------
@app.route("/api/earnings/<path:ticker>")
def api_earnings_history(ticker):
    """Vrací poslední kvartální earnings (actual vs. estimate) + agregát beat/miss."""
    ticker = ticker.upper()
    ck = f"eh:{ticker}"
    cached = kv_get_json(ck)
    if cached and (time.time() - cached.get("ts", 0) < 24 * 3600):
        return jsonify({"ok": True, "cached": True, **cached})
    fk = os.environ.get("FINNHUB_KEY")
    if not fk:
        return jsonify({"ok": False, "error": "no_key"})
    try:
        r = requests.get("https://finnhub.io/api/v1/stock/earnings",
                         params={"symbol": ticker, "limit": 12, "token": fk},
                         timeout=6)
        raw = r.json() or []
    except Exception:
        return jsonify({"ok": False, "error": "fetch"})
    items = []
    beats = misses = inline = 0
    for x in (raw or [])[:8]:
        act = x.get("actual"); est = x.get("estimate")
        if act is None or est is None:
            continue
        surprise = act - est
        pct = (surprise / abs(est) * 100.0) if est else 0
        result = "beat" if surprise > 0 else ("miss" if surprise < 0 else "inline")
        if result == "beat": beats += 1
        elif result == "miss": misses += 1
        else: inline += 1
        items.append({
            "period": x.get("period"),
            "quarter": x.get("quarter"),
            "year": x.get("year"),
            "actual": act, "estimate": est,
            "surprise": round(surprise, 3),
            "surprise_pct": round(pct, 1),
            "result": result,
        })
    total = beats + misses + inline
    summary = {
        "beat_count": beats, "miss_count": misses, "inline_count": inline,
        "beat_rate": round(beats / total * 100.0, 1) if total else None,
        "sample": total,
    }
    out = {"ts": int(time.time()), "items": items, "summary": summary,
           "updated": datetime.now(timezone.utc).strftime("%H:%M UTC")}
    try: kv_set_json(ck, out)
    except Exception: pass
    return jsonify({"ok": True, "cached": False, **out})


# ---------------------------------------------------------------------------
# 13F – top institucionální držitelé (SEC EDGAR quarterly filings)
# Free, ale komplexnější parsing → agregujeme veřejné index-y společností.
# ---------------------------------------------------------------------------
# Vybraní top-tier správci majetku (Berkshire Hathaway, BlackRock, Vanguard, atd.)
# Sledujeme jejich CIK a v jejich 13F kontrolujeme, zda drží daný ticker.
_TOP_INSTITUTIONS = [
    ("0001067983", "Berkshire Hathaway", "Warren Buffett"),
    ("0001350694", "Bridgewater Associates", "Ray Dalio"),
    ("0001336528", "Renaissance Technologies", "Jim Simons"),
    ("0001745214", "Baupost Group", "Seth Klarman"),
    ("0001603466", "Duquesne Family Office", "Stanley Druckenmiller"),
    ("0001336528", "Renaissance Technologies", "Jim Simons"),
    ("0001061165", "BlackRock Fund Advisors", "BlackRock"),
    ("0000909832", "Vanguard Group", "Vanguard"),
    ("0001077114", "State Street Corp", "State Street"),
    ("0001709323", "Ark Invest", "Cathie Wood"),
    ("0001167483", "Fidelity Management", "Fidelity"),
    ("0001167483", "Fidelity Management & Research", "Fidelity"),
]


@app.route("/api/holders/<path:ticker>")
def api_top_holders(ticker):
    """Vrátí kdo z hlavních institucí drží danou akcii (best-effort z Finnhubu)."""
    ticker = ticker.upper()
    ck = f"holders:{ticker}"
    cached = kv_get_json(ck)
    if cached and (time.time() - cached.get("ts", 0) < 48 * 3600):
        return jsonify({"ok": True, "cached": True, **cached})
    fk = os.environ.get("FINNHUB_KEY")
    items = []
    if fk:
        try:
            # Finnhub má endpoint ownership pro top institucionální držitele
            r = requests.get("https://finnhub.io/api/v1/stock/ownership",
                             params={"symbol": ticker, "limit": 20, "token": fk},
                             timeout=6).json() or {}
            for h in (r.get("ownership") or []):
                items.append({
                    "name": h.get("name"),
                    "share": h.get("share"),
                    "change": h.get("change"),
                    "filing_date": h.get("filingDate"),
                    "portfolio_pct": h.get("portfolioPercent"),
                })
        except Exception:
            pass
    out = {"ts": int(time.time()), "items": items[:15],
           "updated": datetime.now(timezone.utc).strftime("%H:%M UTC")}
    try: kv_set_json(ck, out)
    except Exception: pass
    return jsonify({"ok": True, "cached": False, **out})


# ---------------------------------------------------------------------------
# ALERTS/EVENTS – souhrn klíčových událostí u tickerů (retenční feature)
# ---------------------------------------------------------------------------
def _events_for_ticker(ticker, since_days=10):
    """Pro daný ticker vrátí významné události za posledních N dní.
    Bereme: 8-K (materiální události), Form 4 (insider), 10-Q (nové výsledky),
    analytické revize (Finnhub upgrade grade) a nadcházející earnings."""
    from datetime import date as _date
    today = datetime.now(timezone.utc).date()
    cutoff_iso = (today - timedelta(days=since_days)).strftime("%Y-%m-%d")
    events = []

    # 1) SEC filings (8-K, Form 4, 10-Q)
    try:
        secs = _sec_recent_filings(ticker, limit=25)
        for it in secs:
            if not it.get("date") or it["date"] < cutoff_iso:
                continue
            if it["form"] == "8-K":
                events.append({"ticker": ticker, "date": it["date"], "kind": "material",
                               "icon": "ti-alert-triangle", "severity": "warn",
                               "title": "Materiální událost (8-K)",
                               "hint": "Akvizice, změna vedení, právní spor nebo jiné oznámení.",
                               "url": it.get("url", "")})
            elif it["form"] == "4":
                events.append({"ticker": ticker, "date": it["date"], "kind": "insider",
                               "icon": "ti-user-check", "severity": "good",
                               "title": "Insider transakce (Form 4)",
                               "hint": "Člen vedení nakoupil nebo prodal akcie.",
                               "url": it.get("url", "")})
            elif it["form"] == "10-Q":
                events.append({"ticker": ticker, "date": it["date"], "kind": "quarterly",
                               "icon": "ti-report", "severity": "info",
                               "title": "Kvartální výsledky (10-Q)",
                               "hint": "Čerstvá čísla za poslední kvartál.",
                               "url": it.get("url", "")})
    except Exception:
        pass

    # 2) Analytické revize (Finnhub upgrade/downgrade)
    fk = os.environ.get("FINNHUB_KEY")
    if fk:
        try:
            from_s = (today - timedelta(days=since_days)).strftime("%Y-%m-%d")
            to_s = today.strftime("%Y-%m-%d")
            r = requests.get("https://finnhub.io/api/v1/stock/upgrade-downgrade",
                             params={"symbol": ticker, "from": from_s, "to": to_s, "token": fk},
                             timeout=5).json() or []
            for u in (r or [])[:5]:
                gt = u.get("gradeTime")
                d = ""
                if isinstance(gt, (int, float)) and gt:
                    d = datetime.fromtimestamp(gt, tz=timezone.utc).strftime("%Y-%m-%d")
                if not d or d < cutoff_iso:
                    continue
                from_g = u.get("fromGrade") or "?"
                to_g = u.get("toGrade") or "?"
                comp = u.get("company") or "Analytik"
                action = (u.get("action") or "").lower()
                # up/main/down klasifikace
                is_up = "up" in action or (from_g and to_g and to_g.lower() > from_g.lower())
                events.append({"ticker": ticker, "date": d, "kind": "analyst",
                               "icon": "ti-users-group",
                               "severity": "good" if is_up else "warn",
                               "title": f"{comp}: {from_g} → {to_g}",
                               "hint": "Změna doporučení analytiků.",
                               "url": ""})
        except Exception:
            pass

        # 3) Blížící se earnings (do 7 dnů)
        try:
            to_s2 = (today + timedelta(days=14)).strftime("%Y-%m-%d")
            cal = requests.get("https://finnhub.io/api/v1/calendar/earnings",
                               params={"from": today.strftime("%Y-%m-%d"), "to": to_s2,
                                       "symbol": ticker, "token": fk}, timeout=5).json() or {}
            for e in (cal.get("earningsCalendar") or [])[:1]:
                d = e.get("date")
                if not d:
                    continue
                events.append({"ticker": ticker, "date": d, "kind": "earnings_upcoming",
                               "icon": "ti-calendar-event", "severity": "info",
                               "title": "Blížící se earnings",
                               "hint": "Výsledky hospodaření se blíží — očekávej volatilitu.",
                               "url": ""})
        except Exception:
            pass

    return events


@app.route("/api/alerts/events")
def api_alerts_events():
    """Vrátí souhrn klíčových událostí pro seznam tickerů. Cache 2h."""
    tickers_param = (request.args.get("tickers") or "").upper()
    since_days = max(1, min(30, int(request.args.get("days", "10") or 10)))
    tickers = [t.strip() for t in tickers_param.split(",") if t.strip()][:20]
    if not tickers:
        return jsonify({"ok": True, "events": []})
    ck = f"alerts:{','.join(tickers)}:{since_days}"
    cached = kv_get_json(ck)
    if cached and (time.time() - cached.get("ts", 0) < 7200):
        return jsonify({"ok": True, "cached": True, **cached})
    events = []
    for t in tickers:
        events += _events_for_ticker(t, since_days=since_days)
    events.sort(key=lambda x: x.get("date") or "", reverse=True)
    out = {"ts": int(time.time()), "events": events[:15],
           "updated": datetime.now(timezone.utc).strftime("%H:%M UTC")}
    try: kv_set_json(ck, out)
    except Exception: pass
    return jsonify({"ok": True, "cached": False, **out})


@app.route("/api/sec/<path:ticker>")
def api_sec(ticker):
    ticker = ticker.upper()
    items = _sec_recent_filings(ticker, limit=15)
    # Agregát: kolik insider nákupů/prodejů v posledních 90 dnech,
    # kolik 8-K, kdy poslední 10-Q (kvartál).
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=90)).strftime("%Y-%m-%d")
    ins90 = [i for i in items if i["kind"] == "insider" and i["date"] >= cutoff]
    mat90 = [i for i in items if i["kind"] == "material" and i["date"] >= cutoff]
    latest_10q = next((i for i in items if i["form"] == "10-Q"), None)
    latest_10k = next((i for i in items if i["form"] == "10-K"), None)
    summary = {
        "insider_90d": len(ins90),
        "material_90d": len(mat90),
        "latest_10q_date": latest_10q["date"] if latest_10q else None,
        "latest_10k_date": latest_10k["date"] if latest_10k else None,
    }
    return jsonify({"ok": True, "ticker": ticker, "items": items,
                    "summary": summary,
                    "updated": now.strftime("%H:%M UTC")})


@app.route("/api/exit-signals")
def exit_signals():
    """„Co prodávat teď" – projde zadané (watchlist) tituly a označí ty, které
    PRÁVĚ TEĎ spouští exit/oslabení podle našeho technického skóre. Symetrie
    k nákupním signálům, deterministicky (stejné skóre)."""
    if not _rate_ok("exitsig", 30, 60):
        return jsonify({"ok": True, "count": 0, "results": [], "checked": 0})
    tickers = [t.strip().upper() for t in request.args.get("tickers", "").split(",") if t.strip()][:20]
    items = []
    for t in tickers:
        try:
            daily = yahoo_chart(t, "1y", "1d")
            meta = daily.get("meta", {})
            closes = [c for c in (((daily.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []) if c is not None]
            if len(closes) < 210:
                continue
            ind = compute_indicators(closes, [])
            price = meta.get("regularMarketPrice") or closes[-1]
            score = tech_setup_score(price, ind)
            if score is None:
                continue
            sma200 = ind.get("sma200")
            rsi = ind.get("rsi")
            # POZOR: dřív tu bylo tvrdé „signál k výstupu" při skóre < 45.
            # Walk-forward test ukázal, že vystupovat kvůli zeslábnutí skóre
            # stojí -18,5 pb trefnosti – prodává se tím do slabosti na dně.
            # Z pozice se vystupuje podle plánu (cíl/stop/max dní), tohle je
            # jen informace o stavu titulu, ne pokyn k prodeji.
            if score < VERDICT_SELL:
                level, reason = "weak", "Technicky slabé – drž se plánu (cíl/stop)"
            elif sma200 and price < sma200:
                level, reason = "weak", "Oslabení dlouhodobého trendu"
            elif rsi is not None and rsi > 75:
                level, reason = "weak", "Překoupeno – zvaž výběr zisku"
            else:
                continue  # v pořádku → nezobrazovat
            prev = _prev_close(closes, meta) or price
            items.append({
                "ticker": t, "name": meta.get("shortName") or meta.get("longName") or t,
                "price": _round(price, 2), "currency": meta.get("currency", "USD"),
                "change_pct": _round(((price - prev) / prev * 100) if prev else 0, 2, 0),
                "score": score, "level": level, "reason": reason,
            })
        except Exception:
            continue
    items.sort(key=lambda x: x["score"])  # nejslabší první
    return jsonify({"ok": True, "count": len(items), "results": items, "checked": len(tickers)})


# ---------------------------------------------------------------------------
# Server-side AI (uživatel nezadává žádný klíč) – Elite plán
# ---------------------------------------------------------------------------
def _ai_gate(min_plan="elite"):
    """Vrátí (user, None) když má přístup, jinak (None, (response, status))."""
    user = _auth_user()
    if not user:
        return None, (jsonify({"ok": False, "error": "Přihlas se."}), 401)
    if not analysis_enabled():
        return None, (jsonify({"ok": False, "error": "Analytický motor zatím není nastavený."}), 503)
    rec = kv_get_json(f"user:{user}")
    if user not in admin_users() and PLAN_RANK.get(effective_plan(rec), -1) < PLAN_RANK[min_plan]:
        return None, (jsonify({"ok": False, "error": f"Tato funkce je v plánu {min_plan.capitalize()}.", "upgrade": True}), 402)
    return user, None


@app.route("/api/ai/brief", methods=["POST"])
def ai_brief():
    user, err = _ai_gate("elite")
    if err:
        return err
    stocks = (request.get_json(silent=True) or {}).get("stocks") or []
    compact = [{"t": s.get("ticker"), "price": s.get("price"), "chg": s.get("change_pct")} for s in stocks][:20]
    prompt = ("Jsi tržní analytik. Data sledovaných akcií: " + json.dumps(compact, ensure_ascii=False) +
              '. Vrať POUZE validní JSON: {"summary":"2 věty o dnešní náladě trhu a portfoliu",'
              '"top_picks":[{"ticker":"X","reason":"krátký pádný důvod k nákupu"}]}. Max 2 tipy. Česky.')
    out = call_llm(prompt)
    if not isinstance(out, dict):
        return jsonify({"ok": False, "error": "Nepodařilo se, zkus to znovu."}), 502
    return jsonify({"ok": True, "summary": out.get("summary", ""), "top_picks": out.get("top_picks", [])})


@app.route("/api/ai/eval", methods=["POST"])
def ai_eval():
    user, err = _ai_gate("elite")
    if err:
        return err
    stocks = (request.get_json(silent=True) or {}).get("stocks") or []
    compact = [{"t": s.get("ticker"), "price": s.get("price"), "chg": s.get("change_pct")} for s in stocks][:20]
    prompt = ("Jsi portfolio manažer. Data sledovaných akcií: " + json.dumps(compact, ensure_ascii=False) +
              '. Vrať POUZE validní JSON: {"summary":"2 věty zhodnocení",'
              '"sell":[{"ticker":"X","reason":"proč zvážit prodej","urgency":"IHNED nebo ZVÁŽIT"}],'
              '"buy":[{"ticker":"X","reason":"proč přikoupit/koupit","upside":"+15%"}]}. Max 3 v každé sekci. Česky.')
    out = call_llm(prompt)
    if not isinstance(out, dict):
        return jsonify({"ok": False, "error": "Nepodařilo se, zkus to znovu."}), 502
    return jsonify({"ok": True, "summary": out.get("summary", ""),
                    "sell": out.get("sell", []), "buy": out.get("buy", [])})


@app.route("/api/ai/chat", methods=["POST"])
def ai_chat():
    user, err = _ai_gate("elite")
    if err:
        return err
    body = request.get_json(silent=True) or {}
    context = str(body.get("context", ""))[:1500]
    history = body.get("history") or []
    q = str(body.get("q", ""))[:500]
    prompt = (f"Jsi stručný investiční asistent. Kontext o akcii: {context}\n"
              f"Historie: {json.dumps(history[-6:], ensure_ascii=False)}\n"
              f"Odpověz česky, konkrétně a MAX 4 věty na dotaz: \"{q}\". "
              "Nepoužívej markdown ani hvězdičky. Na konci krátce připomeň, že nejde o investiční doporučení.")
    answer = call_llm(prompt, json_mode=False)
    if not answer:
        return jsonify({"ok": False, "error": "Nepodařilo se, zkus to znovu."}), 502
    return jsonify({"ok": True, "answer": answer})


# ---------------------------------------------------------------------------
# SEO STRÁNKY PER TICKER – veřejná landing page pro každou akcii
# Google ji indexuje → organický traffic hledající "NVDA analýza" apod.
# ---------------------------------------------------------------------------
APP_URL = os.environ.get("APP_URL", "https://myadvantage.site").rstrip("/")


def _html_escape(s):
    if s is None: return ""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _render_seo_stock(ticker):
    """Vrátí SEO-optimizovanou HTML stránku pro daný ticker."""
    ticker = (ticker or "").upper()
    if not ticker or not all(c.isalnum() or c in "-.^=" for c in ticker):
        return "<h1>Ticker není platný</h1>", 400
    # Data (fault-tolerant)
    name = ticker
    price = None
    prev = None
    chg_pct = 0
    currency = "USD"
    sector = ""
    industry = ""
    try:
        d = yahoo_chart(ticker, "1d", "1d")
        meta = d.get("meta", {}) or {}
        name = meta.get("shortName") or meta.get("longName") or ticker
        price = meta.get("regularMarketPrice")
        prev = _prev_close([], meta) or price
        chg_pct = ((price - prev) / prev * 100.0) if (price and prev) else 0
        currency = meta.get("currency", "USD")
    except Exception:
        pass
    fk = os.environ.get("FINNHUB_KEY")
    pe = mcap = None
    if fk:
        try:
            prof = requests.get("https://finnhub.io/api/v1/stock/profile2",
                                params={"symbol": ticker, "token": fk}, timeout=4).json() or {}
            m = (requests.get("https://finnhub.io/api/v1/stock/metric",
                              params={"symbol": ticker, "metric": "all", "token": fk}, timeout=4).json() or {}).get("metric", {}) or {}
            industry = prof.get("finnhubIndustry", "")
            pe = m.get("peTTM")
            mcap = prof.get("marketCapitalization")
            if not name or name == ticker:
                name = prof.get("name") or name
        except Exception:
            pass

    chg_col = "#00E676" if chg_pct >= 0 else "#FF3D00"
    chg_sign = "+" if chg_pct >= 0 else ""
    price_str = f"{price:.2f}" if isinstance(price, (int, float)) else "—"
    pe_str = f"{pe:.1f}" if isinstance(pe, (int, float)) else "—"
    if isinstance(mcap, (int, float)) and mcap:
        mcap_str = (f"{mcap/1000:.1f} B $" if mcap >= 1000 else f"{int(mcap)} M $")
    else:
        mcap_str = "—"

    title = f"{ticker} — {name} | Analýza akcie | MY ADVANTAGE"
    description = (
        f"Aktuální analýza akcie {name} ({ticker}): cena {price_str} {currency}, "
        f"P/E {pe_str}, tržní kap. {mcap_str}. Ověřený nákupní signál, hloubková analýza, "
        f"stop-loss plán. Verdikt ze 6 datových pilířů."
    )
    canonical = f"{APP_URL}/stock/{ticker}"
    og_image = f"{APP_URL}/logo/icon-512.png"

    # JSON-LD structured data pro Google
    jsonld = json.dumps({
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": f"Analýza akcie {name} ({ticker})",
        "description": description,
        "author": {"@type": "Organization", "name": "MY ADVANTAGE"},
        "publisher": {"@type": "Organization", "name": "MY ADVANTAGE",
                      "logo": {"@type": "ImageObject", "url": og_image}},
        "dateModified": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }, ensure_ascii=False)

    html = f"""<!doctype html>
<html lang="cs">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_html_escape(title)}</title>
<meta name="description" content="{_html_escape(description)}">
<link rel="canonical" href="{canonical}">
<meta property="og:type" content="article">
<meta property="og:title" content="{_html_escape(title)}">
<meta property="og:description" content="{_html_escape(description)}">
<meta property="og:url" content="{canonical}">
<meta property="og:image" content="{og_image}">
<meta name="twitter:card" content="summary_large_image">
<meta name="theme-color" content="#FF7A00">
<link rel="icon" href="/logo/icon-32.png">
<script type="application/ld+json">{jsonld}</script>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600&family=Space+Grotesk:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
:root {{ --bg:#050507; --elev:#0e1015; --border:#1e2028; --border-a:#FF7A00; --accent:#FF7A00; --text:#f0f0f0; --text2:#8a8f99; --up:#00E676; --down:#FF3D00; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font-family:'Outfit',sans-serif; line-height:1.55; }}
.wrap {{ max-width:960px; margin:0 auto; padding:24px 20px 60px; }}
.brand {{ display:flex; align-items:center; gap:10px; font-family:'Space Grotesk',sans-serif; font-weight:800; font-size:22px; letter-spacing:.5px; margin-bottom:26px; }}
.brand .b {{ width:36px; height:36px; background:var(--accent); border-radius:11px; }}
.brand span {{ color:var(--accent); }}
h1 {{ font-family:'Space Grotesk',sans-serif; font-size:44px; font-weight:800; margin:0 0 4px; letter-spacing:-.5px; }}
h1 small {{ display:block; font-size:16px; font-weight:500; color:var(--text2); margin-top:8px; }}
.hero {{ background:var(--elev); border:1px solid var(--border); border-radius:22px; padding:28px 32px; margin-bottom:22px; }}
.px {{ display:flex; align-items:baseline; gap:18px; margin-top:16px; flex-wrap:wrap; }}
.px .p {{ font-family:'Space Grotesk',sans-serif; font-weight:800; font-size:38px; }}
.px .c {{ font-family:'Space Grotesk',sans-serif; font-weight:700; font-size:18px; padding:5px 14px; border-radius:12px; background:{'rgba(0,230,118,0.14)' if chg_pct >= 0 else 'rgba(255,61,0,0.14)'}; color:{chg_col}; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin-top:20px; }}
.cell {{ background:rgba(255,255,255,0.03); border:1px solid var(--border); border-radius:12px; padding:12px 14px; }}
.cell .l {{ font-size:11px; color:var(--text2); text-transform:uppercase; letter-spacing:.6px; font-weight:600; }}
.cell .v {{ font-family:'Space Grotesk',sans-serif; font-weight:700; font-size:18px; margin-top:3px; }}
.cta {{ margin-top:24px; text-align:center; }}
.cta a {{ display:inline-flex; align-items:center; gap:8px; background:var(--accent); color:#1a0e00; padding:14px 28px; font-family:'Space Grotesk',sans-serif; font-weight:700; text-decoration:none; border-radius:14px; font-size:16px; transition:transform .15s, box-shadow .2s; }}
.cta a:hover {{ transform:translateY(-2px); box-shadow:0 12px 28px rgba(255,122,0,0.35); }}
h2 {{ font-family:'Space Grotesk',sans-serif; font-size:24px; margin:38px 0 14px; }}
p {{ color:#c9ccd3; }}
.pill-row {{ display:flex; gap:8px; flex-wrap:wrap; margin:12px 0 4px; }}
.pill {{ background:var(--elev); border:1px solid var(--border); border-radius:999px; padding:8px 14px; font-size:13px; color:var(--text2); }}
.pillar {{ background:var(--elev); border:1px solid var(--border); border-radius:14px; padding:16px 18px; margin-bottom:10px; }}
.pillar b {{ color:var(--accent); }}
.foot {{ text-align:center; color:var(--text2); font-size:12px; margin-top:44px; padding-top:20px; border-top:1px solid var(--border); }}
</style>
</head>
<body>
<div class="wrap">
  <div class="brand"><div class="b"></div>MY <span>ADVANTAGE</span></div>

  <div class="hero">
    <h1>{_html_escape(ticker)} <small>{_html_escape(name)}</small></h1>
    <div class="px">
      <div class="p">{price_str} <span style="font-size:16px;color:var(--text2);font-weight:600;">{_html_escape(currency)}</span></div>
      <div class="c">{chg_sign}{chg_pct:.2f}%</div>
    </div>
    <div class="grid">
      <div class="cell"><div class="l">P/E</div><div class="v">{pe_str}</div></div>
      <div class="cell"><div class="l">Tržní kap.</div><div class="v">{mcap_str}</div></div>
      <div class="cell"><div class="l">Sektor</div><div class="v" style="font-size:14px">{_html_escape(industry or '—')}</div></div>
    </div>
    <div class="cta">
      <a href="/?ticker={_html_escape(ticker)}">🚀 Otevřít plnou analýzu v aplikaci</a>
    </div>
  </div>

  <h2>Naše analýza akcie {_html_escape(name)}</h2>
  <p>MY ADVANTAGE vydává verdikt <b>Koupit / Držet / Prodat</b> ze 6 datových pilířů. Nakupujeme <em>slevu v uptrendu</em> — nechytáme padající nůž. U každého obchodu počítáme stop-loss.</p>

  <div class="pill-row">
    <span class="pill">✅ Ověřeno backtestem 5 let</span>
    <span class="pill">🎯 ~65 % trefnost TOP signálu</span>
    <span class="pill">📊 6 datových pilířů</span>
    <span class="pill">🛡️ Stop-loss u každého obchodu</span>
  </div>

  <h2>Ze kterých pilířů skládáme verdikt</h2>
  <div class="pillar"><b>1. Technika (30 %)</b> — nákup poklesu v dlouhodobém uptrendu, žádné chytání padajícího nože.</div>
  <div class="pillar"><b>2. Valuace (20 %)</b> — jestli je akcie levná nebo drahá vůči svým ziskům (P/E, P/B).</div>
  <div class="pillar"><b>3. Růst & ziskovost (25 %)</b> — roste firma, má marže, reálně vydělává?</div>
  <div class="pillar"><b>4. Analytici (22 %)</b> — konsenzus Wall Street a prostor k cílové ceně.</div>
  <div class="pillar"><b>5. Nálada &amp; instituce (13 %)</b> — sentiment titulků, insider transakce, revize analytiků.</div>
  <div class="pillar"><b>6. SEC podání (10 %)</b> — oficiální americká data: Form 4 (insider), 10-Q, 8-K.</div>

  <h2>Chceš vidět aktuální verdikt pro {_html_escape(ticker)}?</h2>
  <p>Otevři plnou hloubkovou analýzu s cenou vstupu, cílem, stop-lossem, scénáři a kompletním rozborem.</p>
  <div class="cta">
    <a href="/?ticker={_html_escape(ticker)}">Otevřít analýzu {_html_escape(ticker)} →</a>
  </div>

  <div class="foot">
    MY ADVANTAGE je nástroj pro vzdělávací a informační účely. Neposkytuje investiční poradenství.
    Historická výkonnost nezaručuje budoucí výsledky.
  </div>
</div>
</body>
</html>"""
    return html, 200


@app.route("/stock/<path:ticker>")
def seo_stock(ticker):
    html, status = _render_seo_stock(ticker)
    resp = make_response(html, status)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    resp.headers["Cache-Control"] = "public, max-age=600, s-maxage=3600"
    return resp


# Vybraný seznam populárních tickerů pro sitemap
_SITEMAP_TICKERS = [
    # US Blue-chip
    "AAPL","MSFT","GOOGL","AMZN","NVDA","META","TSLA","BRK.B","JPM","V",
    "MA","JNJ","XOM","WMT","PG","LLY","AVGO","HD","MRK","KO","PEP","BAC",
    "ABBV","CVX","CRM","AMD","INTC","NFLX","ADBE","DIS","PYPL","T","VZ",
    "QCOM","IBM","GS","MS","BA","CAT","GE","NKE","MCD","SBUX","COST","UNH",
    "PFE","CSCO","ORCL","BABA","SHOP","SQ","PLTR","COIN","RIVN","LCID",
    # ETF
    "SPY","QQQ","VOO","VTI","VTV","IWM","EFA","EEM",
    # CZ akcie (BCPP) – silně pro CZ SEO
    "CEZ.PR","KOMB.PR","MONET.PR","PHILIP.PR","ERSTE.PR","KOFOL.PR",
    "COLT.PR","CETV.PR","FORT.PR","PRIM.PR","GEVO.PR",
    # Evropské big names
    "SAP.DE","SIE.DE","ALV.DE","MBG.DE","ASML.AS","ULVR.L","BP.L","SHEL.L",
]


# ---------------------------------------------------------------------------
# BLOG – edukační obsah pro SEO + budování důvěry
# Články jako plain-text data uvnitř modulu. Žádná DB nutná.
# ---------------------------------------------------------------------------
_BLOG_POSTS = [
    {
        "slug": "co-je-stop-loss",
        "title": "Co je stop-loss a proč ho vždycky používat",
        "excerpt": "Ochranný prodejní příkaz, který ti může zachránit portfolio. Vysvětlujeme, jak ho nastavit, na kolika procentech, a proč bez něj obchodovat nedávej smysl.",
        "keywords": "stop-loss, ochrana kapitálu, risk management, investování",
        "date": "2026-06-20",
        "body": """
Stop-loss je jednoduchá věc: řekneš „prodej mi to, když cena klesne na X". A brokera to udělá automaticky, i když zrovna spíš.

Bez stop-lossu obchodovat NEDÁVÁ smysl. Tady je proč:

**1. Emoce jsou horší nepřítel než trh.** Když ti akcie padá o 20 %, řekneš si „počkám, ono to poroste". Pak padá o 40 %, 60 %. Necháš to. Stop-loss udělá to, co bys měl udělat ty, ale nedokážeš.

**2. Statistika je krutá.** Když ztratíš 50 %, musíš získat 100 %, abys byl na nule. Když ztratíš 20 %, stačí +25 %. Rozdíl je obrovský.

**3. Malé ztráty jsou přijatelné, velké tě zabijí.** Profi obchodník má win-rate 55–65 %. Ne 90 %. Rozdíl mezi ním a začátečníkem: **ztráty drží malé**.

**Kolik nastavit?**
- Volatilní akcie (NVDA, TSLA): 8–12 %
- Stabilní blue-chip (JNJ, KO): 4–6 %
- V MY ADVANTAGE počítáme stop-loss individuálně podle volatility (ATR)

**Position sizing** je s tím párový nástroj. Když máš kapitál 100 000 Kč, obchod s 5% stop-lossem a riskuješ max 2 % kapitálu, koupíš tolik akcií, abys při stop-lossu ztratil přesně 2 000 Kč. Ne víc.

Zkus si to spočítat v naší kalkulačce **„Kolik koupit?"** — je zdarma v tool baru.
""",
    },
    {
        "slug": "jak-cist-pe-ratio",
        "title": "Jak číst P/E ratio — a kdy tě oklame",
        "excerpt": "P/E je nejznámější ukazatel valuace. Ale sám o sobě ti neřekne skoro nic. Naučíme tě, kdy je nízké P/E past a kdy vysoké P/E dává smysl.",
        "keywords": "P/E ratio, valuace, ocenění, fundamenty",
        "date": "2026-06-22",
        "body": """
P/E = cena akcie ÷ zisk na akcii (EPS). Říká, kolik let firma poroste, abys dostal zpátky svou investici (pokud zisk zůstane stejný).

**Jednoduchý pohled:**
- P/E 10 = akcie je „levná"
- P/E 30 = akcie je „drahá"

**Realita:**
- P/E 10 může znamenat firmu v úpadku (trh čeká pokles zisků)
- P/E 30 může být fér cena pro firmu, která roste 25 % ročně

**Kdy je nízké P/E past?**
- Cyklické firmy na vrcholu cyklu (banky, aerolinky, těžaři): P/E vypadá nízké, ale zisky jsou dočasné.
- Firmy před bankrotem — trh cenu srazí, ale zisk zatím ještě je z předchozích let.
- Účetní triky — jednorázový příjem nadhodnotil zisk.

**Kdy je vysoké P/E fér?**
- Rychle rostoucí SaaS firma (30 %+ růstu tržeb) → P/E 40 může být levné.
- Kvalitní franchisa (Costco, Visa) → trh platí prémium za spolehlivost.

**Lepší ukazatel: PEG** — P/E dělené růstem zisků. PEG < 1 = obvykle podhodnocené vůči růstu.

**Náš přístup v MY ADVANTAGE:**
- P/E je jen 1 ze 4 subskóre ve valuačním pilíři (20 % váha)
- Vždycky se díváme na kontext: sektor, růst tržeb, ROE
- Nikdy nekupujeme jen „levné" P/E — musí být v uptrendu a s kvalitními fundamenty
""",
    },
    {
        "slug": "6-piliru-analyzy",
        "title": "6 pilířů naší analýzy — jak počítáme verdikt",
        "excerpt": "Naše doporučení Koupit/Držet/Prodat není odhad. Je to matematický průměr 6 nezávislých datových pilířů. Ukazujeme, co v každém je.",
        "keywords": "MA Skóre, verdikt, backtest, datová analýza",
        "date": "2026-06-25",
        "body": """
Každá akcie v MY ADVANTAGE dostane skóre 0–100 a verdikt (Koupit ≥ 70, Držet 50–69, Prodat < 50). Skóre se počítá deterministicky ze 6 pilířů:

**1. Technika (váha 30 %)**
Nákup poklesu v dlouhodobém uptrendu. Klíčové indikátory: SMA 50/200 relace, RSI, volatilita, momentum. Backtestem ověřeno — ~65 % TOP signálů skončí v plusu.

**2. Valuace (váha 20 %)**
P/E, P/B, P/S vůči sektoru a historii. Levná akcie s růstem = ideál.

**3. Růst a ziskovost (váha 25 %)**
Roste firma? Marže? ROE > 15 %? Cash-flow pozitivní?

**4. Analytici (váha 22 %)**
Konsenzus 30+ investičních bank. Cílová cena. Nedávné upgrades/downgrades.

**5. Nálada & instituce (váha 13 %)**
Sentiment titulků, insider transakce (Form 4), revize analytiků.

**6. SEC podání (váha 10 %)**
Oficiální americká data: čerstvost 10-Q, insider nákupy, materiální 8-K události.

**Jak se váhy přepočítávají:**
Když některý pilíř nemá data (např. neamerická akcie nemá SEC), zbývající pilíře se proporčně převáží. Suma vždy = 100 %.

**Jistota (confidence)**
- Pokrytí daty (0.4× váha)
- Shoda pilířů (0.35× — jsou všechny za "Koupit"?)
- Odstup od 50 (0.25×)

Před blížícími se earnings snižujeme jistotu o 18 % — je to událost s vysokou volatilitou.

**Co NEDĚLÁ verdikt:**
- Není to garance zisku
- Není to timing pro daytrading (horizont do 6 týdnů)
- Není to nahrazení tvého vlastního úsudku
""",
    },
    {
        "slug": "co-je-short-interest",
        "title": "Co je short interest a jak z něj poznat short squeeze",
        "excerpt": "Když víc než 20 % akcií drží short prodejci, může vypuknout squeeze — cena vystřelí. Vysvětlujeme mechaniku a jak to použít.",
        "keywords": "short interest, short squeeze, GME, retail investing",
        "date": "2026-06-27",
        "body": """
**Short pozice** = spekulace na pokles. Prodejce si akcii půjčí od makléře, hned ji prodá, a doufá, že ji později koupí levněji. Rozdíl si nechá.

**Short interest** = kolik % free-floatu (volně obchodovaných akcií) je právě „shortnutých".

**Kdy to začíná být zajímavé:**
- Short interest > 20 %: napjatá situace
- > 30 %: potenciál na short squeeze
- > 50 %: extrémní (viz GameStop 2021)

**Short squeeze:**
Když cena stoupá, shortaři musí nakoupit zpět, aby zavřeli pozici. Nákup zvedne cenu ještě víc. Panika. Cena vyletí desetkrát nahoru za pár dní.

**Short ratio (days to cover)**
Kolik dní by trvalo pokrýt všechny shorty při průměrném objemu obchodů. Vysoké číslo = potenciál na squeeze.

**Kdy short interest naopak varuje:**
- Wall Street chytří lidé vsadili proti — často mají důvod.
- Pokud firma nemá katalyzátor (novinky, výsledky), cena zůstane pod tlakem.

**Náš přístup:**
V detailu akcie ukazujeme oba údaje. Nezapočítáváme je přímo do verdiktu (je to spíš kontrariánsky signál), ale je to informace na kontext.
""",
    },
    {
        "slug": "jak-cist-sec-filings",
        "title": "SEC filings pro začátečníky — 10-K, 10-Q, 8-K, Form 4",
        "excerpt": "Oficiální podání americké komise pro cenné papíry ti řeknou vše, co CEO říct musí. Naučíme se je číst.",
        "keywords": "SEC EDGAR, 10-K, insider trading, Form 4",
        "date": "2026-06-29",
        "body": """
SEC (Securities and Exchange Commission) je americký regulátor cenných papírů. Každá veřejně obchodovaná firma v USA musí do SEC podávat oficiální dokumenty. Všechny jsou **zdarma dostupné** na sec.gov.

**10-K — Výroční zpráva**
Kompletní roční audit. Detailní fundamenty, rizika, konkurence, management. Nejdůležitější dokument roku. Vyplácí se přečíst sekce „Risk Factors".

**10-Q — Kvartální zpráva**
Neauditovaná čísla za kvartál. Rychlý pohled na tržby, marže, cash-flow. Přichází ~45 dní po konci kvartálu.

**8-K — Materiální událost**
Firma musí do 4 pracovních dnů oznámit, když se stane něco významného: akvizice, odchod CFO, žaloba, změna auditora, ztráta velkého kontraktu.

**Form 4 — Insider transakce**
Když člen vedení nebo významný akcionář nakupuje/prodává akcie firmy. Signifikantní signál: nákupy = důvěra, prodeje = varování (ne vždy, mohou být z osobních důvodů).

**SC 13D / 13G — Institucionální podíly**
Když někdo koupí > 5 % akcií firmy. 13D = aktivní (chce ovlivnit), 13G = pasivní.

**DEF 14A — Pozvánka na valnou hromadu**
Detail odměn CEO, plánované akvizice, návrhy na hlasování.

**Náš přístup v MY ADVANTAGE:**
- SEC data tvoří 6. pilíř verdiktu (10 % váha)
- V detailu akcie máš záložku „SEC podání" s posledními 15 filings
- Barevné odlišení podle důležitosti
- Klik = originál na sec.gov

Reálná hodnota: prodejci na Wall Street mají celé týmy, které SEC filings čtou hodinu po podání. Ty teď máš to samé.
""",
    },
]

_BLOG_INDEX = {p["slug"]: p for p in _BLOG_POSTS}


def _render_blog_shell(inner_html, title, description, canonical, extra_head=""):
    return f"""<!doctype html>
<html lang="cs">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_html_escape(title)}</title>
<meta name="description" content="{_html_escape(description)}">
<link rel="canonical" href="{canonical}">
<meta property="og:type" content="article">
<meta property="og:title" content="{_html_escape(title)}">
<meta property="og:description" content="{_html_escape(description)}">
<meta property="og:image" content="{APP_URL}/logo/icon-512.png">
<meta name="twitter:card" content="summary_large_image">
<meta name="theme-color" content="#FF7A00">
<link rel="icon" href="/logo/icon-32.png">
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600&family=Space+Grotesk:wght@400;600;700;800&display=swap" rel="stylesheet">
{extra_head}
<style>
:root {{ --bg:#050507; --elev:#0e1015; --border:#1e2028; --accent:#FF7A00; --text:#f0f0f0; --text2:#8a8f99; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font-family:'Outfit',sans-serif; line-height:1.7; }}
.wrap {{ max-width:760px; margin:0 auto; padding:24px 20px 60px; }}
.brand {{ display:flex; align-items:center; gap:10px; font-family:'Space Grotesk',sans-serif; font-weight:800; font-size:20px; letter-spacing:.5px; margin-bottom:26px; text-decoration:none; color:var(--text); }}
.brand .b {{ width:32px; height:32px; background:var(--accent); border-radius:9px; }}
.brand span {{ color:var(--accent); }}
h1 {{ font-family:'Space Grotesk',sans-serif; font-size:38px; font-weight:800; margin:24px 0 8px; letter-spacing:-.5px; line-height:1.15; }}
h2 {{ font-family:'Space Grotesk',sans-serif; font-size:24px; margin:32px 0 10px; }}
h3 {{ font-family:'Space Grotesk',sans-serif; font-size:18px; margin:22px 0 8px; }}
p, li {{ color:#c9ccd3; font-size:16px; }}
p {{ margin:12px 0; }}
b {{ color:var(--text); }}
.meta {{ color:var(--text2); font-size:13px; margin-bottom:22px; }}
.card {{ display:block; background:var(--elev); border:1px solid var(--border); border-radius:14px; padding:20px 22px; margin-bottom:12px; text-decoration:none; color:var(--text); transition:border-color .18s;}}
.card:hover {{ border-color:var(--accent); }}
.card h3 {{ margin:0 0 6px; color:var(--text); }}
.card p {{ margin:0; color:var(--text2); font-size:14px; }}
.card .date {{ font-size:11px; color:var(--accent); letter-spacing:.5px; font-weight:700; text-transform:uppercase; margin-bottom:8px; }}
.cta {{ margin:36px 0 12px; text-align:center; }}
.cta a {{ display:inline-flex; align-items:center; gap:8px; background:var(--accent); color:#1a0e00; padding:14px 28px; font-family:'Space Grotesk',sans-serif; font-weight:700; text-decoration:none; border-radius:14px; font-size:16px; }}
.foot {{ text-align:center; color:var(--text2); font-size:12px; margin-top:44px; padding-top:20px; border-top:1px solid var(--border); }}
.back {{ color:var(--text2); font-size:13px; text-decoration:none; }}
.back:hover {{ color:var(--accent); }}
</style>
</head>
<body>
<div class="wrap">
<a class="brand" href="/"><div class="b"></div>MY <span>ADVANTAGE</span></a>
{inner_html}
<div class="cta">
  <a href="/">🚀 Vyzkoušej MY ADVANTAGE</a>
</div>
<div class="foot">
  MY ADVANTAGE je nástroj pro vzdělávací a informační účely. Neposkytuje investiční poradenství.
</div>
</div>
</body>
</html>"""


def _md_to_html(md):
    """Velmi jednoduchý převodník: **bold**, řádkové paragrafy, seznamy."""
    lines = md.strip().split("\n")
    out = []
    para = []
    for ln in lines:
        s = ln.rstrip()
        if not s:
            if para:
                out.append("<p>" + " ".join(para) + "</p>")
                para = []
            continue
        # Nadpis: řádek začíná **X** a končí **
        if s.startswith("**") and s.endswith("**") and s.count("**") == 2:
            if para:
                out.append("<p>" + " ".join(para) + "</p>"); para = []
            out.append("<h3>" + s.strip("*") + "</h3>")
            continue
        # Bod seznamu
        if s.startswith("- "):
            if para:
                out.append("<p>" + " ".join(para) + "</p>"); para = []
            item = s[2:]
            item = item.replace("**", "§§")
            # inline bold
            while "§§" in item:
                item = item.replace("§§", "<b>", 1).replace("§§", "</b>", 1)
            out.append("<li>" + item + "</li>")
            continue
        # inline bold + p
        s2 = s.replace("**", "§§")
        while "§§" in s2:
            s2 = s2.replace("§§", "<b>", 1).replace("§§", "</b>", 1)
        para.append(s2)
    if para:
        out.append("<p>" + " ".join(para) + "</p>")
    # obal seznamy
    joined = "\n".join(out)
    # jednoduše: <li>… ihned za sebou zabalit do <ul>
    import re as _re
    joined = _re.sub(r"(<li>[\s\S]*?</li>)(\s*<li>[\s\S]*?</li>)*",
                     lambda m: "<ul>" + m.group(0) + "</ul>", joined)
    return joined


@app.route("/blog")
def blog_index():
    inner = ["<h1>Blog — jak přemýšlet o investování</h1>",
             "<p class='meta'>Praktické tipy, jak číst čísla, chránit kapitál a nedělat začátečnické chyby.</p>"]
    for p in _BLOG_POSTS:
        inner.append(
            f"<a class='card' href='/blog/{p['slug']}'>"
            f"<div class='date'>{p['date']}</div>"
            f"<h3>{_html_escape(p['title'])}</h3>"
            f"<p>{_html_escape(p['excerpt'])}</p>"
            f"</a>"
        )
    html = _render_blog_shell(
        "\n".join(inner),
        title="Blog — investování bez příkras | MY ADVANTAGE",
        description="Edukační články o technické analýze, valuaci, stop-lossu, SEC filings a dalších investičních tématech.",
        canonical=f"{APP_URL}/blog",
    )
    resp = make_response(html)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    resp.headers["Cache-Control"] = "public, max-age=1800"
    return resp


@app.route("/blog/<path:slug>")
def blog_post(slug):
    slug = (slug or "").strip("/").lower()
    p = _BLOG_INDEX.get(slug)
    if not p:
        return _render_blog_shell(
            "<h1>Článek nenalezen</h1><p><a class='back' href='/blog'>← zpět na blog</a></p>",
            "Článek nenalezen | MY ADVANTAGE",
            "Hledaný článek na blogu MY ADVANTAGE neexistuje.",
            f"{APP_URL}/blog",
        ), 404
    jsonld = json.dumps({
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": p["title"],
        "description": p["excerpt"],
        "datePublished": p["date"],
        "dateModified": p["date"],
        "author": {"@type": "Organization", "name": "MY ADVANTAGE"},
        "publisher": {"@type": "Organization", "name": "MY ADVANTAGE",
                      "logo": {"@type": "ImageObject", "url": f"{APP_URL}/logo/icon-512.png"}},
        "keywords": p.get("keywords", ""),
    }, ensure_ascii=False)
    inner = [
        f"<a class='back' href='/blog'>← zpět na blog</a>",
        f"<h1>{_html_escape(p['title'])}</h1>",
        f"<div class='meta'>{p['date']} · MY ADVANTAGE</div>",
        _md_to_html(p["body"]),
    ]
    html = _render_blog_shell(
        "\n".join(inner),
        title=f"{p['title']} | MY ADVANTAGE Blog",
        description=p["excerpt"],
        canonical=f"{APP_URL}/blog/{slug}",
        extra_head=f'<script type="application/ld+json">{jsonld}</script>',
    )
    resp = make_response(html)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


@app.route("/sitemap.xml")
def sitemap():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    urls = [f"{APP_URL}/", f"{APP_URL}/#about", f"{APP_URL}/blog"]
    urls += [f"{APP_URL}/blog/{p['slug']}" for p in _BLOG_POSTS]
    urls += [f"{APP_URL}/stock/{t}" for t in _SITEMAP_TICKERS]
    xml = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for u in urls:
        xml.append(f"  <url><loc>{u}</loc><lastmod>{today}</lastmod><changefreq>daily</changefreq></url>")
    xml.append("</urlset>")
    resp = make_response("\n".join(xml))
    resp.headers["Content-Type"] = "application/xml; charset=utf-8"
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


@app.route("/robots.txt")
def robots():
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /api/\n"
        f"Sitemap: {APP_URL}/sitemap.xml\n"
    )
    resp = make_response(body)
    resp.headers["Content-Type"] = "text/plain; charset=utf-8"
    return resp


@app.route("/api/stock/<path:ticker>")
def get_stock_detail(ticker):
    ticker = ticker.upper()
    period = request.args.get("period", "6mo")
    want_model = request.args.get("full") == "1"  # náš verdikt jen při plném otevření (ne při změně období)

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
        daily_closes = None
        try:
            daily = yahoo_chart(ticker, "1y", "1d")
            dq = ((daily.get("indicators") or {}).get("quote") or [{}])[0]
            daily_closes = dq.get("close") or []
            indicators = compute_indicators(daily_closes, dq.get("volume") or [])
        except Exception:
            indicators = compute_indicators(closes_for_sma, vols)

        price = _round(meta.get("regularMarketPrice", closes_for_sma[-1]), 3)
        # Denní změna VŽDY z denní řady – graf může být 5minutový i roční.
        if daily_closes:
            prev = _prev_close(daily_closes)
        elif period == "1d" or not is_intraday:
            prev = _prev_close(closes_for_sma if not is_intraday else [], meta)
        else:
            prev = None
        prev = _round(prev or price, 3)
        change_pct = ((price - prev) / prev) * 100 if prev else 0
        high52 = _round(meta.get("fiftyTwoWeekHigh", price), 3) or price
        low52 = _round(meta.get("fiftyTwoWeekLow", price), 3) or price

        score, score_label, score_comps = signal_score(price, indicators, high52, low52)

        # Fundamenty (best-effort)
        f = fetch_fundamentals(ticker)
        div = _clean(f.get("dividend_yield"), None)
        if div is not None:
            div = round(div * 100 if div < 1 else div, 2)

        # Náš verdikt (MA Skóre) – stejný model jako Hloubková analýza, aby se
        # detail a analýza NIKDY nerozcházely. Počítá se jen při plném otevření.
        verdict_model = None
        verdict_levels = None
        if want_model:
            try:
                facts = gather_facts(ticker)
                verdict_model = compute_verdict_model(facts)
                verdict_levels = compute_levels(facts, 10)
            except Exception:
                verdict_model = None

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
            "tech_rating": technical_rating(price, indicators),
            "model": verdict_model,
            "levels": verdict_levels,
            "chart": chart_data,
            "sma20": sma20,
            "sma50": sma50,
            "sma200": sma200,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": f"Nepodařilo se stáhnout detail: {e}"})


# ---------------------------------------------------------------------------
# Ranní souhrn e-mailem (Vercel Cron volá /api/cron/morning)
# ---------------------------------------------------------------------------
def _top_opps_for_summary(n=3):
    """Nejvyšší hodnota & momentum z posledního skenu (pro e-maily)."""
    try:
        return ((market_scan(allow_step=False) or {}).get("value") or [])[:n]
    except Exception:
        return []


def _signals_email_rows(signals, n=4):
    """Společné řádky nákupních signálů pro ranní i týdenní e-mail. Jen tituly,
    které nepřekročí limit 2 na sektor – stejně jako „Co koupit teď" v appce."""
    rows = []
    for s in [x for x in signals if (x.get("sector_rank") or 1) <= SECTOR_CAP][:n]:
        vm = f"hodnota+momentum {s['vm']}/100 · " if s.get("vm") is not None else ""
        rows.append(f"<div style='padding:8px 0;border-top:1px solid #23262f'>"
                    f"<b style='color:#00C853'>Koupit</b> &nbsp;<b>{_html_escape(s.get('ticker') or '')}</b> "
                    f"<span style='color:#9ba1b0'>{_html_escape(s.get('name') or '')}</span><br>"
                    f"<span style='color:#9ba1b0;font-size:12px'>{vm}cíl {s.get('target')} (+{s.get('reward_pct')} %) · "
                    f"stop {s.get('stop')} (−{s.get('risk_pct')} %) · {EXIT_HORIZON_LABEL}</span></div>")
    return "".join(rows)


def _opps_email_rows(top_opps):
    """Společné řádky „podhodnocené s momentem" pro ranní i týdenní e-mail."""
    rows = []
    for o in top_opps:
        bits = [f"hodnota+momentum {o.get('score')}/100 (#{o.get('vm_rank')})"]
        if o.get("pe"):
            bits.append(f"P/E {o['pe']:.0f}")
        if o.get("mom_pct") is not None:
            bits.append(f"momentum {o['mom_pct']:+.0f} %")
        rows.append(f"<div style='padding:8px 0;border-top:1px solid #23262f'>"
                    f"<b>{_html_escape(o['ticker'])}</b> <span style='color:#9ba1b0'>"
                    f"{_html_escape(o.get('name') or '')} · {' · '.join(bits)}</span></div>")
    return "".join(rows)


def _market_brief():
    """Ranní situace: co dělá trh, co se nejvíc hýbe a co se k tomu píše.

    Stojí na STEJNÝCH primitivech jako dashboard (_fetch_macro_one, _MACRO_TICKERS,
    _SECTOR_ETFS, _fetch_rss) – žádná druhá kopie logiky. Zprávy se ukazují VEDLE
    pohybů, ne jako jejich vysvětlení: tvrdit „sektor klesl kvůli tomuhle titulku"
    by byla vymyšlená příčinnost, kterou z dat nemáme."""
    out = {"macro": [], "best": None, "worst": None, "news": [], "mood": None}
    for (tk, label, unit, lo, hi, low_calm) in _MACRO_TICKERS:
        d = _fetch_macro_one(tk)
        if d:
            out["macro"].append({"label": label, "unit": unit, "price": d["price"],
                                 "chg": d["chg_pct"],
                                 "regime": _macro_regime(d["price"], lo, hi, low_calm)})
    vix = next((m for m in out["macro"] if m["label"] == "VIX"), None)
    if vix and vix["price"] is not None:
        # Nejen ÚROVEŇ, ale i denní skok. VIX 17,8 je sám o sobě neutrální,
        # ale skok o +13 % za den znamená, že se něco děje – hlásit u toho
        # „trh je neutrální" odporuje titulkům hned vedle.
        jump = vix.get("chg") or 0
        if vix["price"] > 25 or jump >= 10:
            out["mood"] = "nervózní"
        elif vix["price"] < 15 and jump <= 3:
            out["mood"] = "klidný"
        else:
            out["mood"] = "neutrální"
    secs = []
    for (tk, label, _icon) in _SECTOR_ETFS:
        d = _fetch_macro_one(tk)
        if d and d.get("chg_pct") is not None:
            secs.append({"label": label, "chg": d["chg_pct"]})
    if secs:
        secs.sort(key=lambda x: x["chg"])
        out["worst"], out["best"] = secs[0], secs[-1]
    # Pořadí = od nejvíc burzovních zdrojů po obecné zpravodajství. Obecné feedy
    # mají nízkou výtěžnost po filtru, proto jsou až na konci jako doplněk.
    # DENNÍ SHRNUTÍ (kurzy_svet/roklen mívají skutečný wrap-up dne s konkrétním
    # vysvětlením pohybu) jde vždy první, i kdyby v pořadí zdrojů bylo pozdě –
    # je to nejblíž tomu, co uživatel chce: ne titulek, ale důvod.
    seen, rest = set(), []
    daily_wrap = None
    for key in ("roklen", "kurzy_svet", "seekingalpha", "fool", "marketwatch", "cnbc", "e15"):
        for it in (_fetch_rss(key) or [])[:10]:
            t = (it.get("title") or "").strip()
            k = t.lower()[:60]
            if not t or k in seen or not _is_market_news(t):
                continue
            seen.add(k)
            low = t.lower()
            # Denní shrnutí dostane delší limit – ať se blurb stihne dostat
            # k samotnému DŮVODU ("Jedním z hlavních zdrojů obav byl…"),
            # ne uříznout těsně před ním jako s kratším limitem u ostatních.
            is_wrap = daily_wrap is None and ("shrnutí" in low or "denní" in low)
            entry = {"title": t, "link": it.get("link") or "",
                     "blurb": _news_blurb(it, limit=220 if is_wrap else 120)}
            if is_wrap:
                daily_wrap = entry
            else:
                rest.append(entry)
        if len(rest) + (1 if daily_wrap else 0) >= 6:
            break
    out["news"] = ([daily_wrap] if daily_wrap else []) + rest
    out["news"] = out["news"][:4]
    return out


# Obecné zpravodajské feedy vedle burzovních zpráv vozí i lifestyle a zdravotnictví.
# Ukázat pod „nejvíc klesá Zdravotnictví" článek o školném vnoučat je horší než
# neukázat nic – naznačuje to souvislost, která neexistuje. Radši prázdno než šum.
_MARKET_WORDS = (
    "stock", "stocks", "market", "markets", "fed", "rate", "rates", "yield",
    "inflation", "cpi", "earnings", "nasdaq", "s&p", "dow", "tariff", "oil",
    "recession", "rally", "selloff", "sell-off", "bond", "treasury", "economy",
    "jobs", "payroll", "gdp", "dollar", "shares", "investors", "trading",
    "akcie", "akcií", "trh", "trhy", "trhů", "inflace", "sazb", "výnos",
    "ropa", "ropy", "burza", "burzy", "investor", "ekonomik", "dolar", "koruna",
)


def _strip_html(text):
    """Vyčistí RSS summary od HTML značek a entit. Bez tohohle by se do e-mailu
    i webu dostal syrový <img src=...> tag (dělá to zdroj e15) – needitované HTML
    v datech, ne markup, který bychom chtěli vykreslit."""
    import re as _re
    import html as _html
    t = _re.sub(r"<[^>]+>", " ", text or "")
    t = _html.unescape(t)
    return _re.sub(r"\s+", " ", t).strip()


def _news_blurb(item, limit=150):
    """Krátká věta s KONKRÉTNÍM důvodem z RSS summary, ne jen holý titulek.
    Kurzy_svet/Roklen mívají v summary skutečné vysvětlení pohybu
    ("eskalace na Blízkém východě posiluje obavy...") – to je přesně to,
    co dřív holý titulek nedal."""
    t = _strip_html(item.get("summary") or "")
    if not t or t.lower().startswith((item.get("title") or "").lower()[:20]):
        return ""
    return (t[:limit].rsplit(" ", 1)[0] + "…") if len(t) > limit else t


def _is_market_news(title):
    t = (title or "").lower()
    return any(w in t for w in _MARKET_WORDS)


def _todays_action(email, top_signals, risk=None):
    """Jedna věta: co dnes udělat. Tohle je ta část „nechci to řešit"."""
    try:
        pos = _positions_for(email, risk=risk)
    except Exception:
        return None
    act = [i for i in pos["items"] if i["status"] != "open"]
    alloc = pos.get("allocation") or _allocation(0, risk)
    lead = ""
    if alloc.get("cash_pct"):
        lead = (f"Trh je rozkolísaný (volatilita {alloc.get('spy_vol')} %) – drž {alloc['exposure_pct']} % "
                f"v akciích a {alloc['cash_pct']:.0f} % v hotovosti. ")
    if act:
        parts = []
        for i in act[:3]:
            if i["status"] == "target":
                parts.append(f"vyber zisk u {i['ticker']}")
            elif i["status"] == "stop":
                parts.append(f"ukonči {i['ticker']} na stopu")
            else:
                parts.append(f"přehodnoť {i['ticker']} (vypršel horizont)")
        return "Dnes: " + lead + cap(", ".join(parts)) + "."
    free = alloc["slots_max"] - alloc["slots_used"]
    # Za „Trh je rozkolísaný… ." pokračuje nová věta → velké písmeno.
    cap = (lambda t: t[:1].upper() + t[1:]) if lead else (lambda t: t)
    slots = (f"{free} volnou pozici" if free == 1 else f"{free} volné pozice" if 2 <= free <= 4
             else f"{free} volných pozic")
    if top_signals:
        smap = ((market_scan(allow_step=False) or {}).get("sector_map") or {})
        held = {}
        for i in pos["items"]:
            if i["status"] == "open":
                sct = smap.get(i["ticker"]) or "?"
                held[sct] = held.get(sct, 0) + 1
        mine = {i["ticker"] for i in pos["items"] if i["status"] == "open"}
        top_signals = [x for x in top_signals if x["ticker"] not in mine
                       and held.get(x.get("sector") or "?", 0) < SECTOR_CAP]
    if free and top_signals:
        s0 = top_signals[0]
        vm = f"hodnota+momentum {s0['vm']}/100, " if s0.get("vm") is not None else ""
        return (f"Dnes: {lead}{cap('máš')} {slots} z {alloc['slots_max']}. Nejsilnější "
                f"signál je {s0.get('ticker')} ({vm}váha {alloc['per_position_pct']} %). "
                f"Zbytek drží indexové jádro.")
    if free:
        return (f"Dnes: {lead}{cap('žádný')} nový signál. Nech kapitál v indexovém jádru – "
                "hotovost bez důvodu nevydělává.")
    return f"Dnes: {lead}{cap('nic')}. Všech {alloc['slots_max']} pozic běží podle plánu."


def _brief_payload(user=None, date=None, risk=None):
    """Strukturovaná data ranního přehledu – JEDEN zdroj pro web i e-mail.

    Tržní část se archivuje pod `brief:<datum>`, takže si jde rozkliknout i
    zpětně („proč to tehdy padalo"). Pozice a instrukce jsou per-uživatel a
    počítají se vždy živě – archivovat je nedává smysl.
    """
    today = _today_iso()
    date = date or today
    market, signals = None, []
    digest = None
    if date == today:
        market = _market_brief()
        signals = _todays_signals()[:5]
        try:
            dg = _news_digest()
            digest = {k: dg.get(k) for k in ("ai", "ai_status", "story", "headline", "events", "long_term")}
            st = dg.get("story") or {}
            if st.get("label") and isinstance(market, dict):
                market["mood"] = st["label"]          # stejná nálada všude (ne jen podle VIX)
        except Exception:
            digest = None
        try:                       # ulož snímek dne do archivu
            kv_set_json(f"brief:{date}", {"market": market, "signals": signals, "digest": digest,
                                          "generated": int(time.time())})
            kv_sadd("brief:dates", date)
        except Exception:
            pass
    else:
        snap = kv_get_json(f"brief:{date}") or {}
        market, signals, digest = snap.get("market"), snap.get("signals") or [], snap.get("digest")
        if not market:
            return {"ok": False, "date": date, "error": "Pro tenhle den nemáme uložený přehled."}

    out = {"ok": True, "date": date, "is_today": date == today,
           "market": market, "signals": signals, "digest": digest}
    if date == today:
        out["risk"] = _market_risk()
    if user and date == today:
        pos = _positions_for(user, risk=risk)
        out["positions"] = pos
        out["action"] = _todays_action(user, signals, risk)
        # „Proč" k pohybům – stejný zdroj jako e-mail, jen jako data pro web.
        # Form 4 (insider) vynechán: podává se pořád a pohyb nevysvětluje.
        try:
            pf = kv_get_json(f"portfolio:{user}") or {}
            tk = [i["ticker"] for i in (pos.get("items") or [])]
            tk += [t for t in (pf.get("watchlist") or [])[:5] if t not in tk]
            why = []
            for t in tk[:5]:
                for e in _events_for_ticker(t, since_days=5):
                    if e.get("kind") in ("material", "analyst", "quarterly", "earnings"):
                        why.append({"ticker": t, "title": e.get("title"),
                                    "hint": e.get("hint"), "severity": e.get("severity")})
                        break
                if len(why) >= 3:
                    break
            out["why"] = why
        except Exception:
            out["why"] = []
    return out


@app.route("/api/brief")
def api_brief():
    """Ranní přehled na webu. Bez parametru dnešní, s ?date=YYYY-MM-DD z archivu."""
    if not _rate_ok("brief", 60, 60):
        return jsonify({"ok": False, "error": "Moc požadavků, zkus to za chvíli."}), 429
    date = (request.args.get("date") or "").strip()
    if date and not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return jsonify({"ok": False, "error": "Špatný formát data."}), 400
    try:
        return jsonify(_brief_payload(_auth_user(), date or None, request.args.get("risk")))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/brief/dates")
def api_brief_dates():
    """Dny, ke kterým máme uložený přehled (nejnovější první)."""
    try:
        ds = sorted([d for d in (kv_smembers("brief:dates") or []) if d], reverse=True)
        today = _today_iso()
        if today not in ds:
            ds.insert(0, today)
        return jsonify({"ok": True, "dates": ds[:60]})
    except Exception:
        return jsonify({"ok": True, "dates": [_today_iso()]})


def _portfolio_why(tickers, limit=3):
    """„Proč" k pohybům v portfoliu – skutečné události (SEC podání, revize
    analytiků), ne domyšlená příčinnost. Staví na `_events_for_ticker`,
    stejném zdroji, jaký používá karta událostí v appce."""
    # Form 4 (insider) se podává při každém prodeji akcií manažerem – děje se to
    # pořád a s pohybem ceny obvykle nesouvisí. Vydávat ho za „proč to padá"
    # by byla falešná příčinnost, tak ho sem nepouštíme. Zbylé druhy událostí
    # (8-K, revize analytiků, výsledky) vysvětlují pohyb reálně.
    GOOD = ("material", "analyst", "quarterly", "earnings")
    rows = []
    for t in tickers[:5]:
        try:
            rel = [e for e in _events_for_ticker(t, since_days=5)
                   if e.get("kind") in GOOD]
            for e in rel[:1]:
                col = "#FFB347" if e.get("severity") == "warn" else "#9ba1b0"
                rows.append(
                    f"<div style='padding:5px 0;font-size:13px'>"
                    f"<b>{_html_escape(t)}</b> · <span style='color:{col}'>"
                    f"{_html_escape(e.get('title') or '')}</span>"
                    f"<div style='color:#9ba1b0;font-size:12px'>"
                    f"{_html_escape(e.get('hint') or '')}</div></div>")
        except Exception:
            continue
        if len(rows) >= limit:
            break
    if not rows:
        return ""
    return ("<div style='color:#9ba1b0;font-size:12px;margin:12px 0 4px'>"
            "Proč se to hýbe:</div>" + "".join(rows))


def build_morning_summary_html(email, top_opps, top_signals=None):
    pf = kv_get_json(f"portfolio:{email}") or {}
    watch = (pf.get("watchlist") or [])[:8]
    rows = ""
    for t in watch:
        try:
            res = yahoo_chart(t, "1d", "1d")
            meta = res.get("meta", {})
            price = meta.get("regularMarketPrice")
            prev = _prev_close([], meta) or price
            chg = ((price - prev) / prev * 100) if prev else 0
            col = "#00C853" if chg >= 0 else "#FF3D00"
            rows += (f"<tr><td style='padding:6px 0'><b>{t}</b></td>"
                     f"<td style='padding:6px 0;text-align:right'>{round(price,2)} {meta.get('currency','')}</td>"
                     f"<td style='padding:6px 0;text-align:right;color:{col}'>{'+' if chg>=0 else ''}{chg:.2f}%</td></tr>")
        except Exception:
            continue
    watch_html = (f"<div style='color:#9ba1b0;font-size:12px;margin:14px 0 4px'>Sledované akcie:</div>"
                  f"<table style='width:100%;border-collapse:collapse;font-size:14px'>{rows}</table>") if rows else ""
    opp_html = ""
    if top_opps:
        opp_html = ("<h3 style='font-size:16px;margin:22px 0 8px'>💎 Podhodnocené s momentem</h3>"
                    + _opps_email_rows(top_opps))
    sig_html = ""
    if top_signals:
        sig_html = ("<h3 style='font-size:16px;margin:22px 0 8px'>✅ Dnešní nákupní signály</h3>"
                    + _signals_email_rows(top_signals))
    # Otevřené pozice – co dnes vyžaduje akci podle plánu (cíl/stop/horizont).
    # Celý stav portfolia, ne jen to, co křičí. Uživatel chce ráno vidět,
    # co se s jeho penězi děje – i když se zrovna nic dělat nemá.
    pos_html, why_tickers = "", []
    try:
        pos = _positions_for(email)
        items = pos.get("items") or []
        why_tickers = [i["ticker"] for i in items]
        if items:
            rws = "".join(
                f"<div style='padding:8px 0;border-bottom:1px solid #22262f'>"
                f"<b>{i['ticker']}</b> "
                f"<span style='color:#9ba1b0;font-size:13px'>{_html_escape(i.get('label') or '')}"
                f" · {i.get('days_held', 0)} dní</span> "
                f"<span style='float:right;color:"
                f"{'#00C853' if (i['return_pct'] or 0) >= 0 else '#FF3D00'}'>"
                f"{'+' if (i['return_pct'] or 0) >= 0 else ''}{i['return_pct']} %</span></div>"
                for i in items[:6])
            a = pos.get("allocation") or {}
            alloc_line = (f"<div style='color:#9ba1b0;font-size:12px;margin:8px 0 10px'>"
                          f"Aktivní signály {a.get('active_pct')} % "
                          f"({a.get('slots_used')}/{a.get('slots_max')} pozic) · "
                          f"indexové jádro {a.get('core_pct')} %</div>") if a else ""
            pos_html = ("<div style='color:#9ba1b0;font-size:12px;margin:4px 0'>"
                        "Otevřené pozice proti plánu:</div>" + rws + alloc_line)
        else:
            pos_html = ("<div style='color:#9ba1b0;font-size:13px;margin:4px 0 10px'>"
                        "Žádná otevřená pozice — kapitál patří do indexového jádra.</div>")
    except Exception:
        pos_html = ""
    # Ranní situace nahoru – „co se děje a proč" má být první, co uvidíš.
    brief_html = ""
    try:
        b = _market_brief()
        # Barva podle REŽIMU, ne podle znaménka – rostoucí VIX je špatná zpráva,
        # i když je změna kladná (zelené „VIX +8 %" by čtenáře mátlo).
        _rc = {"calm": "#00C853", "stress": "#FF3D00", "high": "#FFB347"}
        mac = " · ".join(
            f"{m['label']} <b style='color:{_rc.get(m.get('regime'), '#9ba1b0')}'>"
            f"{m['price']}{m['unit']}</b> "
            f"<span style='opacity:.75'>{'+' if (m['chg'] or 0) >= 0 else ''}{m['chg']} %</span>"
            for m in b["macro"] if m.get("price") is not None)
        try:
            dg = _news_digest()
        except Exception:
            dg = {}
        st = dg.get("story") or {}
        if st.get("label"):
            b["mood"] = st["label"]
        mood = (f"<p style='line-height:1.6;margin:0 0 10px'>Trh je dnes "
                f"<b>{b['mood']}</b>.</p>") if b.get("mood") else ""
        if st.get("what"):
            mood += (f"<p style='line-height:1.6;margin:0 0 8px;font-size:14px'>{_html_escape(st['what'])}</p>"
                     + (f"<p style='line-height:1.6;margin:0 0 8px;font-size:13px;color:#9ba1b0'>{_html_escape(st['why'])}</p>"
                        if st.get("why") else "")
                     + "".join(f"<div style='padding:8px 0;border-top:1px solid #23262f;font-size:13px;line-height:1.5'>"
                               f"<b>{_html_escape(d['title'])}</b> – {_html_escape(d['text'])}</div>"
                               for d in (st.get("drivers") or [])[:3]))
        movers = ""
        if b.get("best") and b.get("worst"):
            movers = (f"<p style='margin:10px 0 0;font-size:14px'>Nejvíc roste "
                      f"<b>{b['best']['label']}</b> "
                      f"<span style='color:#00C853'>{b['best']['chg']:+.2f} %</span>, "
                      f"nejvíc klesá <b>{b['worst']['label']}</b> "
                      f"<span style='color:#FF3D00'>{b['worst']['chg']:+.2f} %</span>.</p>")
        news = ""
        if dg.get("ai") and dg.get("events"):
            col = {"pozitivní": "#00C853", "negativní": "#FF3D00"}
            rows = "".join(
                f"<div style='padding:10px 0;border-top:1px solid #23262f;font-size:13px;line-height:1.5'>"
                f"<b style='color:{col.get(e['impact'], '#FFB347')}'>●</b> <b>{_html_escape(e['title'])}</b><br>"
                f"<span style='color:#9ba1b0'>Co se stalo:</span> {_html_escape(e['what'])}<br>"
                + (f"<span style='color:#9ba1b0'>Proč je to důležité:</span> {_html_escape(e['why'])}" if e.get("why") else "")
                + "</div>" for e in dg["events"][:4])
            news = ((f"<p style='margin:14px 0 6px;font-size:14px'><b>{_html_escape(dg.get('headline') or '')}</b></p>"
                     if dg.get("headline") else "") + rows +
                    "<p style='color:#9ba1b0;font-size:12px;margin:8px 0 0'>Pro dlouhodobý plán: zprávy jsou kontext, "
                    "ne signál – kvůli nim nic neměň.</p>")
        elif b.get("news") and not st.get("what"):
            def _news_row(n):
                blurb = (f"<div style='color:#9ba1b0;font-size:12px;margin:2px 0 0 14px'>"
                        f"{_html_escape(n['blurb'])}</div>") if n.get("blurb") else ""
                return (f"<div style='padding:6px 0;font-size:13px'>• "
                       f"<a href='{_html_escape(n['link'])}' style='color:#F0A030;text-decoration:none;font-weight:600'>"
                       f"{_html_escape(n['title'])}</a>{blurb}</div>")
            news = ("<p style='margin:14px 0 6px;font-size:13px;color:#9ba1b0'>"
                    "Co se k tomu dnes píše:</p>" + "".join(_news_row(n) for n in b["news"]))
        brief_html = ("<h3 style='font-size:16px;margin:4px 0 8px'>🌍 Ranní situace</h3>"
                      + mood
                      + (f"<p style='font-size:13px;color:#9ba1b0;margin:0'>{mac}</p>" if mac else "")
                      + movers + news)
    except Exception:
        brief_html = ""

    action = _todays_action(email, top_signals or [])
    action_html = (f"<div style='margin:18px 0 4px;padding:14px 16px;border-radius:12px;"
                   f"background:rgba(255,122,0,0.10);border:1px solid rgba(255,122,0,0.30)'>"
                   f"<b style='color:#F0A030'>{_html_escape(action)}</b></div>") if action else ""

    why_html = _portfolio_why(why_tickers + [t for t in watch if t not in why_tickers])
    portfolio_html = ("<h3 style='font-size:16px;margin:22px 0 8px'>📊 Tvé portfolio</h3>"
                      + pos_html + watch_html + why_html)

    return _email_shell("Ranní přehled trhu ☀️",
                        "<p style='line-height:1.6'>Dobré ráno!</p>" +
                        brief_html + portfolio_html + action_html +
                        sig_html + opp_html +
                        "<p style='color:#9ba1b0;font-size:12px;margin-top:20px'>Notifikace vypneš v appce v profilu. "
                        "Není to investiční doporučení.</p>")


def _plan_email_html(email):
    """Sekce „Tvůj investiční plán" do sobotního e-mailu: hodnota v Kč a změna
    za týden, úkoly (ořez/úklid bez daně) a první víkend v měsíci připomínka
    pravidelného vkladu. Bez uložených pozic prázdné."""
    hrec = kv_get_json(f"holdings:{email}") or {}
    lots = hrec.get("holdings") or []
    if not lots:
        return ""
    rows, total, _missing, scan = _value_holdings(lots)
    if not total:
        return ""
    plan = _build_plan(rows, hrec.get("risk") or _user_risk(email), scan)
    # Změna za týden jen z pohybu cen (v Kč) u toho, co držíš – nové vklady
    # by jinak vypadaly jako výdělek.
    last = kv_get_json(f"plan:last:{email}") or {}
    px_now = {r["ticker"]: r["value_czk"] / r["qty"] for r in rows if r["qty"]}
    try:
        kv_set_json(f"plan:last:{email}", {"ts": int(time.time()), "px": {t: round(v, 4) for t, v in px_now.items()}})
    except Exception:
        pass
    chg = ""
    old = last.get("px") or {}
    if old and time.time() - last.get("ts", 0) < 10 * 86400:
        both = [r for r in rows if r["ticker"] in old and r["qty"]]
        base = sum(r["qty"] * old[r["ticker"]] for r in both)
        if base:
            d = sum(r["qty"] * (px_now[r["ticker"]] - old[r["ticker"]]) for r in both)
            col = "#00C853" if d >= 0 else "#FF3D00"
            chg = (f"<span style='color:{col};font-size:14px;margin-left:8px'>{'+' if d >= 0 else '−'}{_kc(abs(d))}"
                   f" ({'+' if d >= 0 else '−'}{_pc(abs(d) / base * 100)} %) za týden</span>")
    core = sum(r["weight"] for r in rows if r.get("status") == "core")
    html = (f"<h3 style='font-size:16px;margin:18px 0 10px'>🧭 Tvůj investiční plán</h3>"
            f"<div style='background:#12141c;border:1px solid #23262f;border-radius:12px;padding:14px 16px;margin-bottom:12px'>"
            f"<div style='font-size:22px;font-weight:700'>{_kc(total)}{chg}</div>"
            f"<div style='color:#9ba1b0;font-size:13px;margin-top:4px'>Jádro {_pc(core, 0)} % (cíl {plan['core_pct']} %) · "
            f"akcie v plánu {plan['slots_used']}/{plan['slots']}</div></div>")
    for a in plan["actions"][:3]:
        verb = "Oříznout" if a["type"] == "trim" else "Prodat"
        html += (f"<div style='padding:10px 12px;border-radius:10px;background:rgba(255,179,71,0.1);"
                 f"border:1px solid rgba(255,179,71,0.3);margin-bottom:8px;font-size:14px;line-height:1.5'>"
                 f"✂️ <b>{verb} {a['ticker']} za ≈ {_kc(a['amount_czk'])}</b> – {a['why']}</div>")
    if not plan["actions"]:
        html += "<div style='color:#9ba1b0;font-size:14px'>✅ Tento týden nemusíš nic dělat – plán drží.</div>"
    if datetime.now(timezone.utc).day <= 7:
        html += (f"<div style='margin-top:12px;font-size:14px;line-height:1.5'>📅 <b>Začátek měsíce – čas na pravidelný vklad.</b> "
                 f"Otevři appku → Moje portfolio → <i>Kam s penězi</i>, zadej částku a appka řekne, co koupit.</div>")
    return html


def build_weekly_digest_html(email, top_signals=None, top_opps=None):
    """Sobotní shrnutí týdne pro uživatele."""
    pf = kv_get_json(f"portfolio:{email}") or {}
    watch = (pf.get("watchlist") or [])[:15]

    # Výkon watchlistu za posledních 7 dní (equal-weighted vs SPY)
    perf_html = ""
    if watch:
        try:
            best_t, best_c, worst_t, worst_c = None, None, None, None
            gains, losses = 0, 0
            rows = []
            for t in watch[:12]:
                try:
                    d = yahoo_chart(t, "5d", "1d")
                    closes = [c for c in (((d.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []) if c is not None]
                    if len(closes) < 2:
                        continue
                    first_wk = closes[0]
                    last = closes[-1]
                    chg = (last - first_wk) / first_wk * 100.0 if first_wk else 0
                    if chg > 0: gains += 1
                    if chg < 0: losses += 1
                    if best_c is None or chg > best_c: best_t, best_c = t, chg
                    if worst_c is None or chg < worst_c: worst_t, worst_c = t, chg
                    col = "#00C853" if chg >= 0 else "#FF3D00"
                    rows.append((chg, f"<tr><td style='padding:5px 0'><b>{t}</b></td>"
                                       f"<td style='padding:5px 0;text-align:right;color:{col}'>{'+' if chg>=0 else ''}{chg:.2f}%</td></tr>"))
                except Exception:
                    continue
            rows.sort(key=lambda x: -x[0])
            table = "".join(r[1] for r in rows[:10])
            summary = (f"<div style='background:#12141c;border:1px solid #23262f;border-radius:12px;"
                       f"padding:14px 16px;margin-bottom:14px'>"
                       f"<div style='color:#8a8f99;font-size:12px;letter-spacing:.5px;text-transform:uppercase'>Týdenní bilance</div>"
                       f"<div style='margin-top:6px'>"
                       f"<b style='color:#00C853'>{gains}↑</b> &nbsp; <b style='color:#FF3D00'>{losses}↓</b>"
                       f"</div></div>")
            hilo = ""
            if best_t and best_c and best_c > 0:
                hilo += f"<div style='margin:8px 0'>🏆 Nejlepší: <b>{best_t}</b> <span style='color:#00C853'>+{best_c:.2f}%</span></div>"
            if worst_t and worst_c and worst_c < 0:
                hilo += f"<div style='margin:8px 0'>📉 Nejhorší: <b>{worst_t}</b> <span style='color:#FF3D00'>{worst_c:.2f}%</span></div>"
            perf_html = ("<h3 style='font-size:16px;margin:18px 0 10px'>📊 Jak si vedla tvá watchlist</h3>" +
                         summary + hilo +
                         ("<table style='width:100%;border-collapse:collapse;font-size:14px;margin-top:10px'>" + table + "</table>" if table else ""))
        except Exception:
            perf_html = ""

    # Top signály týdne
    sig_html = ""
    if top_signals:
        sig_html = ("<h3 style='font-size:16px;margin:22px 0 10px'>🎯 Nákupní signály</h3>"
                    + _signals_email_rows(top_signals))

    # Klíčové události u sledovaných akcií (last 7 days)
    events_html = ""
    try:
        allev = []
        for t in watch[:15]:
            allev += _events_for_ticker(t, since_days=7)
        allev.sort(key=lambda x: x.get("date") or "", reverse=True)
        if allev[:6]:
            items = ""
            for ev in allev[:6]:
                col = {"good": "#00C853", "warn": "#FFC400", "info": "#4fa3ff"}.get(ev.get("severity"), "#8a8f99")
                items += (f"<div style='padding:8px 0;border-top:1px solid #23262f'>"
                          f"<b>{ev.get('ticker')}</b> · <span style='color:{col}'>{ev.get('title','')}</span><br>"
                          f"<span style='color:#9ba1b0;font-size:12px'>{ev.get('date','')} · {ev.get('hint','')}</span></div>")
            events_html = "<h3 style='font-size:16px;margin:22px 0 10px'>🔔 Klíčové události u tvých akcií</h3>" + items
    except Exception:
        events_html = ""

    # Příležitosti
    opp_html = ""
    if top_opps:
        opp_html = ("<h3 style='font-size:16px;margin:22px 0 10px'>💎 Podhodnocené s momentem</h3>"
                    + _opps_email_rows(top_opps))

    intro = ("<p style='line-height:1.6'>Hezký víkend! Tady je tvé <b>shrnutí týdne</b> " +
             "— jak si vedla tvá watchlist, nejlepší signály z modelu a klíčové události.</p>")

    cta = (f"<div style='text-align:center;margin:28px 0 12px'>"
           f"<a href='{APP_URL}/' style='display:inline-block;background:#FF7A00;color:#1a0e00;"
           f"padding:12px 26px;font-weight:700;text-decoration:none;border-radius:12px'>"
           f"🚀 Otevřít MY ADVANTAGE</a></div>")

    try:
        plan_html = _plan_email_html(email)
    except Exception:
        plan_html = ""

    return _email_shell("Týdenní shrnutí 📊",
                        intro + plan_html + perf_html + sig_html + events_html + opp_html + cta +
                        "<p style='color:#9ba1b0;font-size:12px;margin-top:20px'>"
                        "Sobotní digest posíláme, pokud jsi ho zapnul v profilu. "
                        "Vypneš ho tam samým přepínačem. Není to investiční doporučení.</p>")


SEND_HOUR_PRAGUE = 8   # v kolik má digest odejít místního času


def _prague_hour():
    """Aktuální hodina v Praze. Vercel crony umí jen UTC, takže se posun musí
    dopočítat – jinak by digest po přechodu na zimní čas chodil o hodinu dřív.
    Primárně `zoneinfo`; když v runtime chybí tzdata, spočítá se EU pravidlo
    (letní čas = poslední neděle v březnu až poslední neděle v říjnu)."""
    now = datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        return now.astimezone(ZoneInfo("Europe/Prague")).hour
    except Exception:
        pass

    def _last_sunday(year, month):
        d = datetime(year, month, 31, tzinfo=timezone.utc)
        while d.month != month:
            d -= timedelta(days=1)
        return d - timedelta(days=(d.weekday() + 1) % 7)

    start = _last_sunday(now.year, 3).replace(hour=1)    # 01:00 UTC
    end = _last_sunday(now.year, 10).replace(hour=1)
    offset = 2 if start <= now < end else 1
    return (now + timedelta(hours=offset)).hour


def _cron_auth():
    """Ověření Vercel Cronu (fail-closed). Bez CRON_SECRET endpoint nedělá nic.
    Vrací chybovou odpověď, nebo None když je vše v pořádku."""
    secret = os.environ.get("CRON_SECRET")
    auth = request.headers.get("Authorization", "")
    if not secret or (auth != f"Bearer {secret}" and request.args.get("secret") != secret):
        return jsonify({"ok": False, "error": "Neautorizováno."}), 401
    return None


def _todays_signals(allow_step=False):
    """Dnešní signály ze skenu S&P 500. Krok skenu smí udělat jen cron
    (allow_step=True) – uživatelský požadavek dostane poslední hotový sken."""
    try:
        return (market_scan(allow_step=allow_step) or {}).get("signals") or []
    except Exception:
        return []


def digest_sending_enabled():
    """Rozesílání digestů je ZÁMĚRNĚ vypnuté, dokud se nezapne env DIGEST_SEND=on.
    Přehled se i tak každý den spočítá a uloží do archivu, takže si ho jde
    prohlédnout na webu – jen se nikomu nic neposílá."""
    return (os.environ.get("DIGEST_SEND") or "off").strip().lower() in ("on", "1", "true", "yes")


def _broadcast(notif_key, subject, build_html, limit=200):
    """Rozešle e-mail všem, kdo mají v profilu zapnutý `notif_key`.
    Jedna chybná adresa nesmí shodit celou rozesílku."""
    users = kv_smembers("users")[:limit]
    recips = [e for e in users if ((kv_get_json(f"user:{e}") or {}).get("notif") or {}).get(notif_key)]
    stat = {"ts": int(time.time()), "recipients": len(recips), "sent": 0, "failed": 0, "error": None,
            "sending_enabled": digest_sending_enabled()}
    if not stat["sending_enabled"]:
        stat["error"] = "Rozesílání je vypnuté (env DIGEST_SEND není „on“)."
    else:
        for email in recips:
            try:
                if send_email(email, subject, build_html(email)):
                    stat["sent"] += 1
                else:
                    stat["failed"] += 1
                    stat["error"] = _LAST_EMAIL_ERROR["msg"]
            except Exception as e:
                stat["failed"] += 1
                stat["error"] = str(e)[:300]
    try:      # diagnostika pro admina: poslední rozesílka
        kv_set_json(f"cron:send:{notif_key}", stat)
    except Exception:
        pass
    return stat["sent"]


@app.route("/api/cron/weekly-digest")
def cron_weekly_digest():
    """Sobotní ranní digest za uplynulý týden. Vercel Cron – secret required."""
    err = _cron_auth()
    if err:
        return err
    if not (cloud_enabled() and email_enabled()):
        return jsonify({"ok": False, "error": "Chybí úložiště nebo e-mail."}), 503
    try:
        top_opps = _top_opps_for_summary(3)
        top_signals = _todays_signals()
        sent = _broadcast("weekly", "📊 Týdenní shrnutí – MY ADVANTAGE",
                          lambda em: build_weekly_digest_html(em, top_signals, top_opps))
        return jsonify({"ok": True, "sent": sent})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/admin/email-status")
def admin_email_status():
    """Proč (ne)chodí ranní/týdenní e-maily – vše na jednom místě."""
    admin = _auth_admin()
    if not admin:
        return jsonify({"ok": False, "error": "Přístup jen pro admina."}), 403
    users = kv_smembers("users") or []
    cnt = {"morning": 0, "weekly": 0}
    for e in users[:500]:
        n = (kv_get_json(f"user:{e}") or {}).get("notif") or {}
        for k in cnt:
            cnt[k] += 1 if n.get(k) else 0
    me = (kv_get_json(f"user:{admin}") or {}).get("notif") or {}
    return jsonify({"ok": True, "email_enabled": email_enabled(), "sender": os.environ.get("SENDER_EMAIL") or None,
                    "digest_send": digest_sending_enabled(), "cron_secret": bool(os.environ.get("CRON_SECRET")),
                    "recipients": cnt, "me": {"email": admin, "morning": bool(me.get("morning")), "weekly": bool(me.get("weekly"))},
                    "last_run_morning": kv_get_json("cron:run:morning"), "last_send_morning": kv_get_json("cron:send:morning"),
                    "last_send_weekly": kv_get_json("cron:send:weekly"), "send_hour": SEND_HOUR_PRAGUE,
                    "ai_enabled": analysis_enabled(), "news_status": kv_get_json("news:digest:status")})


@app.route("/api/admin/weekly-test", methods=["POST"])
def admin_weekly_test():
    admin = _auth_admin()
    if not admin:
        return jsonify({"ok": False, "error": "Nepřihlášen jako admin."}), 401
    if not email_enabled():
        return jsonify({"ok": False, "error": "Emaily nejsou nastavené."}), 503
    try:
        top_opps = _top_opps_for_summary(3)
        sig = kv_get_json(f"signals:{_today()}") or {}
        top_signals = (sig or {}).get("results") or []
        ok = send_email(admin, "📊 Týdenní shrnutí – MY ADVANTAGE (TEST)",
                        build_weekly_digest_html(admin, top_signals, top_opps))
        return jsonify({"ok": ok, "sent_to": admin, "error": _LAST_EMAIL_ERROR["msg"]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/cron/scan")
def cron_scan():
    """Ranní sken celého S&P 500. Běží v několika spuštěních (5:00, 5:20, 5:40 UTC);
    každé pokračuje tam, kde předchozí skončilo. Chráněno CRON_SECRET."""
    err = _cron_auth()
    if err:
        return err
    try:
        s = _scan_step()
        if s:
            return jsonify({"ok": True, "done": True, "scanned": s.get("scanned"),
                            "signals": len(s.get("signals") or [])})
        return jsonify({"ok": True, "done": False})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/admin/scan", methods=["POST"])
def admin_scan():
    """Ruční krok skenu (Admin). Když nedoběhne najednou, další klik pokračuje."""
    if not _auth_admin():
        return jsonify({"ok": False, "error": "Přístup jen pro admina."}), 403
    try:
        s = _scan_step()
        if s:
            return jsonify({"ok": True, "done": True, "scanned": s.get("scanned"),
                            "signals": len(s.get("signals") or []), "updated": s.get("updated")})
        return jsonify({"ok": True, "done": False})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/profile/risk", methods=["POST"])
def save_risk_profile():
    """Uloží rizikový profil k účtu – ranní e-mail ho pak umí použít i bez prohlížeče."""
    user = _auth_user()
    if not user:
        return jsonify({"ok": False, "error": "Nepřihlášeno."}), 401
    r = (request.get_json(silent=True) or {}).get("risk")
    if r not in RISK_PROFILES:
        return jsonify({"ok": False, "error": "Neznámý profil."}), 400
    try:
        rec = kv_get_json(f"user:{user}")
        if not rec:
            return jsonify({"ok": False, "error": "Účet neexistuje."}), 404
        rec["risk"] = r
        kv_set_json(f"user:{user}", rec)
        return jsonify({"ok": True, "risk": r})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/admin/morning-test", methods=["POST"])
def admin_morning_test():
    """Pošle ranní souhrn na admina – test, že to celé funguje."""
    admin = _auth_admin()
    if not admin:
        return jsonify({"ok": False, "error": "Přístup jen pro admina."}), 403
    if not email_enabled():
        return jsonify({"ok": False, "error": "E-maily nejsou nastavené."}), 503
    try:
        top_opps = _top_opps_for_summary(3)
        ok = send_email(admin, "☀️ Ranní přehled (test) – MY ADVANTAGE",
                        build_morning_summary_html(admin, top_opps, _todays_signals()))
        return jsonify({"ok": ok, "sent_to": admin, "error": _LAST_EMAIL_ERROR["msg"]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _warm_plan_news():
    """Ráno projde 50 nejlepších titulů dle hodnoty+momenta: SEC 8-K a zprávy přes AI
    (příznak vážného průšvihu). Pak uživatel v „Kam s penězi" nečeká a pauza platí i
    pro tituly, které ještě nikdo nedrží."""
    scan = market_scan(allow_step=False) or {}
    ranks, _v, _s, names, _m = _scan_maps(scan)
    top = [t.replace(".", "-") for t, _r in sorted(ranks.items(), key=lambda x: x[1])[:50]]
    _red_flags(top)
    for i in range(0, len(top), 20):
        chunk = top[i:i + 20]
        _mine_digest(chunk, {t: names.get(t.replace("-", "."), t) for t in chunk})


@app.route("/api/cron/morning")
def cron_morning():
    # Ochrana (fail-closed): vyžaduje env CRON_SECRET. Vercel Cron posílá
    # Authorization: Bearer <CRON_SECRET>. Bez nastaveného secretu endpoint nic nedělá.
    err = _cron_auth()
    if err:
        return err
    if not (cloud_enabled() and email_enabled()):
        return jsonify({"ok": False, "error": "Chybí úložiště nebo e-mail."}), 503
    try:
        top_signals = _todays_signals(allow_step=True)
        top_opps = _top_opps_for_summary(3)
        # Archiv vzniká VŽDY, i když se nerozesílá – jinak by v historii na webu
        # zely díry za dny, kdy bylo posílání vypnuté.
        try:
            _brief_payload()
        except Exception:
            pass
        try:
            _warm_plan_news()
        except Exception:
            pass
        # Cron běží ve dvou UTC časech (6:00 a 7:00), aby jeden z nich vždy padl
        # na 8:00 pražského času – letního i zimního. Odešle jen ten správný.
        hour = _prague_hour()
        try:      # diagnostika: kdy cron opravdu běžel (Vercel může spouštět s posunem)
            kv_set_json("cron:run:morning", {"ts": int(time.time()), "prague_hour": hour,
                                             "send_hour": SEND_HOUR_PRAGUE})
        except Exception:
            pass
        if hour != SEND_HOUR_PRAGUE:
            return jsonify({"ok": True, "sent": 0, "skipped": True,
                            "prague_hour": hour, "send_hour": SEND_HOUR_PRAGUE})
        sent = _broadcast("morning", "☀️ Ranní přehled trhu – MY ADVANTAGE",
                          lambda em: build_morning_summary_html(em, top_opps, top_signals))
        return jsonify({"ok": True, "sent": sent, "prague_hour": hour,
                        "sending_enabled": digest_sending_enabled()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# Lokální test: python api/index.py -> http://127.0.0.1:5001
if __name__ == "__main__":
    app.run(debug=True, port=5001, host="127.0.0.1")
