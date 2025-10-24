# server.py
import os, math, json
from flask import Flask, jsonify, Response
import requests
from urllib.parse import urlencode

POLY_KEY = os.environ.get("POLYGON_API_KEY", "").strip()
BASE = "https://api.polygon.io/v3/snapshot/options"

app = Flask(__name__)

# ------- أدوات مساعده -------
def get_chain(symbol: str, expiration_date: str|None=None, max_pages:int=8):
    """يسحب Snapshot Chain من بوليغون مع ترقيم الصفحات (cursor) بدون أي بارامترات غير مسموحة."""
    if not POLY_KEY:
        return {"error":"Missing POLYGON_API_KEY env"}, []
    params = {"greeks":"true", "apiKey": POLY_KEY}
    if expiration_date:
        params["expiration_date"] = expiration_date

    url = f"{BASE}/{symbol.upper()}"
    all_items, pages = [], 0
    cursor = None
    while pages < max_pages:
        full = url + ("?" + urlencode(params) if cursor is None else f"?cursor={cursor}")
        r = requests.get(full, timeout=30)
        try:
            j = r.json()
        except Exception:
            return {"status":"ERROR", "http":r.status_code, "raw":r.text}, []
        if r.status_code != 200 or j.get("status") != "OK":
            return {"status":"ERROR", "http":r.status_code, "data":j}, []
        items = j.get("results") or []
        all_items.extend(items)
        cursor = j.get("next_url")
        if not cursor: break
        # next_url من بوليغون تحتوي كل شيء، نحتاج فقط قيمة cursor:
        cursor = cursor.split("cursor=")[-1]
        pages += 1
    return None, all_items

def cumulative_gamma_by_strike(items):
    """
    يحسب Σ CUMULATIVE GEX عند كل Strike = (مجموع Gamma للـ CALL) - (مجموع Gamma للـ PUT)
    ويعيد أيضًا السعر الحالي للأصل.
    """
    # سعر الأصل (من أول عنصر فيه underlying_asset)
    underlying_price = None
    for it in items:
        p = it.get("underlying_asset", {}).get("price")
        if isinstance(p, (int,float)) and p>0:
            underlying_price = p
            break

    buckets = {}  # strike -> {"call":sum_gamma, "put":sum_gamma}
    for it in items:
        det = it.get("details", {})
        g   = it.get("greeks", {})
        t   = det.get("contract_type")  # "call"/"put"
        strike = det.get("strike_price")
        gamma = g.get("gamma")
        if not (isinstance(strike,(int,float)) and isinstance(gamma,(int,float))):
            continue
        if strike not in buckets:
            buckets[strike] = {"call":0.0, "put":0.0}
        buckets[strike][t] += gamma

    rows = []
    for k,v in buckets.items():
        cum = v["call"] - v["put"]
        rows.append({"strike":float(k),
                     "cum_gamma": float(cum),
                     "call_gamma": float(v["call"]),
                     "put_gamma":  float(v["put"])})
    # رتب حسب السترَيك
    rows.sort(key=lambda r: r["strike"])
    return underlying_price, rows

def pick_walls(rows, price, around_pct=0.35, depth=3):
    """
    يختار أقوى الجدران:
     - CALL walls: أعلى قيم cum_gamma الموجبة
     - PUT  walls: أدنى قيم cum_gamma (السالبة)
    ضمن نطاق ±around_pct من السعر (افتراضي 35% زي معظم سكربتات GEX Lite).
    """
    if not (isinstance(price,(int,float)) and price>0):
        price = None
    flt = rows
    if price:
        lo, hi = price*(1-around_pct), price*(1+around_pct)
        flt = [r for r in rows if lo <= r["strike"] <= hi]

    pos = [r for r in flt if r["cum_gamma"]>0]
    neg = [r for r in flt if r["cum_gamma"]<0]
    pos.sort(key=lambda r: r["cum_gamma"], reverse=True)
    neg.sort(key=lambda r: r["cum_gamma"])  # الأكثر سلبًا أولًا
    return pos[:depth], neg[:depth]

def bar_len_from_pct(pct, max_len=120):
    """طول الشريط في البارات الأفقية داخل Pine (عدد البارات إلى اليمين)."""
    pct = max(0.0, min(1.0, pct))
    return max(10, int(round(pct*max_len)))

def make_pine(symbol:str, price:float, pos_rows, neg_rows, add_levels=4):
    """
    يولّد Pine v5 مطابق للإعدادات الافتراضية (Σ CUMULATIVE) مع:
      - Call walls (أخضر)
      - Put  walls (أحمر)
      - Lines + Bars + % with info مفعّلة
      - Line extension = ←  (يسار)
      - Depth = 3
      - Additional levels (7..10) = add_levels (افتراضي 4)
    """
    # أحسب نسب لكل جدار مقابل أقوى جدار من نفس النوع (زي المؤشر)
    strongest_pos = pos_rows[0]["cum_gamma"] if pos_rows else 0.0
    strongest_neg = abs(neg_rows[0]["cum_gamma"]) if neg_rows else 0.0

    def pack(arr, strongest, kind):
        out = []
        for r in arr:
            val = r["cum_gamma"]
            pct = (val/strongest) if (strongest>0 and kind=="call") else (abs(val)/strongest if strongest>0 else 0.0)
            out.append({"strike":r["strike"], "value":val, "pct":pct})
        return out

    calls = pack(pos_rows, strongest_pos, "call")
    puts  = pack(neg_rows, strongest_neg, "put")

    # Additional levels: نضيف من المتبقي حسب الترتيب حتى add_levels كحد أقصى لكل جانب
    # (إذا عندك بيانات أكثر، تقدر توسّع لاحقًا)
    pine_lines = []
    def emit_side(side, color_expr):
        for i, r in enumerate(side, start=1):
            s  = r["strike"]
            pc = r["pct"]
            L  = bar_len_from_pct(pc)  # طول الشريط
            txt = f'{int(round(pc*100))}%'
            pine_lines.append(f"""
// {('CALL' if color_expr.find('lime')!=-1 else 'PUT')} wall #{i}
line.new(bar_index, {s}, bar_index - 1000, {s}, extend=extend.left, color={color_expr}, width=1, style=line.style_solid)
var line bar{i}{'C' if color_expr.find('lime')!=-1 else 'P'} = na
if barstate.islast
    if not na(bar{i}{'C' if color_expr.find('lime')!=-1 else 'P'})
        line.delete(bar{i}{'C' if color_expr.find('lime')!=-1 else 'P'})
    bar{i}{'C' if color_expr.find('lime')!=-1 else 'P'} := line.new(bar_index, {s}, bar_index + {L}, {s}, color={color_expr}, width=6)
    label.new(bar_index + {L} + 2, {s}, "{txt}", textcolor=color.white, color=color.new({color_expr}, 0), style=label.style_label_left, size=size.large)
""")

    emit_side(calls, "color.new(color.lime, 0)")  # أخضر
    emit_side(puts,  "color.new(color.red,  0)")  # أحمر

    # سكربت Pine كامل:
    pine = f"""//@version=5
indicator("Bassam GEX – Σ CUMULATIVE | {symbol}", overlay=true, max_lines_count=500, max_labels_count=500)

// ========== Inputs (مطابقة للواجهة) ==========
grpGex = "{'{'} GEX SETTINGS {'}'}"
calcMode = input.string("Σ CUMULATIVE", "GEX calculation method:", options=["Σ CUMULATIVE"], group=grpGex)
selHighlight = input.bool(true, "Enable selection highlight", group=grpGex)

grpDesign = "{'{'} DESIGN SETTINGS {'}'}"
callDepth = input.int(3, "Depth", minval=1, maxval=10, group=grpDesign, inline="CALL")
callColor = input.color(color.new(color.lime,0), "CALL color", group=grpDesign, inline="CALL")

putDepth  = input.int(3, "Depth", minval=1, maxval=10, group=grpDesign, inline="PUT")
putColor  = input.color(color.new(color.red,0), "PUT color", group=grpDesign, inline="PUT")

grpDisp = "{'{'} DISPLAY SETTINGS {'}'}"
fontSize = input.string("L", "Font Size", options=["S","M","L"], group=grpDisp, inline="F")
labelOff = input.int(30, "And label offset", group=grpDisp, inline="F")
enStrk   = input.bool(true, "Enable strikes", group=grpDisp)
enLines  = input.bool(true, "Lines", group=grpDisp)
enBars   = input.bool(true, "Bars", group=grpDisp)
enPct    = input.bool(true, "% with info", group=grpDisp)
extSel   = input.string("←", "If Lines enabled, then line Extension", options=["←","→","↔","none"], group=grpDisp)

// ========== سعر مرجعي ==========
var float refPrice = na
if na(refPrice)
    refPrice := close

// ========== جدران ناتجة من الخادم (CALL/PUT) ==========
var string sym = "{symbol.upper()}"
// سيتم رسم الجدران أدناه:

{''.join(pine_lines)}
"""
    return pine

# ------- واجهات API -------
@app.route("/")
def home():
    return jsonify({"ok":True, "usage":"/SYMBOL/pine  or  /SYMBOL  (JSON summary)"}), 200

@app.route("/<symbol>")
def summary(symbol):
    err, items = get_chain(symbol)
    if err: 
        return jsonify({"error":err, "symbol":symbol.upper()}), 502
    price, rows = cumulative_gamma_by_strike(items)
    pos, neg = pick_walls(rows, price, around_pct=0.35, depth=3)

    return jsonify({
        "symbol": symbol.upper(),
        "price": round(price, 2) if price else None,
        "total_contracts": len(items),
        "call_walls": [{"strike":r["strike"], "cum_gamma":r["cum_gamma"]} for r in pos],
        "put_walls":  [{"strike":r["strike"], "cum_gamma":r["cum_gamma"]} for r in neg]
    })

@app.route("/<symbol>/pine")
def pine(symbol):
    err, items = get_chain(symbol)
    if err:
        # أعرض الخطأ داخل النص لكي تعرف السبب لو حصل
        return Response(json.dumps({"error":err,"symbol":symbol.upper()}), mimetype="text/plain"), 502

    price, rows = cumulative_gamma_by_strike(items)
    pos, neg = pick_walls(rows, price, around_pct=0.35, depth=3)

    pine_code = make_pine(symbol.upper(), price, pos, neg, add_levels=4)
    header = f"// Generated: Σ CUMULATIVE from Polygon snapshot | Symbol={symbol.upper()} | Price={round(price,2) if price else 'na'}\n"
    return Response(header + pine_code, mimetype="text/plain")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
