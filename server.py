# ============================================================
# Bassam GEX PRO v6.3 – Gamma Zones Bars + Unified 100% Scale
# - Weekly EM centered at current price (1h)
# - Gamma Zones as BAR blocks (spot + 3 above + 3 below)
# - Unified Gamma normalization (strongest = 100%)
# - Full JSON/Pine integration
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

# -------------------- Gamma extraction ----------------------
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

# -------------------- Gamma Zones logic ---------------------
def gamma_zones(rows, price, expiry):
    """Return spot, top 3 above, top 3 below (strike, gamma)."""
    if not expiry or not price:
        return None, [], []
    rows = [r for r in rows if r.get("details", {}).get("expiration_date") == expiry]
    gdata = []
    for r in rows:
        strike = r.get("details", {}).get("strike_price")
        gamma  = _gamma_from_row(r)
        if isinstance(strike, (int,float)) and isinstance(gamma, (int,float)):
            gdata.append((float(strike), gamma))
    if not gdata:
        return None, [], []

    # الأقرب للسعر الحالي (spot)
    spot = min(gdata, key=lambda x: abs(x[0]-price))

    # الأعلى / الأدنى
    above = sorted([x for x in gdata if x[0] > price], key=lambda x: abs(x[1]), reverse=True)[:3]
    below = sorted([x for x in gdata if x[0] < price], key=lambda x: abs(x[1]), reverse=True)[:3]

    return spot, above, below

# -------------------- OI + IV + Gamma -----------------------
def analyze_oi_iv(rows, expiry, limit):
    rows = [r for r in rows if r.get("details", {}).get("expiration_date") == expiry]
    if not rows: return None, [], []
    price = None
    for r in rows:
        p = r.get("underlying_asset", {}).get("price")
        if isinstance(p, (int,float)) and p > 0:
            price = float(p)
            break

    calls, puts = [], []
    for r in rows:
        det = r.get("details", {})
        strike = det.get("strike_price")
        ctype  = det.get("contract_type")
        gamma  = _gamma_from_row(r)
        iv     = r.get("implied_volatility", 0)
        if not isinstance(strike, (int,float)): continue
        if ctype == "call":
            calls.append((strike, gamma, iv))
        elif ctype == "put":
            puts.append((strike, gamma, iv))

    all_g = [abs(g) for (_,g,_) in (calls+puts)]
    global_max = max(all_g) if all_g else 1
    if global_max == 0: global_max = 1

    def normalize(side): return [(s, g/global_max, iv) for (s,g,iv) in side]
    return price, normalize(calls)[:limit], normalize(puts)[:limit]

# -------------------- EM Calculation ------------------------
def compute_weekly_em(rows, expiry):
    if not expiry: return None, None, None
    wk = [r for r in rows if r.get("details", {}).get("expiration_date")==expiry]
    if not wk: return None, None, None
    price = next((r.get("underlying_asset",{}).get("price") for r in wk if r.get("underlying_asset")), None)
    if not price: return None,None,None
    ivs = [r.get("implied_volatility",0) for r in wk if isinstance(r.get("implied_volatility"),(int,float))]
    iv_annual = sum(ivs)/len(ivs) if ivs else None
    y,m,d = map(int,expiry.split("-"))
    days = max((dt.date(y,m,d)-TODAY()).days,1)
    em = price * (iv_annual or 0) * math.sqrt(days/365)
    return price, iv_annual, em

# -------------------- Update + Cache ------------------------
def update_symbol_data(sym):
    rows = fetch_all(sym)
    expiries = sorted({r.get("details",{}).get("expiration_date") for r in rows if r.get("details",{}).get("expiration_date")})
    if not expiries: return None
    exp = expiries[0]
    price, calls, puts = analyze_oi_iv(rows, exp, 5)
    spot, above, below = gamma_zones(rows, price, exp)
    em_price, em_iv, em_val = compute_weekly_em(rows, exp)
    return {
        "symbol": sym,
        "em": {"price": em_price, "iv_annual": em_iv, "weekly_em": em_val},
        "weekly": {"calls": calls, "puts": puts, "expiry": exp},
        "gamma_zones": {"spot": spot, "above": above, "below": below},
        "timestamp": time.time()
    }

def get_data(sym):
    now=time.time()
    if sym in CACHE and now-CACHE[sym]["timestamp"]<CACHE_EXPIRY:
        return CACHE[sym]
    d=update_symbol_data(sym)
    if d: CACHE[sym]=d
    return d

# ---------------------- /all/json ---------------------------
@app.route("/all/json")
def all_json():
    if not POLY_KEY: return _err("Missing POLYGON_API_KEY",401)
    data={}
    for s in SYMBOLS:
        d=get_data(s)
        if d: data[s]=d
    return jsonify({"status":"OK","symbols":SYMBOLS,"updated":dt.datetime.utcnow().isoformat()+"Z","data":data})

# ---------------------- /all/pine ---------------------------
@app.route("/all/pine")
def all_pine():
    out=[]
    for s in SYMBOLS:
        d=get_data(s)
        if not d: continue
        gz=d.get("gamma_zones",{})
        spot,above,below=gz.get("spot"),gz.get("above"),gz.get("below")
        spot_txt=f"{spot[0]:.6f}" if spot else "na"
        above_txt=",".join(f"{x[0]:.6f}" for x in above) if above else ""
        below_txt=",".join(f"{x[0]:.6f}" for x in below) if below else ""
        block=f"""
// === {s} Gamma Zones Bars ===
if syminfo.ticker == "{s}"
    var float spotG = {spot_txt}
    var float[] aboveG = array.new_float()
    if "{above_txt}" != ""
        aboveG := array.from({above_txt})
    var float[] belowG = array.new_float()
    if "{below_txt}" != ""
        belowG := array.from({below_txt})

    // --- Draw Bars ---
    if not na(spotG)
        line.new(bar_index-2, spotG, bar_index+28, spotG, color=color.new(color.gray,30), width=8)
        label.new(bar_index+30, spotG, "⚡ Spot Γ", style=label.style_label_left, color=color.new(color.rgb(220,220,220),0), textcolor=color.black, size=size.small)
    for i=0 to array.size(aboveG)-1
        y=array.get(aboveG,i)
        line.new(bar_index-2, y, bar_index+25, y, color=color.new(color.rgb(180,180,180),40), width=6)
        label.new(bar_index+28, y, "📈 Γ+"+str.tostring(i+1), style=label.style_label_left, color=color.new(color.rgb(220,220,220),0), textcolor=color.black, size=size.small)
    for i=0 to array.size(belowG)-1
        y=array.get(belowG,i)
        line.new(bar_index-2, y, bar_index+25, y, color=color.new(color.rgb(160,160,160),40), width=6)
        label.new(bar_index+28, y, "📉 Γ-"+str.tostring(i+1), style=label.style_label_left, color=color.new(color.rgb(220,220,220),0), textcolor=color.black, size=size.small)
"""
        out.append(block)
    pine=f"""//@version=5
indicator("Bassam GEX PRO v6.3", overlay=true, max_lines_count=500, max_labels_count=500)
{''.join(out)}"""
    return Response(pine, mimetype="text/plain")

# ------------------------ Run -------------------------------
if __name__=="__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",10000)))
