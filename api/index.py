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
import os
import json
import hmac
import hashlib
import base64
import secrets
import re
import time
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


def send_email(to_email, subject, html):
    """Pošle transakční e-mail přes Brevo. Vrací True/False. Nikdy nevyhodí výjimku."""
    if not email_enabled():
        return False
    try:
        r = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"api-key": os.environ["BREVO_API_KEY"],
                     "Content-Type": "application/json", "accept": "application/json"},
            data=json.dumps({
                "sender": {"email": os.environ["SENDER_EMAIL"], "name": "MY STOCKS"},
                "to": [{"email": to_email}],
                "subject": subject,
                "htmlContent": html,
            }), timeout=8)
        return r.status_code in (200, 201)
    except Exception:
        return False


def _email_shell(title, body_html):
    return f"""<div style="font-family:Arial,sans-serif;background:#0b0d12;padding:32px;color:#e8eaf0">
      <div style="max-width:520px;margin:0 auto;background:#12141c;border:1px solid #23262f;border-radius:18px;padding:32px">
        <div style="font-size:24px;font-weight:800;margin-bottom:18px">MY <span style="color:#FF7A00">STOCKS</span></div>
        <h2 style="font-size:20px;margin:0 0 14px">{title}</h2>
        {body_html}
        <p style="color:#9ba1b0;font-size:12px;margin-top:28px">Tento e-mail ti přišel z aplikace MY STOCKS.</p>
      </div></div>"""


def send_welcome_email(email):
    html = _email_shell(
        "Vítej v MY STOCKS! 🎉",
        "<p style='line-height:1.6'>Tvůj účet je připravený a právě ti začala "
        "<b>7denní zkušební verze plánu Pro</b> — máš odemčené všechny funkce: "
        "scanner příležitostí, technické hodnocení, doporučení analytiků i AI analýzu.</p>"
        "<p style='line-height:1.6'>Přejeme šťastnou ruku při investování!</p>")
    return send_email(email, "Vítej v MY STOCKS 🎉", html)


def send_reset_email(email, link):
    html = _email_shell(
        "Obnovení hesla",
        f"<p style='line-height:1.6'>Klikni na tlačítko a nastav si nové heslo. "
        f"Odkaz platí 1 hodinu.</p>"
        f"<p style='margin:22px 0'><a href='{link}' style='background:#FF7A00;color:#000;"
        f"text-decoration:none;padding:12px 22px;border-radius:10px;font-weight:700'>Nastavit nové heslo</a></p>"
        f"<p style='color:#9ba1b0;font-size:12px'>Pokud jsi o reset nežádal, e-mail ignoruj.</p>")
    return send_email(email, "Obnovení hesla – MY STOCKS", html)


# ---- Předplatné / plány ----
TRIAL_DAYS = 7
VALID_PLANS = ("start", "trader", "pro")


def admin_users():
    return set(x.strip().lower() for x in (os.environ.get("ADMIN_USERS", "")).split(",") if x.strip())


def effective_plan(rec):
    """Skutečně aktivní plán dle stavu účtu (trial/expirace)."""
    plan = (rec or {}).get("plan", "start")
    now = int(time.time())
    if plan == "comped":
        return "pro"
    if plan == "trial":
        return "pro" if now <= (rec.get("trial_ends") or 0) else "start"
    if plan in ("trader", "pro"):
        pu = rec.get("plan_until")
        if pu and now > pu:
            return "start"
        return plan
    return "start"


def subscription_info(username, rec):
    rec = rec or {}
    now = int(time.time())
    eff = effective_plan(rec)
    info = {
        "plan": rec.get("plan", "start"),
        "effective": eff,
        "trial_ends": rec.get("trial_ends"),
        "plan_until": rec.get("plan_until"),
        "is_admin": username in admin_users(),
    }
    if rec.get("plan") == "trial" and rec.get("trial_ends"):
        info["days_left"] = max(0, (rec["trial_ends"] - now) // 86400 + (1 if (rec["trial_ends"] - now) % 86400 else 0))
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


@app.route("/api/account/status")
def account_status():
    return jsonify({"cloud": cloud_enabled()})


@app.route("/api/register", methods=["POST"])
def register():
    if not cloud_enabled():
        return jsonify({"ok": False, "error": "Účty nejsou nastavené (chybí úložiště)."}), 400
    body = request.get_json(silent=True) or {}
    email = _norm_email(body.get("email"))
    password = body.get("password") or ""
    if not EMAIL_RE.match(email):
        return jsonify({"ok": False, "error": "Zadej platný e-mail."}), 400
    if len(password) < 4:
        return jsonify({"ok": False, "error": "Heslo musí mít aspoň 4 znaky."}), 400
    invite = (body.get("invite") or "").strip().lower()
    try:
        if kv_get_json(f"user:{email}"):
            return jsonify({"ok": False, "error": "Účet s tímto e-mailem už existuje."}), 409

        now = int(time.time())
        plan, trial_ends = "trial", now + TRIAL_DAYS * 86400

        # Zvací kód → plný přístup (Pro) natrvalo, kód se spotřebuje
        if invite:
            inv = kv_get_json(f"invite:{invite}")
            if not inv:
                return jsonify({"ok": False, "error": "Neplatný zvací kód."}), 400
            if inv.get("used_by"):
                return jsonify({"ok": False, "error": "Tento kód už byl použit."}), 409
            plan, trial_ends = "comped", None
            inv["used_by"] = email
            inv["used_at"] = now
            kv_set_json(f"invite:{invite}", inv)

        salt, ph = hash_password(password)
        rec = {"salt": salt, "hash": ph, "created": now, "plan": plan, "email": email}
        if trial_ends:
            rec["trial_ends"] = trial_ends
        kv_set_json(f"user:{email}", rec)
        kv_set_json(f"portfolio:{email}", {"watchlist": [], "positions": {}, "alerts": []})
        kv_sadd("users", email)
        send_welcome_email(email)
        return jsonify({"ok": True, "token": make_token(email), "user": email,
                        "subscription": subscription_info(email, rec)})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Chyba úložiště: {e}"}), 500


@app.route("/api/login", methods=["POST"])
def login():
    if not cloud_enabled():
        return jsonify({"ok": False, "error": "Účty nejsou nastavené (chybí úložiště)."}), 400
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
    if len(password) < 4:
        return jsonify({"ok": False, "error": "Heslo musí mít aspoň 4 znaky."}), 400
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
        return jsonify({"ok": True, "user": user, "subscription": subscription_info(user, rec)})
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
    if plan not in VALID_PLANS and plan not in ("comped", "trial"):
        return jsonify({"ok": False, "error": "Neplatný plán."}), 400
    try:
        rec = kv_get_json(f"user:{username}")
        if not rec:
            return jsonify({"ok": False, "error": "Uživatel neexistuje."}), 404
        rec["plan"] = plan
        now = int(time.time())
        if plan in ("trader", "pro"):
            rec["plan_until"] = (now + int(days) * 86400) if days else None
        elif plan == "trial":
            rec["trial_ends"] = now + int(days or TRIAL_DAYS) * 86400
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


@app.route("/api/admin/invites", methods=["GET", "POST"])
def admin_invites():
    if not _auth_admin():
        return jsonify({"ok": False, "error": "Přístup jen pro admina."}), 403
    try:
        if request.method == "POST":
            count = int((request.get_json(silent=True) or {}).get("count", 1))
            count = max(1, min(count, 20))
            created = []
            for _ in range(count):
                code = secrets.token_hex(4)  # 8 znaků
                kv_set_json(f"invite:{code}", {"created": int(time.time()), "used_by": None})
                kv_sadd("invites", code)
                created.append(code)
            return jsonify({"ok": True, "created": created})
        # GET – seznam kódů
        out = []
        for code in kv_smembers("invites"):
            inv = kv_get_json(f"invite:{code}") or {}
            out.append({"code": code, "used_by": inv.get("used_by"),
                        "created": inv.get("created"), "used_at": inv.get("used_at")})
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

    # Fundamenty (P/E, EPS, beta, dividenda)
    try:
        m = (fh("stock/metric", {"symbol": sym, "metric": "all"}) or {}).get("metric", {}) or {}
        out["pe_ratio"] = _round(m.get("peTTM") or m.get("peBasicExclExtraTTM"), 2)
        out["eps"] = _round(m.get("epsTTM"), 2)
        out["beta"] = _round(m.get("beta"), 2)
        out["dividend_yield"] = _round(m.get("dividendYieldIndicatedAnnual"), 2)
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
            "tech_rating": technical_rating(price, indicators),
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
