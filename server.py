# server.py — Bassam OI[Pro] v1.8 – Weekly & Monthly Credit Spread Analyzer
import os, json, datetime as dt, requests
from flask import Flask, jsonify, Response, request

app = Flask(__name__)
POLY_KEY  = (os.environ.get("POLYGON_API_KEY") or "").strip()
BASE_SNAP = "https://api.polygon.io/v3/snapshot/options"
TODAY     = dt.date.today

# ─────────────────────────────
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
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"error": "Invalid JSON"}

# ─────────────────────────────
def fetch_all(symbol):
    """يجلب جميع صفحات snapshot بحد 50"""
    url = f"{BASE_SNAP}/{symbol.upper()}"
    cursor, all_rows = None, []
    for _ in range(10):
        params = {"greeks": "true", "limit": 50}
        if cursor:
            params["cursor"] = cursor
        status, j = _get(url, params)
        if status != 200 or j.get("status") != "OK":
            break
        rows = j.get("results") or []
        all_rows.extend(rows)
        cursor = j.get("next_url")
        if not cursor:
            break
        cursor = cursor.split("cursor=")[-1]
    return all_rows

# ─────────────────────────────
def find_next_expiry(symbol, mode):
    """يحدد أقرب أسبوعية أو شهرية حسب نوع الاستراتيجية"""
    rows = fetch_all(symbol)
    if not rows:
        return None, {"why": "no option data"}, None

    today = TODAY().isoformat()
    expiries = sorted({
        r.get("details", {}).get("expiration_date")
        for r in rows if r.get("details", {}).get("expiration_date")
    })
    expiries = [d for d in expiries if d >= today]
    if not expiries:
        return None, {"why": "no upcoming expiry"}, None

    # الأسبوعية: تواريخ تنتهي خلال 7 أيام
    # الشهرية: تواريخ آخر جمعة من الشهر فقط
    if mode == "weekly":
        exp_target = expiries[0]
    else:  # monthly
        exp_target = next((d for d in expiries if d.endswith("-28") or d.endswith("-29") or d.endswith("-30") or d.endswith("-31")), expiries[-1])

    return exp_target, None, rows

# ─────────────────────────────
def analyze_oi(rows, expiry, mode):
    """يحسب أقوى مستويات CALL و PUT حسب النمط"""
    rows = [r for r in rows if r.get("details", {}).get("expiration_date") == expiry]
    price = None
    for r in rows:
        p = r.get("underlying_asset", {}).get("price")
        if isinstance(p, (int, float)) and p > 0:
            price = p
            break
    if not price:
        return None, [], []

    low, high = price * 0.8, price * 1.2
    rows = [
        r for r in rows
        if low <= (r.get("details", {}).get("strike_price") or 0) <= high
    ]

    calls, puts = [], []
    for r in rows:
        det = r.get("details", {})
        strike = det.get("strike_price")
        ctype = det.get("contract_type")
        oi = r.get("open_interest")
        if not (isinstance(strike, (int, float)) and isinstance(oi, (int, float))):
            continue
        if ctype == "call":
            calls.append((strike, oi))
        elif ctype == "put":
            puts.append((strike, oi))

    limit = 3 if mode == "weekly" else 6
    top_calls = sorted(calls, key=lambda x: x[1], reverse=True)[:limit]
    top_puts  = sorted(puts, key=lambda x: x[1], reverse=True)[:limit]
    return price, top_calls, top_puts

# ─────────────────────────────
def make_pine(symbol, exp, price, top_calls, top_puts, mode):
    def fmt(arr): return ",".join(str(round(x[0], 2)) for x in arr)
    def pct(arr):
        if not arr: return "1.0"
        base = arr[0][1]
        return ",".join(str(round(x[1] / base, 2)) for x in arr)

    title = f"Bassam OI[Pro] • {'Weekly' if mode=='weekly' else 'Monthly'} | {symbol.upper()} | Exp {exp}"
    return f"""//@version=5
indicator("{title}", overlay=true, max_lines_count=500, max_labels_count=500)

// Auto-fetched from Polygon snapshot
calls_strikes = array.from({fmt(top_calls)})
calls_pct     = array.from({pct(top_calls)})
puts_strikes  = array.from({fmt(top_puts)})
puts_pct      = array.from({pct(top_puts)})

if barstate.islast
    // 🟩 CALL Walls
    for i = 0 to array.size(calls_strikes) - 1
        y = array.get(calls_strikes, i)
        p = array.get(calls_pct, i)
        h = int(math.max(8, p * 150))
        line.new(bar_index - 5, y, bar_index + h, y, color=color.new(color.lime, 0), width=8)
        label.new(bar_index + h, y, "CALL " + str.tostring(y) + "\\n" + str.tostring(math.round(p * 100)) + "% OI", 
            style=label.style_label_left, textcolor=color.white, color=color.new(color.lime, 70))

    // 🟥 PUT Walls
    for i = 0 to array.size(puts_strikes) - 1
        y = array.get(puts_strikes, i)
        p = array.get(puts_pct, i)
        h = int(math.max(8, p * 150))
        line.new(bar_index - 5, y, bar_index + h, y, color=color.new(color.red, 0), width=8)
        label.new(bar_index + h, y, "PUT " + str.tostring(y) + "\\n" + str.tostring(math.round(p * 100)) + "% OI",
            style=label.style_label_left, textcolor=color.white, color=color.new(color.red, 70))

    // 💎 السعر الحالي
    line.new(bar_index - 10, {price}, bar_index + 50, {price}, color=color.new(color.aqua, 0), width=2)
    label.new(bar_index + 50, {price}, "Price: " + str.tostring({price}), textcolor=color.aqua)
"""

# ─────────────────────────────
@app.route("/<symbol>/pine")
def pine(symbol):
    if not POLY_KEY:
        return _err("Missing POLYGON_API_KEY", 401)
    mode = request.args.get("mode", "weekly").lower()
    exp, err, rows = find_next_expiry(symbol, mode)
    if err:
        return _err("Failed to find expiry", 502, err, symbol)
    price, top_calls, top_puts = analyze_oi(rows, exp, mode)
    pine_code = make_pine(symbol, exp, price, top_calls, top_puts, mode)
    return Response(pine_code, mimetype="text/plain")

@app.route("/<symbol>/json")
def json_route(symbol):
    if not POLY_KEY:
        return _err("Missing POLYGON_API_KEY", 401)
    mode = request.args.get("mode", "weekly").lower()
    exp, err, rows = find_next_expiry(symbol, mode)
    if err:
        return _err("Failed to find expiry", 502, err, symbol)
    price, top_calls, top_puts = analyze_oi(rows, exp, mode)
    return jsonify({
        "symbol": symbol.upper(),
        "mode": mode,
        "expiry": exp,
        "price": round(price, 2) if price else None,
        "call_walls": [{"strike": s, "oi": oi} for s, oi in top_calls],
        "put_walls": [{"strike": s, "oi": oi} for s, oi in top_puts]
    })

@app.route("/")
def home():
    return jsonify({
        "status": "OK ✅",
        "usage": {
            "weekly_json": "/AAPL/json?mode=weekly",
            "monthly_json": "/AAPL/json?mode=monthly",
            "weekly_pine": "/AAPL/pine?mode=weekly",
            "monthly_pine": "/AAPL/pine?mode=monthly"
        },
        "author": "Bassam OI[Pro] v1.8 – Weekly & Monthly Analyzer"
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
