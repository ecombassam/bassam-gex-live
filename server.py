# ============================================================
# Bassam GEX PRO v5.9 – No Gamma Duplication (Final Polished)
# - Local Gamma Zones per symbol (no overlap)
# - No duplicated lines/labels (using barstate.islast)
# - Weekly Expected Move (centered at current price 1h)
# - Readable gray labels (black text)
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
            if dt.date(y, m, dd).weekday() == 4:
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

# -------------------- Γ helpers -----------------------------
def _gamma_from_row(r):
    g = r.get("gamma_exposure")
    if isinstance(g, (int, float)):
        return float(g)
    greeks = r.get("greeks", {})
    gamma_val = greeks.get("gamma", 0)
    oi_val = r.get("open_interest", 0)
    try:
        return float(gamma_val) * float(oi_val) * 100.0
    except Exception:
        return 0.0

# -------------------- Gamma zones ---------------------------
def top_gamma_zones(rows, price, expiry):
    if not expiry or price is None:
        return None, [], []

    rows = [r for r in rows if r.get("details", {}).get("expiration_date") == expiry]
    gamma_data = []
    for r in rows:
        strike = r.get("details", {}).get("strike_price")
        gamma  = _gamma_from_row(r)
        if isinstance(strike, (int, float)) and isinstance(gamma, (int, float)):
            gamma_data.append((float(strike), float(gamma)))

    if not gamma_data:
        return None, [], []

    spot = min(gamma_data, key=lambda x: abs(x[0] - price))
    above = sorted([d for d in gamma_data if d[0] > price],
                   key=lambda x: abs(x[1]), reverse=True)[:3]
    below = sorted([d for d in gamma_data if d[0] < price],
                   key=lambda x: abs(x[1]), reverse=True)[:3]
    return spot, above, below

# -------------------- Expected Move (EM) --------------------
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

# -------------------- Update + Cache ------------------------
def update_symbol_data(symbol):
    rows = fetch_all(symbol)
    expiries = list_future_expiries(rows)
    if not expiries: return None

    exp_w = nearest_weekly(expiries)
    exp_m = nearest_monthly(expiries)
    use_monthly_for_weekly = (exp_w == exp_m)
    target_exp = exp_m if use_monthly_for_weekly else exp_w

    em_price, em_iv, em_value = compute_weekly_em(rows, target_exp)
    spot, above, below = top_gamma_zones(rows, em_price, target_exp)

    return {
        "symbol": symbol,
        "expiry_used": target_exp,
        "duplicate": use_monthly_for_weekly,
        "em": {"price": em_price, "iv_annual": em_iv, "weekly_em": em_value},
        "gamma_zones": {
            "spot": spot,
            "above": above,
            "below": below
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

# ---------------------- /all/pine ---------------------------
@app.route("/all/pine")
def all_pine():
    if not POLY_KEY: return _err("Missing POLYGON_API_KEY", 401)

    def strikes_only(tuples):
        return [s for (s, _) in tuples] if tuples else []

    blocks = []
    for sym in SYMBOLS:
        data = get_symbol_data(sym)
        if not data: continue

        gz      = data.get("gamma_zones", {})
        spot    = gz.get("spot")
        above_s = strikes_only(gz.get("above"))
        below_s = strikes_only(gz.get("below"))

        spot_txt  = "na" if not spot else f"{float(spot[0]):.6f}"
        above_txt = ",".join(f"{float(v):.6f}" for v in above_s) if above_s else ""
        below_txt = ",".join(f"{float(v):.6f}" for v in below_s) if below_s else ""

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
    duplicate_expiry = {dup_str}

    // === Expected Move lines ===
    em_value = {em_txt}
    em_iv    = {iv_txt}
    em_price = {pr_txt}
    currentPrice = request.security(syminfo.tickerid, "60", close)

    if barstate.islast
        // --- Expected Move ---
        if not na(em_value)
            up = currentPrice + em_value
            dn = currentPrice - em_value
            gold = color.rgb(255,215,0)
            line.new(bar_index-5, up, bar_index+5, up, extend=extend.both, color=gold, style=line.style_dotted, width=2)
            line.new(bar_index-5, dn, bar_index+5, dn, extend=extend.both, color=gold, style=line.style_dotted, width=2)
            label.new(bar_index, up, "📈 أعلى مدى متوقع: " + str.tostring(up, "#.##"), style=label.style_label_down, color=gold, textcolor=color.black, size=size.small)
            label.new(bar_index, dn, "📉 أدنى مدى متوقع: " + str.tostring(dn, "#.##"), style=label.style_label_up, color=gold, textcolor=color.black, size=size.small)

        // --- Gamma Zones ---
        float spotG = {spot_txt}
        float[] aboveG = array.new_float()
        if "{above_txt}" != ""
            aboveG := array.from({above_txt})
        float[] belowG = array.new_float()
        if "{below_txt}" != ""
            belowG := array.from({below_txt})

        if not na(spotG)
            line.new(bar_index-3, spotG, bar_index+3, spotG, color=color.yellow, width=3)
            label.new(bar_index+6, spotG, "⚡ gamma", style=label.style_label_left, color=color.new(color.rgb(220,220,220), 0), textcolor=color.black, size=size.small)

        for i = 0 to array.size(aboveG)-1
            y = array.get(aboveG, i)
            line.new(bar_index-3, y, bar_index+3, y, color=color.new(color.red, 0), width=2, style=line.style_dashed)
            label.new(bar_index+5, y, "📈 gamma" + str.tostring(i+1), style=label.style_label_left, color=color.new(color.rgb(220,220,220), 0), textcolor=color.black, size=size.small)

        for i = 0 to array.size(belowG)-1
            y = array.get(belowG, i)
            line.new(bar_index-3, y, bar_index+3, y, color=color.new(color.green, 0), width=2, style=line.style_dashed)
            label.new(bar_index+5, y, "📉 gamma" + str.tostring(i+1), style=label.style_label_left, color=color.new(color.rgb(220,220,220), 0), textcolor=color.black, size=size.small)
"""
        blocks.append(block)

    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=3)))
    last_update = now.strftime("%Y-%m-%d %H:%M:%S")

    pine = f"""//@version=5
// Last Update (Riyadh): {last_update}
indicator("Bassam GEX PRO (v5.9) – No Gamma Duplication", overlay=true, max_lines_count=500, max_labels_count=500, dynamic_requests=true)

// --- Per-symbol blocks ---
{''.join(blocks)}
"""
    return Response(pine, mimetype="text/plain")

# ---------------------- /all/json ---------------------------
@app.route("/all/json")
def all_json():
    if not POLY_KEY:
        return _err("Missing POLYGON_API_KEY", 401)
    all_data = {}
    for sym in SYMBOLS:
        data = get_symbol_data(sym)
        if data:
            all_data[sym] = {
                "expiry_used": data.get("expiry_used"),
                "em": data.get("em"),
                "gamma_zones": data.get("gamma_zones"),
                "timestamp": data["timestamp"]
            }
    return jsonify({
        "status": "OK",
        "symbols": SYMBOLS,
        "updated": dt.datetime.utcnow().isoformat() + "Z",
        "data": all_data
    })

# ---------------------- /em/json ----------------------------
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

# ------------------------ Root ------------------------------
@app.route("/")
def home():
    return jsonify({
        "status": "OK ✅",
        "message": "Bassam GEX PRO v5.9 running (No Gamma Duplication)",
        "note": "Data cache loading in background..."
    })

# ------------------------ Background Loader -----------------
def warmup_cache():
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
    threading.Thread(target=warmup_cache, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
