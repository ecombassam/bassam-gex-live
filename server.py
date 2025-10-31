# ============================================================
# Bassam GEX PRO v5.4 – Dual Week Selector (Current / Next)
# - Weekly & Monthly Net Gamma Exposure (Directional Colors)
# - User can switch between Current / Next week inside TradingView
# - 7 bars max per expiry: Top3 + Strongest + Top3
# - ±25% range around spot, ignoring <20% of max
# - Includes /all/pine, /all/json, /em/json endpoints
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
CACHE_EXPIRY = 3600  # 1h cache

# ---------------------- Helpers ----------------------
def _err(msg, http=502):
    return Response(json.dumps({"error": msg}, ensure_ascii=False),
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

# ---------------------- Polygon fetch ----------------------
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

# ---------------------- Expiries ----------------------
def list_future_expiries(rows):
    expiries = sorted({
        r.get("details", {}).get("expiration_date")
        for r in rows if r.get("details", {}).get("expiration_date")
    })
    today = TODAY().isoformat()
    return [d for d in expiries if d >= today]

def nearest_weekly(expiries, next_week=False):
    fridays = []
    for d in expiries:
        try:
            y, m, dd = map(int, d.split("-"))
            if dt.date(y, m, dd).weekday() == 4:
                fridays.append(d)
        except Exception:
            continue
    fridays = sorted(fridays)
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

# ---------------------- Net Gamma Aggregation ----------------------
def _aggregate_gamma_by_strike(rows, price, split_by_price=True):
    calls_map, puts_map = {}, {}
    if price is None: return calls_map, puts_map

    low_bound  = price * 0.75
    high_bound = price * 1.25

    for r in rows:
        det = r.get("details", {}) or {}
        strike = det.get("strike_price")
        ctype = det.get("contract_type")
        oi = r.get("open_interest")
        iv = r.get("implied_volatility")
        greeks = r.get("greeks") or {}
        und = r.get("underlying_asset") or {}
        uprice = und.get("price", price)
        if not (isinstance(strike, (int,float)) and isinstance(oi,(int,float)) and isinstance(uprice,(int,float))):
            continue
        if split_by_price and not (low_bound <= strike <= high_bound): continue
        gamma = float(greeks.get("gamma",0) or 0)
        iv_val = float(iv) if isinstance(iv,(int,float)) else 0.0
        net_gamma = gamma * oi * 100.0 * uprice
        target = calls_map if ctype=="call" else puts_map if ctype=="put" else None
        if target is None: continue
        if strike not in target:
            target[strike] = {"net_gamma":0.0,"iv":iv_val,"count":0}
        target[strike]["net_gamma"] += net_gamma
        target[strike]["iv"] = (target[strike]["iv"]*target[strike]["count"] + iv_val)/(target[strike]["count"]+1)
        target[strike]["count"] += 1
    for d in (calls_map, puts_map):
        for k,v in d.items():
            d[k] = {"net_gamma":float(v["net_gamma"]), "iv":float(v["iv"])}
    return calls_map, puts_map

def _pick_top7_directional(calls_map, puts_map):
    all_items = [(float(s), float(v["net_gamma"]), float(v["iv"])) for s,v in {**calls_map,**puts_map}.items()]
    if not all_items: return []
    max_abs = max(abs(x[1]) for x in all_items) or 1.0
    all_items = [x for x in all_items if abs(x[1]) >= 0.2*max_abs]
    pos = sorted([t for t in all_items if t[1]>0], key=lambda x:x[1], reverse=True)
    neg = sorted([t for t in all_items if t[1]<0], key=lambda x:x[1])
    top_pos, top_neg = pos[:3], neg[:3]
    strongest = max(all_items, key=lambda x:abs(x[1]))
    sel, seen = [], set()
    def add_unique(items):
        for (s,g,iv) in items:
            key = (round(s,6), round(g,6))
            if key not in seen:
                seen.add(key); sel.append((s,g,iv))
    add_unique(top_pos); add_unique([strongest]); add_unique(top_neg)
    if len(sel)<7:
        rem = [x for x in all_items if (round(x[0],6),round(x[1],6)) not in seen]
        for x in sorted(rem,key=lambda x:abs(x[1]),reverse=True):
            if len(sel)>=7: break
            add_unique([x])
    return sorted(sel,key=lambda x:x[0])[:7]

# ---------------------- Analysis ----------------------
def analyze_gamma_iv(rows, expiry):
    rows = [r for r in rows if r.get("details", {}).get("expiration_date")==expiry]
    if not rows: return None,[]
    price = next((float(r.get("underlying_asset",{}).get("price")) for r in rows if isinstance(r.get("underlying_asset",{}).get("price"),(int,float))), None)
    if not price: return None,[]
    calls_map, puts_map = _aggregate_gamma_by_strike(rows,price)
    return price,_pick_top7_directional(calls_map,puts_map)

# ---------------------- Normalize ----------------------
def normalize_for_pine(picks):
    if not picks: return [],[],[],[]
    max_abs = max(abs(v) for (_,v,__) in picks) or 1.0
    strikes=[round(s,2) for (s,_,__) in picks]
    pcts=[round(abs(v)/max_abs,4) for (_,v,__) in picks]
    ivs=[round(iv,4) for (_,__,iv) in picks]
    signs=[1 if v>0 else -1 for (_,v,__) in picks]
    return strikes,pcts,ivs,signs

def arr_or_empty(arr): return f"array.from({','.join(f'{float(x):.6f}' for x in arr)})" if arr else "array.new_float()"
def arr_or_empty_int(arr): return f"array.from({','.join(str(int(x)) for x in arr)})" if arr else "array.new_int()"

# ---------------------- Expected Move ----------------------
def compute_weekly_em(rows,expiry):
    if not expiry: return None,None,None
    price = next((float(r.get("underlying_asset",{}).get("price")) for r in rows if isinstance(r.get("underlying_asset",{}).get("price"),(int,float))), None)
    if not price: return None,None,None
    wk=[r for r in rows if r.get("details",{}).get("expiration_date")==expiry]
    if not wk: return price,None,None
    ivs=[float(r.get("implied_volatility",0)) for r in wk if isinstance(r.get("implied_volatility"),(int,float))]
    if not ivs: return price,None,None
    iv_annual=sum(ivs)/len(ivs)
    y,m,d=map(int,expiry.split("-"));days=max((dt.date(y,m,d)-TODAY()).days,1)
    em=price*iv_annual*math.sqrt(days/365.0)
    return price,iv_annual,em

# ---------------------- Update + Cache ----------------------
def update_symbol_data(symbol):
    rows=fetch_all(symbol);expiries=list_future_expiries(rows)
    if not expiries: return None
    exp_cur=nearest_weekly(expiries,False)
    exp_nxt=nearest_weekly(expiries,True)
    exp_m=nearest_monthly(expiries)
    cur_p,cur_x=analyze_gamma_iv(rows,exp_cur)
    nxt_p,nxt_x=analyze_gamma_iv(rows,exp_nxt)
    mon_p,mon_x=analyze_gamma_iv(rows,exp_m)
    em_p,em_iv,em_val=compute_weekly_em(rows,exp_cur)
    return {"symbol":symbol,
        "weekly_current":{"expiry":exp_cur,"price":cur_p,"picks":cur_x},"weekly_next":{"expiry":exp_nxt,"price":nxt_p,"picks":nxt_x},"monthly":{"expiry":exp_m,"price":mon_p,"picks":mon_x},"em":{"price":em_p,"iv_annual":em_iv,"weekly_em":em_val},"timestamp":time.time()}

def get_symbol_data(symbol):
    now=time.time()
    if symbol in CACHE and now-CACHE[symbol]["timestamp"]<CACHE_EXPIRY:
        return CACHE[symbol]
    data=update_symbol_data(symbol)
    if data: CACHE[symbol]=data
    return data

# ---------------------- /all/pine ----------------------
@app.route("/all/pine")
def all_pine():
    if not POLY_KEY: return _err("Missing POLYGON_API_KEY",401)
    blocks=[]
    for sym in SYMBOLS:
        d=get_symbol_data(sym)
        if not d: continue
        wc_s,wc_p,wc_iv,wc_sgn=normalize_for_pine(d["weekly_current"]["picks"])
        wn_s,wn_p,wn_iv,wn_sgn=normalize_for_pine(d["weekly_next"]["picks"])
        m_s,m_p,m_iv,m_sgn=normalize_for_pine(d["monthly"]["picks"])
        block=f"""
//========= {sym} =========
if syminfo.ticker == "{sym}"
    clear_visuals(optLines,optLabels)
    if mode=="Weekly"
        if weekMode=="Current"
            draw_bars({arr_or_empty(wc_s)},{arr_or_empty(wc_p)},{arr_or_empty(wc_iv)},{arr_or_empty_int(wc_sgn)})
        if weekMode=="Next"
            draw_bars({arr_or_empty(wn_s)},{arr_or_empty(wn_p)},{arr_or_empty(wn_iv)},{arr_or_empty_int(wn_sgn)})
    if mode=="Monthly"
        draw_bars({arr_or_empty(m_s)},{arr_or_empty(m_p)},{arr_or_empty(m_iv)},{arr_or_empty_int(m_sgn)})
"""
        blocks.append(block)

    now=dt.datetime.now(dt.timezone(dt.timedelta(hours=3)))
    pine=f"""//@version=5
indicator("GEX PRO (v5.4 – Dual Week Selector)",overlay=true,max_lines_count=500,max_labels_count=500,dynamic_requests=true)
// Last Update (Riyadh): {now:%Y-%m-%d %H:%M:%S}

mode=input.string("Weekly","Expiry Mode",options=["Weekly","Monthly"])
weekMode=input.string("Current","Expiry Week",options=["Current","Next"])

var line[] optLines=array.new_line()
var label[] optLabels=array.new_label()

clear_visuals(_L,_Lb)=>
    for l in _L
        line.delete(l)
    array.clear(_L)
    for lb in _Lb
        label.delete(lb)
    array.clear(_Lb)

draw_bars(_s,_p,_iv,_sgn)=>
    if barstate.islast and array.size(_s)>0
        for i=0 to array.size(_s)-1
            y=array.get(_s,i)
            pct=array.get(_p,i)
            iv=array.get(_iv,i)
            sgn=array.get(_sgn,i)
            col=sgn>0?color.new(color.lime,20):sgn<0?color.new(color.rgb(220,50,50),20):color.new(color.gray,40)
            alpha=90-int(pct*70)
            col:=color.new(col,alpha)
            bar_len=int(math.max(10,pct*50))
            line.new(bar_index+3,y,bar_index+bar_len+12,y,color=col,width=6)
            label.new(bar_index+bar_len+2,y,str.tostring(pct*100,"#.##")+"% | IV "+str.tostring(iv*100,"#.##")+"%",style=label.style_label_left,color=color.rgb(95,93,93),textcolor=color.white,size=size.small)

{''.join(blocks)}
"""
    return Response(pine,mimetype="text/plain")

# ---------------------- /all/json ----------------------
@app.route("/all/json")
def all_json():
    if not POLY_KEY: return _err("Missing POLYGON_API_KEY",401)
    all_data={}
    for sym in SYMBOLS:
        d=get_symbol_data(sym)
        if not d: continue
        def _to(picks): return [{"strike":s,"net_gamma":ng,"iv":iv} for (s,ng,iv) in picks[:7]]
        all_data[sym]={
            "weekly_current":{"expiry":d["weekly_current"]["expiry"],"price":d["weekly_current"]["price"],"top7":_to(d["weekly_current"]["picks"])},
            "weekly_next":{"expiry":d["weekly_next"]["expiry"],"price":d["weekly_next"]["price"],"top7":_to(d["weekly_next"]["picks"])},
            "monthly":{"expiry":d["monthly"]["expiry"],"price":d["monthly"]["price"],"top7":_to(d["monthly"]["picks"])},
            "em":d.get("em"),"timestamp":d["timestamp"]}
    return jsonify({"status":"OK","symbols":SYMBOLS,"updated":dt.datetime.utcnow().isoformat()+"Z","data":all_data})

# ---------------------- /em/json ----------------------
@app.route("/em/json")
def em_json():
    if not POLY_KEY: return _err("Missing POLYGON_API_KEY",401)
    out={sym:get_symbol_data(sym)["em"] for sym in SYMBOLS if get_symbol_data(sym) and get_symbol_data(sym).get("em",{}).get("weekly_em") is not None}
    return jsonify({"status":"OK","updated":dt.datetime.utcnow().isoformat()+"Z","data":out})

# ---------------------- Root ----------------------
@app.route("/")
def home():
    return jsonify({"status":"OK ✅","message":"Bassam GEX PRO v5.4 – Dual Week Selector running successfully","note":"User can switch between Current / Next week inside TradingView"})

# ---------------------- Background Loader ----------------------
def warmup_cache():
    print("🔄 Warming cache...")
    for sym in SYMBOLS:
        try:
            get_symbol_data(sym);print(f"✅ Cached {sym}")
        except Exception as e: print(f"⚠️ {sym}: {e}")
    print("✅ Warmup complete.")

if __name__=="__main__":
    import threading
    threading.Thread(target=warmup_cache,daemon=True).start()
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",10000)))
