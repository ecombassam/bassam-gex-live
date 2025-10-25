# server.py — Bassam OI[Pro] v3.4 – IV Enhanced Edition (Weekly OR Monthly)
import os, json, datetime as dt, requests
from flask import Flask, jsonify, Response

app = Flask(__name__)
POLY_KEY  = (os.environ.get("POLYGON_API_KEY") or "").strip()
BASE_SNAP = "https://api.polygon.io/v3/snapshot/options"
TODAY     = dt.date.today

#────────────────────────────────────────────
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
    try: return r.status_code, r.json()
    except Exception: return r.status_code, {"error": "Invalid JSON"}

#────────────────────────────────────────────
def fetch_all(symbol):
    url = f"{BASE_SNAP}/{symbol.upper()}"
    cursor, all_rows = None, []
    for _ in range(10):
        params = {"limit": 50}
        if cursor: params["cursor"] = cursor
        status, j = _get(url, params)
        if status != 200 or j.get("status") != "OK": break
        rows = j.get("results") or []
        all_rows.extend(rows)
        cursor = j.get("next_url")
        if not cursor: break
        cursor = cursor.split("cursor=")[-1]
    return all_rows

#────────────────────────────────────────────
def find_expiries(rows):
    expiries = sorted({
        r.get("details", {}).get("expiration_date")
        for r in rows if r.get("details", {}).get("expiration_date")
    })
    today = TODAY().isoformat()
    return [d for d in expiries if d >= today]

def analyze_oi_iv(rows, expiry, limit):
    rows = [r for r in rows if r.get("details", {}).get("expiration_date") == expiry]
    if not rows:
        return [], []
    calls, puts = [], []
    for r in rows:
        det = r.get("details", {})
        greeks = r.get("greeks", {})
        strike = det.get("strike_price")
        ctype  = det.get("contract_type")
        oi     = r.get("open_interest")
        iv     = greeks.get("iv")
        if not (isinstance(strike, (int, float)) and isinstance(oi, (int, float)) and isinstance(iv, (int, float))):
            continue
        if ctype == "call":
            calls.append((strike, oi, iv))
        elif ctype == "put":
            puts.append((strike, oi, iv))
    top_calls = sorted(calls, key=lambda x: x[1], reverse=True)[:limit]
    top_puts  = sorted(puts, key=lambda x: x[1], reverse=True)[:limit]
    return top_calls, top_puts

#────────────────────────────────────────────
@app.route("/<symbol>/json")
def json_route(symbol):
    if not POLY_KEY:
        return _err("Missing POLYGON_API_KEY", 401)
    rows = fetch_all(symbol)
    expiries = find_expiries(rows)
    if not expiries:
        return _err("No upcoming expiries found", 404, {"why": "empty list"}, symbol)

    exp_week = expiries[0]
    w_calls, w_puts = analyze_oi_iv(rows, exp_week, 3)
    exp_month = next((d for d in expiries if d.endswith("-28") or d.endswith("-29") or d.endswith("-30") or d.endswith("-31")), expiries[-1])
    m_calls, m_puts = analyze_oi_iv(rows, exp_month, 6)

    return jsonify({
        "symbol": symbol.upper(),
        "weekly": {
            "expiry": exp_week,
            "call_walls": [{"strike": s, "oi": oi, "iv": round(iv, 3)} for s, oi, iv in w_calls],
            "put_walls": [{"strike": s, "oi": oi, "iv": round(iv, 3)} for s, oi, iv in w_puts]
        },
        "monthly": {
            "expiry": exp_month,
            "call_walls": [{"strike": s, "oi": oi, "iv": round(iv, 3)} for s, oi, iv in m_calls],
            "put_walls": [{"strike": s, "oi": oi, "iv": round(iv, 3)} for s, oi, iv in m_puts]
        }
    })

#────────────────────────────────────────────
@app.route("/<symbol>/pine")
def pine_route(symbol):
    if not POLY_KEY:
        return _err("Missing POLYGON_API_KEY", 401)
    rows = fetch_all(symbol)
    expiries = find_expiries(rows)
    if not expiries:
        return _err("No upcoming expiries found", 404, {"why": "empty list"}, symbol)

    exp_week = expiries[0]
    w_calls, w_puts = analyze_oi_iv(rows, exp_week, 3)
    exp_month = next((d for d in expiries if d.endswith("-28") or d.endswith("-29") or d.endswith("-30") or d.endswith("-31")), expiries[-1])
    m_calls, m_puts = analyze_oi_iv(rows, exp_month, 6)

    def normalize(data):
        if not data: return []
        base = data[0][1]
        return [(s, round(oi / base, 2), round(iv, 2)) for s, oi, iv in data]

    w_calls_n, w_puts_n = normalize(w_calls), normalize(w_puts)
    m_calls_n, m_puts_n = normalize(m_calls), normalize(m_puts)

    title = f"Bassam OI[Pro] • v3.4 IV Enhanced | {symbol.upper()}"
    pine = f"""//@version=5
indicator("{title}", overlay=true, max_lines_count=500, max_labels_count=500)

mode = input.string("Weekly", "Expiry Mode", options=["Weekly","Monthly"], group="Settings")

draw_levels(_strikes, _pcts, _ivs, _base_col) =>
    for i = 0 to array.size(_strikes) - 1
        y = array.get(_strikes, i)
        p = array.get(_pcts, i)
        iv = array.get(_ivs, i)
        alpha = 90 - int(p * 70)
        bar_col = color.new(_base_col, alpha)
        bar_len = int(math.max(10, p * 120))
        line.new(bar_index - 5, y, bar_index + bar_len, y, color=bar_col, width=6)
        label.new(bar_index + bar_len + 2, y,
            str.tostring(int(p * 100)) + "% | IV " + str.tostring(iv, format.mintick),
            textcolor=color.white, style=label.style_none, size=size.small)

if barstate.islast
    if mode == "Weekly"
        weekly_calls = array.from({','.join(str(s) for s, _, _ in w_calls_n)})
        weekly_cpct  = array.from({','.join(str(p) for _, p, _ in w_calls_n)})
        weekly_civ   = array.from({','.join(str(iv) for _, _, iv in w_calls_n)})
        weekly_puts  = array.from({','.join(str(s) for s, _, _ in w_puts_n)})
        weekly_ppct  = array.from({','.join(str(p) for _, p, _ in w_puts_n)})
        weekly_piv   = array.from({','.join(str(iv) for _, _, iv in w_puts_n)})
        draw_levels(weekly_calls, weekly_cpct, weekly_civ, color.lime)
        draw_levels(weekly_puts, weekly_ppct, weekly_piv, color.red)
    if mode == "Monthly"
        monthly_calls = array.from({','.join(str(s) for s, _, _ in m_calls_n)})
        monthly_cpct  = array.from({','.join(str(p) for _, p, _ in m_calls_n)})
        monthly_civ   = array.from({','.join(str(iv) for _, _, iv in m_calls_n)})
        monthly_puts  = array.from({','.join(str(s) for s, _, _ in m_puts_n)})
        monthly_ppct  = array.from({','.join(str(p) for _, p, _ in m_puts_n)})
        monthly_piv   = array.from({','.join(str(iv) for _, _, iv in m_puts_n)})
        draw_levels(monthly_calls, monthly_cpct, monthly_civ, color.new(color.green, 0))
        draw_levels(monthly_puts, monthly_ppct, monthly_piv, color.new(color.red, 0))
"""

    return Response(pine, mimetype="text/plain")

#────────────────────────────────────────────
@app.route("/")
def home():
    return jsonify({
        "status": "OK ✅",
        "usage": {
            "json": "/AAPL/json",
            "pine": "/AAPL/pine"
        },
        "author": "Bassam OI[Pro] v3.4 – IV Enhanced Edition",
        "features": [
            "Weekly OR Monthly mode (exclusive)",
            "Color gradient by OI strength",
            "Displays IV next to percentage"
        ]
    })

#────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
