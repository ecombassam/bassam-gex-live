# server.py — Bassam OI[Lite] v1.0 – Weekly OI Walls Analyzer
import os, json, datetime as dt, requests
from flask import Flask, jsonify, Response

app = Flask(__name__)

# إعدادات عامة
POLY_KEY  = (os.environ.get("POLYGON_API_KEY") or "").strip()
BASE_SNAP = "https://api.polygon.io/v3/snapshot/options"
TODAY     = dt.date.today

# 🔹 وظائف مساعدة
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

def next_friday():
    today = TODAY()
    days_ahead = 4 - today.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    return (today + dt.timedelta(days=days_ahead)).isoformat()

# 🔹 جلب بيانات Options من بوليغون
def fetch_oi_data(symbol):
    url = f"{BASE_SNAP}/{symbol.upper()}"
    status, j = _get(url, {"greeks": "false", "limit": 250})
    if status != 200 or j.get("status") != "OK":
        return None, j
    rows = j.get("results") or []
    if not rows: return None, {"why": "no option data"}

    target_exp = next_friday()
    filtered = [r for r in rows if r.get("details", {}).get("expiration_date") == target_exp]
    if not filtered: return None, {"why": f"no contracts for {target_exp}"}
    return filtered, target_exp

# 🔹 استخراج OI الأعلى فوق وتحت السعر
def analyze_oi(rows):
    price = next((r.get("underlying_asset", {}).get("price") for r in rows if isinstance(r.get("underlying_asset", {}).get("price"), (int, float))), None)
    calls, puts = [], []

    for r in rows:
        det = r.get("details", {})
        strike = det.get("strike_price")
        ctype = det.get("contract_type")
        oi = r.get("open_interest")
        if not (isinstance(strike, (int, float)) and isinstance(oi, (int, float))): continue
        if ctype == "call": calls.append((strike, oi))
        elif ctype == "put": puts.append((strike, oi))

    # فصل حسب السعر الحالي
    calls_above = [(s, oi) for s, oi in calls if s >= price]
    puts_below  = [(s, oi) for s, oi in puts if s <= price]

    top_calls = sorted(calls_above, key=lambda x: x[1], reverse=True)[:3]
    top_puts  = sorted(puts_below, key=lambda x: x[1], reverse=True)[:3]
    return price, top_calls, top_puts

# 🔹 بناء كود PineScript
def make_pine(symbol, exp, price, top_calls, top_puts):
    def fmt(arr): return ",".join(str(round(x[0], 2)) for x in arr)
    def pct(arr):
        if not arr: return "1.0"
        base = arr[0][1]
        return ",".join(str(round(x[1] / base, 2)) for x in arr)

    title = f"Bassam OI[Lite] • Open Interest Walls (v1.0) | {symbol.upper()} | Exp {exp}"
    return f"""//@version=5
indicator("{title}", overlay=true, max_lines_count=500, max_labels_count=500)

calls_strikes = array.from({fmt(top_calls)})
calls_pct     = array.from({pct(top_calls)})
puts_strikes  = array.from({fmt(top_puts)})
puts_pct      = array.from({pct(top_puts)})

if barstate.islast
    // CALL Walls
    for i = 0 to array.size(calls_strikes) - 1
        y = array.get(calls_strikes, i)
        p = array.get(calls_pct, i)
        w = int(math.max(6, p * 120))
        line.new(bar_index - 5, y, bar_index + w - 5, y, color=color.new(color.lime, 0), width=6)
        label.new(bar_index + w, y, str.tostring(math.round(p * 100)) + "% OI", style=label.style_label_left, textcolor=color.white, color=color.new(color.lime, 70))

    // PUT Walls
    for i = 0 to array.size(puts_strikes) - 1
        y = array.get(puts_strikes, i)
        p = array.get(puts_pct, i)
        w = int(math.max(6, p * 120))
        line.new(bar_index - 5, y, bar_index + w - 5, y, color=color.new(color.red, 0), width=6)
        label.new(bar_index + w, y, str.tostring(math.round(p * 100)) + "% OI", style=label.style_label_left, textcolor=color.white, color=color.new(color.red, 70))

    // HVL Label
    label.new(bar_index + 5, (high + low)/2, "OI Σ Weekly ({exp})", textcolor=color.aqua, style=label.style_label_left)
"""

# 🔹 المسارات
@app.route("/<symbol>/pine")
def pine(symbol):
    if not POLY_KEY: return _err("Missing POLYGON_API_KEY", 401)
    data, exp = fetch_oi_data(symbol)
    if not data: return _err("No OI data", 502)
    price, top_calls, top_puts = analyze_oi(data)
    pine_code = make_pine(symbol, exp, price, top_calls, top_puts)
    return Response(pine_code, mimetype="text/plain")

@app.route("/<symbol>/json")
def json_route(symbol):
    if not POLY_KEY: return _err("Missing POLYGON_API_KEY", 401)
    data, exp = fetch_oi_data(symbol)
    if not data: return _err("No OI data", 502)
    price, top_calls, top_puts = analyze_oi(data)
    return jsonify({
        "symbol": symbol.upper(),
        "expiry": exp,
        "price": round(price, 2),
        "call_walls": [{"strike": s, "oi": oi} for s, oi in top_calls],
        "put_walls": [{"strike": s, "oi": oi} for s, oi in top_puts]
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
