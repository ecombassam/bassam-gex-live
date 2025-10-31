# ============================================================
# Bassam GEX PRO v5.0 – Net Gamma Exposure Edition (1h)
# - Weekly EM lines centered at current price (1h)
# - No duplication of lines/labels
# - Option bars now use Net Gamma Exposure instead of OI
# - Colors readable on both dark/light chart backgrounds
# ============================================================

import os, json, datetime as dt, requests, time, math
from flask import Flask, jsonify, Response

app = Flask(__name__)
POLY_KEY  = (os.environ.get("VVn7upcnEAu9o6wdok91K_dhUcqm9YgN") or "").strip()
BASE_SNAP = "https://api.polygon.io/v3/snapshot/options"
TODAY     = dt.date.today

SYMBOLS = [
    "AAPL","META","MSFT","NVDA","TSLA","GOOGL","AMD",
    "CRWD","SPY","PLTR","LULU","LLY","COIN","MSTR","APP","ASML"
]

CACHE = {}
CACHE_EXPIRY = 3600  # 1h

# ---------------------- Common helpers ----------------------
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

# ---------------------- Polygon fetch -----------------------
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

# ----------------- Net Gamma + IV analysis -----------------
def analyze_gamma_iv(rows, expiry, per_side_limit, split_by_price=True):
    """
    تُرجع:
      price: سعر الأصل الأساسي
      top_calls:  [(strike, net_gamma_signed, iv), ...]  (أعلى |net_gamma|)
      top_puts:   [(strike, net_gamma_signed, iv), ...]  (أعلى |net_gamma|)
    """
    rows = [r for r in rows if r.get("details", {}).get("expiration_date") == expiry]
    if not rows: return None, [], []
    price = None
    for r in rows:
        p = r.get("underlying_asset", {}).get("price")
        if isinstance(p, (int, float)) and p > 0:
            price = float(p)
            break

    # تجميع حسب السترايك لكل نوع (calls/puts): مجموع net_gamma عند نفس السترايك
    calls_map = {}
    puts_map  = {}

    for r in rows:
        det    = r.get("details", {}) or {}
        strike = det.get("strike_price")
        ctype  = det.get("contract_type")
        oi     = r.get("open_interest")
        iv     = r.get("implied_volatility")
        greeks = r.get("greeks") or {}

        if not (isinstance(strike, (int, float)) and isinstance(oi, (int, float)) and isinstance(price, (int, float))):
            continue

        gamma = greeks.get("gamma", 0.0)
        try:
            gamma = float(gamma)
        except Exception:
            gamma = 0.0

        iv_val = float(iv) if isinstance(iv, (int, float)) else 0.0

        # net gamma exposure الموقعة
        # call/put لا نعكس الإشارة يدويًا؛ نستخدم جاما كما هي من الـ greeks (غالبًا موجبة)
        # الإشارة الموقعة مفيدة في JSON، بينما الرسم يُطبّع على القيمة المطلقة.
        net_gamma = gamma * float(oi) * 100.0 * float(price)

        if ctype == "call":
            if strike not in calls_map:
                calls_map[strike] = {"net_gamma": 0.0, "iv": iv_val}
            calls_map[strike]["net_gamma"] += net_gamma
            # حدّث IV الأقرب (نأخذ متوسط بسيط)
            calls_map[strike]["iv"] = (calls_map[strike]["iv"] + iv_val) / 2.0 if calls_map[strike]["iv"] else iv_val

        elif ctype == "put":
            if strike not in puts_map:
                puts_map[strike] = {"net_gamma": 0.0, "iv": iv_val}
            puts_map[strike]["net_gamma"] += net_gamma
            puts_map[strike]["iv"] = (puts_map[strike]["iv"] + iv_val) / 2.0 if puts_map[strike]["iv"] else iv_val

    # تحويل إلى قوائم
    calls = [(float(s), float(v["net_gamma"]), float(v["iv"])) for s, v in calls_map.items()]
    puts  = [(float(s), float(v["net_gamma"]), float(v["iv"])) for s, v in puts_map.items()]

    # فلترة بناءً على السعر (مثل السابق)
    if split_by_price and isinstance(price, (int, float)):
        calls = [(s, g, iv) for (s, g, iv) in calls if s >= price]
        puts  = [(s, g, iv) for (s, g, iv) in puts  if s <= price]

    # فرز بأكبر |net_gamma|
    calls = sorted(calls, key=lambda x: abs(x[1]), reverse=True)[:per_side_limit]
    puts  = sorted(puts,  key=lambda x: abs(x[1]), reverse=True)[:per_side_limit]
    return price, calls, puts

# -------------------- Pine normalization -------------------
def normalize_for_pine(data):
    """
    data: [(strike, net_gamma_signed, iv), ...]
    تُرجع:
      strikes: [floats]
      pcts:    [0..1] (normalize by max(|net_gamma|))
      ivs:     [floats]
    """
    if not data: return [], [], []
    base = max(abs(val) for _, val, _ in data) or 1.0
    strikes = [round(float(s), 2) for (s, _, _) in data]
    pcts    = [round((abs(val) / base), 4) for (_, val, _) in data]  # التطبيع على القيمة المطلقة للرسم
    ivs     = [round(float(iv), 4) for (_, _, iv) in data]
    return strikes, pcts, ivs

def to_pine_array(arr):
    return ",".join(f"{float(x):.6f}" for x in arr if x is not None)

def arr_or_empty(arr):
    txt = to_pine_array(arr)
    return f"array.from({txt})" if txt else "array.new_float()"

# -------------------- Expected Move (EM) -------------------
# EM = Price * IV_annual * sqrt(days/365)
def compute_weekly_em(rows, weekly_expiry):
    if not weekly_expiry:
        return None, None, None
    price = None
    for r in rows:
        p = r.get("underlying_asset", {}).get("price")
        if isinstance(p, (int, float)) and p > 0:
            price = float(p); break
    if price is None:
        return None, None, None

    wk_rows = [r for r in rows if r.get("details", {}).get("expiration_date") == weekly_expiry]
    if not wk_rows: return price, None, None

    calls = [r for r in wk_rows if r.get("details", {}).get("contract_type") == "call"]
    puts  = [r for r in wk_rows if r.get("details", {}).get("contract_type") == "put"]

    def closest_iv(side_rows):
        best, best_diff = None, 1e18
        for r in side_rows:
            strike = r.get("details", {}).get("strike_price")
            iv     = r.get("implied_volatility")
            if isinstance(strike, (int,float)) and isinstance(iv, (int,float)):
                diff = abs(float(strike) - price)
                if diff < best_diff: best_diff, best = diff, float(iv)
        return best

    c_iv, p_iv = closest_iv(calls), closest_iv(puts)
    if c_iv is None and p_iv is None: return price, None, None
    iv_annual = c_iv if p_iv is None else p_iv if c_iv is None else (c_iv + p_iv)/2.0

    y, m, d = map(int, weekly_expiry.split("-"))
    exp_date = dt.date(y, m, d)
    days = max((exp_date - TODAY()).days, 1)
    em = price * iv_annual * math.sqrt(days / 365.0)
    return price, iv_annual, em

# -------------------- Update + Cache -----------------------
def update_symbol_data(symbol):
    rows = fetch_all(symbol)
    expiries = list_future_expiries(rows)
    if not expiries: return None

    exp_w = nearest_weekly(expiries)
    exp_m = nearest_monthly(expiries)
    use_monthly_for_weekly = (exp_w == exp_m)

    if use_monthly_for_weekly and exp_m:
        _, w_calls, w_puts = analyze_gamma_iv(rows, exp_m, 3)
    else:
        _, w_calls, w_puts = analyze_gamma_iv(rows, exp_w, 3) if exp_w else (None, [], [])
    _, m_calls, m_puts = analyze_gamma_iv(rows, exp_m, 4)

    em_price, em_iv, em_value = compute_weekly_em(rows, exp_w if not use_monthly_for_weekly else exp_m)

    return {
        "symbol": symbol,
        "weekly": {"calls": w_calls, "puts": w_puts, "expiry": exp_w},
        "monthly": {"calls": m_calls, "puts": m_puts, "expiry": exp_m},
        "duplicate": use_monthly_for_weekly,
        "em": {"price": em_price, "iv_annual": em_iv, "weekly_em": em_value},
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
    if not POLY_KEY: return _err("Missing POLYGON_API_KEY", 401)

    blocks = []
    for sym in SYMBOLS:
        data = get_symbol_data(sym)
        if not data: continue

        wc_s, wc_p, wc_iv = normalize_for_pine(data["weekly"]["calls"])
        wp_s, wp_p, wp_iv = normalize_for_pine(data["weekly"]["puts"])
        mc_s, mc_p, mc_iv = normalize_for_pine(data["monthly"]["calls"])
        mp_s, mp_p, mp_iv = normalize_for_pine(data["monthly"]["puts"])

        dup_str = "true" if data.get("duplicate") else "false"
        em_val  = data.get("em", {}).get("weekly_em")
        em_iv   = data.get("em", {}).get("iv_annual")
        em_prc  = data.get("em", {}).get("price")
        em_txt  = "na" if em_val is None else f"{float(em_val):.6f}"
        iv_txt  = "na" if em_iv  is None else f"{float(em_iv):.6f}"
        pr_txt  = "na" if em_prc is None else f"{float(em_prc):.6f}"

        block = f"""
//========= {sym} =========
if syminfo.ticker == "{sym}"
    title = " PRO • " + mode + " | {sym}"
    duplicate_expiry = {dup_str}

    showWeekly := true
    showMonthly := true

    if mode == "Weekly"
        if duplicate_expiry
            showMonthly := true
        else
            showWeekly  := true
    else
        showMonthly := true

    // === Option bars: per-symbol, no-dup (Net Gamma Exposure) ===
    clear_visuals(optLines, optLabels)
    if showWeekly
        draw_side({arr_or_empty(wc_s)}, {arr_or_empty(wc_p)}, {arr_or_empty(wc_iv)}, color.lime)
        draw_side({arr_or_empty(wp_s)}, {arr_or_empty(wp_p)}, {arr_or_empty(wp_iv)}, color.rgb(220,50,50))
    if showMonthly
        draw_side(array.from({to_pine_array(mc_s)}), array.from({to_pine_array(mc_p)}), array.from({to_pine_array(mc_iv)}), color.new(color.green, 0))
        draw_side(array.from({to_pine_array(mp_s)}), array.from({to_pine_array(mp_p)}), array.from({to_pine_array(mp_iv)}), color.new(#b02727, 0))

    // === Expected Move lines (centered at current price 1h), no-dup ===
    em_value = {em_txt}
    em_iv    = {iv_txt}
    em_price = {pr_txt}

    // مركز حول السعر الحالي (W) لضمان تحديث حي حتى على فريم أسبوعي
    currentPrice = request.security(syminfo.tickerid, "W", close)

    var line emTop  = line.new(na, na, na, na)
    var line emBot  = line.new(na, na, na, na)
    var label emTopL = na
    var label emBotL = na

    if not na(em_value)
        up = currentPrice + em_value
        dn = currentPrice - em_value

        gold = color.rgb(255, 215, 0)

        line.set_xy1(emTop, bar_index - 5, up)
        line.set_xy2(emTop, bar_index + 5, up)
        line.set_xy1(emBot, bar_index - 5, dn)
        line.set_xy2(emBot, bar_index + 5, dn)
        line.set_extend(emTop, extend.both)
        line.set_extend(emBot, extend.both)
        line.set_color(emTop, color.new(gold, 0))
        line.set_color(emBot, color.new(gold, 0))
        line.set_width(emTop, 2)
        line.set_width(emBot, 2)
        line.set_style(emTop, line.style_dotted)
        line.set_style(emBot, line.style_dotted)

        if not na(emTopL)
            label.delete(emTopL)
        if not na(emBotL)
            label.delete(emBotL)

        emTopL := label.new(bar_index, up, "📈 أعلى مدى متوقع: " + str.tostring(up, "#.##"), style=label.style_label_down, color=color.new(gold, 0), textcolor=color.black, size=size.small)
        emBotL := label.new(bar_index, dn, "📉 أدنى مدى متوقع: " + str.tostring(dn, "#.##"), style=label.style_label_up,   color=color.new(gold, 0), textcolor=color.black, size=size.small)
"""
        blocks.append(block)

    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=3)))
    last_update = now.strftime("%Y-%m-%d %H:%M:%S")

    pine = f"""//@version=5
// Last Update (Riyadh): {last_update}
indicator("GEX PRO (v5.0) – Net Gamma Exposure", overlay=true, max_lines_count=500, max_labels_count=500, dynamic_requests=true)

// إعدادات عامة
mode = input.string("Weekly", "Expiry Mode", options=["Weekly","Monthly"])
showHVL   = input.bool(true, "Show HVL", inline="hvl")
baseColor = color.new(color.yellow, 0)
zoneWidth = 2.0

// تعريف المتغيرات الأساسية قبل الاستخدام
var bool showWeekly  = false
var bool showMonthly = false

// مصفوفات للرسم العام
var line[]  optLines  = array.new_line()
var label[] optLabels = array.new_label()

// دالة تنظيف جميع الرسومات القديمة
clear_visuals(_optLines, _optLabels) =>
    if array.size(_optLines) > 0
        for l in _optLines
            line.delete(l)
        array.clear(_optLines)
    if array.size(_optLabels) > 0
        for lb in _optLabels
            label.delete(lb)
        array.clear(_optLabels)

// دالة رسم الأشرطة الخاصة بالأوبشن (Net Gamma Exposure مطبّع على [0..1])
draw_side(_s, _p, _iv, _col) =>
    if barstate.islast and array.size(_s) > 0 and array.size(_p) > 0 and array.size(_iv) > 0
        for i = 0 to array.size(_s) - 1
            y  = array.get(_s, i)
            p  = array.get(_p, i)    // هذا p = |net_gamma| / max(|net_gamma|)
            iv = array.get(_iv, i)
            alpha   = 90 - int(p * 70)
            bar_col = color.new(_col, alpha)
            bar_len = int(math.max(10, p * 50))
            line.new(bar_index + 3, y, bar_index + bar_len - 12, y, color=bar_col, width=6)
            label.new(bar_index + bar_len + 2, y, str.tostring(p*100, "#.##") + "% | IV " + str.tostring(iv*100, "#.##") + "%", style=label.style_label_left, color=color.rgb(95, 93, 93), textcolor=color.white, size=size.small)


// --- Per-symbol blocks ---
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
                    "calls": [{"strike": s, "net_gamma": ng, "iv": iv} for (s, ng, iv) in data["weekly"]["calls"]],
                    "puts":  [{"strike": s, "net_gamma": ng, "iv": iv} for (s, ng, iv) in data["weekly"]["puts"]],
                },
                "monthly": {
                    "expiry": data["monthly"].get("expiry"),
                    "calls": [{"strike": s, "net_gamma": ng, "iv": iv} for (s, ng, iv) in data["monthly"]["calls"]],
                    "puts":  [{"strike": s, "net_gamma": ng, "iv": iv} for (s, ng, iv) in data["monthly"]["puts"]],
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

# ---------------------- /em/json ---------------------------
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
    # استجابة سريعة جدًا لنجاح النشر
    return jsonify({
        "status": "OK ✅",
        "message": "Bassam GEX PRO server is running (Net Gamma Exposure)",
        "note": "Data cache loading in background..."
    })

# ------------------------ Background Loader -----------------------------
def warmup_cache():
    """تحميل بيانات الشركات بعد التشغيل بدون تعطيل السيرفر"""
    print("🔄 Warming up cache in background...")
    for sym in SYMBOLS:
        try:
            get_symbol_data(sym)
            print(f"✅ Cached {sym}")
        except Exception as e:
            print(f"⚠️ Failed to cache {sym}: {e}")
    print("✅ Cache warm-up complete.")

if __name__ == "__main__":
    import threading
    # تشغيل تحميل البيانات في الخلفية بعد الإقلاع
    threading.Thread(target=warmup_cache, daemon=True).start()
    # تشغيل السيرفر نفسه
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
