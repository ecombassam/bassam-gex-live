# server.py
import os, json, math, datetime as dt
from flask import Flask, jsonify, Response
import requests
from urllib.parse import urlencode

# ──────────────────────────────────────────────────────────────────────────────
# 1) إعداد مفتاح Polygon (ندعم أكثر من اسم للمتغير لتفادي اللبس)
# ──────────────────────────────────────────────────────────────────────────────
POLY_KEY = (
    os.environ.get("POLYGON_API_KEY")
    or os.environ.get("POLYGON_KEY")
    or os.environ.get("POLYGON_APIKEY")
    or ""
).strip()

BASE_SNAPSHOT = "https://api.polygon.io/v3/snapshot/options"
BASE_REF      = "https://api.polygon.io/v3/reference/options/contracts"

app = Flask(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# أدوات اتصال: نرسل المفتاح بطريقتين (param + Authorization) لضمان القبول
# ──────────────────────────────────────────────────────────────────────────────
def poly_get(url: str, params: dict):
    """GET مع apiKey في الـ params + Authorization header كحل مزدوج."""
    if not POLY_KEY:
        return 401, {"status":"ERROR","error":"Missing POLYGON_API_KEY env"}
    p = dict(params or {})
    p["apiKey"] = POLY_KEY
    headers = {"Authorization": f"Bearer {POLY_KEY}"}
    r = requests.get(url, params=p, headers=headers, timeout=30)
    try:
        data = r.json()
    except Exception:
        data = {"status":"ERROR","raw": r.text}
    return r.status_code, data

# ──────────────────────────────────────────────────────────────────────────────
# أقرب تاريخ إنقضاء (نستخرج أقرب تاريخ >= اليوم) ونعتبره 1st Weekly
# ──────────────────────────────────────────────────────────────────────────────
def nearest_expiry(symbol: str) -> str | None:
    # نجيب حتى 200 عقد مرجعي ونأخذ أقرب تاريخ
    params = {
        "underlying_ticker": symbol.upper(),
        "order": "asc",
        "sort": "expiration_date",
        "limit": 200,
        "expired": "false",
    }
    code, data = poly_get(BASE_REF, params)
    if code != 200 or data.get("status") != "OK":
        return None
    dates = []
    today = dt.date.today()
    for c in data.get("results", []):
        exp = (c.get("expiration_date") or "").split("T")[0]
        try:
            d = dt.date.fromisoformat(exp)
        except Exception:
            continue
        if d >= today:
            dates.append(d)
    return min(dates).isoformat() if dates else None

# ──────────────────────────────────────────────────────────────────────────────
# سحب Snapshot Chain مع دعم الترميز (cursor)
# ──────────────────────────────────────────────────────────────────────────────
def get_chain(symbol: str, expiration_date: str | None = None, max_pages: int = 6):
    if not POLY_KEY:
        return {"status":"ERROR","error":"Missing POLYGON_API_KEY env"}, []
    url = f"{BASE_SNAPSHOT}/{symbol.upper()}"
    params = {"greeks": "true"}
    if expiration_date:
        params["expiration_date"] = expiration_date

    items, pages, cursor = [], 0, None
    while pages < max_pages:
        if cursor:
            # next_url من Polygon يحمل كل شيء، نحتاج فقط cursor
            q = {"cursor": cursor}
            code, data = poly_get(url, q)
        else:
            code, data = poly_get(url, params)

        if code != 200 or data.get("status") != "OK":
            return {"status":"ERROR", "http":code, "data":data}, []

        results = data.get("results") or []
        items.extend(results)
        nx = data.get("next_url")
        if not nx:
            break
        cursor = nx.split("cursor=")[-1]
        pages += 1
    return None, items

# ──────────────────────────────────────────────────────────────────────────────
# حساب Σ CUMULATIVE GEX لكل Strike
# ──────────────────────────────────────────────────────────────────────────────
def cumulative_gamma_by_strike(items):
    price = None
    for it in items:
        p = it.get("underlying_asset", {}).get("price")
        if isinstance(p, (int, float)) and p > 0:
            price = p
            break

    buckets = {}  # strike -> {"call":sum, "put":sum}
    for it in items:
        det = it.get("details", {})
        g   = it.get("greeks", {})
        t   = (det.get("contract_type") or "").lower()  # "call"/"put"
        k   = det.get("strike_price")
        gam = g.get("gamma")
        if t not in ("call", "put"):  # تجاهل الغير معروف
            continue
        if not (isinstance(k,(int,float)) and isinstance(gam,(int,float))):
            continue
        if k not in buckets:
            buckets[k] = {"call":0.0,"put":0.0}
        buckets[k][t] += gam

    rows = []
    for k,v in buckets.items():
        cum = v["call"] - v["put"]
        rows.append({
            "strike": float(k),
            "cum_gamma": float(cum),
            "call_gamma": float(v["call"]),
            "put_gamma":  float(v["put"]),
        })
    rows.sort(key=lambda r: r["strike"])
    return price, rows

# ──────────────────────────────────────────────────────────────────────────────
# اختيار أقوى الجدران (Calls/ Puts) داخل نطاق ±35% من السعر
# ──────────────────────────────────────────────────────────────────────────────
def pick_walls(rows, price, around_pct=0.35, depth=3, add_levels=4):
    lo, hi = None, None
    if isinstance(price, (int,float)) and price > 0:
        lo, hi = price*(1-around_pct), price*(1+around_pct)

    def within(r):
        if lo is None: return True
        return lo <= r["strike"] <= hi

    flt = [r for r in rows if within(r)]
    pos = [r for r in flt if r["cum_gamma"] > 0]  # CALL
    neg = [r for r in flt if r["cum_gamma"] < 0]  # PUT

    pos.sort(key=lambda r: r["cum_gamma"], reverse=True)
    neg.sort(key=lambda r: r["cum_gamma"])  # الأكثر سلباً أولاً

    # العمق الأساسي + المستويات الإضافية (7..10)
    pos_take = pos[: depth + add_levels]
    neg_take = neg[: depth + add_levels]

    # نحسب نسبة القوة لكل مستوى مقارنة بأقوى مستوى في جانبه
    def pack(arr):
        if not arr:
            return []
        strongest = abs(arr[0]["cum_gamma"])
        out = []
        for r in arr:
            pct = 0.0 if strongest == 0 else abs(r["cum_gamma"]) / strongest
            out.append({"strike": r["strike"], "pct": pct, "raw": r["cum_gamma"]})
        return out

    return pack(pos_take), pack(neg_take)

# ──────────────────────────────────────────────────────────────────────────────
# توليد كود Pine v5 يطابق واجهة Options GEX[Lite]
# ──────────────────────────────────────────────────────────────────────────────
def make_pine(symbol: str, title_suffix: str, price: float,
              call_levels, put_levels, base_depth=3, add_levels=4):
    # نقسم المستويات: الأساس (3) + الإضافي (حتى 4)
    calls_base = call_levels[:base_depth]
    puts_base  = put_levels[:base_depth]
    calls_add  = call_levels[base_depth: base_depth + add_levels]
    puts_add   = put_levels[base_depth: base_depth + add_levels]

    def emit_side(side, is_call: bool, start_idx: int):
        color_expr = "color.new(color.lime, 0)" if is_call else "color.new(color.red, 0)"
        side_tag   = "CALL" if is_call else "PUT"
        lines = []
        for i, r in enumerate(side, start=start_idx):
            s   = r["strike"]
            pct = r["pct"]
            bar_len = max(10, int(round(pct * 120)))  # مقياس البارات
            pct_txt = int(round(pct*100))
            ident   = f"bar{i}{'C' if is_call else 'P'}"
            lines.append(f"""
// {side_tag} wall #{i}
line.new(bar_index, {s}, bar_index - 1000, {s}, extend=extend.left, color={color_expr}, width=1)
var line {ident} = na
if barstate.islast
    if not na({ident})
        line.delete({ident})
    {ident} := line.new(bar_index, {s}, bar_index + {bar_len}, {s}, color={color_expr}, width=6)
    if showPct
        label.new(bar_index + {bar_len} + labelOff, {s}, str.format("{'{'}0,number{'}'}%", {pct_txt}),
                  textcolor=color.white, color=color.new({color_expr}, 0),
                  style=label.style_label_left, size=labelSize)
""")
        return "".join(lines)

    # كود Pine
    pine = f"""//@version=5
// Generated from Polygon snapshot | Symbol={symbol} | {title_suffix} | Price={round(price,2) if price else 'na'}
indicator("Bassam GEX – Σ CUMULATIVE | {symbol} | {title_suffix}", overlay=true, max_lines_count=600, max_labels_count=600)

// ── GEX SETTINGS ─────────────────────────────────────────────────────────────
grpGex = "{{ GEX SETTINGS }}"
_optim = input.string("[Optimal Monthly]", "Gamma Profile to Expiry ⚠ README ⚠", options=["[Optimal Monthly]","[Every Expiry]","[First Monthly]","[1st Weekly]","[2nd Weekly]","[3rd Weekly]","[4th Weekly]","[Next]","[First (if 0DTE, then that)]"], group=grpGex)
_ignoredDirect = input.string("", "... or directly:", group=grpGex)
calcMode = input.string("Σ CUMULATIVE", "GEX calculation method:", options=["Σ CUMULATIVE"], group=grpGex)
selHighlight = input.bool(true, "Enable selection highlight", group=grpGex)

// ── DESIGN SETTINGS ──────────────────────────────────────────────────────────
grpDesign = "{{ DESIGN SETTINGS }}"
enCallWalls = input.bool(true, "Call Gamma Walls", group=grpDesign, inline="CALL")
callColor   = input.color(color.new(color.lime, 0), "CALL color", group=grpDesign, inline="CALL")
callDepth   = input.int({base_depth}, "Depth", minval=1, maxval=10, group=grpDesign, inline="CALL")

enHVL   = input.bool(true, "HVL enabled", group=grpDesign, inline="HVL")
hvlColor= input.color(color.new(color.teal, 0), "", group=grpDesign, inline="HVL")

enPutWalls = input.bool(true, "PUT Gamma Walls", group=grpDesign, inline="PUT")
putColor   = input.color(color.new(color.red, 0), "And depth", group=grpDesign, inline="PUT")
putDepth   = input.int({base_depth}, "", minval=1, maxval=10, group=grpDesign, inline="PUT")

addCnt  = input.int({add_levels}, "Additional (7..10.) GEX levels  And lvl count", minval=0, maxval=4, group=grpDesign)
linePix = input.int(1, "Basic GEX line pixel width", group=grpDesign)
transAp = input.string("Every Border", "Transition Zones with appearance:", options=["Every Border","Disabled"], group=grpDesign)
sqzCol  = input.color(color.new(color.blue, 90), "Exteme/Squeeze Zone color", group=grpDesign)

// ── DISPLAY SETTINGS ─────────────────────────────────────────────────────────
grpDisp = "{{ DISPLAY SETTINGS }}"
fontSel = input.string("L", "Font Size", options=["S","M","L"], group=grpDisp, inline="F")
labelOff= input.int(2, "And label offset", group=grpDisp, inline="F")
enStrk  = input.bool(true, "Enable strikes", group=grpDisp)
enLines = input.bool(true, "Lines", group=grpDisp)
enBars  = input.bool(true, "Bars", group=grpDisp)
showPct = input.bool(true, "% with info", group=grpDisp)
extSel  = input.string("←", "If Lines enabled, then line Extension", options=["←","→","↔","none"], group=grpDisp)
showDbg = input.bool(false, "【 Show Debug Info 】", group=grpDisp)
inputsInStatus = input.bool(false, "Inputs in status line", group=grpDisp)

// تحويل حجم الخط
labelSize = fontSel == "S" ? size.small : fontSel == "M" ? size.normal : size.large

// ── بيانات الجدران (مضمَّنة من الخادم) ──────────────────────────────────────
// Calls (base + additional)
var float[] calls_strike = array.from({",".join([str(x["strike"]) for x in calls_base + calls_add])})
var float[] calls_pct    = array.from({",".join([str(round(x["pct"],6)) for x in calls_base + calls_add])})

// Puts (base + additional)
var float[] puts_strike = array.from({",".join([str(x["strike"]) for x in puts_base + puts_add])})
var float[] puts_pct    = array.from({",".join([str(round(x["pct"],6)) for x in puts_base + puts_add])})

// رسم
if enCallWalls
    // الأساسية
    for i = 0 to callDepth-1
        if i < array.size(calls_strike)
            k = array.get(calls_strike, i)
            p = array.get(calls_pct, i)
            line.new(bar_index, k, bar_index - 1000, k, extend=extend.left, color=callColor, width=linePix)
            if enBars
                L = math.max(10, math.round(p*120))
                var line l = na
                if barstate.islast
                    if not na(l)
                        line.delete(l)
                    l := line.new(bar_index, k, bar_index + L, k, color=callColor, width=6)
                    if showPct
                        label.new(bar_index + L + labelOff, k, str.format("{'{'}0,number{'}'}%", math.round(p*100)), textcolor=color.white, color=color.new(callColor,0), style=label.style_label_left, size=labelSize)
    // الإضافية
    for i = 0 to addCnt-1
        idx = callDepth + i
        if idx < array.size(calls_strike)
            k = array.get(calls_strike, idx)
            p = array.get(calls_pct, idx)
            line.new(bar_index, k, bar_index - 1000, k, extend=extend.left, color=callColor, width=linePix)
            if enBars
                L = math.max(10, math.round(p*120))
                var line la = na
                if barstate.islast
                    if not na(la)
                        line.delete(la)
                    la := line.new(bar_index, k, bar_index + L, k, color=callColor, width=6)
                    if showPct
                        label.new(bar_index + L + labelOff, k, str.format("{'{'}0,number{'}'}%", math.round(p*100)), textcolor=color.white, color=color.new(callColor,0), style=label.style_label_left, size=labelSize)

if enPutWalls
    for i = 0 to putDepth-1
        if i < array.size(puts_strike)
            k = array.get(puts_strike, i)
            p = array.get(puts_pct, i)
            line.new(bar_index, k, bar_index - 1000, k, extend=extend.left, color=putColor, width=linePix)
            if enBars
                L = math.max(10, math.round(p*120))
                var line lp = na
                if barstate.islast
                    if not na(lp)
                        line.delete(lp)
                    lp := line.new(bar_index, k, bar_index + L, k, color=putColor, width=6)
                    if showPct
                        label.new(bar_index + L + labelOff, k, str.format("{'{'}0,number{'}'}%", math.round(p*100)), textcolor=color.white, color=color.new(putColor,0), style=label.style_label_left, size=labelSize)

// Debug
if showDbg
    label.new(bar_index, high, "Calls="+str.tostring(array.size(calls_strike))+"  Puts="+str.tostring(array.size(puts_strike)), style=label.style_label_down, color=color.new(color.gray,80))
"""
    return pine

# ──────────────────────────────────────────────────────────────────────────────
# واجهات الويب
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/")
def home():
    return jsonify({"ok": True, "usage": "/SYMBOL/pine  or  /SYMBOL (JSON)"}), 200

@app.route("/<symbol>")
def summary(symbol):
    exp = nearest_expiry(symbol)  # أقرب تاريخ
    err, items = get_chain(symbol, expiration_date=exp)
    if err:
        return jsonify({"error": err, "symbol": symbol.upper()}), 502
    price, rows = cumulative_gamma_by_strike(items)
    calls, puts = pick_walls(rows, price, around_pct=0.35, depth=3, add_levels=4)
    return jsonify({
        "symbol": symbol.upper(),
        "expiry": exp,
        "price": round(price, 2) if price else None,
        "calls": calls,
        "puts":  puts,
        "total_contracts": len(items)
    })

@app.route("/<symbol>/pine")
def pine(symbol):
    exp = nearest_expiry(symbol)
    exp_title = f"1st Weekly | Exp {exp if exp else 'None'}"
    err, items = get_chain(symbol, expiration_date=exp)
    if err:
        # نطبع الخطأ كنص لتفادي الحجب داخل TradingView
        return Response(json.dumps({"error": err, "symbol": symbol.upper()}), mimetype="text/plain"), 502

    price, rows = cumulative_gamma_by_strike(items)
    calls, puts = pick_walls(rows, price, around_pct=0.35, depth=3, add_levels=4)
    pine_code = make_pine(symbol.upper(), exp_title, price, calls, puts, base_depth=3, add_levels=4)
    header = f"// Generated from Polygon snapshot | Symbol={symbol.upper()} | {exp_title} | Price={round(price,2) if price else 'na'}\n"
    return Response(header + pine_code, mimetype="text/plain")

if __name__ == "__main__":
    # طباعة للتأكد أن المفتاح لُوِّد (لن يظهر المفتاح كاملاً)
    print("Polygon Key Loaded:", (POLY_KEY[:6] + "...") if POLY_KEY else "EMPTY")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
