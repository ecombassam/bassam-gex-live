# server.py
import os, math, json, datetime
from flask import Flask, jsonify, Response
import requests
from urllib.parse import urlencode

# ===== إعداد مفتاح بوليغون =====
POLY_KEY = os.environ.get("POLYGON_API_KEY") or os.environ.get("POLYGON_KEY") or ""
POLY_KEY = POLY_KEY.strip()
BASE = "https://api.polygon.io/v3/snapshot/options"
EXP_BASE = "https://api.polygon.io/v3/reference/options/expirations"

app = Flask(__name__)

print("✅ Polygon Key Loaded:", POLY_KEY[:6] + "..." if POLY_KEY else "❌ EMPTY")

# ===== جلب أقرب تاريخ Expiry =====
def get_next_expiry(symbol: str):
    """يرجع أقرب تاريخ Expiry (مثل 1st Weekly أو Optimal Monthly)"""
    if not POLY_KEY:
        return None
    url = f"{EXP_BASE}?ticker={symbol.upper()}"
    headers = {"Authorization": f"Bearer {POLY_KEY}"}
    r = requests.get(url, headers=headers, timeout=20)
    if r.status_code != 200:
        return None
    j = r.json()
    exps = j.get("results", [])
    today = datetime.date.today()
    for e in exps:
        try:
            d = datetime.date.fromisoformat(e)
            if d >= today:
                return e  # أول تاريخ بعد اليوم
        except:
            continue
    return None

# ===== جلب بيانات الـ Chain =====
def get_chain(symbol: str, expiration_date: str | None = None, max_pages: int = 8):
    if not POLY_KEY:
        return {"error": "Missing POLYGON_API_KEY env"}, []
    params = {"greeks": "true"}
    if expiration_date:
        params["expiration_date"] = expiration_date

    url = f"{BASE}/{symbol.upper()}"
    all_items, pages = [], 0
    cursor = None
    headers = {"Authorization": f"Bearer {POLY_KEY}"}

    while pages < max_pages:
        full = url + ("?" + urlencode(params) if cursor is None else f"?cursor={cursor}")
        r = requests.get(full, headers=headers, timeout=30)
        try:
            j = r.json()
        except Exception:
            return {"status": "ERROR", "http": r.status_code, "raw": r.text}, []
        if r.status_code != 200 or j.get("status") != "OK":
            return {"status": "ERROR", "http": r.status_code, "data": j}, []
        items = j.get("results") or []
        all_items.extend(items)
        cursor = j.get("next_url")
        if not cursor:
            break
        cursor = cursor.split("cursor=")[-1]
        pages += 1
    return None, all_items

# ===== حساب Σ CUMULATIVE Gamma =====
def cumulative_gamma_by_strike(items):
    underlying_price = None
    for it in items:
        p = it.get("underlying_asset", {}).get("price")
        if isinstance(p, (int, float)) and p > 0:
            underlying_price = p
            break

    buckets = {}
    for it in items:
        det = it.get("details", {})
        g = it.get("greeks", {})
        t = det.get("contract_type")
        strike = det.get("strike_price")
        gamma = g.get("gamma")
        if not (isinstance(strike, (int, float)) and isinstance(gamma, (int, float))):
            continue
        if strike not in buckets:
            buckets[strike] = {"call": 0.0, "put": 0.0}
        buckets[strike][t] += gamma

    rows = []
    for k, v in buckets.items():
        cum = v["call"] - v["put"]
        rows.append(
            {
                "strike": float(k),
                "cum_gamma": float(cum),
                "call_gamma": float(v["call"]),
                "put_gamma": float(v["put"]),
            }
        )
    rows.sort(key=lambda r: r["strike"])
    return underlying_price, rows

# ===== اختيار الجدران الأقوى =====
def pick_walls(rows, price, around_pct=0.35, depth=3):
    if not (isinstance(price, (int, float)) and price > 0):
        price = None
    flt = rows
    if price:
        lo, hi = price * (1 - around_pct), price * (1 + around_pct)
        flt = [r for r in rows if lo <= r["strike"] <= hi]

    pos = [r for r in flt if r["cum_gamma"] > 0]
    neg = [r for r in flt if r["cum_gamma"] < 0]
    pos.sort(key=lambda r: r["cum_gamma"], reverse=True)
    neg.sort(key=lambda r: r["cum_gamma"])
    return pos[:depth], neg[:depth]

def bar_len_from_pct(pct, max_len=120):
    pct = max(0.0, min(1.0, pct))
    return max(10, int(round(pct * max_len)))

# ===== إنشاء كود Pine =====
def make_pine(symbol: str, price: float, expiry: str, pos_rows, neg_rows, add_levels=4):
    strongest_pos = pos_rows[0]["cum_gamma"] if pos_rows else 0.0
    strongest_neg = abs(neg_rows[0]["cum_gamma"]) if neg_rows else 0.0

    def pack(arr, strongest, kind):
        out = []
        for r in arr:
            val = r["cum_gamma"]
            pct = (
                (val / strongest)
                if (strongest > 0 and kind == "call")
                else (abs(val) / strongest if strongest > 0 else 0.0)
            )
            out.append({"strike": r["strike"], "value": val, "pct": pct})
        return out

    calls = pack(pos_rows, strongest_pos, "call")
    puts = pack(neg_rows, strongest_neg, "put")

    pine_lines = []

    def emit_side(side, color_expr):
        for i, r in enumerate(side, start=1):
            s = r["strike"]
            pc = r["pct"]
            L = bar_len_from_pct(pc)
            txt = f'{int(round(pc * 100))}%'
            pine_lines.append(
                f"""
// {('CALL' if 'lime' in color_expr else 'PUT')} wall #{i}
line.new(bar_index, {s}, bar_index - 1000, {s}, extend=extend.left, color={color_expr}, width=1)
if barstate.islast
    var line bar{i}{'C' if 'lime' in color_expr else 'P'} = na
    if not na(bar{i}{'C' if 'lime' in color_expr else 'P'})
        line.delete(bar{i}{'C' if 'lime' in color_expr else 'P'})
    bar{i}{'C' if 'lime' in color_expr else 'P'} := line.new(bar_index, {s}, bar_index + {L}, {s}, color={color_expr}, width=6)
    label.new(bar_index + {L} + 2, {s}, "{txt}", textcolor=color.white, color=color.new({color_expr}, 0), style=label.style_label_left, size=size.large)
"""
            )

    emit_side(calls, "color.new(color.lime, 0)")
    emit_side(puts, "color.new(color.red, 0)")

    pine = f"""//@version=5
indicator("Bassam GEX – Σ CUMULATIVE | {symbol} | 1st Weekly | Exp {expiry}", overlay=true, max_lines_count=500, max_labels_count=500)
{''.join(pine_lines)}
"""
    return pine

# ===== الواجهات =====
@app.route("/")
def home():
    return jsonify({"ok": True, "usage": "/SYMBOL/pine  or  /SYMBOL (JSON summary)"}), 200

@app.route("/<symbol>")
def summary(symbol):
    expiry = get_next_expiry(symbol)
    err, items = get_chain(symbol, expiration_date=expiry)
    if err:
        return jsonify({"error": err, "symbol": symbol.upper()}), 502
    price, rows = cumulative_gamma_by_strike(items)
    pos, neg = pick_walls(rows, price, 0.35, 3)
    return jsonify({
        "symbol": symbol.upper(),
        "expiry": expiry,
        "price": round(price, 2) if price else None,
        "call_walls": pos,
        "put_walls": neg
    })

@app.route("/<symbol>/pine")
def pine(symbol):
    expiry = get_next_expiry(symbol)
    err, items = get_chain(symbol, expiration_date=expiry)
    if err:
        return Response(json.dumps({"error": err, "symbol": symbol.upper()}), mimetype="text/plain"), 502
    price, rows = cumulative_gamma_by_strike(items)
    pos, neg = pick_walls(rows, price, 0.35, 3)
    pine_code = make_pine(symbol.upper(), price, expiry, pos, neg, 4)
    header = f"// Generated from Polygon snapshot | Symbol={symbol.upper()} | Exp={expiry} | Price={round(price,2) if price else 'na'}\n"
    return Response(header + pine_code, mimetype="text/plain")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
