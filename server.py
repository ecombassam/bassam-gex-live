# server.py — Bassam OI[Pro] v2.0 – Weekly + Monthly Combined JSON
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
def fetch_all(symbol):
    """يجلب جميع صفحات snapshot (حد 50)"""
    url = f"{BASE_SNAP}/{symbol.upper()}"
    cursor, all_rows = None, []
    for _ in range(10):
        params = {"limit": 50}
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
def find_expiries(rows):
    expiries = sorted({
        r.get("details", {}).get("expiration_date")
        for r in rows if r.get("details", {}).get("expiration_date")
    })
    today = TODAY().isoformat()
    return [d for d in expiries if d >= today]

def analyze_oi(rows, expiry, limit):
    rows = [r for r in rows if r.get("details", {}).get("expiration_date") == expiry]
    if not rows:
        return None, [], []

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

    top_calls = sorted(calls, key=lambda x: x[1], reverse=True)[:limit]
    top_puts  = sorted(puts, key=lambda x: x[1], reverse=True)[:limit]
    return price, top_calls, top_puts

# ─────────────────────────────
@app.route("/<symbol>/json")
def json_route(symbol):
    if not POLY_KEY:
        return _err("Missing POLYGON_API_KEY", 401)
    rows = fetch_all(symbol)
    expiries = find_expiries(rows)
    if not expiries:
        return _err("No upcoming expiries found", 404, {"why": "empty list"}, symbol)

    # أقرب أسبوعي (أول تاريخ)
    exp_week = expiries[0]
    price, w_calls, w_puts = analyze_oi(rows, exp_week, 3)

    # أقرب شهري (آخر جمعة أو تاريخ آخر الشهر)
    exp_month = next((d for d in expiries if d.endswith("-28") or d.endswith("-29") or d.endswith("-30") or d.endswith("-31")), expiries[-1])
    _, m_calls, m_puts = analyze_oi(rows, exp_month, 6)

    return jsonify({
        "symbol": symbol.upper(),
        "price": round(price, 2) if price else None,
        "weekly": {
            "expiry": exp_week,
            "call_walls": [{"strike": s, "oi": oi} for s, oi in w_calls],
            "put_walls": [{"strike": s, "oi": oi} for s, oi in w_puts]
        },
        "monthly": {
            "expiry": exp_month,
            "call_walls": [{"strike": s, "oi": oi} for s, oi in m_calls],
            "put_walls": [{"strike": s, "oi": oi} for s, oi in m_puts]
        }
    })
# ─────────────────────────────
@app.route("/<symbol>/pine")
def pine_route(symbol):
    """يُولّد كود PineScript مباشرة من نفس بيانات /json"""
    if not POLY_KEY:
        return _err("Missing POLYGON_API_KEY", 401)
    rows = fetch_all(symbol)
    expiries = find_expiries(rows)
    if not expiries:
        return _err("No upcoming expiries found", 404, {"why": "empty list"}, symbol)

    exp_week = expiries[0]
    price, w_calls, w_puts = analyze_oi(rows, exp_week, 3)
    exp_month = next((d for d in expiries if d.endswith("-28") or d.endswith("-29") or d.endswith("-30") or d.endswith("-31")), expiries[-1])
    _, m_calls, m_puts = analyze_oi(rows, exp_month, 6)

    title = f"Bassam OI[Pro] • Weekly+Monthly | {symbol.upper()}"
    pine_code = f"""//@version=5
indicator("{title}", overlay=true, max_lines_count=500, max_labels_count=500)

// السعر الحالي
price = {round(price, 2) if price else 'na'}

// Weekly
weekly_calls = array.from({','.join(str(s) for s, _ in w_calls)})
weekly_puts  = array.from({','.join(str(s) for s, _ in w_puts)})

// Monthly
monthly_calls = array.from({','.join(str(s) for s, _ in m_calls)})
monthly_puts  = array.from({','.join(str(s) for s, _ in m_puts)})

show_weekly = input.bool(true, "Show Weekly Levels")
show_monthly = input.bool(false, "Show Monthly Levels")

draw_levels(_arr, _col) =>
    for i = 0 to array.size(_arr) - 1
        y = array.get(_arr, i)
        line.new(bar_index - 5, y, bar_index + 120, y, color=_col, width=6)

if barstate.islast
    if show_weekly
        draw_levels(weekly_calls, color.new(color.lime, 0))
        draw_levels(weekly_puts, color.new(color.red, 0))
    if show_monthly
        draw_levels(monthly_calls, color.new(color.green, 40))
        draw_levels(monthly_puts, color.new(color.purple, 40))
    line.new(bar_index - 10, price, bar_index + 50, price, color=color.aqua, width=2)
"""

    return Response(pine_code, mimetype="text/plain")

@app.route("/")
def home():
    return jsonify({
        "status": "OK ✅",
        "usage": {
            "json": "/AAPL/json",
            "example_in_TV": "Use mode selector inside indicator settings"
        },
        "author": "Bassam OI[Pro] v2.0 – Dual Weekly & Monthly"
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
