# server.py — Bassam OI[Lite] v1.2 – Weekly Credit Spread Analyzer
import os, json, datetime as dt, requests
from flask import Flask, jsonify, Response

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
def fetch_weekly_oi(symbol):
    """يجلب فقط العقود الأسبوعية من بوليغون"""
    url = f"{BASE_SNAP}/{symbol.upper()}"
    status, j = _get(url, {"limit": 500})
    if status != 200 or j.get("status") != "OK":
        return None, None, j

    rows = j.get("results") or []
    if not rows:
        return None, None, {"why": "no option data"}

    today = TODAY().isoformat()

    # 🔹 نختار فقط العقود الأسبوعية (رموزها تحتوي على W أو Exp < الشهرية)
    weekly_rows = [r for r in rows if "W" in (r.get("details", {}).get("ticker") or "")]
    if not weekly_rows:
        weekly_rows = rows  # fallback لو مافي "W"

    expiries = sorted({r.get("details", {}).get("expiration_date") for r in weekly_rows if r.get("details", {}).get("expiration_date")})
    exp_target = next((d for d in expiries if d >= today), expiries[-1] if expiries else None)

    filtered = [r for r in weekly_rows if r.get("details", {}).get("expiration_date") == exp_target]
    if not filtered:
        return None, exp_target, {"why": "no weekly contracts"}
    return filtered, exp_target, None

# ─────────────────────────────
def analyze_oi(rows):
    price = None
    for r in rows:
        p = r.get("underlying_asset", {}).get("price")
        if isinstance(p, (int, float)) and p > 0:
            price = p
            break

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

    calls_above = [(s, oi) for s, oi in calls if not price or s >= price]
    puts_below  = [(s, oi) for s, oi in puts if not price or s <= price]

    top_calls = sorted(calls_above, key=lambda x: x[1], reverse=True)[:3]
    top_puts  = sorted(puts_below, key=lambda x: x[1], reverse=True)[:3]
    return price, top_calls, top_puts

# ─────────────────────────────
def make_pine(symbol, exp, price, top_calls, top_puts):
    def fmt(arr): return ",".join(str(round(x[0], 2)) for x in arr)
    def pct(arr):
        if not arr: return "1.0"
        base = arr[0][1]
        return ",".join(str(round(x[1] / base, 2)) for x in arr)

    title = f"Bassam OI[Lite] • Weekly Credit Spread Walls | {symbol.upper()} | Exp {exp}"
    return f"""//@version=5
indicator("{title}", overlay=true, max_lines_count=500, max_labels_count=500)

// Auto-fetched from Polygon snapshot (Weekly)
calls_strikes = array.from({fmt(top_calls)})
calls_pct     = array.from({pct(top_calls)})
puts_strikes  = array.from({fmt(top_puts)})
puts_pct      = array.from({pct(top_puts)})

if barstate.islast
    // CALL Walls (مناطق بيع سبريد كول)
    for i = 0 to array.size(calls_strikes) - 1
        y = array.get(calls_strikes, i)
        p = array.get(calls_pct, i)
        w = int(math.max(6, p * 120))
        line.new(bar_index - 5, y, bar_index + w - 5, y, color=color.new(color.lime, 0), width=6)
        label.new(bar_index + w, y, str.tostring(math.round(p * 100)) + "% OI", style=label.style_label_left, textcolor=color.white, color=color.new(color.lime, 70))

    // PUT Walls (مناطق بيع سبريد بوت)
    for i = 0 to array.size(puts_strikes) - 1
        y = array.get(puts_strikes, i)
        p = array.get(puts_pct, i)
        w = int(math.max(6, p * 120))
        line.new(bar_index - 5, y, bar_index + w - 5, y, color=color.new(color.red, 0), width=6)
        label.new(bar_index + w, y, str.tostring(math.round(p * 100)) + "% OI", style=label.style_label_left, textcolor=color.white, color=color.new(color.red, 70))

    label.new(bar_index + 5, (high + low)/2, "Weekly OI Σ (Exp {exp})", textcolor=color.aqua, style=label.style_label_left)
"""

# ─────────────────────────────
@app.route("/<symbol>/pine")
def pine(symbol):
    if not POLY_KEY:
        return _err("Missing POLYGON_API_KEY", 401)
    rows, exp_target, err = fetch_weekly_oi(symbol)
    if err:
        return _err("Failed to get weekly OI data", 502, err)
    price, top_calls, top_puts = analyze_oi(rows)
    pine_code = make_pine(symbol, exp_target, price, top_calls, top_puts)
    return Response(pine_code, mimetype="text/plain")

@app.route("/<symbol>/json")
def json_route(symbol):
    if not POLY_KEY:
        return _err("Missing POLYGON_API_KEY", 401)
    rows, exp_target, err = fetch_weekly_oi(symbol)
    if err:
        return _err("Failed to get weekly OI data", 502, err)
    price, top_calls, top_puts = analyze_oi(rows)
    return jsonify({
        "symbol": symbol.upper(),
        "expiry": exp_target,
        "price": round(price, 2) if price else None,
        "call_walls": [{"strike": s, "oi": oi} for s, oi in top_calls],
        "put_walls": [{"strike": s, "oi": oi} for s, oi in top_puts]
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
