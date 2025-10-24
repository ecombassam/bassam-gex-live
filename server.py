import os, math, datetime as dt
from datetime import timezone, timedelta
from flask import Flask, request, jsonify, Response
import requests

app = Flask(__name__)

POLYGON_KEY = os.environ.get("POLYGON_API_KEY") or os.environ.get("POLYGON_KEY")
POLY_BASE = "https://api.polygon.io"

# ---------- Utilities ----------

def third_friday(d: dt.date) -> dt.date:
    # ثالث جمعة من شهر تاريخ d
    first = d.replace(day=1)
    # weekday(): Monday=0 ... Sunday=6  -> we need Friday=4
    first_friday = first + timedelta(days=(4 - first.weekday()) % 7)
    return first_friday + timedelta(weeks=2)

def nearest_monthly_after(today: dt.date) -> dt.date:
    # أقرب ثالث جمعة >= اليوم
    cand = third_friday(today)
    return cand if cand >= today else third_friday((today.replace(day=1) + timedelta(days=32)).replace(day=1))

def fetch_polygon(url: str, params: dict):
    params = dict(params or {})
    if POLYGON_KEY:
        params["apiKey"] = POLYGON_KEY
    r = requests.get(url, params=params, timeout=30)
    try:
        data = r.json()
    except Exception:
        r.raise_for_status()
    if r.status_code >= 400 or data.get("status") == "ERROR":
        raise RuntimeError({"status": "ERROR", "http": r.status_code, "data": data})
    return data

def fetch_chain(symbol: str, expiry: str | None, greeks=True, limit=500):
    """يجيب Snapshot Chain + Pagination (إن وجد)، ويعيد list بالكونتراكتات + price."""
    url = f"{POLY_BASE}/v3/snapshot/options/{symbol.upper()}"
    params = {"greeks": "true" if greeks else "false", "limit": limit}
    if expiry:
        params["expiration_date"] = expiry
    out, cursor = [], None
    price = None
    for _ in range(12):  # سقف أمان للـ pagination
        if cursor:
            params["cursor"] = cursor
        data = fetch_polygon(url, params)
        res = data.get("results", []) or []
        for o in res:
            ua = o.get("underlying_asset") or {}
            if ua.get("price") is not None:
                price = float(ua["price"])
            out.append(o)
        cursor = data.get("next_url") and data.get("next_url").split("cursor=")[-1]
        if not cursor:
            break
    if not out:
        raise RuntimeError({"status": "EMPTY", "msg": "No options data"})
    if price is None:
        # fallback: اسحب آخر سعر للسهم من آخر عنصر
        for o in reversed(out):
            ua = o.get("underlying_asset") or {}
            if ua.get("price") is not None:
                price = float(ua["price"]); break
    return out, price

def pick_monthly_expiry(symbol: str) -> str | None:
    """حاول قصر السلسلة على أقرب شهرية (ثالث جمعة)."""
    today = dt.datetime.now(timezone.utc).date()
    target = nearest_monthly_after(today).strftime("%Y-%m-%d")
    return target  # نرجّعها مباشرة؛ لو مافيه بيانات، سيُرمى خطأ بالأعلى ونتعامل معه

# ---------- GEX Cumulative Profile ----------

def build_cumulative_gex(contracts: list, price: float):
    """
    نجمع GEX لكل strike: gex = gamma * OI * 100
    CALL = +, PUT = -
    ثم نعمل cumulative sum عبر السترايكات (تصاعدي).
    """
    per_strike = {}
    for o in contracts:
        d = (o.get("details") or {})
        g = (o.get("greeks") or {})
        if "gamma" not in g or g["gamma"] is None:
            continue
        strike = d.get("strike_price")
        if strike is None: 
            continue
        ct = d.get("contract_type", "").lower()
        oi = o.get("open_interest", 0) or 0
        gamma = float(g["gamma"])
        # إشارة: CALL + ، PUT -
        sign = 1.0 if ct == "call" else -1.0 if ct == "put" else 0.0
        gex = gamma * float(oi) * 100.0 * sign
        per_strike[float(strike)] = per_strike.get(float(strike), 0.0) + gex

    if not per_strike:
        return [], [], []

    # نرتب strikes ونبني cumulative
    strikes_sorted = sorted(per_strike.keys())
    cum = []
    running = 0.0
    for s in strikes_sorted:
        running += per_strike[s]
        cum.append((s, running))

    # جدران CALL = أعلى قمم موجبة، PUT = أدنى قيعان سالبة
    return strikes_sorted, per_strike, cum

def top_levels_from_cum(cum, depth_pos=3, depth_neg=3, extra_levels=4):
    """
    نستخرج أعلى قمم موجبة (Call walls) وأدنى قيعان سالبة (Put walls).
    ثم نحسب المستويات الإضافية حتى نصل إلى 10 إجماليًا (7..10).
    """
    # نحسب القمم والقيعان البارزة باستخدام اختلافات محلية بسيطة
    peaks = []   # (value, strike)
    troughs = [] # (value, strike)
    n = len(cum)
    for i in range(1, n-1):
        s, v = cum[i]
        if v > cum[i-1][1] and v > cum[i+1][1]:
            peaks.append((v, s))
        if v < cum[i-1][1] and v < cum[i+1][1]:
            troughs.append((v, s))
    # fallback لو السلسلة ناعمة
    if not peaks and n:
        peaks = sorted([(cum[i][1], cum[i][0]) for i in range(n)], reverse=True)[:depth_pos]
    else:
        peaks = sorted(peaks, key=lambda x: x[0], reverse=True)[:depth_pos]
    if not troughs and n:
        troughs = sorted([(cum[i][1], cum[i][0]) for i in range(n)])[:depth_neg]
    else:
        troughs = sorted(troughs, key=lambda x: x[0])[:depth_neg]

    # المستويات الإضافية: بعد أخذ (3+3)=6، نضيف 4 من التاليين من حيث المَطلق
    remaining = sorted([(abs(v), s, v) for (v, s) in peaks + troughs] + 
                       [(abs(cum[i][1]), cum[i][0], cum[i][1]) for i in range(len(cum))],
                       key=lambda x: x[0], reverse=True)
    extra = []
    picked = set((s for _, s in peaks)) | set((s for _, s in troughs))
    for _, s, v in remaining:
        if s in picked:
            continue
        extra.append((v, s))
        picked.add(s)
        if len(extra) >= extra_levels:
            break

    call_walls = [{"strike": round(s, 2), "cum": v} for (v, s) in peaks]
    put_walls  = [{"strike": round(s, 2), "cum": v} for (v, s) in troughs]
    additional = [{"strike": round(s, 2), "cum": v} for (v, s) in extra]
    return call_walls, put_walls, additional

def percent_bars(items):
    if not items:
        return []
    m = max(abs(x["cum"]) for x in items) or 1.0
    for x in items:
        x["pct"] = round(abs(x["cum"]) / m * 100.0, 2)
    return items

# ---------- Endpoints ----------

@app.route("/")
def root():
    return jsonify({"ok": True, "hint": "Use /AAPL or /AAPL/pine?expiry=YYYY-MM-DD&depth=3&extras=4"})

@app.route("/<symbol>")
def json_gex(symbol):
    try:
        # إعدادات من الإعدادات التي أرسلتها:
        depth = int(request.args.get("depth", 3))       # عمق الجدران
        extras = int(request.args.get("extras", 4))     # Additional (7..10)
        expiry = request.args.get("expiry")
        if not expiry:
            expiry = pick_monthly_expiry(symbol)

        contracts, price = fetch_chain(symbol, expiry, greeks=True, limit=250)
        strikes, per_strike, cum = build_cumulative_gex(contracts, price)
        if not cum:
            return jsonify({"error": "No gamma data", "symbol": symbol}), 200

        calls, puts, additional = top_levels_from_cum(cum, depth_pos=depth, depth_neg=depth, extra_levels=extras)
        percent_bars(calls); percent_bars(puts); percent_bars(additional)

        return jsonify({
            "symbol": symbol.upper(),
            "expiry": expiry,
            "price": round(price, 2) if price is not None else None,
            "total_contracts": len(contracts),
            "calls": calls,           # أقوى 3
            "puts": puts,             # أقوى 3
            "additional": additional  # (7..10) أربعة مستويات إضافية
        })
    except Exception as e:
        return jsonify({"error": str(e), "symbol": symbol.upper()}), 200

@app.route("/<symbol>/pine")
def pine(symbol):
    """
    يُنتج سكربت Pine يرسم:
    - Lines للجدران (CALL خضراء / PUT حمراء)
    - Bars أفقية مع نسبة القوة %
    - يسحب الإعدادات depth/extras/expiry من query string
    """
    depth = int(request.args.get("depth", 3))
    extras = int(request.args.get("extras", 4))
    expiry = request.args.get("expiry") or pick_monthly_expiry(symbol)

    try:
        contracts, price = fetch_chain(symbol, expiry, greeks=True, limit=500)
        _, _, cum = build_cumulative_gex(contracts, price)
        calls, puts, additional = top_levels_from_cum(cum, depth, depth, extras)
        percent_bars(calls); percent_bars(puts); percent_bars(additional)

        # نحول القوائم إلى مصفوفات Pine
        def arr_str(x): return ",".join(str(round(v["strike"], 2)) for v in x)
        def pct_str(x): return ",".join(str(int(round(v["pct"]))) for v in x)

        calls_strikes = arr_str(calls)
        calls_pct     = pct_str(calls)
        puts_strikes  = arr_str(puts)
        puts_pct      = pct_str(puts)
        extra_strikes = arr_str(additional)
        extra_pct     = pct_str(additional)

        pine = f"""//@version=5
indicator("Bassam GEX – Σ CUMULATIVE  |  {symbol.upper()}  |  Exp {expiry}", overlay=true, max_lines_count=500, max_labels_count=500)

// ===== Inputs (مطابقة لفلسفة Options GEX[Lite]) =====
enable_lines   = input.bool(true,  "Lines")
enable_bars    = input.bool(true,  "Bars")
show_percent   = input.bool(true,  "% with info")
line_extend    = input.string("←", "If Lines enabled, then line Extension", options=["←","→","⟷"])
font_size      = input.string("L", "Font Size", options=["S","M","L"])
label_offset   = input.int(30,     "And label offset")

// ===== Data from API (مُولّدة من الخادم) =====
var float[] CALL_STRIKES = array.from({calls_strikes})
var int[]   CALL_PCT     = array.from({calls_pct})
var float[] PUT_STRIKES  = array.from({puts_strikes})
var int[]   PUT_PCT      = array.from({puts_pct})
var float[] EXT_STRIKES  = array.from({extra_strikes})
var int[]   EXT_PCT      = array.from({extra_pct})

dir = line_extend == "←" ? extend.left : line_extend == "→" ? extend.right : extend.both

f_bar(x, y, pct, col) =>
    // يرسم شريط أفقي + نسبة
    if enable_bars
        l = line.new(bar_index, y, bar_index + 200, y, extend=extend.right, color=col, width=3)
        if show_percent
            label.new(bar_index + 201, y, str.tostring(pct) + "%", style=label.style_label_left, textcolor=color.white, color=color.new(col, 80), size=size.large)

// CALL Walls (أخضر)
for i = 0 to array.size(CALL_STRIKES)-1
    s = array.get(CALL_STRIKES, i)
    p = array.get(CALL_PCT, i)
    if enable_lines
        line.new(bar_index-5, s, bar_index+5, s, extend=dir, color=color.new(color.lime, 0), width=1)
    f_bar(bar_index, s, p, color.lime)

// PUT Walls (أحمر)
for i = 0 to array.size(PUT_STRIKES)-1
    s = array.get(PUT_STRIKES, i)
    p = array.get(PUT_PCT, i)
    if enable_lines
        line.new(bar_index-5, s, bar_index+5, s, extend=dir, color=color.new(color.red, 0), width=1)
    f_bar(bar_index, s, p, color.red)

// Additional (7..10) باللون الرمادي
for i = 0 to array.size(EXT_STRIKES)-1
    s = array.get(EXT_STRIKES, i)
    p = array.get(EXT_PCT, i)
    if enable_lines
        line.new(bar_index-5, s, bar_index+5, s, extend=dir, color=color.new(color.gray, 30), width=1)
    f_bar(bar_index, s, p, color.gray)

// ملاحظة: HVL/Flip يمكن إضافته لاحقاً بحساب موضع أقصى ΣGEX محلي وإبرازه بلون مستقل.
"""
        return Response(pine, mimetype="text/plain; charset=utf-8")
    except Exception as e:
        return jsonify({"error": str(e), "symbol": symbol.upper()}), 200

# ---------- Run (for local) ----------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
