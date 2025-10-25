# server.py — Bassam OI[Pro] v4.2 – Multi-Symbol SmartMode + IV% + AskGroup(240m) + Hourly Cache
import os, json, time, datetime as dt, requests
from flask import Flask, jsonify, Response

app = Flask(__name__)
POLY_KEY  = (os.environ.get("POLYGON_API_KEY") or "").strip()
BASE_SNAP = "https://api.polygon.io/v3/snapshot/options"
TODAY     = dt.date.today

# قائمة الرموز
SYMBOLS = [
    "AAPL","META","MSFT","NVDA","TSLA","AMZN","GOOGL","AMD",
    "NFLX","SPY","QQQ","SPX","IWM","NVDS","SOXX","SMH"
]

# ========= Utils =========
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
        return r.status_code, {"error": "Invalid JSON", "raw": r.text[:500]}

def fetch_all_pages(symbol):
    """يجلب جميع صفحات snapshot (حد 50 في الصفحة)"""
    url = f"{BASE_SNAP}/{symbol.upper()}"
    cursor, all_rows = None, []
    for _ in range(10):
        params = {"limit": 50}
        if cursor: params["cursor"] = cursor
        status, j = _get(url, params)
        if status != 200 or j.get("status") != "OK":
            return None, {"status": status, "resp": j}
        rows = j.get("results") or []
        all_rows.extend(rows)
        cursor = j.get("next_url")
        if not cursor:
            break
        cursor = cursor.split("cursor=")[-1]
    return all_rows, None

def future_expiries(rows):
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
    """أقرب شهري منطقي: آخر جمعة من الشهر وإلا آخر تاريخ ضمن الشهر، وإلا آخر متاح."""
    if not expiries: return None
    first = expiries[0]
    y, m, _ = map(int, first.split("-"))
    month_list = [d for d in expiries if d.startswith(f"{y:04d}-{m:02d}-")]
    last_friday = None
    for d in month_list:
        Y, M, D = map(int, d.split("-"))
        if dt.date(Y, M, D).weekday() == 4:
            last_friday = d
    if last_friday: return last_friday
    return month_list[-1] if month_list else expiries[-1]

def analyze_oi_iv(rows, expiry, per_side_limit, split_by_price=True):
    """يرجع أعلى OI مع IV (Calls فوق السعر، Puts تحت السعر عند تفعيل split_by_price)."""
    rows_e = [r for r in rows if r.get("details", {}).get("expiration_date") == expiry]
    if not rows_e: return None, [], []
    price = None
    for r in rows_e:
        p = r.get("underlying_asset", {}).get("price")
        if isinstance(p, (int, float)) and p > 0:
            price = p
            break
    calls, puts = [], []
    for r in rows_e:
        det = r.get("details", {})
        strike = det.get("strike_price")
        ctype  = det.get("contract_type")
        oi     = r.get("open_interest")
        iv     = r.get("implied_volatility")
        if not (isinstance(strike, (int,float)) and isinstance(oi,(int,float))):
            continue
        iv = float(iv) if isinstance(iv,(int,float)) else 0.0
        if ctype == "call": calls.append((strike, oi, iv))
        elif ctype == "put": puts.append((strike, oi, iv))
    if split_by_price and isinstance(price,(int,float)):
        calls = [(s,oi,iv) for (s,oi,iv) in calls if s >= price]
        puts  = [(s,oi,iv) for (s,oi,iv) in puts  if s <= price]
    top_calls = sorted(calls, key=lambda x: x[1], reverse=True)[:per_side_limit]
    top_puts  = sorted(puts,  key=lambda x: x[1], reverse=True)[:per_side_limit]
    return price, top_calls, top_puts

def norm_lists(triples):
    """[(strike, oi, iv)] → strikes[], pct[], iv[]  (pct = oi / max)"""
    if not triples: return [], [], []
    base = max(oi for _,oi,_ in triples) or 1.0
    s = [round(x[0],2) for x in triples]
    p = [round(x[1]/base,4) for x in triples]
    v = [round(x[2],4) for x in triples]
    return s,p,v

# ========= Hourly Cache =========
CACHE = {
    "ts": 0.0,
    "per_symbol": {}  # sym -> {"weekly": {...}, "monthly": {...}}
}
CACHE_TTL = 3600.0  # seconds

def refresh_all(force=False):
    now = time.time()
    if (not force) and (now - CACHE["ts"] < CACHE_TTL) and CACHE["per_symbol"]:
        return
    if not POLY_KEY:
        return
    out = {}
    for sym in SYMBOLS:
        rows, err = fetch_all_pages(sym)
        if err or rows is None:
            out[sym] = {"weekly": {"expiry": None, "calls": [], "puts": []},
                        "monthly":{"expiry": None, "calls": [], "puts": []}}
            continue
        exps = future_expiries(rows)
        if not exps:
            out[sym] = {"weekly": {"expiry": None, "calls": [], "puts": []},
                        "monthly":{"expiry": None, "calls": [], "puts": []}}
            continue
        exp_w = nearest_weekly(exps)
        exp_m = nearest_monthly(exps)
        _, w_calls, w_puts = analyze_oi_iv(rows, exp_w, 3) if exp_w else (None,[],[])
        _, m_calls, m_puts = analyze_oi_iv(rows, exp_m, 6) if exp_m else (None,[],[])
        out[sym] = {
            "weekly":  {"expiry": exp_w, "calls": w_calls, "puts": w_puts},
            "monthly": {"expiry": exp_m, "calls": m_calls, "puts": m_puts}
        }
    CACHE["per_symbol"] = out
    CACHE["ts"] = now

# ========= JSON endpoints =========
@app.route("/<symbol>/json")
def json_one(symbol):
    if not POLY_KEY: return _err("Missing POLYGON_API_KEY", 401, sym=symbol)
    refresh_all()
    sym = symbol.upper()
    if sym not in SYMBOLS:
        return _err("Unsupported symbol", 404, {"supported": SYMBOLS}, sym)
    data = CACHE["per_symbol"].get(sym, {})
    wk = data.get("weekly", {})
    mo = data.get("monthly", {})
    def pack(side):
        return [{"strike": s, "oi": oi, "iv": iv} for (s,oi,iv) in side]
    return jsonify({
        "symbol": sym,
        "weekly":  {"expiry": wk.get("expiry"),
                    "call_walls": pack(wk.get("calls",[])),
                    "put_walls":  pack(wk.get("puts",[]))},
        "monthly": {"expiry": mo.get("expiry"),
                    "call_walls": pack(mo.get("calls",[])),
                    "put_walls":  pack(mo.get("puts",[]))}
    })

@app.route("/all/json")
def json_all():
    if not POLY_KEY: return _err("Missing POLYGON_API_KEY", 401)
    refresh_all()
    out = {}
    for sym, blob in CACHE["per_symbol"].items():
        def pack(side):
            return [{"strike": s, "oi": oi, "iv": iv} for (s,oi,iv) in side]
        wk = blob.get("weekly", {})
        mo = blob.get("monthly", {})
        out[sym] = {
            "weekly":  {"expiry": wk.get("expiry"),
                        "call_walls": pack(wk.get("calls",[])),
                        "put_walls":  pack(wk.get("puts",[]))},
            "monthly": {"expiry": mo.get("expiry"),
                        "call_walls": pack(mo.get("calls",[])),
                        "put_walls":  pack(mo.get("puts",[]))}
        }
    return jsonify({"updated_unix": int(CACHE["ts"]), "symbols": out})

# ========= Pine builders =========
def pine_for_symbol(sym, wk, mo):
    # weekly arrays
    wc_s, wc_p, wc_iv = norm_lists(wk.get("calls", []))
    wp_s, wp_p, wp_iv = norm_lists(wk.get("puts",  []))
    # monthly arrays
    mc_s, mc_p, mc_iv = norm_lists(mo.get("calls", []))
    mp_s, mp_p, mp_iv = norm_lists(mo.get("puts",  []))
    def arr(vals): return ", ".join(map(str, vals)) if vals else ""
    name = sym
    return f"""
// == {name} ==
var float[] {name}_w_cs = array.from({arr(wc_s)})
var float[] {name}_w_cp = array.from({arr(wc_p)})
var float[] {name}_w_ci = array.from({arr(wc_iv)})
var float[] {name}_w_ps = array.from({arr(wp_s)})
var float[] {name}_w_pp = array.from({arr(wp_p)})
var float[] {name}_w_pi = array.from({arr(wp_iv)})

var float[] {name}_m_cs = array.from({arr(mc_s)})
var float[] {name}_m_cp = array.from({arr(mc_p)})
var float[] {name}_m_ci = array.from({arr(mc_iv)})
var float[] {name}_m_ps = array.from({arr(mp_s)})
var float[] {name}_m_pp = array.from({arr(mp_p)})
var float[] {name}_m_pi = array.from({arr(mp_iv)})
"""

def build_all_pine():
    lines = []
    for sym in SYMBOLS:
        blob = CACHE["per_symbol"].get(sym, {"weekly":{},"monthly":{}})
        lines.append(pine_for_symbol(sym, blob.get("weekly",{}), blob.get("monthly",{})))
    arrays_block = "\n".join(lines)

    # نص Pine موحد
    pine = f"""//@version=5
indicator("Bassam GEX[Pro] • SmartMode (Multi) | IV% + AskGroup(240m)", overlay=true, max_lines_count=500, max_labels_count=500)

// ===== User Settings =====
mode = input.string("Weekly", "Expiry Mode", options=["Weekly","Monthly"], group="Settings")

// ===== helper: match current chart symbol to our list =====
symU = str.upper(syminfo.ticker)
rootU = str.upper(syminfo.root)
isSym(s) =>
    str.contains(symU, s) or (symU == s) or (rootU == s)

// ===== data arrays for each symbol (server-filled) =====
{arrays_block}

// ===== pick arrays for current symbol & mode =====
get_arrays(_s) =>
    // returns: c_strikes, c_pct, c_iv, p_strikes, p_pct, p_iv
    var float[] cs = array.new_float()
    var float[] cp = array.new_float()
    var float[] ci = array.new_float()
    var float[] ps = array.new_float()
    var float[] pp = array.new_float()
    var float[] pi = array.new_float()
    if mode == "Weekly"
        cs := array.copy(array.get(@_s+"_w_cs"))
        cp := array.copy(array.get(@_s+"_w_cp"))
        ci := array.copy(array.get(@_s+"_w_ci"))
        ps := array.copy(array.get(@_s+"_w_ps"))
        pp := array.copy(array.get(@_s+"_w_pp"))
        pi := array.copy(array.get(@_s+"_w_pi"))
    else
        cs := array.copy(array.get(@_s+"_m_cs"))
        cp := array.copy(array.get(@_s+"_m_cp"))
        ci := array.copy(array.get(@_s+"_m_ci"))
        ps := array.copy(array.get(@_s+"_m_ps"))
        pp := array.copy(array.get(@_s+"_m_pp"))
        pi := array.copy(array.get(@_s+"_m_pi"))
    [cs, cp, ci, ps, pp, pi]

// --- resolve symbol code (must match server list) ---
symCode = 
    isSym("AAPL")  ? "AAPL" :
    isSym("META")  ? "META" :
    isSym("MSFT")  ? "MSFT" :
    isSym("NVDA")  ? "NVDA" :
    isSym("TSLA")  ? "TSLA" :
    isSym("AMZN")  ? "AMZN" :
    isSym("GOOGL") ? "GOOGL":
    isSym("AMD")   ? "AMD"  :
    isSym("NFLX")  ? "NFLX" :
    isSym("SPY")   ? "SPY"  :
    isSym("QQQ")   ? "QQQ"  :
    isSym("SPX")   ? "SPX"  :
    isSym("IWM")   ? "IWM"  :
    isSym("NVDS")  ? "NVDS" :
    isSym("SOXX")  ? "SOXX" :
    isSym("SMH")   ? "SMH"  : "AAPL"  // fallback

// manual multiplexer because Pine can’t build names dynamically:
pick_arrays(_code) =>
    if _code == "AAPL"
        [{ 'AAPL_w_cs' }, { 'AAPL_w_cp' }, { 'AAPL_w_ci' }, { 'AAPL_w_ps' }, { 'AAPL_w_pp' }, { 'AAPL_w_pi' },
         { 'AAPL_m_cs' }, { 'AAPL_m_cp' }, { 'AAPL_m_ci' }, { 'AAPL_m_ps' }, { 'AAPL_m_pp' }, { 'AAPL_m_pi' }]
    if _code == "META"
        [{ 'META_w_cs' }, { 'META_w_cp' }, { 'META_w_ci' }, { 'META_w_ps' }, { 'META_w_pp' }, { 'META_w_pi' },
         { 'META_m_cs' }, { 'META_m_cp' }, { 'META_m_ci' }, { 'META_m_ps' }, { 'META_m_pp' }, { 'META_m_pi' }]
    if _code == "MSFT"
        [{ 'MSFT_w_cs' }, { 'MSFT_w_cp' }, { 'MSFT_w_ci' }, { 'MSFT_w_ps' }, { 'MSFT_w_pp' }, { 'MSFT_w_pi' },
         { 'MSFT_m_cs' }, { 'MSFT_m_cp' }, { 'MSFT_m_ci' }, { 'MSFT_m_ps' }, { 'MSFT_m_pp' }, { 'MSFT_m_pi' }]
    if _code == "NVDA"
        [{ 'NVDA_w_cs' }, { 'NVDA_w_cp' }, { 'NVDA_w_ci' }, { 'NVDA_w_ps' }, { 'NVDA_w_pp' }, { 'NVDA_w_pi' },
         { 'NVDA_m_cs' }, { 'NVDA_m_cp' }, { 'NVDA_m_ci' }, { 'NVDA_m_ps' }, { 'NVDA_m_pp' }, { 'NVDA_m_pi' }]
    if _code == "TSLA"
        [{ 'TSLA_w_cs' }, { 'TSLA_w_cp' }, { 'TSLA_w_ci' }, { 'TSLA_w_ps' }, { 'TSLA_w_pp' }, { 'TSLA_w_pi' },
         { 'TSLA_m_cs' }, { 'TSLA_m_cp' }, { 'TSLA_m_ci' }, { 'TSLA_m_ps' }, { 'TSLA_m_pp' }, { 'TSLA_m_pi' }]
    if _code == "AMZN"
        [{ 'AMZN_w_cs' }, { 'AMZN_w_cp' }, { 'AMZN_w_ci' }, { 'AMZN_w_ps' }, { 'AMZN_w_pp' }, { 'AMZN_w_pi' },
         { 'AMZN_m_cs' }, { 'AMZN_m_cp' }, { 'AMZN_m_ci' }, { 'AMZN_m_ps' }, { 'AMZN_m_pp' }, { 'AMZN_m_pi' }]
    if _code == "GOOGL"
        [{ 'GOOGL_w_cs' }, { 'GOOGL_w_cp' }, { 'GOOGL_w_ci' }, { 'GOOGL_w_ps' }, { 'GOOGL_w_pp' }, { 'GOOGL_w_pi' },
         { 'GOOGL_m_cs' }, { 'GOOGL_m_cp' }, { 'GOOGL_m_ci' }, { 'GOOGL_m_ps' }, { 'GOOGL_m_pp' }, { 'GOOGL_m_pi' }]
    if _code == "AMD"
        [{ 'AMD_w_cs' }, { 'AMD_w_cp' }, { 'AMD_w_ci' }, { 'AMD_w_ps' }, { 'AMD_w_pp' }, { 'AMD_w_pi' },
         { 'AMD_m_cs' }, { 'AMD_m_cp' }, { 'AMD_m_ci' }, { 'AMD_m_ps' }, { 'AMD_m_pp' }, { 'AMD_m_pi' }]
    if _code == "NFLX"
        [{ 'NFLX_w_cs' }, { 'NFLX_w_cp' }, { 'NFLX_w_ci' }, { 'NFLX_w_ps' }, { 'NFLX_w_pp' }, { 'NFLX_w_pi' },
         { 'NFLX_m_cs' }, { 'NFLX_m_cp' }, { 'NFLX_m_ci' }, { 'NFLX_m_ps' }, { 'NFLX_m_pp' }, { 'NFLX_m_pi' }]
    if _code == "SPY"
        [{ 'SPY_w_cs' }, { 'SPY_w_cp' }, { 'SPY_w_ci' }, { 'SPY_w_ps' }, { 'SPY_w_pp' }, { 'SPY_w_pi' },
         { 'SPY_m_cs' }, { 'SPY_m_cp' }, { 'SPY_m_ci' }, { 'SPY_m_ps' }, { 'SPY_m_pp' }, { 'SPY_m_pi' }]
    if _code == "QQQ"
        [{ 'QQQ_w_cs' }, { 'QQQ_w_cp' }, { 'QQQ_w_ci' }, { 'QQQ_w_ps' }, { 'QQQ_w_pp' }, { 'QQQ_w_pi' },
         { 'QQQ_m_cs' }, { 'QQQ_m_cp' }, { 'QQQ_m_ci' }, { 'QQQ_m_ps' }, { 'QQQ_m_pp' }, { 'QQQ_m_pi' }]
    if _code == "SPX"
        [{ 'SPX_w_cs' }, { 'SPX_w_cp' }, { 'SPX_w_ci' }, { 'SPX_w_ps' }, { 'SPX_w_pp' }, { 'SPX_w_pi' },
         { 'SPX_m_cs' }, { 'SPX_m_cp' }, { 'SPX_m_ci' }, { 'SPX_m_ps' }, { 'SPX_m_pp' }, { 'SPX_m_pi' }]
    if _code == "IWM"
        [{ 'IWM_w_cs' }, { 'IWM_w_cp' }, { 'IWM_w_ci' }, { 'IWM_w_ps' }, { 'IWM_w_pp' }, { 'IWM_w_pi' },
         { 'IWM_m_cs' }, { 'IWM_m_cp' }, { 'IWM_m_ci' }, { 'IWM_m_ps' }, { 'IWM_m_pp' }, { 'IWM_m_pi' }]
    if _code == "NVDS"
        [{ 'NVDS_w_cs' }, { 'NVDS_w_cp' }, { 'NVDS_w_ci' }, { 'NVDS_w_ps' }, { 'NVDS_w_pp' }, { 'NVDS_w_pi' },
         { 'NVDS_m_cs' }, { 'NVDS_m_cp' }, { 'NVDS_m_ci' }, { 'NVDS_m_ps' }, { 'NVDS_m_pp' }, { 'NVDS_m_pi' }]
    if _code == "SOXX"
        [{ 'SOXX_w_cs' }, { 'SOXX_w_cp' }, { 'SOXX_w_ci' }, { 'SOXX_w_ps' }, { 'SOXX_w_pp' }, { 'SOXX_w_pi' },
         { 'SOXX_m_cs' }, { 'SOXX_m_cp' }, { 'SOXX_m_ci' }, { 'SOXX_m_ps' }, { 'SOXX_m_pp' }, { 'SOXX_m_pi' }]
    // SMH
    [{ 'SMH_w_cs' }, { 'SMH_w_cp' }, { 'SMH_w_ci' }, { 'SMH_w_ps' }, { 'SMH_w_pp' }, { 'SMH_w_pi' },
     { 'SMH_m_cs' }, { 'SMH_m_cp' }, { 'SMH_m_ci' }, { 'SMH_m_ps' }, { 'SMH_m_pp' }, { 'SMH_m_pi' }]

// ===== draw side (bars + % + IV) =====
draw_side(_strikes, _pcts, _ivs, _base_col) =>
    for i = 0 to array.size(_strikes) - 1
        y  = array.get(_strikes, i)
        p  = array.get(_pcts, i)
        iv = array.get(_ivs, i)
        alpha   = 90 - int(p * 70)
        bar_col = color.new(_base_col, alpha)
        bar_len = int(math.max(10, p * 50))
        line.new(bar_index + 3, y, bar_index + bar_len - 12, y, color=bar_col, width=6)
        label.new(bar_index + bar_len + 1, y,
                  str.tostring(p*100, "#.##") + "%  |  IV " + str.tostring(iv*100, "#.##") + "%",
                  style=label.style_none, textcolor=color.white, size=size.small)

// ===== 240m Ask Group (fixed timeframe) =====
h240 = request.security(syminfo.tickerid, "240", high)
l240 = request.security(syminfo.tickerid, "240", low)
c240 = request.security(syminfo.tickerid, "240", close)

rb             = input.int(10,  "Ask Pivot Period (240m)", minval=10, group="Ask Group (240m)")
prd            = input.int(284, "Loopback Period", minval=100, maxval=500, group="Ask Group (240m)")
nump           = input.int(2,   "S/R strength", minval=1, group="Ask Group (240m)")
ChannelW       = input.int(10,  "Channel Width %", minval=5, group="Ask Group (240m)")
label_location = input.int(10,  "Label Location +-", group="Ask Group (240m)")
linestyle      = input.string("Dashed","Line Style", options=["Solid","Dotted","Dashed"], group="Ask Group (240m)")
LineColor      = input.color(color.new(color.blue,20), "Line Color", group="Ask Group (240m)")
drawhl         = input.bool(true, "Draw Highest/Lowest Pivots", group="Ask Group (240m)")
showpp         = input.bool(true,"Show Pivot Points", group="Ask Group (240m)")

ph = ta.pivothigh(h240, rb, rb)
pl = ta.pivotlow(l240,  rb, rb)

plotshape(showpp and not na(ph), title="PH", text="انعكاس", style=shape.labeldown, color=color.new(color.white,100), textcolor=color.red,  location=location.abovebar, offset=-rb)
plotshape(showpp and not na(pl), title="PL", text="انعكاس", style=shape.labelup,   color=color.new(color.white,100), textcolor=color.lime, location=location.belowbar, offset=-rb)

sr_levels  = array.new_float(21, na)
prdhighest = ta.highest(h240, prd)
prdlowest  = ta.lowest(l240,  prd)
cwidth     = (prdhighest - prdlowest) * ChannelW / 100
aas        = array.new_bool(41, true)
var sr_lines = array.new_line(21, na)

if not na(ph) or not na(pl)
    for x = 0 to array.size(sr_lines) - 1
        if not na(array.get(sr_lines, x))
            line.delete(array.get(sr_lines, x))
        array.set(sr_lines, x, na)

    highestph = prdlowest
    lowestpl  = prdhighest
    countpp = 0

    for x = 0 to prd
        if na(c240[x]) or countpp > 40
            break
        if not na(ph[x]) or not na(pl[x])
            highestph := math.max(highestph, nz(ph[x], prdlowest), nz(pl[x], prdlowest))
            lowestpl  := math.min(lowestpl,  nz(ph[x], prdhighest), nz(pl[x], prdhighest))
            countpp += 1
            if array.get(aas, countpp)
                upl = (not na(ph[x]) ? h240[x + rb] : l240[x + rb]) + cwidth
                dnl = (not na(ph[x]) ? h240[x + rb] : l240[x + rb]) - cwidth
                tmp = array.new_bool(41, true)
                cnt = 0
                tpoint = 0
                for xx = 0 to prd
                    if na(c240[xx]) or cnt > 40
                        break
                    if not na(ph[xx]) or not na(pl[xx])
                        chg = false
                        cnt += 1
                        if array.get(aas, cnt)
                            if not na(ph[xx]) and h240[xx + rb] <= upl and h240[xx + rb] >= dnl
                                tpoint += 1
                                chg := true
                            if not na(pl[xx]) and l240[xx + rb] <= upl and l240[xx + rb] >= dnl
                                tpoint += 1
                                chg := true
                        if chg and cnt < 41
                            array.set(tmp, cnt, false)
                if tpoint >= nump
                    for g = 0 to 40
                        if not array.get(tmp, g)
                            array.set(aas, g, false)
                    if not na(ph[x]) and countpp < 21
                        array.set(sr_levels, countpp, h240[x + rb])
                    if not na(pl[x]) and countpp < 21
                        array.set(sr_levels, countpp, l240[x + rb])

style = linestyle == "Solid" ? line.style_solid : linestyle == "Dotted" ? line.style_dotted : line.style_dashed
for x = 0 to array.size(sr_levels) - 1
    lvl = array.get(sr_levels, x)
    if not na(lvl)
        col = lvl < c240 ? color.new(color.lime, 0) : color.new(color.red, 0)
        array.set(sr_lines, x, line.new(bar_index - 1, lvl, bar_index, lvl, color=col, width=1, style=style, extend=extend.both))

// labels for highest/lowest (from 240m series)
var label highestLabel = na
var label lowestLabel  = na
if drawhl
    newHigh = ta.highest(h240, prd)
    newLow  = ta.lowest(l240,  prd)
    if na(highestLabel) or label.get_y(highestLabel) != newHigh
        if not na(highestLabel)
            label.delete(highestLabel)
        highestLabel := label.new(bar_index + label_location, newHigh, "Highest PH " + str.tostring(newHigh),
                                  color=color.new(color.silver, 0), textcolor=color.black, style=label.style_label_down)
    if na(lowestLabel) or label.get_y(lowestLabel) != newLow
        if not na(lowestLabel)
            label.delete(lowestLabel)
        lowestLabel := label.new(bar_index + label_location, newLow, "Lowest PL " + str.tostring(newLow),
                                 color=color.new(color.silver, 0), textcolor=color.black, style=label.style_label_up)

// ===== header label (mode + symbol) =====
var label hdr = na
if barstate.islast
    if not na(hdr)
        label.delete(hdr)
    hdr := label.new(bar_index, high, "GEX PRO • " + mode + " | " + syminfo.ticker,
                     style=label.style_label_left, textcolor=color.white, color=color.new(color.black, 100))

// ===== render the OI walls =====
var float[] C_S = array.new_float(), C_P = array.new_float(), C_I = array.new_float()
var float[] P_S = array.new_float(), P_P = array.new_float(), P_I = array.new_float()

// load arrays for selected symbol + mode
if barstate.islast
    // manual dispatch: pull arrays for the resolved symCode
    // (Pine can't concat names dynamically in a safe way, so we replicate by ifs)
    if symCode == "AAPL"
        C_S := mode == "Weekly" ? AAPL_w_cs : AAPL_m_cs
        C_P := mode == "Weekly" ? AAPL_w_cp : AAPL_m_cp
        C_I := mode == "Weekly" ? AAPL_w_ci : AAPL_m_ci
        P_S := mode == "Weekly" ? AAPL_w_ps : AAPL_m_ps
        P_P := mode == "Weekly" ? AAPL_w_pp : AAPL_m_pp
        P_I := mode == "Weekly" ? AAPL_w_pi : AAPL_m_pi
    if symCode == "META"
        C_S := mode == "Weekly" ? META_w_cs : META_m_cs
        C_P := mode == "Weekly" ? META_w_cp : META_m_cp
        C_I := mode == "Weekly" ? META_w_ci : META_m_ci
        P_S := mode == "Weekly" ? META_w_ps : META_m_ps
        P_P := mode == "Weekly" ? META_w_pp : META_m_pp
        P_I := mode == "Weekly" ? META_w_pi : META_m_pi
    // ... (نفس البلوك لباقي الرموز)
    if symCode == "MSFT"
        C_S := mode == "Weekly" ? MSFT_w_cs : MSFT_m_cs
        C_P := mode == "Weekly" ? MSFT_w_cp : MSFT_m_cp
        C_I := mode == "Weekly" ? MSFT_w_ci : MSFT_m_ci
        P_S := mode == "Weekly" ? MSFT_w_ps : MSFT_m_ps
        P_P := mode == "Weekly" ? MSFT_w_pp : MSFT_m_pp
        P_I := mode == "Weekly" ? MSFT_w_pi : MSFT_m_pi
    if symCode == "NVDA"
        C_S := mode == "Weekly" ? NVDA_w_cs : NVDA_m_cs
        C_P := mode == "Weekly" ? NVDA_w_cp : NVDA_m_cp
        C_I := mode == "Weekly" ? NVDA_w_ci : NVDA_m_ci
        P_S := mode == "Weekly" ? NVDA_w_ps : NVDA_m_ps
        P_P := mode == "Weekly" ? NVDA_w_pp : NVDA_m_pp
        P_I := mode == "Weekly" ? NVDA_w_pi : NVDA_m_pi
    if symCode == "TSLA"
        C_S := mode == "Weekly" ? TSLA_w_cs : TSLA_m_cs
        C_P := mode == "Weekly" ? TSLA_w_cp : TSLA_m_cp
        C_I := mode == "Weekly" ? TSLA_w_ci : TSLA_m_ci
        P_S := mode == "Weekly" ? TSLA_w_ps : TSLA_m_ps
        P_P := mode == "Weekly" ? TSLA_w_pp : TSLA_m_pp
        P_I := mode == "Weekly" ? TSLA_w_pi : TSLA_m_pi
    if symCode == "AMZN"
        C_S := mode == "Weekly" ? AMZN_w_cs : AMZN_m_cs
        C_P := mode == "Weekly" ? AMZN_w_cp : AMZN_m_cp
        C_I := mode == "Weekly" ? AMZN_w_ci : AMZN_m_ci
        P_S := mode == "Weekly" ? AMZN_w_ps : AMZN_m_ps
        P_P := mode == "Weekly" ? AMZN_w_pp : AMZN_m_pp
        P_I := mode == "Weekly" ? AMZN_w_pi : AMZN_m_pi
    if symCode == "GOOGL"
        C_S := mode == "Weekly" ? GOOGL_w_cs : GOOGL_m_cs
        C_P := mode == "Weekly" ? GOOGL_w_cp : GOOGL_m_cp
        C_I := mode == "Weekly" ? GOOGL_w_ci : GOOGL_m_ci
        P_S := mode == "Weekly" ? GOOGL_w_ps : GOOGL_m_ps
        P_P := mode == "Weekly" ? GOOGL_w_pp : GOOGL_m_pp
        P_I := mode == "Weekly" ? GOOGL_w_pi : GOOGL_m_pi
    if symCode == "AMD"
        C_S := mode == "Weekly" ? AMD_w_cs : AMD_m_cs
        C_P := mode == "Weekly" ? AMD_w_cp : AMD_m_cp
        C_I := mode == "Weekly" ? AMD_w_ci : AMD_m_ci
        P_S := mode == "Weekly" ? AMD_w_ps : AMD_m_ps
        P_P := mode == "Weekly" ? AMD_w_pp : AMD_m_pp
        P_I := mode == "Weekly" ? AMD_w_pi : AMD_m_pi
    if symCode == "NFLX"
        C_S := mode == "Weekly" ? NFLX_w_cs : NFLX_m_cs
        C_P := mode == "Weekly" ? NFLX_w_cp : NFLX_m_cp
        C_I := mode == "Weekly" ? NFLX_w_ci : NFLX_m_ci
        P_S := mode == "Weekly" ? NFLX_w_ps : NFLX_m_ps
        P_P := mode == "Weekly" ? NFLX_w_pp : NFLX_m_pp
        P_I := mode == "Weekly" ? NFLX_w_pi : NFLX_m_pi
    if symCode == "SPY"
        C_S := mode == "Weekly" ? SPY_w_cs : SPY_m_cs
        C_P := mode == "Weekly" ? SPY_w_cp : SPY_m_cp
        C_I := mode == "Weekly" ? SPY_w_ci : SPY_m_ci
        P_S := mode == "Weekly" ? SPY_w_ps : SPY_m_ps
        P_P := mode == "Weekly" ? SPY_w_pp : SPY_m_pp
        P_I := mode == "Weekly" ? SPY_w_pi : SPY_m_pi
    if symCode == "QQQ"
        C_S := mode == "Weekly" ? QQQ_w_cs : QQQ_m_cs
        C_P := mode == "Weekly" ? QQQ_w_cp : QQQ_m_cp
        C_I := mode == "Weekly" ? QQQ_w_ci : QQQ_m_ci
        P_S := mode == "Weekly" ? QQQ_w_ps : QQQ_m_ps
        P_P := mode == "Weekly" ? QQQ_w_pp : QQQ_m_pp
        P_I := mode == "Weekly" ? QQQ_w_pi : QQQ_m_pi
    if symCode == "SPX"
        C_S := mode == "Weekly" ? SPX_w_cs : SPX_m_cs
        C_P := mode == "Weekly" ? SPX_w_cp : SPX_m_cp
        C_I := mode == "Weekly" ? SPX_w_ci : SPX_m_ci
        P_S := mode == "Weekly" ? SPX_w_ps : SPX_m_ps
        P_P := mode == "Weekly" ? SPX_w_pp : SPX_m_pp
        P_I := mode == "Weekly" ? SPX_w_pi : SPX_m_pi
    if symCode == "IWM"
        C_S := mode == "Weekly" ? IWM_w_cs : IWM_m_cs
        C_P := mode == "Weekly" ? IWM_w_cp : IWM_m_cp
        C_I := mode == "Weekly" ? IWM_w_ci : IWM_m_ci
        P_S := mode == "Weekly" ? IWM_w_ps : IWM_m_ps
        P_P := mode == "Weekly" ? IWM_w_pp : IWM_m_pp
        P_I := mode == "Weekly" ? IWM_w_pi : IWM_m_pi
    if symCode == "NVDS"
        C_S := mode == "Weekly" ? NVDS_w_cs : NVDS_m_cs
        C_P := mode == "Weekly" ? NVDS_w_cp : NVDS_m_cp
        C_I := mode == "Weekly" ? NVDS_w_ci : NVDS_m_ci
        P_S := mode == "Weekly" ? NVDS_w_ps : NVDS_m_ps
        P_P := mode == "Weekly" ? NVDS_w_pp : NVDS_m_pp
        P_I := mode == "Weekly" ? NVDS_w_pi : NVDS_m_pi
    if symCode == "SOXX"
        C_S := mode == "Weekly" ? SOXX_w_cs : SOXX_m_cs
        C_P := mode == "Weekly" ? SOXX_w_cp : SOXX_m_cp
        C_I := mode == "Weekly" ? SOXX_w_ci : SOXX_m_ci
        P_S := mode == "Weekly" ? SOXX_w_ps : SOXX_m_ps
        P_P := mode == "Weekly" ? SOXX_w_pp : SOXX_m_pp
        P_I := mode == "Weekly" ? SOXX_w_pi : SOXX_m_pi
    if symCode == "SMH"
        C_S := mode == "Weekly" ? SMH_w_cs : SMH_m_cs
        C_P := mode == "Weekly" ? SMH_w_cp : SMH_m_cp
        C_I := mode == "Weekly" ? SMH_w_ci : SMH_m_ci
        P_S := mode == "Weekly" ? SMH_w_ps : SMH_m_ps
        P_P := mode == "Weekly" ? SMH_w_pp : SMH_m_pp
        P_I := mode == "Weekly" ? SMH_w_pi : SMH_m_pi

// draw
if barstate.islast
    draw_side(C_S, C_P, C_I, color.lime)
    draw_side(P_S, P_P, P_I, color.red)
"""
    return pine

# ========= Pine endpoints =========
@app.route("/<symbol>/pine")
def pine_one(symbol):
    if not POLY_KEY: return _err("Missing POLYGON_API_KEY", 401, sym=symbol)
    refresh_all()
    sym = symbol.upper()
    if sym not in SYMBOLS:
        return _err("Unsupported symbol", 404, {"supported": SYMBOLS}, sym)
    blob = CACHE["per_symbol"].get(sym, {"weekly":{},"monthly":{}})
    # نبني Pine مصغّر لرمز واحد باستخدام البناء العام ثم نترك الملتبلكسر يختار الرمز
    pine = build_all_pine()
    return Response(pine, mimetype="text/plain")

@app.route("/all/pine")
def pine_all():
    if not POLY_KEY: return _err("Missing POLYGON_API_KEY", 401)
    refresh_all()
    pine = build_all_pine()
    return Response(pine, mimetype="text/plain")

# ========= Home =========
@app.route("/")
def home():
    return jsonify({
        "status": "OK ✅",
        "cache_last_updated_unix": int(CACHE["ts"]),
        "usage": {
            "all_json": "/all/json",
            "all_pine": "/all/pine",
            "one_json": "/AAPL/json",
            "one_pine": "/AAPL/pine"
        },
        "symbols": SYMBOLS,
        "notes": [
            "يتم تحديث البيانات تلقائيًا كل ساعة (In-memory cache).",
            "داخل TradingView اختر Weekly أو Monthly (عرض حصري).",
            "Ask Group pivots/lines محسوبة على 240m دايمًا."
        ]
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
