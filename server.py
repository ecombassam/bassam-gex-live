# server.py — Bassam GEX[Lite] v1.8 Lux Precision Fix (stable)
import os, json, math, datetime as dt
from flask import Flask, jsonify, Response
import requests

app = Flask(__name__)

# 🔹 إعدادات عامة
POLY_KEY  = (os.environ.get("POLYGON_API_KEY") or "").strip()
BASE_SNAP = "https://api.polygon.io/v3/snapshot/options"
TODAY     = dt.date.today

# ──────────────────────────────
# وظائف مساعدة
# ──────────────────────────────
def _err(msg, http=502, data=None, sym=None):
    body = {"error": msg}
    if data is not None: body["data"] = data
    if sym: body["symbol"] = sym.upper()
    return Response(json.dumps(body, ensure_ascii=False),
                    status=http, mimetype="application/json")

def _get(url, params=None):
    params = params or {}
    params["apiKey"] = POLY_KEY
    headers = {"Authorization": f"Bearer {POLY_KEY}"} if POLY_KEY else {}
    r = requests.get(url, params=params, headers=headers, timeout=30)
    try:
        j = r.json()
    except Exception:
        return r.status_code, {"raw": r.text}
    return r.status_code, j

def _need_key(): return not POLY_KEY

# ──────────────────────────────
# 🔹 1) إيجاد أقرب Weekly Expiration
# ──────────────────────────────
def find_first_weekly_date(symbol):
    url = f"{BASE_SNAP}/{symbol.upper()}"
    status, j = _get(url, {"greeks": "true", "limit": 100})
    if status != 200 or j.get("status") != "OK":
        return None, j
    rows = j.get("results") or []
    today = TODAY().isoformat()
    expiries = sorted({
        it.get("details", {}).get("expiration_date")
        for it in rows
        if it.get("details", {}).get("expiration_date") and it.get("details", {}).get("expiration_date") >= today
    })
    if not expiries:
        return None, {"why": "no expiries"}
    return expiries[0], None

# ──────────────────────────────
# 🔹 2) جلب سلسلة العقود للاكسباير المحدد
# ──────────────────────────────
def fetch_chain(symbol, exp):
    url = f"{BASE_SNAP}/{symbol.upper()}"
    status, j = _get(url, {"greeks": "true", "expiration_date": exp})
    if status != 200 or j.get("status") != "OK":
        return None, j
    return j.get("results") or [], None

# ──────────────────────────────
# 🔹 3) حساب CUMULATIVE GAMMA
# ──────────────────────────────
def cumulative_gamma(items):
    price = next(
        (it.get("underlying_asset", {}).get("price")
         for it in items if isinstance(it.get("underlying_asset", {}).get("price"), (int, float))),
        None
    )
    bucket = {}
    for it in items:
        d, g = it.get("details", {}), it.get("greeks", {})
        t, strike, gamma = d.get("contract_type"), d.get("strike_price"), g.get("gamma")
        if not isinstance(strike, (int, float)) or not isinstance(gamma, (int, float)):
            continue
        b = bucket.setdefault(strike, {"call": 0.0, "put": 0.0})
        if t == "call": b["call"] += gamma
        elif t == "put": b["put"] += gamma
    rows = [{"strike": k, "cum": v["call"] - v["put"], "call": v["call"], "put": v["put"]}
            for k, v in bucket.items()]
    rows.sort(key=lambda x: x["strike"])
    return price, rows

# ──────────────────────────────
# 🔹 4) تحديد الجدران CALL / PUT
# ──────────────────────────────
def pick_walls(rows, price, around=0.35, depth=3):
    lo, hi = price * (1 - around), price * (1 + around)
    filt = [r for r in rows if lo <= r["strike"] <= hi]
    pos = sorted([r for r in filt if r["cum"] > 0], key=lambda r: r["cum"], reverse=True)
    neg = sorted([r for r in filt if r["cum"] < 0], key=lambda r: r["cum"])
    return pos[:depth], neg[:depth]

# ──────────────────────────────
# 🔹 5) بناء سكربت PineScript النهائي
# ──────────────────────────────
def make_pine(symbol, exp, price, pos, neg):
    def norm(arr):
        base = abs(arr[0]["cum"]) if arr else 1
        return [{"strike": r["strike"], "pct": abs(r["cum"]) / base} for r in arr]

    calls, puts = norm(pos), norm(neg)
    c_strikes = ",".join(str(round(r["strike"], 2)) for r in calls) or "260,257.5"
    c_pcts    = ",".join(str(round(r["pct"], 2)) for r in calls) or "1.0,0.8"
    p_strikes = ",".join(str(round(r["strike"], 2)) for r in puts)  or "250,247.5"
    p_pcts    = ",".join(str(round(r["pct"], 2)) for r in puts)  or "0.04,0.02"

    title = f"Bassam GEX[Lite] • Σ CUMULATIVE (v1.8 Lux Precision Fix) | {symbol.upper()} | Exp {exp}"
    return f"""//@version=5
indicator("{title}", overlay=true, max_lines_count=500, max_labels_count=500)

// ───────── بيانات GEX الحية ─────────
calls_strikes = array.from({c_strikes})
calls_pct     = array.from({c_pcts})
puts_strikes  = array.from({p_strikes})
puts_pct      = array.from({p_pcts})

// 🌈 Lux Zones (CALL / PUT / Transition) — ترسم مرة واحدة فقط
if barstate.islast and array.size(calls_strikes) >= 2 and array.size(puts_strikes) >= 2
    callTop = array.get(calls_strikes, 0)
    callBot = array.get(calls_strikes, 1)
    putTop  = array.get(puts_strikes, 0)
    putBot  = array.get(puts_strikes, 1)
    hvlMid  = (putTop + callBot) / 2
    // 🟩 CALL Zone
    box.new(left=bar_index - 60, top=callTop, right=bar_index + 60, bottom=hvlMid,
        bgcolor=color.new(color.lime, 85))
    // 🟦 PUT Zone
    box.new(left=bar_index - 60, top=hvlMid, right=bar_index + 60, bottom=putBot,
        bgcolor=color.new(color.aqua, 85))
    // ⬜ Transition Zone
    box.new(left=bar_index - 60, top=callBot, right=bar_index + 60, bottom=putTop,
        bgcolor=color.new(color.gray, 90))

// 🟩 CALL Bars & Labels
for i = 0 to array.size(calls_strikes) - 1
    y = array.get(calls_strikes, i)
    pct = array.get(calls_pct, i)
    w = int(math.max(6, pct * 120))
    line.new(bar_index - 5, y, bar_index + w - 5, y, color=color.new(color.lime, 90), width=6)
    label.new(bar_index + w, y, str.tostring(math.round(pct * 100)) + "%",
        style=label.style_label_left, textcolor=color.white, color=color.new(color.lime, 70))

// 🔴 PUT Bars & Labels
for i = 0 to array.size(puts_strikes) - 1
    y = array.get(puts_strikes, i)
    pct = array.get(puts_pct, i)
    w = int(math.max(6, pct * 120))
    line.new(bar_index - 5, y, bar_index + w - 5, y, color=color.new(color.red, 90), width=6)
    label.new(bar_index + w, y, str.tostring(math.round(pct * 100)) + "%",
        style=label.style_label_left, textcolor=color.white, color=color.new(color.red, 70))

// 🌀 HVL Label
if barstate.islast
    label.new(bar_index + 5, (high + low) / 2, "HVL Σ 0DTE ({dt.date.today():%m/%d})",
        textcolor=color.aqua, style=label.style_label_left)
"""

# ──────────────────────────────
# 🔹 Routes
# ──────────────────────────────
@app.route("/")
def home():
    return jsonify({"ok": True, "usage": "/AAPL/pine  |  /AAPL/json"})

@app.route("/<symbol>/pine")
def pine(symbol):
    if _need_key():
        return _err("Missing POLYGON_API_KEY", 401)
    exp, e = find_first_weekly_date(symbol)
    if e:
        return _err("No expiry", 502, e)
    items, e2 = fetch_chain(symbol, exp)
    if e2:
        return _err("Invalid Polygon", 502, e2)
    price, rows = cumulative_gamma(items)
    pos, neg = pick_walls(rows, price)
    pine_code = make_pine(symbol, exp, price, pos, neg)
    return Response(pine_code, mimetype="text/plain")

@app.route("/<symbol>/json")
def js(symbol):
    if _need_key():
        return _err("Missing POLYGON_API_KEY", 401)
    exp, e = find_first_weekly_date(symbol)
    if e:
        return _err("No expiry", 502, e)
    items, e2 = fetch_chain(symbol, exp)
    if e2:
        return _err("Invalid Polygon", 502, e2)
    price, rows = cumulative_gamma(items)
    pos, neg = pick_walls(rows, price)
    return jsonify({
        "symbol": symbol.upper(),
        "expiry": exp,
        "price": round(price, 2) if price else None,
        "call_walls": pos[:3],
        "put_walls": neg[:3],
        "total_levels": len(rows)
    })

# ──────────────────────────────
# 🚀 Run
# ──────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
