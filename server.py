# ============================================================
# Bassam GEX PRO v4.7 – Weekly EM Lines
# Adds IV-based Expected Move lines (weekly) for all symbols
# ============================================================

import os, json, datetime as dt, requests, time, math
from flask import Flask, jsonify, Response

app = Flask(__name__)
POLY_KEY  = (os.environ.get("POLYGON_API_KEY") or "").strip()
BASE_SNAP = "https://api.polygon.io/v3/snapshot/options"
TODAY     = dt.date.today

SYMBOLS = [
    "AAPL","META","MSFT","NVDA","TSLA","GOOGL","AMD",
    "CRWD","SPY","PLTR","LULU","LLY","COIN","MSTR","APP","ASML"
]

CACHE = {}
CACHE_EXPIRY = 3600  # 1h

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

# ---------------------- Polygon Fetch -----------------------
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
        if "cursor=" in cursor:
            cursor = cursor.split("cursor=")[-1]
        else:
            cursor = None
    return all_rows

# ------------------------ Expiries --------------------------
def list_future_expiries(rows):
    expiries = sorted({
        r.get("details", {}).get("expiration_date")
        for r in rows if r.get("details", {}).get("expiration_date")
    })
    today = TODAY().isoformat()
    return [d for d in expiries if d >= today]

def nearest_weekly(expiries):
    for d in expiries:
        try:
            y, m, dd = map(int, d.split("-"))
            if dt.date(y, m, dd).weekday() == 4:  # Friday
                return d
        except Exception:
            continue
    return expiries[0] if expiries else None

def nearest_monthly(expiries):
    if not expiries: return None
    first = expiries[0]
    y, m, _ = map(int, first.split("-"))
    month_list = [d for d in expiries if d.startswith(f"{y:04d}-{m:02d}-")]
    last_friday = None
    for d in month_list:
        Y, M, D = map(int, d.split("-"))
        if dt.date(Y, M, D).weekday() == 4:
            last_friday = d
    return last_friday or (month_list[-1] if month_list else expiries[-1])

# -------------------- OI + IV Analysis ---------------------
def analyze_oi_iv(rows, expiry, per_side_limit, split_by_price=True):
    rows = [r for r in rows if r.get("details", {}).get("expiration_date") == expiry]
    if not rows: return None, [], []
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
        ctype  = det.get("contract_type")
        oi     = r.get("open_interest")
        iv     = r.get("implied_volatility")
        if not (isinstance(strike, (int, float)) and isinstance(oi, (int, float))):
            continue
        iv = float(iv) if isinstance(iv, (int,float)) else 0.0
        if ctype == "call": calls.append((strike, oi, iv))
        elif ctype == "put": puts.append((strike, oi, iv))

    if split_by_price and isinstance(price, (int, float)):
        calls = [(s, oi, iv) for (s, oi, iv) in calls if s >= price]
        puts  = [(s, oi, iv) for (s, oi, iv) in puts if s <= price]

    top_calls = sorted(calls, key=lambda x: x[1], reverse=True)[:per_side_limit]
    top_puts  = sorted(puts,  key=lambda x: x[1], reverse=True)[:per_side_limit]
    return price, top_calls, top_puts

def normalize_for_pine(data):
    if not data: return [], [], []
    base = max(oi for _, oi, _ in data) or 1.0
    strikes = [round(float(s), 2) for (s, _, _) in data]
    pcts    = [round((oi / base), 4) for (_, oi, _) in data]
    ivs     = [round(float(iv), 4) for (_, _, iv) in data]
    return strikes, pcts, ivs

def to_pine_array(arr):
    return ",".join(f"{float(x):.6f}" for x in arr if x is not None)

def arr_or_empty(arr):
    txt = to_pine_array(arr)
    return f"array.from({txt})" if txt else "array.new_float()"

# -------------------- Expected Move (EM) -------------------
# >>> EM: استخرج IV السنوي عند الـ ATM (متوسط كول/بوت الأقرب للسعر) ثم احسب
# EM = Price * IV * sqrt(days/365)
def compute_weekly_em(rows, weekly_expiry):
    if not weekly_expiry: 
        return None, None, None
    # السعر الحالي من أي صف
    price = None
    for r in rows:
        p = r.get("underlying_asset", {}).get("price")
        if isinstance(p, (int, float)) and p > 0:
            price = float(p)
            break
    if price is None:
        return None, None, None

    # صفوف الأسبوع المحدد
    wk_rows = [r for r in rows if r.get("details", {}).get("expiration_date") == weekly_expiry]
    if not wk_rows:
        return price, None, None

    # اختر الكول/البوت الأقرب للـ ATM
    calls = [r for r in wk_rows if r.get("details", {}).get("contract_type") == "call"]
    puts  = [r for r in wk_rows if r.get("details", {}).get("contract_type") == "put"]

    def closest_iv(side_rows):
        best = None
        best_diff = 1e18
        for r in side_rows:
            strike = r.get("details", {}).get("strike_price")
            iv     = r.get("implied_volatility")
            if isinstance(strike, (int,float)) and isinstance(iv, (int,float)):
                diff = abs(float(strike) - price)
                if diff < best_diff:
                    best_diff = diff
                    best = float(iv)
        return best

    c_iv = closest_iv(calls)
    p_iv = closest_iv(puts)
    if c_iv is None and p_iv is None:
        return price, None, None

    # متوسط IV السنوي عند الـ ATM (لو أحدهما مفقود، استخدم الآخر)
    if c_iv is None: iv_annual = p_iv
    elif p_iv is None: iv_annual = c_iv
    else: iv_annual = (c_iv + p_iv) / 2.0

    # أيام فعليّة حتى الانقضاء
    y, m, d = map(int, weekly_expiry.split("-"))
    exp_date = dt.date(y, m, d)
    days = max((exp_date - TODAY()).days, 1)

    em = price * iv_annual * math.sqrt(days / 365.0)  # :contentReference[oaicite:1]{index=1}
    return price, iv_annual, em

# -------------------- Update + Cache -----------------------
def update_symbol_data(symbol):
    rows = fetch_all(symbol)
    expiries = list_future_expiries(rows)
    if not expiries:
        return None

    exp_w = nearest_weekly(expiries)
    exp_m = nearest_monthly(expiries)

    use_monthly_for_weekly = (exp_w == exp_m)

    if use_monthly_for_weekly and exp_m:
        _, w_calls, w_puts = analyze_oi_iv(rows, exp_m, 3)
    else:
        _, w_calls, w_puts = analyze_oi_iv(rows, exp_w, 3) if exp_w else (None, [], [])

    _, m_calls, m_puts = analyze_oi_iv(rows, exp_m, 4)

    # >>> EM: احسب EM الأسبوعي
    em_price, em_iv, em_value = compute_weekly_em(rows, exp_w if not use_monthly_for_weekly else exp_m)

    # قصير المدى (اختياري كما في نسختك)
    exp_short = None
    today = dt.date.today()
    for d in expiries:
        y, m, dd = map(int, d.split("-"))
        exp_date = dt.date(y, m, dd)
        if 0 < (exp_date - today).days <= 4:
            exp_short = d
            break
    _, short_calls, short_puts = analyze_oi_iv(rows, exp_short, 3) if exp_short else (None, [], [])

    return {
        "symbol": symbol,
        "short": {"calls": short_calls, "puts": short_puts},
        "weekly": {"calls": w_calls, "puts": w_puts, "expiry": exp_w},
        "monthly": {"calls": m_calls, "puts": m_puts, "expiry": exp_m},
        "duplicate": use_monthly_for_weekly,
        "em": {  # >>> EM payload
            "price": em_price,
            "iv_annual": em_iv,
            "weekly_em": em_value
        },
        "timestamp": time.time()
    }

def get_symbol_data(symbol):
    now = time.time()
    if symbol in CACHE and (now - CACHE[symbol]["timestamp"] < CACHE_EXPIRY):
        return CACHE[symbol]
    data = update_symbol_data(symbol)
    if data: CACHE[symbol] = data
    return data

# ---------------------- /all/pine --------------------------
@app.route("/all/pine")
def all_pine():
    if not POLY_KEY:
        return _err("Missing POLYGON_API_KEY", 401)

    blocks = []
    for sym in SYMBOLS:
        data = get_symbol_data(sym)
        if not data:
            continue

        wc_s, wc_p, wc_iv = normalize_for_pine(data["weekly"]["calls"])
        wp_s, wp_p, wp_iv = normalize_for_pine(data["weekly"]["puts"])
        mc_s, mc_p, mc_iv = normalize_for_pine(data["monthly"]["calls"])
        mp_s, mp_p, mp_iv = normalize_for_pine(data["monthly"]["puts"])

        dup_str = "true" if data.get("duplicate") else "false"

        # >>> EM fields per symbol (None -> na)
        em_val = data.get("em", {}).get("weekly_em")
        em_iv  = data.get("em", {}).get("iv_annual")
        em_prc = data.get("em", {}).get("price")
        em_txt = "na" if em_val is None else f"{float(em_val):.6f}"
        iv_txt = "na" if em_iv  is None else f"{float(em_iv):.6f}"
        pr_txt = "na" if em_prc is None else f"{float(em_prc):.6f}"

        block = f"""
//========= {sym} =========
if syminfo.ticker == "{sym}"
    title = " PRO • " + mode + " | {sym}"
    duplicate_expiry = {dup_str}

    bool showWeekly = false
    bool showMonthly = false

    if mode == "Weekly"
        if duplicate_expiry
            showMonthly := true
            showWeekly  := false
        else
            showWeekly  := true
            showMonthly := false
    else if mode == "Monthly"
        showMonthly := true
        showWeekly  := false

    // --- Expected Move (server) ---
    em_value = {em_txt}         // points
    em_iv    = {iv_txt}         // annual IV
    em_price = {pr_txt}         // last underlying price

    // --- Option bars (كما هي)
    if showWeekly
        draw_side({arr_or_empty(wc_s)}, {arr_or_empty(wc_p)}, {arr_or_empty(wc_iv)}, color.lime)
        draw_side({arr_or_empty(wp_s)}, {arr_or_empty(wp_p)}, {arr_or_empty(wp_iv)}, color.red)

    if showMonthly
        draw_side(array.from({to_pine_array(mc_s)}), array.from({to_pine_array(mc_p)}), array.from({to_pine_array(mc_iv)}), color.new(color.green, 0))
        draw_side(array.from({to_pine_array(mp_s)}), array.from({to_pine_array(mp_p)}), array.from({to_pine_array(mp_iv)}), color.new(#b02727, 0))

    // --- Weekly open as center ----
    // === خطوط المدى الأسبوعي المتوقع بدون تكرار ===
    var line emTop  = na
    var line emBot  = na
    var label emTopL = na
    var label emBotL = na

    // السعر الحالي كأساس (وليس افتتاح الأسبوع)
    currentPrice = request.security(syminfo.tickerid, timeframe.period, close)

    if barstate.islast and not na(em_value)
        up = currentPrice + em_value
        dn = currentPrice - em_value

        // حذف أي خطوط سابقة
        if not na(emTop)
            line.delete(emTop)
            line.delete(emBot)
            label.delete(emTopL)
            label.delete(emBotL)

        // رسم خطي المدى مرة واحدة فقط
        emTop  := line.new(bar_index, up, bar_index + 1, up, extend = extend.both, color = color.new(color.yellow, 0), width = 2, style = line.style_dotted)
        emBot  := line.new(bar_index, dn, bar_index + 1, dn, extend = extend.both, color = color.new(color.yellow, 0), width = 2, style = line.style_dotted)
        emTopL := label.new(bar_index, up, "📈 أعلى مدى متوقع: " + str.tostring(up, "#.##"),style = label.style_label_down, color = color.new(color.yellow, 0),textcolor = color.black, size = size.small)
        emBotL := label.new(bar_index, dn, "📉 أدنى مدى متوقع: " + str.tostring(dn, "#.##"),style = label.style_label_up, color = color.new(color.yellow, 0),textcolor = color.black, size = size.small)


"""
        blocks.append(block)

    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=3)))
    last_update = now.strftime("%Y-%m-%d %H:%M:%S")

    pine = f"""//@version=5
// Last Update (Riyadh): {last_update}
indicator("GEX PRO + Weekly EM", overlay=true, max_lines_count=500, max_labels_count=500, dynamic_requests=true)

// إعدادات عامة موجودة لديك
mode = input.string("Weekly", "Expiry Mode", options=["Weekly","Monthly"])
showHVL   = true
baseColor = color.new(color.yellow, 0)
zoneWidth = 2.0

draw_side(_s, _p, _iv, _col) =>
    if array.size(_s) == 0 or array.size(_p) == 0 or array.size(_iv) == 0
        na
    else
        var line[]  linesArr  = array.new_line()
        var label[] labelsArr = array.new_label()
        for l in linesArr
            line.delete(l)
        for lb in labelsArr
            label.delete(lb)
        array.clear(linesArr)
        array.clear(labelsArr)
        for i = 0 to array.size(_s) - 1
            y  = array.get(_s, i)
            p  = array.get(_p, i)
            iv = array.get(_iv, i)
            alpha   = 90 - int(p * 70)
            bar_col = color.new(_col, alpha)
            bar_len = int(math.max(10, p * 100))
            lineRef  = line.new(bar_index + 3, y, bar_index + bar_len - 12, y, color=bar_col, width=6)
            labelRef = label.new(bar_index + bar_len + 2, y, str.tostring(p*100, "#.##") + "% | IV " + str.tostring(iv*100, "#.##") + "%", style=label.style_none, textcolor=color.white, size=size.small)

// ---- per-symbol blocks ----
{''.join(blocks)}
"""
    return Response(pine, mimetype="text/plain")

# ---------------------- /all/json --------------------------
@app.route("/all/json")
def all_json():
    if not POLY_KEY:
        return _err("Missing POLYGON_API_KEY", 401)
    all_data = {}
    for sym in SYMBOLS:
        data = get_symbol_data(sym)
        if data:
            all_data[sym] = {
                "weekly": {
                    "expiry": data["weekly"].get("expiry"),
                    "calls": [{"strike": s, "oi": oi, "iv": iv} for (s, oi, iv) in data["weekly"]["calls"]],
                    "puts":  [{"strike": s, "oi": oi, "iv": iv} for (s, oi, iv) in data["weekly"]["puts"]],
                },
                "monthly": {
                    "expiry": data["monthly"].get("expiry"),
                    "calls": [{"strike": s, "oi": oi, "iv": iv} for (s, oi, iv) in data["monthly"]["calls"]],
                    "puts":  [{"strike": s, "oi": oi, "iv": iv} for (s, oi, iv) in data["monthly"]["puts"]],
                },
                "em": data.get("em"),
                "timestamp": data["timestamp"]
            }

    return jsonify({
        "status": "OK",
        "symbols": SYMBOLS,
        "updated": dt.datetime.utcnow().isoformat() + "Z",
        "data": all_data
    })

# ---------------------- /em/json (جديد) --------------------
@app.route("/em/json")
def em_json():
    if not POLY_KEY:
        return _err("Missing POLYGON_API_KEY", 401)
    out = {}
    for sym in SYMBOLS:
        d = get_symbol_data(sym)
        if d and d.get("em", {}).get("weekly_em") is not None:
            out[sym] = d["em"]
    return jsonify({"status": "OK", "updated": dt.datetime.utcnow().isoformat()+"Z", "data": out})

# ------------------------ Root -----------------------------
@app.route("/")
def home():
    return jsonify({
        "status": "OK ✅",
        "symbols": SYMBOLS,
        "author": "Bassam GEX PRO v4.7 – Weekly EM",
        "interval": "240m ثابت",
        "update": "كل ساعة تلقائيًا",
        "usage": {"all_pine": "/all/pine", "all_json": "/all/json", "em_json": "/em/json"},
        "cache_items": len(CACHE)
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
