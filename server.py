# server.py — Bassam GEX Top-Gamma Generator (v1.0)
# متطلبات: pip install flask requests
import os, json, math, datetime as dt
from flask import Flask, request, Response, jsonify
import requests

app = Flask(__name__)

POLY_KEY = (os.environ.get("POLYGON_API_KEY") or "").strip()
BASE = "https://api.polygon.io/v3/snapshot/options"

def jerr(msg, http=502, extra=None):
    body = {"error": msg}
    if extra is not None: body["data"] = extra
    return Response(json.dumps(body, ensure_ascii=False), status=http, mimetype="application/json")

def fetch_chain(symbol: str):
    """
    يجلب سلسلة الخيارات عبر Option Chain Snapshot.
    """
    if not POLY_KEY:
        return None, "POLYGON_API_KEY مفقود"

    url = f"{BASE}/{symbol.upper()}"
    params = {"apiKey": POLY_KEY}
    all_results = []
    r = requests.get(url, params=params, timeout=30)
    if r.status_code != 200:
        return None, f"Polygon error {r.status_code}: {r.text[:200]}"
    data = r.json()
    results = data.get("results") or []
    all_results.extend(results)
    return {"raw": all_results, "meta": data}, None


def get_underlying_price(any_result, fallback=math.nan):
    # بعض الاستجابات تضع السعر ضمن كل عقد (underlyingPrice) أو في meta
    up = None
    # جرّب في عنصر عقد:
    if isinstance(any_result, dict):
        up = (any_result.get("underlyingPrice") or
              (any_result.get("underlying_asset") or {}).get("price"))
        # مسميات شائعة أخرى:
        if up is None:
            u = any_result.get("underlyingAsset") or any_result.get("underlying")
            if isinstance(u, dict):
                up = u.get("price") or u.get("lastTradePrice")
    try:
        return float(up)
    except:
        return fallback

def pick_nearest_expiry(results):
    # نختار أقرب تاريخ انتهاء من بين العقود (حتى تكون المستويات عملية)
    exp_dates = {}
    for c in results:
        exp = c.get("expiration_date") or c.get("expirationDate")
        if exp:
            exp_dates.setdefault(exp, 0)
            exp_dates[exp] += 1
    if not exp_dates:
        return None
    return sorted(exp_dates.keys())[0]  # الأقرب زمنياً بصيغة YYYY-MM-DD

def aggregate_gamma_by_strike(results, expiry=None):
    """
    نجمع |gamma| لكل strike (كول + بوت) على نفس تاريخ الانتهاء (إن تم تمريره).
    نرجع dict: strike -> abs_gamma_sum
    """
    agg = {}
    chosen = []
    for c in results:
        exp = c.get("expiration_date") or c.get("expirationDate")
        if expiry and exp != expiry:
            continue
        greeks = c.get("greeks") or {}
        g = greeks.get("gamma")
        strike = c.get("strike_price") or c.get("strikePrice") or c.get("strike")
        # تجاهل القيَم الناقصة
        if g is None or strike is None:
            continue
        try:
            g = float(g)
            k = float(strike)
        except:
            continue
        # نجمع |gamma| (قوة) بغض النظر عن النوع
        agg[k] = agg.get(k, 0.0) + abs(g)
        chosen.append(c)
    return agg, chosen

def split_top_n(agg, underlying_price, n=3):
    above = [(k, v) for k, v in agg.items() if k > underlying_price]
    below = [(k, v) for k, v in agg.items() if k < underlying_price]
    above.sort(key=lambda x: x[1], reverse=True)
    below.sort(key=lambda x: x[1], reverse=True)
    return above[:n], below[:n]

def to_pine(symbol, underlying_price, top_above, top_below):
    """
    يولّد سكربت Pine v5 يرسم مستويات STRIKES كأشرطة أفقية (box) ملونة بتدرّج حسب القوة.
    ملاحظة: تم اختيار box.new لتمثيل "أعمدة" عند مستويات السعر (Y)،
    وعرض العمود (السُمك) يعتمد على قوة الجاما (مُطبّع 0..1).
    يمكنك تعديل الإعدادات من Inputs أسفل السكربت.
    """
    def arr(nums):
        return ",".join(f"{x:.4f}" for x in nums)

    strikes_above = [k for k, _ in top_above]
    power_above   = [v for _, v in top_above]
    strikes_below = [k for k, _ in top_below]
    power_below   = [v for _, v in top_below]

    all_powers = (power_above + power_below) or [1.0]
    max_p = max(all_powers) if all_powers else 1.0

    pine = f"""//@version=5
indicator("Bassam GEX Top Γ (Auto from Polygon) — {symbol}", overlay=true, max_labels_count=500, max_boxes_count=500)

// ————— مدخلات التصميم —————
groupD = "Design"
colUp   = input.color(color.new(color.lime, 0), "لون أعلى السعر", group=groupD)
colDn   = input.color(color.new(color.red,  0), "لون أسفل السعر", group=groupD)
barsW   = input.int(18, "عرض العمود (عدد الشموع)", minval=4, step=1, group=groupD)
thick   = input.float(0.002, "سُمك العمود نسبة من السعر", minval=0.0002, step=0.0002, group=groupD)
showLbl = input.bool(true, "إظهار ملصق القوة", group=groupD)

// ————— بيانات مولّدة —————
var string _src = "Polygon.io Option Chain Snapshot"
var float uPrice = {underlying_price:.4f}

// أقوى 3 فوق + 3 تحت (strike، قوة مطلقة مجمعة)
strikes_above = array.from({arr(strikes_above)})
power_above   = array.from({arr(power_above)})
strikes_below = array.from({arr(strikes_below)})
power_below   = array.from({arr(power_below)})

// للتطبيع 0..1
maxPow = {max_p:.12f}
norm(x) => maxPow == 0 ? 0.0 : x / maxPow

// نرسم على آخر barsW شمعة كي تظهر الأعمدة على يمين الشارت
left  = bar_index - barsW
right = bar_index

// دالة رسم عمود عند مستوى السعر (كـ box)
draw_column(level, pwr, baseColor) =>
    n = norm(pwr)
    transp = 80 - int(n * 80)   // كلما زادت القوة قلّ الشفافية
    col = color.new(baseColor, transp)
    half = uPrice * thick * (0.33 + n)  // السُمك يتزايد مع القوة
    top    = level + half
    bottom = level - half
    b = box.new(left, top, right, bottom, border_color=col, bgcolor=col)
    if showLbl
        label.new(right, top, str.tostring(level) + " | Γ " + str.tostring(pwr, format.mintick), textcolor=color.white, style=label.style_label_right, bgcolor=col)
    b

// رسم الأعمدة
for i = 0 to array.size(strikes_above)-1
    draw_column(array.get(strikes_above, i), array.get(power_above, i), colUp)

for i = 0 to array.size(strikes_below)-1
    draw_column(array.get(strikes_below, i), array.get(power_below, i), colDn)

// خط السعر الحالي للمرجع
plot(uPrice, "السعر الحالي", color=color.new(color.gray, 0), linewidth=2)

// سياق المصدر
var label srcL = na
if barstate.islast
    if na(srcL)
        srcL := label.new(bar_index, uPrice, "Source: " + _src, style=label.style_label_lower_left, textcolor=color.white, bgcolor=color.new(color.silver, 40))
    else
        label.set_x(srcL, bar_index)
"""
    return pine

@app.get("/")
def root():
    return jsonify({"ok": True, "service": "Bassam GEX Top-Gamma Generator", "docs": "/tv?symbol=TSLA"})

@app.get("/tv")
def tv_code():
    """
    يولّد كود Pine v5 مباشر.
    بارامترات:
      symbol=TSLA (إلزامي)
      n=3 (اختيار أقوى N فوق وتحت)
      scope=nearest (افتراضي: أقرب انتهاء) | all (كل السلسلة)
    """
    symbol = (request.args.get("symbol") or "").upper().strip()
    if not symbol:
        return jerr("الباراميتر symbol مفقود. مثال: /tv?symbol=TSLA")

    try:
        n = int(request.args.get("n", "3"))
    except:
        n = 3
    scope = (request.args.get("scope") or "nearest").lower()

    data, err = fetch_chain(symbol)
    if err: return jerr(err)

    results = data["raw"]
    if not results:
        return jerr("لم يتم العثور على عقود خيارات")

    # السعر: خذه من أول عقد كمرجع
    u = get_underlying_price(results[0], fallback=math.nan)
    if math.isnan(u):
        # محاولة بديلة: بعض الاستجابات تضع السعر ضمن كل عقد بنفس الاسم
        for c in results:
            u = get_underlying_price(c, fallback=math.nan)
            if not math.isnan(u): break
    if math.isnan(u):
        return jerr("تعذّر تحديد السعر الحالي للأصل")

    expiry = None
    if scope == "nearest":
        expiry = pick_nearest_expiry(results)

    agg, _ = aggregate_gamma_by_strike(results, expiry=expiry)
    if not agg:
        return jerr("لا توجد قيم Gamma صالحة في السلسلة")

    top_above, top_below = split_top_n(agg, u, n=n)

    pine = to_pine(symbol, u, top_above, top_below)
    return Response(pine, mimetype="text/plain; charset=utf-8")

@app.get("/api/json")
def api_json():
    """
    بديل JSON لإرجاع القوائم فقط (للاختبار أو الاستخدام الحر):
    /api/json?symbol=TSLA&n=3&scope=nearest
    """
    symbol = (request.args.get("symbol") or "").upper().strip()
    n = int(request.args.get("n", "3"))
    scope = (request.args.get("scope") or "nearest").lower()
    data, err = fetch_chain(symbol)
    if err: return jerr(err)

    results = data["raw"]
    if not results:
        return jerr("لم يتم العثور على عقود خيارات")

    u = get_underlying_price(results[0], fallback=math.nan)
    if math.isnan(u):
        for c in results:
            u = get_underlying_price(c, fallback=math.nan)
            if not math.isnan(u): break
    if math.isnan(u):
        return jerr("تعذّر تحديد السعر الحالي للأصل")

    expiry = pick_nearest_expiry(results) if scope == "nearest" else None
    agg, _ = aggregate_gamma_by_strike(results, expiry=expiry)
    above, below = split_top_n(agg, u, n=n)
    return jsonify({
        "symbol": symbol,
        "underlying_price": u,
        "expiry_scope": "nearest" if expiry else "all",
        "expiry_used": expiry,
        "top_above": [{"strike": k, "gamma_abs": v} for k, v in above],
        "top_below": [{"strike": k, "gamma_abs": v} for k, v in below],
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
