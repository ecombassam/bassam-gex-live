# server.py — Bassam GEX NetGamma (Credit / Leap Edition)
# المتطلبات: pip install flask requests
import os, json, math, datetime as dt
from flask import Flask, request, Response, jsonify
import requests

app = Flask(__name__)

POLY_KEY = (os.environ.get("POLYGON_API_KEY") or "").strip()
BASE = "https://api.polygon.io/v3/snapshot/options"

# ──────────────────────────────
# أدوات مساعدة
# ──────────────────────────────
def jerr(msg, http=502, extra=None):
    body = {"error": msg}
    if extra is not None:
        body["data"] = extra
    return Response(json.dumps(body, ensure_ascii=False), status=http, mimetype="application/json")

def fetch_greeks(symbol: str):
    """جلب بيانات Snapshot/Greeks من Polygon."""
    if not POLY_KEY:
        return None, "POLYGON_API_KEY مفقود"
    url = f"{BASE}/{symbol.upper()}/greeks"
    r = requests.get(url, params={"apiKey": POLY_KEY}, timeout=30)
    if r.status_code != 200:
        return None, f"Polygon error {r.status_code}: {r.text[:200]}"
    data = r.json()
    results = data.get("results") or []
    return {"raw": results, "meta": data}, None

def get_underlying_price_from_any(result, fallback=math.nan):
    up = None
    if isinstance(result, dict):
        up = (result.get("underlying_price") or
              result.get("underlyingPrice") or
              (result.get("underlying_asset") or {}).get("price") or
              (result.get("underlyingAsset") or {}).get("price"))
    try:
        return float(up)
    except:
        return fallback

def aggregate_net_gamma_by_strike(results):
    """NetGamma = (Gamma*OI_call) - (Gamma*OI_put)."""
    agg = {}
    for c in results:
        greeks = c.get("greeks") or {}
        gamma  = greeks.get("gamma")
        oi     = c.get("open_interest") or c.get("openInterest")
        strike = c.get("strike_price") or c.get("strikePrice") or c.get("strike")
        typ    = (c.get("contract_type") or c.get("option_type") or "").lower()
        if not (gamma and oi and strike and typ in ["call", "put"]):
            continue
        try:
            g = float(gamma)
            oi = float(oi)
            k = float(strike)
        except:
            continue
        net = g * oi if typ == "call" else -g * oi
        agg[k] = agg.get(k, 0) + net
    return agg

def split_top_n_by_abs(agg, underlying_price, n=3):
    above = [(k, v) for k, v in agg.items() if k > underlying_price]
    below = [(k, v) for k, v in agg.items() if k < underlying_price]
    above.sort(key=lambda x: abs(x[1]), reverse=True)
    below.sort(key=lambda x: abs(x[1]), reverse=True)
    return above[:n], below[:n]

# ──────────────────────────────
# إنشاء كود PineScript
# ──────────────────────────────
def to_pine(symbol, underlying_price, top_above, top_below):
    def arr(nums):
        return ",".join(f"{x:.4f}" for x in nums)

    strikes_above = [k for k, _ in top_above]
    net_above     = [v for _, v in top_above]
    strikes_below = [k for k, _ in top_below]
    net_below     = [v for _, v in top_below]
    all_vals = (net_above + net_below) or [1.0]
    max_abs  = max([abs(x) for x in all_vals]) if all_vals else 1.0

    return f"""//@version=5
indicator("Bassam NetΓ (Polygon.io) — {symbol}", overlay=true, max_boxes_count=500, max_labels_count=500)

// ╭─────────────────────────────╮
// │ إعدادات الاستراتيجية        │
// ╰─────────────────────────────╯
strategyType = input.string("Credit", "نوع الاستراتيجية", options=["Credit", "Leap"])
daysRange = strategyType == "Credit" ? 7 : 30

// ╭─────────────────────────────╮
// │ إعدادات الشكل                │
// ╰─────────────────────────────╯
groupD = "Design"
barsW   = input.int(18, "عرض العمود (شموع)", minval=4, step=1, group=groupD)
baseThk = input.float(0.002, "سُمك الأساس", minval=0.0002, step=0.0002, group=groupD)
showLbl = input.bool(true, "إظهار الملصقات", group=groupD)
posCol  = input.color(color.new(color.lime, 0), "لون الموجب", group=groupD)
negCol  = input.color(color.new(color.red,  0), "لون السالب", group=groupD)

// ╭─────────────────────────────╮
// │ بيانات NetGamma              │
// ╰─────────────────────────────╯
var float uPrice = {underlying_price:.4f}
strikes_above = array.from({arr(strikes_above)})
net_above     = array.from({arr(net_above)})
strikes_below = array.from({arr(strikes_below)})
net_below     = array.from({arr(net_below)})

maxAbs = {max_abs:.8f}
norm(x) => maxAbs == 0 ? 0.0 : math.abs(x)/maxAbs

left  = bar_index - barsW
right = bar_index

draw_col(level, netg) =>
    n = norm(netg)
    col = netg > 0 ? color.new(posCol, 80-int(n*80)) : color.new(negCol, 80-int(n*80))
    half = uPrice * baseThk * (0.33 + n)
    box.new(left, level+half, right, level-half, bgcolor=col, border_color=col)
    if showLbl
        label.new(right, level, str.tostring(level) + " | NetΓ " + str.tostring(netg, format.mintick), textcolor=color.white, style=label.style_label_right, bgcolor=col)

for i=0 to array.size(strikes_above)-1
    draw_col(array.get(strikes_above,i), array.get(net_above,i))
for i=0 to array.size(strikes_below)-1
    draw_col(array.get(strikes_below,i), array.get(net_below,i))

plot(uPrice, "السعر الحالي", color=color.new(color.gray,0), linewidth=2)
label.new(bar_index, na, "Strategy: " + strategyType + " (" + str.tostring(daysRange) + " أيام)", style=label.style_label_left)
"""

# ──────────────────────────────
# Flask Endpoints
# ──────────────────────────────
@app.get("/")
def root():
    return jsonify({"ok": True, "service": "Bassam GEX NetGamma (Credit/Leap)", "docs": "/tv?symbol=AAPL"})

@app.get("/tv")
def tv_code():
    """يولّد كود Pine بناءً على NetGamma ومدة العقود."""
    symbol = (request.args.get("symbol") or "").upper().strip()
    if not symbol:
        return jerr("يرجى تمرير symbol، مثال: /tv?symbol=AAPL")

    try:
        n = int(request.args.get("n", "3"))
    except:
        n = 3
    strategy_type = (request.args.get("strategy") or "credit").lower()

    data, err = fetch_greeks(symbol)
    if err: return jerr(err)
    results = data["raw"]
    if not results:
        return jerr("لم يتم العثور على بيانات عقود")

    # 🧭 تحديد المدة الزمنية حسب نوع الاستراتيجية
    today = dt.date.today()
    if strategy_type == "credit":
        max_expiry = today + dt.timedelta(days=7)
    else:
        max_expiry = today + dt.timedelta(days=30)

    # تصفية العقود حسب تاريخ الانتهاء
    filtered = []
    for c in results:
        exp = c.get("expiration_date") or c.get("expirationDate")
        if not exp: continue
        try:
            exp_date = dt.date.fromisoformat(exp)
            if exp_date <= max_expiry:
                filtered.append(c)
        except:
            continue

    if not filtered:
        return jerr("لم يتم العثور على عقود ضمن النطاق الزمني")

    # السعر الأساسي
    u = get_underlying_price_from_any(filtered[0], fallback=math.nan)
    if math.isnan(u):
        for c in filtered:
            u = get_underlying_price_from_any(c, fallback=math.nan)
            if not math.isnan(u): break
    if math.isnan(u):
        return jerr("تعذّر تحديد السعر الحالي")

    agg = aggregate_net_gamma_by_strike(filtered)
    if not agg:
        return jerr("لا توجد قيم Net Gamma صالحة")

    top_above, top_below = split_top_n_by_abs(agg, u, n=n)
    pine = to_pine(symbol, u, top_above, top_below)
    return Response(pine, mimetype="text/plain; charset=utf-8")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
