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


def potential_score(q):
    """Vlastní 'Potenciál' 0-100 – kolik PROSTORU akcie má vyrůst (ne kolik už vyrostla).
    Kombinuje upside k ročnímu maximu, zdravou pozici v pásmu, prostor dle velikosti,
    rozumný roční trend a rostoucí zájem. Vrací (skóre, úroveň, odznaky, upside%, vol_ratio)."""
    price = _qf(q, "regularMarketPrice", 0) or 0
    vol = _qf(q, "regularMarketVolume", 0) or 0
    avg = _qf(q, "averageDailyVolume3Month", 0) or _qf(q, "averageDailyVolume10Day", 0) or 0
    yr = _qf(q, "fiftyTwoWeekChangePercent", 0) or 0
    mcap = _qf(q, "marketCap", 0) or 0
    hi52 = _qf(q, "fiftyTwoWeekHigh", 0) or 0
    lo52 = _qf(q, "fiftyTwoWeekLow", 0) or 0

    upside = ((hi52 - price) / price * 100) if (hi52 and price) else 0
    pos = ((price - lo52) / (hi52 - lo52)) if (hi52 and lo52 and hi52 > lo52) else 0.5
    vol_ratio = (vol / avg) if avg else 0
    badges = []
    score = 0.0

    # 1) Upside k ročnímu maximu (hlavní, až 45 b.)
    score += min(max(upside, 0), 120) / 120 * 45
    if upside >= 80:
        badges.append({"t": f"🎯 Potenciál +{upside:.0f}%", "k": "extreme"})
    elif upside >= 35:
        badges.append({"t": f"🎯 Potenciál +{upside:.0f}%", "k": "hot"})
    elif upside >= 12:
        badges.append({"t": f"🎯 Potenciál +{upside:.0f}%", "k": "up"})

    # 2) Zdravá pozice (ne padající nůž) (až 20 b.)
    if 0.2 <= pos <= 0.7:
        score += 20; badges.append({"t": "✅ Zdravá zóna", "k": "up"})
    elif 0.7 < pos <= 0.85:
        score += 12
    elif pos < 0.2:
        score += 6; badges.append({"t": "⚠️ U dna – vyšší riziko", "k": "down"})

    # 3) Prostor dle velikosti (až 15 b.)
    if mcap and mcap < 2e9:
        score += 15; badges.append({"t": "🏢 Small-cap prostor", "k": "gem"})
    elif mcap and mcap < 10e9:
        score += 10; badges.append({"t": "Mid-cap", "k": "neutral"})
    elif mcap and mcap < 50e9:
        score += 6
    elif mcap:
        score += 3; badges.append({"t": "Blue-chip", "k": "neutral"})

    # 4) Rozumný roční trend (až 12 b.) – odměň stabilní, potrestej kolaps
    if -25 <= yr <= 70:
        score += 12
    elif yr > 70:
        score += 6
    elif yr < -60:
        score -= 6; badges.append({"t": f"📉 {yr:.0f}% za rok", "k": "down"})
    else:
        score += 8

    # 5) Rostoucí zájem / objem (až 8 b.)
    score += min(vol_ratio, 3) / 3 * 8
    if vol_ratio >= 2:
        badges.append({"t": f"🔥 Zvýšený objem {vol_ratio:.1f}×", "k": "hot"})

    score = max(0, min(100, round(score)))
    if score >= 75:
        tier = "VYSOKÝ POTENCIÁL"
    elif score >= 60:
        tier = "SILNÝ"
    elif score >= 45:
        tier = "ZAJÍMAVÝ"
    else:
        tier = "SLEDOVAT"

    return score, tier, badges, round(upside, 1), vol_ratio


def opp_theses(top_items):
    """Jedna AI věta 'proč může vyrůst' pro top tituly, kešováno na den (1 volání/den)."""
    if not analysis_enabled() or not top_items:
        return {}
    date = _today()
    cache = kv_get_json(f"oppthesis:{date}") or {}
    missing = [it for it in top_items if it["ticker"] not in cache][:8]
    if missing:
        lines = [f'{it["ticker"]} ({it.get("name","")}): cena {it["price"]} {it.get("currency","")}, '
                 f'potenciál +{it.get("upside",0)}% k ročnímu maximu, roční změna {it.get("year_pct",0)}%'
                 for it in missing]
        prompt = ("Jsi akciový analytik. Pro každou akcii napiš JEDNU údernou českou větu (max 16 slov), "
                  "PROČ má potenciál výrazně vyrůst. Vrať POUZE validní JSON ve tvaru "
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
    """Scanner PŘÍLEŽITOSTÍ – vlastní 'Potenciál' (kolik prostoru akcie má vyrůst)
    z podhodnocených/růstových screenerů + AI věta proč u top titulů."""
    mode = request.args.get("mode", "all")
    screen_map = {
        "value": ["undervalued_growth_stocks", "undervalued_large_caps"],
        "growth": ["growth_technology_stocks"],
        "small": ["aggressive_small_caps", "small_cap_gainers"],
        "all": ["undervalued_growth_stocks", "growth_technology_stocks",
                "undervalued_large_caps", "aggressive_small_caps"],
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
                yr = _qf(q, "fiftyTwoWeekChangePercent", 0) or 0
                hi52 = _qf(q, "fiftyTwoWeekHigh", 0) or 0
                upside = ((hi52 - price) / price * 100) if (hi52 and price) else 0
                # Filtr kvality: žádné penny-junk, musí být prostor, ne totální kolaps
                if price < 1.5 or upside < 8 or yr < -85:
                    continue
                score, tier, badges, up, vol_ratio = potential_score(q)
                seen[sym] = {
                    "ticker": sym,
                    "name": q.get("shortName") or q.get("displayName") or q.get("longName") or sym,
                    "price": _round(price, 4),
                    "change_pct": _round(_qf(q, "regularMarketChangePercent", 0), 2, 0),
                    "currency": q.get("currency", "USD"),
                    "market_cap": _qf(q, "marketCap", None),
                    "volume": int(_qf(q, "regularMarketVolume", 0) or 0),
                    "vol_ratio": _round(vol_ratio, 1, 0),
                    "year_pct": _round(yr, 1, 0),
                    "upside": up,
                    "high52": _round(hi52, 4),
                    "low52": _round(_qf(q, "fiftyTwoWeekLow", None), 4),
                    "exchange": q.get("fullExchangeName") or q.get("exchange") or "",
                    "score": score,
                    "tier": tier,
                    "badges": badges,
                    "source": scr,
                }
        except Exception:
            continue

    items = sorted(seen.values(), key=lambda x: x["score"], reverse=True)[:40]

    # AI věta 'proč' pro top tituly (kešováno na den)
    try:
        theses = opp_theses(items[:8])
        for it in items[:8]:
            w = theses.get(it["ticker"])
            if w:
                it["why"] = w
    except Exception:
        pass

    return jsonify({
        "ok": True,
        "count": len(items),
        "ai": analysis_enabled(),
        "updated": datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
        "results": items,
    })


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
        pillars.append({"key": "valuation", "label": "Valuace", "weight": 0.20,
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
        pillars.append({"key": "growth", "label": "Růst & ziskovost", "weight": 0.25,
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
        pillars.append({"key": "analysts", "label": "Analytici", "weight": 0.22,
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
        pillars.append({"key": "sentiment", "label": "Nálada & instituce", "weight": 0.13,
                        "score": sentiment_score, "note": " · ".join(sub_notes)})

    # 6) SEC PODÁNÍ – oficiální americká data (Form 4 insider, 8-K materiální události).
    #    Nejsilnější first-hand signál, ale jen pro US akcie. Neamerické tituly ho nemají.
    sec_res = _sec_pillar_score(facts.get("ticker"))
    if sec_res:
        pillars.append({"key": "sec", "label": "Podání SEC", "weight": 0.10,
                        "score": sec_res[0], "note": sec_res[1]})

    # Sníží váhy ostatních pilířů proporčně, aby suma byla pořád ~1
    # (já volil 0.30/0.20/0.25/0.22/0.13 = 1.10; přepočet níže)

    # Kompozitní skóre – váhy přepočítané podle dostupných pilířů
    wsum = sum(p["weight"] for p in pillars)
    composite = round(sum(p["score"] * p["weight"] for p in pillars) / wsum) if wsum else 50
    verdict = "Koupit" if composite >= VERDICT_BUY else ("Prodat" if composite < VERDICT_SELL else "Držet")

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
        "coverage_pct": round(wsum * 100), "earnings_in": earnings_in,
        "pillars": [{**p, "weight": round(p["weight"], 2)} for p in pillars],
    }


def compute_levels(facts, horizon=10):
    """DETERMINISTICKÝ vstup/stop/cíl z volatility a horizontu (ne odhad AI).
    Šíře stop/cíl škáluje očekávaným pohybem za 'horizon' dní (sigma_H z roční
    volatility). Drží rozumné meze. Vrací i poměr zisk:riziko (R:R)."""
    price = facts.get("price")
    if not price:
        return None
    vol = facts.get("volatility_pct")
    sigma_h = ((vol / 100.0) / (252 ** 0.5) * (horizon ** 0.5)) if (vol and vol > 0) else 0.04
    stop_pct = max(0.05, min(2.0 * sigma_h, 0.20))
    tgt_pct = max(0.06, min(2.5 * sigma_h, 0.30))
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
            "HORIZONT je krátkodobý (~2 týdny, náš signál) – do pole 'horizon' napiš \"~2 týdny\" a scénáře "
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
            report["horizon"] = f"~2 týdny ({levels['horizon_days']} obch. dní)"

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


@app.route("/api/track-record")
def track_record():
    """Agregovaná úspěšnost dosavadních verdiktů (re-fetch aktuálních cen)."""
    try:
        verdicts = kv_lrange("verdicts", -200, -1)
        if not verdicts:
            return jsonify({"ok": True, "count": 0, "items": [], "stats": {}})
        # Aktuální ceny (unikátní tickery)
        prices = {}
        for t in {v.get("ticker") for v in verdicts if v.get("ticker")}:
            try:
                res = yahoo_chart(t, "1d", "1d")
                prices[t] = (res.get("meta") or {}).get("regularMarketPrice")
            except Exception:
                prices[t] = None
        items, buy_rets, buy_high_rets = [], [], []
        HIGH_CONF = 78  # práh „vysoká jistota" (skóre modelu)
        now = int(time.time())
        for v in verdicts:
            cur = prices.get(v.get("ticker"))
            entry = v.get("price")
            ret = ((cur - entry) / entry * 100) if (cur and entry) else None
            if v.get("verdict") == "Koupit" and ret is not None:
                buy_rets.append(ret)
                if isinstance(v.get("score"), (int, float)) and v["score"] >= HIGH_CONF:
                    buy_high_rets.append(ret)
            # Vyhodnocení EXIT pravidel (jen pro nákupní verdikty)
            exit_status, exit_label = None, None
            if v.get("verdict") == "Koupit" and cur:
                horizon = v.get("horizon_days", 10) or 10
                cal_days = round(horizon * 7 / 5)  # obchodní → kalendářní dny
                days_held = (now - (v.get("ts") or now)) / 86400
                if v.get("target") and cur >= v["target"]:
                    exit_status, exit_label = "target", "✅ Cíl zasažen — realizuj zisk"
                elif v.get("stop") and cur <= v["stop"]:
                    exit_status, exit_label = "stop", "🛑 Stop zasažen — vystup"
                elif days_held > cal_days:
                    exit_status, exit_label = "expired", "⏳ Horizont vypršel — přehodnoť"
                else:
                    exit_status, exit_label = "open", "Drží se (do cíle/stopu/horizontu)"
            items.append({**v, "current": _round(cur, 3), "return_pct": _round(ret, 2),
                          "exit_status": exit_status, "exit_label": exit_label})
        items = items[-60:][::-1]

        def _agg(rs):
            return {
                "count": len(rs),
                "avg_return": round(sum(rs) / len(rs), 2),
                "win_rate": round(sum(1 for r in rs if r > 0) / len(rs) * 100, 1),
            } if rs else None

        stats = {}
        if buy_rets:
            stats = {
                # ploché klíče kvůli zpětné kompatibilitě
                "buy_count": len(buy_rets),
                "buy_avg_return": round(sum(buy_rets) / len(buy_rets), 2),
                "buy_win_rate": round(sum(1 for r in buy_rets if r > 0) / len(buy_rets) * 100, 1),
                # rozpad podle jistoty
                "buy_all": _agg(buy_rets),
                "buy_high": _agg(buy_high_rets),
                "high_conf_threshold": HIGH_CONF,
            }
        return jsonify({"ok": True, "count": len(verdicts), "items": items, "stats": stats})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# BACKTEST – poctivé ověření techniky na historii (5 let, vč. medvědího 2022).
# Backtestuje se technické setup skóre (máme historické ceny). Fundamenty se
# zpětně (point-in-time) zdarma získat nedají, takže je do backtestu netaháme.
# Žádný look-ahead: na každém dni počítáme skóre jen z dat DO toho dne a měříme
# následný výnos za 'horizon' obchodních dní. Měříme i ALFU = výnos navíc proti
# indexu (SPY), což se nedá ošálit býčím trhem.
# ---------------------------------------------------------------------------
# Široký, sektorově/velikostně rozmanitý US basket (ne jen mega-cap v býčím trhu),
# aby byl backtest reprezentativní.
BACKTEST_BASKET = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "AMD",
    "JPM", "BAC", "V", "GS", "JNJ", "PFE", "UNH", "MRK",
    "WMT", "PG", "KO", "MCD", "NKE", "COST", "HD",
    "XOM", "CVX", "CAT", "BA", "DIS", "T", "INTC", "PLTR",
]


def _fetch_series(ticker, rng="5y"):
    """(dates, closes, vols) z denních dat – dates jako 'YYYY-MM-DD' kvůli zarovnání
    s indexem. Objem zarovnaný s cenou (jen dny, kde je cena)."""
    d = yahoo_chart(ticker, rng, "1d")
    ts = d.get("timestamp") or []
    q = ((d.get("indicators") or {}).get("quote") or [{}])[0]
    cl = q.get("close") or []
    vl = q.get("volume") or []
    dates, closes, vols = [], [], []
    for idx, (t, c) in enumerate(zip(ts, cl)):
        if c is not None:
            dates.append(datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d"))
            closes.append(c)
            v = vl[idx] if idx < len(vl) else None
            vols.append(v if v is not None else 0)
    return dates, closes, vols


def _backtest_ticker(ticker, spy=None, horizon=10, step=5, max_days=1260):
    """Vrací list (score, fwd_return%, excess_vs_SPY% | None, rok, is_top)."""
    out = []
    try:
        dates, closes, vols = _fetch_series(ticker, "5y")
    except Exception:
        return out
    n = len(closes)
    if n < 210 + horizon:
        return out
    i = max(210, n - max_days)
    while i + horizon < n:
        ind = compute_indicators(closes[:i + 1], vols[:i + 1])
        score = tech_setup_score(closes[i], ind)  # stejné skóre jako živý verdikt
        if score is not None:
            fwd = (closes[i + horizon] / closes[i] - 1) * 100
            exc = None
            d0, dH = dates[i], dates[i + horizon]
            if spy and d0 in spy and dH in spy and spy[d0]:
                exc = fwd - (spy[dH] / spy[d0] - 1) * 100
            # TOP = silný setup + klidný objem (stejná logika jako živě)
            out.append((score, fwd, exc, d0[:4], is_top_signal(score, ind)))
        i += step
    return out


def _bt_stats(rows):
    """rows = list (fwd, exc). Vrací trefnost, prům. výnos a alfu (vs index)."""
    if not rows:
        return None
    fwd = [r[0] for r in rows]
    exc = [r[1] for r in rows if r[1] is not None]
    wins = [x for x in fwd if x > 0]
    losses = [x for x in fwd if x <= 0]
    s = {"count": len(fwd),
         "win_rate": round(sum(1 for r in fwd if r > 0) / len(fwd) * 100, 1),
         "avg_return": round(sum(fwd) / len(fwd), 2),
         "avg_win": round(sum(wins) / len(wins), 2) if wins else 0,
         "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0}
    if exc:
        s["alpha"] = round(sum(exc) / len(exc), 2)
        s["beat_index_rate"] = round(sum(1 for e in exc if e > 0) / len(exc) * 100, 1)
    return s


def run_backtest(tickers, horizon=10):
    try:
        sd, sc, _ = _fetch_series("SPY", "5y")
        spy = dict(zip(sd, sc))
    except Exception:
        spy = {}
    samples, used = [], []
    for t in tickers:
        s = _backtest_ticker(t, spy, horizon)
        if s:
            samples += s
            used.append(t)
    if not samples:
        return None
    buys = [(fwd, exc) for (sc_, fwd, exc, yr, top_) in samples if sc_ >= VERDICT_BUY]
    tops = [(fwd, exc) for (sc_, fwd, exc, yr, top_) in samples if top_]
    holds = [(fwd, exc) for (sc_, fwd, exc, yr, top_) in samples if VERDICT_SELL <= sc_ < VERDICT_BUY]
    sells = [(fwd, exc) for (sc_, fwd, exc, yr, top_) in samples if sc_ < VERDICT_SELL]
    # Rozpad „Koupit" i „TOP" podle roku (důkaz, že signál drží i v medvědím 2022)
    by_year, top_year = {}, {}
    for (sc_, fwd, exc, yr, top_) in samples:
        if sc_ >= VERDICT_BUY:
            by_year.setdefault(yr, []).append((fwd, exc))
        if top_:
            top_year.setdefault(yr, []).append((fwd, exc))
    buy_by_year = {yr: _bt_stats(rows) for yr, rows in sorted(by_year.items())}
    top_by_year = {yr: _bt_stats(rows) for yr, rows in sorted(top_year.items())}
    return {
        "horizon_days": horizon, "period": "5 let (vč. propadu 2022)",
        "benchmark": "SPY" if spy else None,
        "tickers": used, "ticker_count": len(used), "sample_count": len(samples),
        "buy": _bt_stats(buys), "top": _bt_stats(tops),
        "hold": _bt_stats(holds), "sell": _bt_stats(sells),
        "baseline": _bt_stats([(fwd, exc) for (_, fwd, exc, yr, top_) in samples]),
        "buy_by_year": buy_by_year, "top_by_year": top_by_year, "top_threshold": VERDICT_TOP,
        "generated": int(time.time()),
        "note": f"Náš nákupní signál (MA Skóre), 5 let, žádný look-ahead. "
                f"Koupit = skóre ≥{VERDICT_BUY}, Prodat = <{VERDICT_SELL}. "
                f"TOP = silný setup + klidný objem (bez prodejního tlaku). "
                f"Alfa = výnos navíc proti indexu (SPY).",
    }


@app.route("/api/admin/backtest", methods=["POST"])
def admin_backtest():
    if not _auth_admin():
        return jsonify({"ok": False, "error": "Přístup jen pro admina."}), 403
    body = request.get_json(silent=True) or {}
    horizon = max(5, min(int(body.get("horizon", 10) or 10), 120))
    tickers = body.get("tickers") if isinstance(body.get("tickers"), list) else None
    tickers = [t.strip().upper() for t in (tickers or BACKTEST_BASKET) if str(t).strip()][:42]
    try:
        res = run_backtest(tickers, horizon)
        if not res:
            return jsonify({"ok": False, "error": "Backtest nevrátil data (zkus jiné tickery)."}), 502
        kv_set_json("backtest:latest", res)
        return jsonify({"ok": True, "result": res})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Chyba backtestu: {e}"}), 500


@app.route("/api/backtest")
def get_backtest():
    """Veřejně dostupný poslední backtest (důkaz úspěšnosti). Cachováno v KV."""
    try:
        return jsonify({"ok": True, "result": kv_get_json("backtest:latest")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _scan_signals():
    """Projede universum a vrátí akcie, které PRÁVĚ TEĎ splňují nákupní signál
    (setup skóre ≥ práh). Stejné skóre i úrovně jako hloubková analýza."""
    items = []
    for t in BACKTEST_BASKET:
        try:
            daily = yahoo_chart(t, "1y", "1d")
            meta = daily.get("meta", {})
            q = ((daily.get("indicators") or {}).get("quote") or [{}])[0]
            raw_cl = q.get("close") or []
            raw_vl = q.get("volume") or []
            closes, vols = [], []
            for k, c in enumerate(raw_cl):
                if c is not None:
                    closes.append(c)
                    v = raw_vl[k] if k < len(raw_vl) else None
                    vols.append(v if v is not None else 0)
            if len(closes) < 210:
                continue
            ind = compute_indicators(closes, vols)
            price = meta.get("regularMarketPrice") or closes[-1]
            score = tech_setup_score(price, ind)
            if score is None or score < VERDICT_BUY:  # jen aktuální nákupní signály
                continue
            lv = compute_levels({"price": price, "volatility_pct": ind.get("volatility")}, 10) or {}
            prev = meta.get("previousClose") or meta.get("chartPreviousClose") or price
            items.append({
                "ticker": t,
                "name": meta.get("shortName") or meta.get("longName") or t,
                "price": _round(price, 2),
                "change_pct": _round(((price - prev) / prev * 100) if prev else 0, 2, 0),
                "currency": meta.get("currency", "USD"),
                "score": score, "rsi": _round(ind.get("rsi"), 1), "note": _setup_note(score),
                "conviction": "top" if is_top_signal(score, ind) else "buy",
                "target": lv.get("target_price"), "stop": lv.get("stop_loss"),
                "reward_pct": lv.get("reward_pct"), "risk_pct": lv.get("risk_pct"), "rr": lv.get("rr"),
            })
        except Exception:
            continue
    items.sort(key=lambda x: x["score"], reverse=True)
    return {"count": len(items), "results": items, "universe": len(BACKTEST_BASKET),
            "horizon_days": 10, "buy_threshold": VERDICT_BUY,
            "updated": datetime.now(timezone.utc).strftime("%H:%M UTC"), "date": _today()}


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
        prev = meta.get("chartPreviousClose") or (closes[-2] if len(closes) >= 2 else price)
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
        prev = meta.get("previousClose") or meta.get("chartPreviousClose") or (closes[-2] if len(closes) >= 2 else price)
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
    "patria": {
        "label": "Patria",
        "flag": "🇨🇿",
        "url": "https://www.patria.cz/rss/zpravodajstvi.xml",
        "fallback": "https://www.patria.cz/rss/rss.xml",
    },
    "kurzy": {
        "label": "Kurzy.cz",
        "flag": "🇨🇿",
        "url": "https://www.kurzy.cz/rss/aktuality.xml",
        "fallback": "https://www.kurzy.cz/rss/",
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
    "reuters": {
        "label": "Reuters Business",
        "flag": "🌐",
        "url": "https://www.reutersagency.com/feed/?best-topics=business-finance&post_type=best",
        "fallback": "https://www.reutersagency.com/feed/?best-topics=business-finance",
    },
}


def _parse_rss(xml_text):
    """Minimalistický RSS/Atom parser – vrací list { title, link, pub_date, summary }."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml_text)
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
    if cached and (time.time() - cached.get("ts", 0) < 1800):
        return cached.get("items", [])
    items = []
    for u in [src["url"], src["fallback"]]:
        try:
            r = requests.get(u, headers=HEADERS, timeout=8)
            items = _parse_rss(r.text)
            if items:
                break
        except Exception:
            continue
    for it in items:
        it["source"] = src["label"]
        it["flag"] = src["flag"]
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

    # 1) Yahoo News (jen když je ticker) – nejrelevantnější k dané akci
    if ticker:
        try:
            r = requests.get(f"https://query2.finance.yahoo.com/v1/finance/search?q={quote(ticker)}&quotesCount=0&newsCount=12",
                             headers=HEADERS, timeout=6)
            data = r.json() or {}
            for n in (data.get("news") or [])[:12]:
                if not n.get("title"):
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


@app.route("/api/signals")
def get_signals():
    """Živé nákupní signály podle BACKTESTEM OVĚŘENÉHO modelu. Kešováno 1×/den."""
    date = _today()
    cached = kv_get_json(f"signals:{date}")
    if cached:
        return jsonify({"ok": True, "cached": True, **cached})
    out = _scan_signals()
    try:
        kv_set_json(f"signals:{date}", out)
    except Exception:
        pass
    return jsonify({"ok": True, "cached": False, **out})


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
            if score < VERDICT_SELL:
                level, reason = "sell", "Technicky slabé – signál k výstupu"
            elif sma200 and price < sma200:
                level, reason = "weak", "Oslabení dlouhodobého trendu"
            elif rsi is not None and rsi > 75:
                level, reason = "weak", "Překoupeno – zvaž výběr zisku"
            else:
                continue  # v pořádku → nezobrazovat
            prev = meta.get("previousClose") or meta.get("chartPreviousClose") or price
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
        prev = meta.get("previousClose") or meta.get("chartPreviousClose") or price
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
- Není to timing pro daytrading (horizont ~2 týdny)
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
    out = []
    try:
        for q in yahoo_screener("undervalued_growth_stocks", 25):
            price = _qf(q, "regularMarketPrice", 0) or 0
            hi52 = _qf(q, "fiftyTwoWeekHigh", 0) or 0
            up = ((hi52 - price) / price * 100) if (hi52 and price) else 0
            if price < 1.5 or up < 8:
                continue
            score, tier, badges, upside, vr = potential_score(q)
            out.append((score, q.get("symbol"), q.get("shortName") or q.get("symbol"), upside,
                        q.get("currency", "USD"), _round(price, 2)))
        out.sort(reverse=True)
    except Exception:
        pass
    return out[:n]


def build_morning_summary_html(email, top_opps, top_signals=None):
    pf = kv_get_json(f"portfolio:{email}") or {}
    watch = (pf.get("watchlist") or [])[:8]
    rows = ""
    for t in watch:
        try:
            res = yahoo_chart(t, "1d", "1d")
            meta = res.get("meta", {})
            price = meta.get("regularMarketPrice")
            prev = meta.get("previousClose") or meta.get("chartPreviousClose") or price
            chg = ((price - prev) / prev * 100) if prev else 0
            col = "#00C853" if chg >= 0 else "#FF3D00"
            rows += (f"<tr><td style='padding:6px 0'><b>{t}</b></td>"
                     f"<td style='padding:6px 0;text-align:right'>{round(price,2)} {meta.get('currency','')}</td>"
                     f"<td style='padding:6px 0;text-align:right;color:{col}'>{'+' if chg>=0 else ''}{chg:.2f}%</td></tr>")
        except Exception:
            continue
    watch_html = (f"<h3 style='font-size:16px;margin:18px 0 8px'>📊 Tvé sledované akcie</h3>"
                  f"<table style='width:100%;border-collapse:collapse;font-size:14px'>{rows}</table>") if rows else ""
    opp_html = ""
    if top_opps:
        items = "".join(
            f"<div style='padding:8px 0;border-top:1px solid #23262f'><b style='color:#00C853'>+{up:.0f}%</b> "
            f"&nbsp;<b>{sym}</b> <span style='color:#9ba1b0'>{name}</span></div>"
            for (sc, sym, name, up, cur, pr) in top_opps)
        opp_html = f"<h3 style='font-size:16px;margin:22px 0 8px'>🎯 Příležitosti s potenciálem</h3>{items}"
    sig_html = ""
    if top_signals:
        sitems = "".join(
            f"<div style='padding:8px 0;border-top:1px solid #23262f'>"
            f"<b style='color:#00C853'>Koupit</b> &nbsp;<b>{s.get('ticker')}</b> "
            f"<span style='color:#9ba1b0'>{s.get('name','')}</span><br>"
            f"<span style='color:#9ba1b0;font-size:12px'>skóre {s.get('score')} · cíl +{s.get('reward_pct')}% / stop −{s.get('risk_pct')}% · ~2 týdny</span></div>"
            for s in top_signals[:4])
        sig_html = ("<h3 style='font-size:16px;margin:22px 0 8px'>✅ Dnešní signály „Sleva v trendu"
                    "</h3>" + sitems)
    return _email_shell("Ranní přehled trhu ☀️",
                        "<p style='line-height:1.6'>Dobré ráno! Tady je tvůj dnešní přehled:</p>" +
                        watch_html + sig_html + opp_html +
                        "<p style='color:#9ba1b0;font-size:12px;margin-top:20px'>Notifikace vypneš v appce v profilu. "
                        "Není to investiční doporučení.</p>")


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
        sitems = "".join(
            f"<div style='padding:10px 0;border-top:1px solid #23262f'>"
            f"<b style='color:#00C853'>Koupit</b> &nbsp;<b>{s.get('ticker')}</b> "
            f"<span style='color:#9ba1b0'>{s.get('name','')}</span><br>"
            f"<span style='color:#9ba1b0;font-size:12px'>MA skóre {s.get('score')} · "
            f"cíl +{s.get('reward_pct')}% / stop −{s.get('risk_pct')}% · ~2 týdny</span></div>"
            for s in top_signals[:4])
        sig_html = "<h3 style='font-size:16px;margin:22px 0 10px'>🎯 Nejlepší signály týdne</h3>" + sitems

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
        items = "".join(
            f"<div style='padding:8px 0;border-top:1px solid #23262f'><b style='color:#00C853'>+{up:.0f}%</b> "
            f"&nbsp;<b>{sym}</b> <span style='color:#9ba1b0'>{name}</span></div>"
            for (sc, sym, name, up, cur, pr) in top_opps)
        opp_html = "<h3 style='font-size:16px;margin:22px 0 10px'>🚀 Příležitosti k prozkoumání</h3>" + items

    intro = ("<p style='line-height:1.6'>Hezký víkend! Tady je tvé <b>shrnutí týdne</b> " +
             "— jak si vedla tvá watchlist, nejlepší signály z modelu a klíčové události.</p>")

    cta = (f"<div style='text-align:center;margin:28px 0 12px'>"
           f"<a href='{APP_URL}/' style='display:inline-block;background:#FF7A00;color:#1a0e00;"
           f"padding:12px 26px;font-weight:700;text-decoration:none;border-radius:12px'>"
           f"🚀 Otevřít MY ADVANTAGE</a></div>")

    return _email_shell("Týdenní shrnutí 📊",
                        intro + perf_html + sig_html + events_html + opp_html + cta +
                        "<p style='color:#9ba1b0;font-size:12px;margin-top:20px'>"
                        "Sobotní digest posíláme, pokud jsi ho zapnul v profilu. "
                        "Vypneš ho tam samým přepínačem. Není to investiční doporučení.</p>")


@app.route("/api/cron/weekly-digest")
def cron_weekly_digest():
    """Sobotní ranní digest za uplynulý týden. Vercel Cron – secret required."""
    secret = os.environ.get("CRON_SECRET")
    auth = request.headers.get("Authorization", "")
    if not secret or (auth != f"Bearer {secret}" and request.args.get("secret") != secret):
        return jsonify({"ok": False, "error": "Neautorizováno."}), 401
    if not (cloud_enabled() and email_enabled()):
        return jsonify({"ok": False, "error": "Chybí úložiště nebo e-mail."}), 503
    sent = 0
    try:
        top_opps = _top_opps_for_summary(3)
        sig = kv_get_json(f"signals:{_today()}") or {}
        if not sig:
            try:
                sig = _scan_signals()
                kv_set_json(f"signals:{_today()}", sig)
            except Exception:
                sig = {}
        top_signals = (sig or {}).get("results") or []
        for email in kv_smembers("users")[:200]:
            rec = kv_get_json(f"user:{email}") or {}
            if not (rec.get("notif") or {}).get("weekly"):
                continue
            try:
                send_email(email, "📊 Týdenní shrnutí – MY ADVANTAGE",
                           build_weekly_digest_html(email, top_signals, top_opps))
                sent += 1
            except Exception:
                continue
        return jsonify({"ok": True, "sent": sent})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


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


@app.route("/api/cron/backtest")
def cron_backtest():
    """Týdně obnoví backtest a uloží do cache (`backtest:latest`), aby banner
    'Ověřeno backtestem' u zákazníků zůstal aktuální bez ruční obsluhy.
    Chráněno CRON_SECRET (fail-closed)."""
    secret = os.environ.get("CRON_SECRET")
    auth = request.headers.get("Authorization", "")
    if not secret or (auth != f"Bearer {secret}" and request.args.get("secret") != secret):
        return jsonify({"ok": False, "error": "Neautorizováno."}), 401
    if not cloud_enabled():
        return jsonify({"ok": False, "error": "Chybí úložiště."}), 503
    try:
        horizon = max(5, min(int(request.args.get("horizon", 10) or 10), 120))
        res = run_backtest(BACKTEST_BASKET, horizon)
        if not res:
            return jsonify({"ok": False, "error": "Backtest nevrátil data."}), 502
        kv_set_json("backtest:latest", res)
        return jsonify({"ok": True, "buy": res.get("buy"), "horizon_days": horizon})
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
        sig = kv_get_json(f"signals:{_today()}")
        if not sig:
            try:
                sig = _scan_signals(); kv_set_json(f"signals:{_today()}", sig)
            except Exception:
                sig = {}
        ok = send_email(admin, "☀️ Ranní přehled (test) – MY ADVANTAGE",
                        build_morning_summary_html(admin, top_opps, (sig or {}).get("results") or []))
        return jsonify({"ok": ok, "sent_to": admin, "error": _LAST_EMAIL_ERROR["msg"]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/cron/morning")
def cron_morning():
    # Ochrana (fail-closed): vyžaduje env CRON_SECRET. Vercel Cron posílá
    # Authorization: Bearer <CRON_SECRET>. Bez nastaveného secretu endpoint nic nedělá.
    secret = os.environ.get("CRON_SECRET")
    auth = request.headers.get("Authorization", "")
    if not secret or (auth != f"Bearer {secret}" and request.args.get("secret") != secret):
        return jsonify({"ok": False, "error": "Neautorizováno."}), 401
    if not (cloud_enabled() and email_enabled()):
        return jsonify({"ok": False, "error": "Chybí úložiště nebo e-mail."}), 503
    sent = 0
    try:
        top_opps = _top_opps_for_summary(3)
        # Dnešní signály – z denní cache, jinak dopočítej (a ulož do cache)
        sig = kv_get_json(f"signals:{_today()}")
        if not sig:
            try:
                sig = _scan_signals()
                kv_set_json(f"signals:{_today()}", sig)
            except Exception:
                sig = {}
        top_signals = (sig or {}).get("results") or []
        for email in kv_smembers("users")[:200]:
            rec = kv_get_json(f"user:{email}") or {}
            if not (rec.get("notif") or {}).get("morning"):
                continue
            try:
                send_email(email, "☀️ Ranní přehled trhu – MY ADVANTAGE",
                           build_morning_summary_html(email, top_opps, top_signals))
                sent += 1
            except Exception:
                continue
        return jsonify({"ok": True, "sent": sent})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# Lokální test: python api/index.py -> http://127.0.0.1:5001
if __name__ == "__main__":
    app.run(debug=True, port=5001, host="127.0.0.1")
