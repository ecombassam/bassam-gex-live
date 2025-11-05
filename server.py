# ============================================================
# Bassam GEX PRO v6.9 – Dual Week + Dynamic EM + Credit Signals (ΔOI + ΔIV)
# - Weekly (Current & Next) + Monthly
# - ΔOI/ΔIV signal per-week (Bullish Credit Put / Bearish Credit Call / Neutral)
# - Only 7 bars per expiry: Top3 + Strongest(|100%|) + Top3
# - Ignore <20% of max |net_gamma|
# - Only strikes within ±25% around current price
# - EM lines follow the same selected week (Current/Next)
# ============================================================

import os, json, datetime as dt, requests, time, math
from flask import Flask, jsonify, Response

app = Flask(__name__)
POLY_KEY  = (os.environ.get("POLYGON_API_KEY") or "").strip()
BASE_SNAP = "https://api.polygon.io/v3/snapshot/options"
TODAY     = dt.date.today
os.makedirs("data", exist_ok=True)
if not os.path.exists("data/all.json"):
    with open("data/all.json", "w", encoding="utf-8") as f:
        json.dump({"updated": None, "symbols": [], "data": {}}, f, ensure_ascii=False, indent=2)

SYMBOLS = [
    "AAPL","META","MSFT","NVDA","TSLA","GOOGL","AMD",
    "CRWD","SPY","PLTR","LULU","LLY","COIN","MSTR","APP","ASML"
]

CACHE = {}
CACHE_EXPIRY = 3600  # 1h

# ⏱️ Baselines (نحفظ خط أساس يومي للمقارنة Δ)
# structure: DAILY_BASE[symbol][expiry] = {"date":"YYYY-MM-DD","calls":x,"puts":y,"iv_atm":z}
DAILY_BASE = {}

# ---------- Config thresholds للـ Credit Signal ----------
MIN_BASE_OI  = 50     # أقل OI إجمالي معقول للقياس

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

# ---------------------- التاريخ -----------------------

def get_next_earnings(symbol):
    """🔹 يجلب أقرب تاريخ إعلان أرباح للسهم (باستخدام Polygon Reference API)"""
    try:
        # طلب بيانات الأرباح الحديثة
        url = f"https://api.polygon.io/v3/reference/earnings?ticker={symbol}"
        status, data = _get(url)
        if status != 200 or "results" not in data:
            return None

        results = data.get("results", [])
        if not results:
            return None

        # نرتب النتائج حسب التاريخ ونأخذ الأقرب للمستقبل
        future_dates = []
        for r in results:
            date_str = r.get("reportDate")
            if not date_str:
                continue
            try:
                d = dt.datetime.strptime(date_str, "%Y-%m-%d").date()
                if d >= TODAY():
                    future_dates.append(d)
            except:
                continue

        if not future_dates:
            return None

        next_date = min(future_dates)
        return next_date.isoformat()

    except Exception as e:
        print(f"[WARN] get_next_earnings({symbol}): {e}")
        return None
    
# ---------------------- Polygon fetch -----------------------
def fetch_all(symbol):
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

def list_fridays(expiries):
    fr = []
    for d in expiries:
        try:
            y, m, dd = map(int, d.split("-"))
            if dt.date(y, m, dd).weekday() == 4:
                fr.append(d)
        except Exception:
            continue
    return sorted(fr)

def nearest_weekly(expiries, next_week=False):
    fridays = list_fridays(expiries)
    if not fridays:
        return expiries[0] if expiries else None
    if next_week and len(fridays) > 1:
        return fridays[1]
    return fridays[0]

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

# ------------- Net Gamma + IV (raw aggregation) -------------
def _aggregate_gamma_by_strike(rows, price, split_by_price=True):
    calls_map, puts_map = {}, {}
    if price is None: return calls_map, puts_map

    low_bound  = price * 0.75
    high_bound = price * 1.25

    for r in rows:
        det    = r.get("details", {}) or {}
        strike = det.get("strike_price")
        ctype  = det.get("contract_type")
        oi     = r.get("open_interest")
        iv     = r.get("implied_volatility")
        greeks = r.get("greeks") or {}
        und    = r.get("underlying_asset") or {}
        uprice = und.get("price", price)

        if not (isinstance(strike, (int, float)) and isinstance(oi, (int, float)) and isinstance(uprice, (int, float))):
            continue

        if split_by_price and not (low_bound <= float(strike) <= high_bound):
            continue

        gamma = float(greeks.get("gamma", 0.0) or 0.0)
        iv_val = float(iv) if isinstance(iv, (int, float)) else 0.0
        sign = 1.0 if ctype == "call" else -1.0
        net_gamma = sign * gamma * float(oi) * 100.0 * float(uprice)

        if ctype == "call":
            if strike not in calls_map:
                calls_map[strike] = {"net_gamma": 0.0, "iv": iv_val, "count": 0}
            calls_map[strike]["net_gamma"] += net_gamma
            calls_map[strike]["iv"] = (calls_map[strike]["iv"] * calls_map[strike]["count"] + iv_val) / (calls_map[strike]["count"] + 1)
            calls_map[strike]["count"] += 1

        elif ctype == "put":
            if strike not in puts_map:
                puts_map[strike] = {"net_gamma": 0.0, "iv": iv_val, "count": 0}
            puts_map[strike]["net_gamma"] += net_gamma
            puts_map[strike]["iv"] = (puts_map[strike]["iv"] * puts_map[strike]["count"] + iv_val) / (puts_map[strike]["count"] + 1)
            puts_map[strike]["count"] += 1

    for d in (calls_map, puts_map):
        for k in list(d.keys()):
            v = d[k]
            d[k] = {"net_gamma": float(v["net_gamma"]), "iv": float(v["iv"])}
    return calls_map, puts_map

def _pick_top7_directional(calls_map, puts_map):
    all_items = []
    for s, v in calls_map.items():
        all_items.append((float(s), float(v["net_gamma"]), float(v["iv"])))
    for s, v in puts_map.items():
        all_items.append((float(s), float(v["net_gamma"]), float(v["iv"])))
    if not all_items: return []
    max_abs = max(abs(x[1]) for x in all_items) or 1.0
    all_items = [x for x in all_items if abs(x[1]) >= 0.2 * max_abs]
    pos = [t for t in all_items if t[1] > 0]
    neg = [t for t in all_items if t[1] < 0]
    pos_sorted = sorted(pos, key=lambda x: x[1], reverse=True)
    neg_sorted = sorted(neg, key=lambda x: x[1])
    top_pos = pos_sorted[:3]
    top_neg = neg_sorted[:3]
    strongest = max(all_items, key=lambda x: abs(x[1]))
    sel, seen = [], set()
    def _add_unique(items):
        for (s, g, iv) in items:
            key = (round(s, 6), round(g, 6))
            if key not in seen:
                sel.append((s, g, iv)); seen.add(key)
    _add_unique(top_pos); _add_unique([strongest]); _add_unique(top_neg)
    if len(sel) < 7:
        remaining = [x for x in all_items if (round(x[0],6), round(x[1],6)) not in seen]
        remaining_sorted = sorted(remaining, key=lambda x: abs(x[1]), reverse=True)
        for x in remaining_sorted:
            if len(sel) >= 7: break
            _add_unique([x])
    return sorted(sel, key=lambda x: x[0])[:7]

# ----------------- Net Gamma + IV analysis -----------------
def analyze_gamma_iv_v51(rows, expiry, split_by_price=True):
    rows = [r for r in rows if r.get("details", {}).get("expiration_date") == expiry]
    if not rows: return None, []
    price = None
    for r in rows:
        p = r.get("underlying_asset", {}).get("price")
        if isinstance(p, (int, float)) and p > 0:
            price = float(p); break
    if price is None: return None, []
    calls_map, puts_map = _aggregate_gamma_by_strike(rows, price, split_by_price=split_by_price)
    picks = _pick_top7_directional(calls_map, puts_map)
    return price, picks

# -------------------- Pine normalization -------------------
def normalize_for_pine_v51(picks):
    if not picks: return [], [], [], []
    max_abs = max(abs(v) for (_, v, __) in picks) or 1.0
    strikes = [round(float(s), 2) for (s, _, __) in picks]
    pcts    = [round(abs(v)/max_abs, 4) for (_, v, __) in picks]
    ivs     = [round(float(iv), 4) for (_, __, iv) in picks]
    signs   = [1 if v > 0 else -1 if v < 0 else 0 for (_, v, __) in picks]
    return strikes, pcts, ivs, signs

def to_pine_array(arr):
    return ",".join(f"{float(x):.6f}" for x in arr if x is not None)

def arr_or_empty(arr):
    txt = to_pine_array(arr)
    return f"array.from({txt})" if txt else "array.new_float()"

def to_pine_int_array(arr):
    return ",".join(str(int(x)) for x in arr)

def arr_or_empty_int(arr):
    txt = to_pine_int_array(arr)
    return f"array.from({txt})" if txt else "array.new_int()"

# -------------------- Expected Move (EM) -------------------
def compute_weekly_em(rows, weekly_expiry):
    if not weekly_expiry: return None, None, None
    price = None
    for r in rows:
        p = r.get("underlying_asset", {}).get("price")
        if isinstance(p, (int, float)) and p > 0:
            price = float(p); break
    if price is None: return None, None, None
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
    y, m, d = map(int, weekly_expiry.split("-")); exp_date = dt.date(y, m, d)
    days = max((exp_date - TODAY()).days, 1)
    em = price * iv_annual * math.sqrt(days / 365.0)
    return price, iv_annual, em
# -------------------- Dynamic Thresholds --------------------
def _dynamic_thresholds(total_oi):
    """
    يحدد الحساسية المناسبة حسب إجمالي OI الأسبوعي.
    """
    if total_oi >= 500_000:
        return 0.10, 0.10, 0.04  # مؤشرات ضخمة مثل SPY / AAPL
    elif total_oi >= 100_000:
        return 0.15, 0.15, 0.05  # أسهم كبرى مثل NVDA / MSFT / META
    elif total_oi >= 30_000:
        return 0.20, 0.20, 0.07  # متوسطة السيولة مثل PLTR / AMD / LULU
    else:
        return 0.25, 0.25, 0.09  # ضعيفة السيولة أو قليلة العقود

# ===================== ΔOI + ΔIV SIGNALS ====================
def _aggregate_oi_iv(rows, expiry, ref_price=None):
    """
    ترجع مجموع OI للكول والبت + IV-ATM تقريبي (أقرب سترايك للسعر).
    """
    rows = [r for r in rows if r.get("details", {}).get("expiration_date") == expiry]
    if not rows: return None
    price = ref_price
    if price is None:
        for r in rows:
            p = r.get("underlying_asset", {}).get("price")
            if isinstance(p, (int, float)) and p > 0:
                price = float(p); break
    calls_oi = 0.0; puts_oi = 0.0
    iv_atm = None; best_diff = 1e18
    for r in rows:
        det = r.get("details", {}) or {}
        strike = det.get("strike_price")
        ctype  = det.get("contract_type")
        oi     = r.get("open_interest")
        iv     = r.get("implied_volatility")
        if isinstance(oi, (int,float)):
            if ctype == "call": calls_oi += float(oi)
            elif ctype == "put": puts_oi += float(oi)
        if isinstance(strike, (int,float)) and isinstance(iv, (int,float)) and isinstance(price, (int,float)):
            diff = abs(float(strike) - float(price))
            if diff < best_diff:
                best_diff = diff; iv_atm = float(iv)
    return {"calls": calls_oi, "puts": puts_oi, "iv_atm": iv_atm, "price": price}

def _get_baseline(symbol, expiry):
    sym_map = DAILY_BASE.get(symbol) or {}
    rec = sym_map.get(expiry)
    if rec:
        last_ts = rec.get("timestamp")
        if last_ts:
            last_dt = dt.datetime.strptime(last_ts, "%Y-%m-%dT%H:%M")
            # اعتبره صالحاً فقط لو لم يمر عليه أكثر من ساعة
            if (dt.datetime.now() - last_dt).total_seconds() < 3600:
                return rec  # baseline set within the last hour

    return None

def _set_baseline(symbol, expiry, agg):
    DAILY_BASE.setdefault(symbol, {})
    DAILY_BASE[symbol][expiry] = {
        "timestamp": dt.datetime.now().strftime("%Y-%m-%dT%H:00"),
        "calls": float(agg["calls"] or 0.0),
        "puts":  float(agg["puts"]  or 0.0),
        "iv_atm": float(agg["iv_atm"] or 0.0)
    }

def _detect_credit_signal(today_agg, base_agg):
    """
    يرجع dict: { 'signal', 'call_rate','put_rate','iv_rate','explain' }
    """
    if not (today_agg and base_agg): 
        return {"signal":"⚪ Neutral (no baseline)","call_rate":None,"put_rate":None,"iv_rate":None,"explain":"no-baseline"}
    base_calls = max(base_agg["calls"], 1.0)
    base_puts  = max(base_agg["puts"],  1.0)
    base_iv    = max(base_agg["iv_atm"], 1e-9)
    total_base_oi = base_agg["calls"] + base_agg["puts"]

    # ⚙️ تحديد الحساسية الديناميكية
    TH_CALL_RATE, TH_PUT_RATE, TH_IV_RATE = _dynamic_thresholds(total_base_oi)

    # احترم حد أدنى للـ OI
    if (base_agg["calls"] + base_agg["puts"]) < MIN_BASE_OI:
        return {"signal":"⚪ Neutral (low base OI)","call_rate":0.0,"put_rate":0.0,"iv_rate":0.0,"explain":"low-base-oi"}

    call_rate = (today_agg["calls"] - base_agg["calls"]) / base_calls
    put_rate  = (today_agg["puts"]  - base_agg["puts"])  / base_puts
    iv_rate   = (today_agg["iv_atm"] - base_agg["iv_atm"]) / base_iv if (today_agg["iv_atm"] and base_agg["iv_atm"]) else 0.0

    # قواعد القرار
    if call_rate >= TH_CALL_RATE and put_rate <= 0.00 and iv_rate >= TH_IV_RATE:
        sig = "📈 Bullish → Credit Put Spread ✅"
    elif put_rate  >= TH_PUT_RATE  and call_rate <= 0.00 and iv_rate >= TH_IV_RATE:
        sig = "📉 Bearish → Credit Call Spread ✅"
    else:
        sig = "⚪ Neutral"

    return {
        "signal": sig,
        "call_rate": round(call_rate, 4),
        "put_rate":  round(put_rate, 4),
        "iv_rate":   round(iv_rate, 4),
        "explain":   "rules-v1"
    }
# ---------------------- Flow Tracking (ΔOI + ΔGamma) ----------------------
def track_flow(symbol, rows, prev_data):
    """
    🔍 يحلل تحركات السيولة بين التحديث الحالي والسابق.
    prev_data = بيانات آخر Snapshot من data/all.json
    """
    try:
        price = None
        for r in rows:
            p = r.get("underlying_asset", {}).get("price")
            if isinstance(p, (int, float)):
                price = float(p)
                break

        if price is None:
            return {"status": "no-price"}

        # 🔹 بناء خريطة OI + Gamma الحالية
        flow_map = {}
        for r in rows:
            det = r.get("details", {})
            strike = det.get("strike_price")
            ctype = det.get("contract_type")
            oi = r.get("open_interest") or 0
            gamma = (r.get("greeks") or {}).get("gamma", 0)
            if not isinstance(strike, (int, float)):
                continue
            key = f"{ctype}_{int(strike)}"
            flow_map[key] = {"oi": oi, "gamma": gamma}

        # 🔹 مقارنة مع البيانات السابقة
        changes = []
        old = prev_data.get("flow", {}) if isinstance(prev_data, dict) else {}
        for key, v in flow_map.items():
            old_v = old.get(key, {"oi": 0, "gamma": 0})
            d_oi = v["oi"] - old_v.get("oi", 0)
            d_gm = v["gamma"] - old_v.get("gamma", 0)
            if abs(d_oi) > 50:  # تجاهل تغيرات بسيطة
                changes.append({"strike": key, "d_oi": round(d_oi, 2), "d_gamma": round(d_gm, 6)})

        # 🔹 تحليل الاتجاه
        puts_up = sum(c["d_oi"] for c in changes if "put" in c["strike"].lower() and c["d_oi"] > 0)
        calls_up = sum(c["d_oi"] for c in changes if "call" in c["strike"].lower() and c["d_oi"] > 0)

        flow_signal = "⚪ محايد"
        if puts_up > calls_up * 1.3:
            flow_signal = "📈 تدفق سيولة إلى عقود PUT (دعم السوق)"
        elif calls_up > puts_up * 1.3:
            flow_signal = "📉 تدفق سيولة إلى عقود CALL (ضغط بيعي)"

        return {
            "flow_signal": flow_signal,
            "puts_up": puts_up,
            "calls_up": calls_up,
            "flow": flow_map
        }
    except Exception as e:
        return {"error": str(e)}

# -------------------- Update + Cache -----------------------
def update_symbol_data(symbol):
    rows = fetch_all(symbol)
    expiries = list_future_expiries(rows)
    if not expiries:
        return None

    # Weekly targets
    exp_curr = nearest_weekly(expiries, next_week=False)
    exp_next = nearest_weekly(expiries, next_week=True)
    exp_m    = nearest_monthly(expiries)

    # Weekly / Monthly picks
    wc_price, wc_picks = analyze_gamma_iv_v51(rows, exp_curr, split_by_price=True) if exp_curr else (None, [])
    wn_price, wn_picks = analyze_gamma_iv_v51(rows, exp_next, split_by_price=True) if exp_next else (None, [])
    m_price,  m_picks  = analyze_gamma_iv_v51(rows, exp_m,    split_by_price=True) if exp_m    else (None, [])

    # EM
    em_curr_price, em_curr_iv, em_curr_value = compute_weekly_em(rows, exp_curr) if exp_curr else (None, None, None)
    em_next_price, em_next_iv, em_next_value = compute_weekly_em(rows, exp_next) if exp_next else (None, None, None)

    # ΔOI + ΔIV signals per weekly expiry
    signals = {}
    for tag, ex in (("current", exp_curr), ("next", exp_next)):
        if ex:
            # aggregate today
            agg_today = _aggregate_oi_iv(rows, ex, ref_price=wc_price if tag=="current" else wn_price)
            # make baseline if not exist for today (أول مرة تُستدعى اليوم)
            base = _get_baseline(symbol, ex)
            if base is None and agg_today:
                _set_baseline(symbol, ex, agg_today)
                base = _get_baseline(symbol, ex)
            # detect
            sig = _detect_credit_signal(agg_today, base)
            signals[tag] = {"expiry": ex, "today": agg_today, "base": base, "signal": sig}
        else:
            signals[tag] = None

    data = {
        "symbol": symbol,
        "weekly_current": {"expiry": exp_curr, "price": wc_price, "picks": wc_picks},
        "weekly_next":    {"expiry": exp_next, "price": wn_price, "picks": wn_picks},
        "monthly":        {"expiry": exp_m,    "price": m_price,  "picks": m_picks},
        "em": {
            "current": {"price": em_curr_price, "iv_annual": em_curr_iv, "weekly_em": em_curr_value},
            "next":    {"price": em_next_price, "iv_annual": em_next_iv, "weekly_em": em_next_value},
        },
        "signals": signals,
        "timestamp": time.time()
    }
    # 🔄 تحليل تدفق السيولة (Flow)
    prev = {}
    try:
        with open("data/all.json", "r", encoding="utf-8") as f:
            prev_file = json.load(f)
            prev = (prev_file.get("data", {}).get(symbol, {}) or {})
    except:
        pass

    flow_result = track_flow(symbol, rows, prev)
    data["flow"] = flow_result

    earn_date = get_next_earnings(symbol)
    data["earnings_date"] = earn_date
    return data

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

        # Weekly CURRENT arrays
        wc_s, wc_p, wc_iv, wc_sgn = normalize_for_pine_v51(data["weekly_current"]["picks"])
        # Weekly NEXT arrays
        wn_s, wn_p, wn_iv, wn_sgn = normalize_for_pine_v51(data["weekly_next"]["picks"])
        # Monthly arrays
        m_s,  m_p,  m_iv,  m_sgn  = normalize_for_pine_v51(data["monthly"]["picks"])

        # EM (current/next)
        em_c = data.get("em", {}).get("current", {}) or {}
        em_n = data.get("em", {}).get("next", {}) or {}

        em_c_val = em_c.get("weekly_em"); em_c_iv = em_c.get("iv_annual"); em_c_pr = em_c.get("price")
        em_n_val = em_n.get("weekly_em"); em_n_iv = em_n.get("iv_annual"); em_n_pr = em_n.get("price")

        emc_txt = "na" if em_c_val is None else f"{float(em_c_val):.6f}"
        emc_ivt = "na" if em_c_iv  is None else f"{float(em_c_iv):.6f}"
        emc_prt = "na" if em_c_pr  is None else f"{float(em_c_pr):.6f}"

        emn_txt = "na" if em_n_val is None else f"{float(em_n_val):.6f}"
        emn_ivt = "na" if em_n_iv  is None else f"{float(em_n_iv):.6f}"
        emn_prt = "na" if em_n_pr  is None else f"{float(em_n_pr):.6f}"

        # Signals
        sigs = data.get("signals", {}) or {}
        sig_curr = sigs.get("current") or {}
        sig_next = sigs.get("next") or {}
        sig_text_curr = sig_curr.get("signal", {}).get("signal", "⚪ Neutral")
        sig_text_next = sig_next.get("signal", {}).get("signal", "⚪ Neutral")

        block = f"""
//========= {sym} =========
if syminfo.ticker == "{sym}"
    title = " PRO • " + mode + " | {sym}"
    
    // --- إشارات السيرفر ---
    sig_text_curr = "{sig_text_curr}"
    sig_text_next = "{sig_text_next}"

    // نظّف الرسومات القديمة
    clear_visuals(optLines, optLabels)

    // Weekly (اختيار الأسبوع من weekMode)
    if mode == "Weekly"
        if weekMode == "Current"
            draw_bars({arr_or_empty(wc_s)}, {arr_or_empty(wc_p)}, {arr_or_empty(wc_iv)}, {arr_or_empty_int(wc_sgn)})
        else
            draw_bars({arr_or_empty(wn_s)}, {arr_or_empty(wn_p)}, {arr_or_empty(wn_iv)}, {arr_or_empty_int(wn_sgn)})

    // Monthly
    if mode == "Monthly"
        draw_bars({arr_or_empty(m_s)}, {arr_or_empty(m_p)}, {arr_or_empty(m_iv)}, {arr_or_empty_int(m_sgn)})

    // === Expected Move lines (gold), تتبع اختيار الأسبوع ===
    em_curr_value = {emc_txt}
    em_curr_iv    = {emc_ivt}
    em_curr_price = {emc_prt}

    em_next_value = {emn_txt}
    em_next_iv    = {emn_ivt}
    em_next_price = {emn_prt}

    // السعر المرجعي الأسبوعي لضبط المركز
    currentPrice = request.security(syminfo.tickerid, "W", close)

    var line emTop  = line.new(na, na, na, na)
    var line emBot  = line.new(na, na, na, na)
    var label emTopL = na
    var label emBotL = na

    em_value = weekMode == "Current" ? em_curr_value : em_next_value
    sel_ok   = not na(em_value)

    if sel_ok
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

        emTopL := label.new(bar_index, up, "📈 أعلى مدى متوقع: " + str.tostring(up, "#.##"),style=label.style_label_down, color=color.new(gold, 0), textcolor=color.black, size=size.small)
        emBotL := label.new(bar_index, dn, "📉 أدنى مدى متوقع: " + str.tostring(dn, "#.##"),style=label.style_label_up,   color=color.new(gold, 0), textcolor=color.black, size=size.small)

    // === Credit Signal Table (ΔOI + ΔIV) ===
    var table sigT = table.new(position.bottom_right, 2, 3)  // عمودين × صفين

    if barstate.islast
        // الصف الأول: الأسبوع الحالي
        table.cell(sigT, 0, 0, "الاسبوع  الحالي", text_color=color.white, bgcolor=color.new(color.black, 0), text_size=size.small)
        table.cell(sigT, 1, 0, sig_text_curr, text_color=color.white, bgcolor=color.new(color.black, 0), text_size=size.small)

        // الصف الثاني: الأسبوع القادم
        table.cell(sigT, 0, 1, "الاسبوع  القادم", text_color=color.white, bgcolor=color.new(color.black, 0), text_size=size.small)
        table.cell(sigT, 1, 1, sig_text_next, text_color=color.white, bgcolor=color.new(color.black, 0), text_size=size.small)
        // الصف الثالث: تاريخ الأرباح القادم
        earn_date = "{data.get('earnings_date') or 'N/A'}"
        table.cell(sigT, 0, 2, "Next Earnings:", text_color=color.new(color.yellow, 0), bgcolor=color.new(color.black, 0), text_size=size.small)
        table.cell(sigT, 1, 2, earn_date, text_color=color.new(color.yellow, 0), bgcolor=color.new(color.black, 0), text_size=size.small)

"""
        blocks.append(block)

    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=3)))
    last_update = now.strftime("%Y-%m-%d %H:%M:%S")

    pine = f"""//@version=5
// Last Update (Riyadh): {last_update}
indicator("GEX PRO (v6.9)", overlay=true, max_lines_count=500, max_labels_count=500, dynamic_requests=true)

// إعدادات عامة
mode     = "Weekly"
weekMode = input.string("Current", "Expiry Week", options=["Current","Next"])

// مصفوفات للرسم العام
var line[]  optLines  = array.new_line()
var label[] optLabels = array.new_label()

// تنظيف
clear_visuals(_optLines, _optLabels) =>
    if array.size(_optLines) > 0
        for l in _optLines
            line.delete(l)
        array.clear(_optLines)
    if array.size(_optLabels) > 0
        for lb in _optLabels
            label.delete(lb)
        array.clear(_optLabels)

// رسم الأشرطة الاتجاهية (حتى 7)
draw_bars(_s, _p, _iv, _sgn) =>
    if barstate.islast and array.size(_s) > 0 and array.size(_p) > 0 and array.size(_iv) > 0 and array.size(_sgn) > 0
        limit = math.min(array.size(_s), 7)
        for i = 0 to limit - 1
            y   = array.get(_s, i)
            pct = array.get(_p, i)
            iv  = array.get(_iv, i)
            sgn = array.get(_sgn, i)

            bar_col = sgn > 0 ? color.new(color.lime, 20) : sgn < 0 ? color.new(color.rgb(220,50,50), 20) : color.new(color.gray, 20)
            alpha   = 90 - int(pct * 70)
            bar_col := color.new(bar_col, alpha)
            bar_len = int(math.max(10, pct * 50))

            line.new(bar_index + 3, y, bar_index + bar_len + 12, y, color=bar_col, width=6)
            label.new(bar_index + bar_len + 2, y, str.tostring(pct*100, "#.##") + "% | IV " + str.tostring(iv*100, "#.##"), style=label.style_label_left, color=color.rgb(95, 93, 93), textcolor=color.white, size=size.small)

// --- Per-symbol blocks ---
{''.join(blocks)}
"""
    return Response(pine, mimetype="text/plain")
# ============================================================
# 🧠 تقييم نوع الفرصة (Put / Call Credit) بناءً على ΔOI و Γ
# ============================================================
def evaluate_credit_opportunity(sig_text, delta_oi_calls, delta_oi_puts, delta_gamma):
    """
    يرجع نص الفرصة + الملاحظة بناءً على حركة السيولة وسلوك Gamma
    """
    # إذا لم تتوفر البيانات
    if delta_oi_calls is None or delta_oi_puts is None or delta_gamma is None:
        return "—", "⚪ بيانات غير مكتملة"

    ratio = 0
    if delta_oi_calls > 0 and math.isfinite(delta_oi_puts / delta_oi_calls):
        ratio = delta_oi_puts / delta_oi_calls


    # 📈 سيولة في PUTs = دعم
    if ratio >= 1.3 and delta_gamma > 0:
        return "✅ افتح Put Credit Spread", "📈 دعم مؤسسي قوي – احتمال ارتداد من الأسفل"

    elif ratio >= 1.0 and delta_gamma < 0:
        return "⚠️ لا تدخل الآن", "🔻 فتح مراكز بيع للتحوط أو مضاربة سلبية – انتظر تأكيد من السعر أو RSI"

    elif delta_oi_calls < 0.1 and delta_oi_puts < 0.1:
        return "🚫 لا صفقة اليوم", "⚪ لا يوجد تحرك حقيقي بالسيولة"

    # 📉 سيولة في CALLs = ضغط بيعي
    elif delta_oi_calls >= 1.3 * delta_oi_puts and delta_gamma < 0:
        return "✅ افتح Call Credit Spread", "📉 ضغط بيعي مؤسسي – مقاومة قوية متوقعة"

    elif delta_oi_calls >= 1.0 * delta_oi_puts and delta_gamma > 0:
        return "⚠️ تجنب الدخول", "⚠️ ارتفاع مضاربي غير مستقر – احتمال ارتفاع مؤقت"

    else:
        return "—", "⚪ اتجاه السيولة غير واضح"
# ============================================================
# 🧾 سجل يومي للفرص المكتشفة (Credit Flow Log)
# ============================================================
def log_opportunity(symbol, credit_text, note, flow_signal):
    log_path = "data/opportunities.json"
    os.makedirs("data", exist_ok=True)
    
    data = {}
    if os.path.exists(log_path):
        with open(log_path, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
            except:
                data = {}

    entry = {
        "timestamp": dt.datetime.utcnow().isoformat() + "Z",
        "credit": credit_text,
        "note": note,
        "flow": flow_signal
    }

    data.setdefault(symbol, []).append(entry)

    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


@app.route("/report/pine/all")
def report_pine_all():
    """تقرير شامل لجميع الشركات (Credit Monitor Report) مع إظهار وقت آخر تحديث البيانات"""
    try:
        now_hhmm = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        with open("data/all.json", "r", encoding="utf-8") as f:
            data = json.load(f)

        # 🩵 حماية من الخطأ إذا الملف كان list بدل dict
        if isinstance(data, list):
            # لو الملف يحتوي قائمة، نحاول نأخذ أول عنصر (قاموس) منها
            if len(data) > 0 and isinstance(data[0], dict):
                data = data[0]
            else:
                data = {"updated": None, "symbols": [], "data": {}}


        updated_iso = data.get("updated") or ""
        updated_display = updated_iso if updated_iso else "غير متوفر"

        symbols = data.get("symbols", [])
        all_data = data.get("data", {})

        def classify(sig_text: str):
            s = (sig_text or "").strip()
            if "Bull" in s or "Put" in s or "📈" in s:
                return "bull", "Credit Put Spread"
            if "Bear" in s or "Call" in s or "📉" in s:
                return "bear", "Credit Call Spread"
            return "neutral", "محايد"

        html = f"""
        <html dir="rtl" lang="ar">
        <head>
        <meta charset="utf-8">
        <title>تقرير Bassam GEX Pro v7.0 – مراقبة فرص Credit – {now_hhmm}</title>
        <style>
            @import url('https://fonts.googleapis.com/css2?family=Tajawal:wght@400;500;700&display=swap');
            :root {{
                --bg: #0a0a0a;
                --panel: #141414;
                --grid: #222;
                --grid-soft: #1a1a1a;
                --text: #f2f2f2;
                --muted: #9aa0a6;
                --accent: #00ffb0;
                --bull: #13f29a;
                --bear: #ff5757;
                --neutral: #bdbdbd;
            }}
            * {{ box-sizing: border-box; }}
            body {{
                font-family: "Tajawal", system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
                background-color: var(--bg);
                color: var(--text);
                padding: 24px;
                line-height: 1.65;
            }}
            .wrap {{ max-width: 1200px; margin: 0 auto; }}
            h1 {{
                color: var(--accent);
                text-align: center;
                margin: 0 0 10px 0;
                font-size: 26px;
                font-weight: 700;
            }}
            .sub {{
                text-align: center;
                color: var(--muted);
                margin-bottom: 24px;
                font-size: 14px;
            }}
            .card {{
                background: var(--panel);
                border: 1px solid var(--grid);
                border-radius: 14px;
                padding: 14px;
                margin-bottom: 18px;
            }}
            table {{
                width: 100%;
                border-collapse: collapse;
                overflow: hidden;
                border-radius: 10px;
            }}
            thead th {{
                background-color: #101010;
                color: var(--accent);
                font-weight: 600;
                border-bottom: 1px solid var(--grid);
                padding: 10px 12px;
                text-align: center;
                white-space: nowrap;
            }}
            tbody td {{
                border-bottom: 1px solid var(--grid);
                padding: 10px 12px;
                text-align: center;
                vertical-align: middle;
            }}
            tbody tr:nth-child(even) {{ background-color: var(--grid-soft); }}
            .chip {{
                display: inline-block;
                padding: 4px 10px;
                border-radius: 999px;
                font-weight: 600;
                font-size: 12px;
            }}
            .bull {{ color: var(--bull); }}
            .bear {{ color: var(--bear); }}
            .neutral {{ color: var(--neutral); }}
            .chip.bull {{ border: 1px solid var(--bull); }}
            .chip.bear {{ border: 1px solid var(--bear); }}
            .chip.neutral {{ border: 1px solid var(--neutral); }}
            .muted {{ color: var(--muted); font-size: 12px; }}
            footer {{
                text-align: center;
                color: var(--muted);
                margin-top: 22px;
                font-size: 13px;
            }}
        </style>
        </head>
        <body>
        <div class="wrap">
            <h1>تقرير Bassam GEX Pro v7.0 – مراقبة فرص  – {now_hhmm}</h1>
            <div class="sub">
                🔄 آخر تحديث من البيانات: <b>{updated_display}</b>
            </div>

            <div class="card">
                <table>
                    <thead>
                        <tr>
                            <th>الرمز</th>
                            <th>الإشارة</th>
                            <th>نوع الصفقة</th>
                            <th>نطاق الجاما الاسبوعي (Top7)</th>
                            <th>الفرصة المقترحة</th>
                            <th>الملاحظة</th>
                            <th>اتجاه السيولة</th>

                        </tr>
                    </thead>
        """
        html += "<tbody>"

        # ========================================
        # 🔹 توليد صفوف التقرير (HTML Table Rows)
        # ========================================
        for sym in symbols:
            s = all_data.get(sym, {})
        
            # 🩵 حماية ذكية ضد بيانات غير صالحة (list أو dict)
            if isinstance(s, list):
                if len(s) > 0 and isinstance(s[0], dict):
                    s = s[0]
                else:
                    # ❗ في حال بيانات السهم فارغة أو خاطئة
                    print(f"[WARN] {sym} has invalid data structure → resetting.")
                    s = {}

            elif not isinstance(s, dict):
                print(f"[WARN] {sym} data type = {type(s)}, expected dict → resetting.")
                s = {}

            # 🟢 حماية ضد العناصر الداخلية المفقودة
            wcur = s.get("weekly_current") or {}
            signals = s.get("signals") or {}
            flow_data = s.get("flow") or {}

            if not isinstance(wcur, dict): wcur = {}
            if not isinstance(signals, dict): signals = {}
            if not isinstance(flow_data, dict): flow_data = {}

            wk = wcur.get("picks", []) if isinstance(wcur, dict) else []
            price = wcur.get("price", 0) if isinstance(wcur, dict) else 0
            expiry = wcur.get("expiry", "") if isinstance(wcur, dict) else ""


            sig_data = s.get("signals", {})
            if isinstance(sig_data, list):
                sig_data = sig_data[0] if sig_data and isinstance(sig_data[0], dict) else {}

            curr = sig_data.get("current", {})
            if isinstance(curr, list):
                curr = curr[0] if curr and isinstance(curr[0], dict) else {}
            
            sig_block = curr.get("signal", {})
            if isinstance(sig_block, list):
                sig_block = sig_block[0] if sig_block and isinstance(sig_block[0], dict) else {}
            
            sig_text = sig_block.get("signal", "⚪ Neutral")


            # 🔹 تحليل الصفقة المقترحة
            credit_text = "—"
            note = "—"

            # 🔹 تحليل الفرصة حسب البيانات
            sig = s.get("signals", {}).get("current", {}).get("signal", {})
            sig_text = sig.get("signal", "⚪ Neutral")
            
            today = s.get("signals", {}).get("current", {}).get("today", {})
            base = s.get("signals", {}).get("current", {}).get("base", {})

            delta_oi_calls = (today.get("calls", 0) - base.get("calls", 0)) / max(base.get("calls", 1), 1)
            delta_oi_puts  = (today.get("puts", 0) - base.get("puts", 0)) / max(base.get("puts", 1), 1)
            delta_gamma    = 0
            
            wk = s.get("weekly_current", {}).get("picks", [])
            if wk:
                gammas = [x.get("net_gamma", 0) for x in wk if isinstance(x, dict)]
                if gammas:
                    delta_gamma = sum(gammas) / len(gammas)

            # 🔍 تقييم الفرصة الذكية
            credit_text, note = evaluate_credit_opportunity(sig_text, delta_oi_calls, delta_oi_puts, delta_gamma)
            
            
            if wk and price:
                nearest = min(wk, key=lambda x: abs(x.get("strike", 0) - price))
                base_strike = nearest.get("strike", 0)
                net_gamma = nearest.get("net_gamma", 0)

                if "📈" in sig_text or "Bull" in sig_text:
                    short_leg = base_strike
                    long_leg = base_strike - 5
                    credit_text = f"📈 Put Credit Spread – بيع {short_leg}P / شراء {long_leg}P (تنتهي {expiry})"
                    note = "📈 دعم قوي أسفل السعر – احتمال ارتداد" if net_gamma > 0 else "⚠️ مراقبة الحركة – Gamma ضعيف حاليًا"

                elif "📉" in sig_text or "Bear" in sig_text:
                    short_leg = base_strike
                    long_leg = base_strike + 5
                    credit_text = f"📉 Call Credit Spread – بيع {short_leg}C / شراء {long_leg}C (تنتهي {expiry})"
                    note = "📉 Gamma سلبي قوي – ضغط بيعي محتمل" if net_gamma < 0 else "⚠️ تأكيد الاتجاه غدًا بعد تحديث OI"
                else:
                    note = "⚪ إشارة محايدة – لم يتأكد الاتجاه بعد"

            # 🔹 نطاق الجاما (Top7)
            if wk:
                gmin = min(wk, key=lambda x: x.get("strike", float("inf"))).get("strike", "")
                gmax = max(wk, key=lambda x: x.get("strike", float("-inf"))).get("strike", "")
                range_text = f"{gmin} → {gmax}"
            else:
                range_text = "—"

            # 🔹 تصنيف الإشارة
            cls, typ = classify(sig_text)
            sig_html = f'<span class="chip {cls}">{sig_text}</span>'

            # 🔹 صف الجدول
            # 🔹 اتجاه السيولة (Flow)
            flow_signal = s.get("flow", {}).get("flow_signal", "—")
            flow_color = "neutral"
            if "PUT" in flow_signal or "📈" in flow_signal:
                flow_color = "bull"
            elif "CALL" in flow_signal or "📉" in flow_signal:
                flow_color = "bear"

            flow_html = f'<span class="chip {flow_color}">{flow_signal}</span>'
            # 🔹 حفظ السجل اليومي
            log_opportunity(sym, credit_text, note, flow_signal)

            # 🔹 صف الجدول مع عمود جديد لاتجاه السيولة
            html += f"""
                <tr>
                    <td><b>{sym}</b></td>
                    <td>{sig_html}</td>
                    <td class="{cls}">{typ}</td>
                    <td>{range_text}</td>
                    <td>{credit_text}</td>
                    <td>{note}</td>
                    <td>{flow_html}</td>
                </tr>
            """


        # ✅ إغلاق HTML بالكامل
        html += f"""
                </tbody>
            </table>
            <div class="muted">* نطاق الجاما محسوب من أعلى 7 مستويات أسبوعية.</div>
        </div>

        <footer>© {dt.datetime.now().year} Bassam Al-Faifi — All Rights Reserved</footer>
    </div>
    </body>
    </html>
        """

        os.makedirs("data", exist_ok=True)
        with open("data/all.json", "w", encoding="utf-8") as f:
            json.dump({
                "updated": updated_iso,
                "symbols": symbols,
                "data": all_data
            }, f, ensure_ascii=False, indent=2)


        return Response(html, mimetype="text/html")
    except Exception as e:
        return jsonify({"error": str(e)})




# ---------------------- /signals/json ----------------------
@app.route("/signals/json")
def signals_json():
    if not POLY_KEY: return _err("Missing POLYGON_API_KEY", 401)
    out = {}
    for sym in SYMBOLS:
        d = get_symbol_data(sym)
        if not d: continue
        out[sym] = d.get("signals", {})
    return jsonify({"status": "OK", "updated": dt.datetime.utcnow().isoformat()+"Z", "data": out})

# ---------------------- /all/json --------------------------
@app.route("/all/json")
def all_json():
    if not POLY_KEY:
        return _err("Missing POLYGON_API_KEY", 401)
    all_data = {}
    for sym in SYMBOLS:
        data = get_symbol_data(sym)
        if not data:
            continue

        def _to_obj(picks):
            out = []
            for (s, ng, iv) in picks[:7]:
                out.append({"strike": s, "net_gamma": ng, "iv": iv})
            return out

        all_data[sym] = {
            "weekly_current": {
                "expiry": data["weekly_current"].get("expiry"),
                "price":  data["weekly_current"].get("price"),
                "top7":   _to_obj(data["weekly_current"].get("picks", []))
            },
            "weekly_next": {
                "expiry": data["weekly_next"].get("expiry"),
                "price":  data["weekly_next"].get("price"),
                "top7":   _to_obj(data["weekly_next"].get("picks", []))
            },
            "monthly": {
                "expiry": data["monthly"].get("expiry"),
                "price":  data["monthly"].get("price"),
                "top7":   _to_obj(data["monthly"].get("picks", []))
            },
            "em": data.get("em"),
            "signals": data.get("signals"),
            "earnings_date": data.get("earnings_date"),
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
        if not d: continue
        out[sym] = d.get("em", {})
    return jsonify({"status": "OK", "updated": dt.datetime.utcnow().isoformat()+"Z", "data": out})

# ------------------------ Root -----------------------------
@app.route("/")
def home():
    return jsonify({
        "status": "OK ✅",
        "message": "Bassam GEX PRO server is running (v6.9 – Dual Week + Dynamic EM + Credit Signals)",
        "note": "Data cache & signals updating..."
    })

# ------------------------ Background Loader ----------------
def warmup_cache():
    print("🔄 Warming up cache in background...")
    for sym in SYMBOLS:
        try:
            get_symbol_data(sym)
            print(f"✅ Cached {sym}")
        except Exception as e:
            print(f"⚠️ Failed to cache {sym}: {e}")
    print("✅ Cache warm-up complete.")


# 🔁 التحديث التلقائي كل ساعة
def auto_refresh():
    import time
    while True:
        try:
            print("🕒 Auto-refresh started...")
            for sym in SYMBOLS:
                try:
                    data = update_symbol_data(sym)
                    if data:
                        CACHE[sym] = data
                        print(f"✅ Updated {sym}")
                except Exception as e:
                    print(f"⚠️ Failed to update {sym}: {e}")

            # حفظ النسخة الكاملة إلى all.json
            all_data = {s: CACHE.get(s, {}) for s in SYMBOLS}
            os.makedirs("data", exist_ok=True)
            with open("data/all.json", "w", encoding="utf-8") as f:
                json.dump({
                    "updated": dt.datetime.utcnow().isoformat() + "Z",
                    "symbols": SYMBOLS,
                    "data": all_data
                }, f, ensure_ascii=False, indent=2)

            print("💾 Saved auto-refresh snapshot.")
        except Exception as e:
            print(f"❌ Auto-refresh error: {e}")

        # ⏰ انتظر ساعة قبل التحديث القادم
        time.sleep(3600)
# ---------------------- /opportunities/json ----------------------
@app.route("/opportunities/json")
def opportunities_json():
    """📊 عرض ملف سجل الفرص اليومية عبر المتصفح"""
    try:
        log_path = "data/opportunities.json"
        if not os.path.exists(log_path):
            return jsonify({"status": "empty", "message": "لم يتم إنشاء أي فرص بعد."})
        with open(log_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return jsonify({"status": "OK", "count": len(data), "data": data})
    except Exception as e:
        return jsonify({"error": str(e)})



if __name__ == "__main__":
    import threading
    threading.Thread(target=warmup_cache, daemon=True).start()
    threading.Thread(target=auto_refresh, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
