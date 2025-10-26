# ============================================================
# Bassam GEX PRO v4.6 – SmartCache Edition
# Multi-Symbol SmartMode + IV% + AskGroup (240m) + Hourly Cache
# AutoMonthFix (show Monthly when Weekly coincides with month-end Friday)
# HVL arrays & drawing scoped per-symbol to avoid collisions
# ============================================================

import os, json, datetime as dt, requests, time
from flask import Flask, jsonify, Response

app = Flask(__name__)
POLY_KEY  = (os.environ.get("POLYGON_API_KEY") or "").strip()
BASE_SNAP = "https://api.polygon.io/v3/snapshot/options"
TODAY     = dt.date.today

# ============================================================
# Official symbols
# ============================================================
SYMBOLS = [
    "AAPL","META","MSFT","NVDA","TSLA","GOOGL","AMD",
    "CRWD","SPY","PLTR","LULU","LLY","COIN","MSTR","APP","ASML"
]

# Smart in-memory cache
CACHE = {}
CACHE_EXPIRY = 3600  # seconds = 1 hour

# ============================================================
# Helpers
# ============================================================
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

# ============================================================
# Polygon Data Fetch
# ============================================================
def fetch_all(symbol):
    """Fetch all snapshot pages (limit 50 per page)"""
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
        # extract cursor token if next_url is provided
        if "cursor=" in cursor:
            cursor = cursor.split("cursor=")[-1]
        else:
            cursor = None
    return all_rows

# ============================================================
# Expiries
# ============================================================
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

# ============================================================
# Analyze OI + IV
# ============================================================
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

# ============================================================
# Normalize for Pine
# ============================================================
def normalize_for_pine(data):
    if not data: return [], [], []
    base = max(oi for _, oi, _ in data) or 1.0
    strikes = [round(float(s), 2) for (s, _, _) in data]
    pcts    = [round((oi / base), 4) for (_, oi, _) in data]
    ivs     = [round(float(iv), 4) for (_, _, iv) in data]
    return strikes, pcts, ivs

def to_pine_array(arr):
    # returns comma-separated floats like: 123.450000,456.780000
    return ",".join(f"{float(x):.6f}" for x in arr if x is not None)

def arr_or_empty(arr):
    """
    Return a valid Pine expr:
      - empty -> array.new_float()
      - values -> array.from(v1,v2,...)
    """
    txt = to_pine_array(arr)
    return f"array.from({txt})" if txt else "array.new_float()"

# ============================================================
# Update + Cache
# ============================================================
def update_symbol_data(symbol):
    rows = fetch_all(symbol)
    expiries = list_future_expiries(rows)
    if not expiries:
        return None

    exp_w = nearest_weekly(expiries)
    exp_m = nearest_monthly(expiries)

    # AutoMonthFix: if the weekly Friday equals the monthly last Friday,
    # use the monthly set for both (so Weekly bars won't be empty)
    use_monthly_for_weekly = (exp_w == exp_m)

    if use_monthly_for_weekly and exp_m:
        _, w_calls, w_puts = analyze_oi_iv(rows, exp_m, 3)
    else:
        _, w_calls, w_puts = analyze_oi_iv(rows, exp_w, 3) if exp_w else (None, [], [])

    _, m_calls, m_puts = analyze_oi_iv(rows, exp_m, 6)

    return {
        "symbol": symbol,
        "weekly": {"calls": w_calls, "puts": w_puts},
        "monthly": {"calls": m_calls, "puts": m_puts},
        "timestamp": time.time()
    }

def get_symbol_data(symbol):
    now = time.time()
    if symbol in CACHE and (now - CACHE[symbol]["timestamp"] < CACHE_EXPIRY):
        return CACHE[symbol]
    data = update_symbol_data(symbol)
    if data: CACHE[symbol] = data
    return data

# ============================================================
# /all/pine endpoint
# ============================================================
@app.route("/all/pine")
def all_pine():
    if not POLY_KEY:
        return _err("Missing POLYGON_API_KEY", 401)

    blocks = []
    for sym in SYMBOLS:
        data = get_symbol_data(sym)
        if not data:
            continue

        # weekly (calls, puts)
        wc_s, wc_p, wc_iv = normalize_for_pine(data["weekly"]["calls"])
        wp_s, wp_p, wp_iv = normalize_for_pine(data["weekly"]["puts"])
        # monthly (calls, puts)
        mc_s, mc_p, mc_iv = normalize_for_pine(data["monthly"]["calls"])
        mp_s, mp_p, mp_iv = normalize_for_pine(data["monthly"]["puts"])

        # Per-symbol Pine block
        block = f"""
//========= {sym} =========
if syminfo.ticker == "{sym}"
    title = "GEX PRO • " + mode + " | {sym}"

    // -------- GEX bars --------
    if mode == "Weekly"
        draw_side({arr_or_empty(wc_s)}, {arr_or_empty(wc_p)}, {arr_or_empty(wc_iv)}, color.lime)
        draw_side({arr_or_empty(wp_s)}, {arr_or_empty(wp_p)}, {arr_or_empty(wp_iv)}, color.red)
    if mode == "Monthly"
        draw_side(array.from({to_pine_array(mc_s)}), array.from({to_pine_array(mc_p)}), array.from({to_pine_array(mc_iv)}), color.new(color.green, 0))
        draw_side(array.from({to_pine_array(mp_s)}), array.from({to_pine_array(mp_p)}), array.from({to_pine_array(mp_iv)}), color.new(#b02727, 0))

    // -------- HVL Smart Zone (scoped to this symbol) --------
    // Arrays (weekly calls) + (monthly calls) to feed HVL
    w_iv = {arr_or_empty(wc_iv)}
    w_s  = {arr_or_empty(wc_s)}
    w_p  = {arr_or_empty(wc_p)}
    m_iv = {arr_or_empty(mc_iv)}
    m_s  = {arr_or_empty(mc_s)}
    m_p  = {arr_or_empty(mc_p)}

    // choose data per user mode, but show monthly if weekly empty
    useWeekly = (mode == "Weekly") and (array.size(w_iv) > 0)
    src_iv  = useWeekly ? w_iv : m_iv
    src_str = useWeekly ? w_s  : m_s
    src_p   = useWeekly ? w_p  : m_p

    var line  h_top = na
    var line  h_bot = na
    var label h_lab = na
    var box   h_box = na

    showHVL   = input.bool(true, "Show HVL Smart Zone", group="GEX HVL")
    baseColor = input.color(color.new(color.yellow, 0), "Neutral Zone Color", group="GEX HVL")
    zoneWidth = input.float(1.0, "Zone Width %", minval=0.2, maxval=5.0, group="GEX HVL")

    float max_iv = 0.0
    float hvl_y  = na
    int   idx    = na

    if showHVL
        for i = 0 to array.size(src_iv) - 1
            iv = array.get(src_iv, i)
            if iv > max_iv
                max_iv := iv
                hvl_y  := array.get(src_str, i)
                idx    := i

        if not na(hvl_y)
            up_val = na(idx) or idx + 1 >= array.size(src_p) ? na : array.get(src_p, idx + 1)
            dn_val = na(idx) or idx - 1 < 0 ? na : array.get(src_p, idx - 1)
            colHVL = baseColor

            if not na(up_val) and not na(dn_val)
                if up_val > dn_val
                    colHVL := color.new(color.lime, 0)
                else if up_val < dn_val
                    colHVL := color.new(color.red, 0)

            if not na(h_top)
                line.delete(h_top)
            if not na(h_bot)
                line.delete(h_bot)
            if not na(h_lab)
                label.delete(h_lab)
            if not na(h_box)
                box.delete(h_box)

            h_top_y = hvl_y * (1 + zoneWidth / 100)
            h_bot_y = hvl_y * (1 - zoneWidth / 100)

            h_box := box.new(left = bar_index - 5, top = h_top_y, right = bar_index + 5, bottom = h_bot_y, bgcolor = color.new(colHVL, 85), border_color = color.new(colHVL, 50))
            h_top := line.new(bar_index - 10, h_top_y, bar_index + 10, h_top_y, extend = extend.both, color = color.new(colHVL, 0), width = 1, style = line.style_dotted)
            h_bot := line.new(bar_index - 10, h_bot_y, bar_index + 10, h_bot_y, extend = extend.both, color = color.new(colHVL, 0), width = 1, style = line.style_dotted)
            h_lab := label.new(bar_index + 5, hvl_y,"HVL " + str.tostring(hvl_y, "#.##") +(colHVL == color.lime ? "  (🟢)" : colHVL == color.red ? "  (🔴)" : "  (🟡)") +"  ±" + str.tostring(zoneWidth, "#.##") + "%", style = label.style_label_left, textcolor = color.black, color = colHVL, size = size.small)
"""
        blocks.append(block)

    # ===== Build full Pine code =====
    pine = f"""//@version=5
indicator("GEX PRO • SmartMode + IV% + AskGroup (240m)", overlay=true, max_lines_count=500, max_labels_count=500)
mode = input.string("Weekly", "Expiry Mode", options=["Weekly","Monthly"], group="Settings")

draw_side(_s, _p, _iv, _col) =>
    // إذا المصفوفات فاضية أو بدون عناصر
    if array.size(_s) == 0 or array.size(_p) == 0 or array.size(_iv) == 0
        // لا ترسم شيء
        na
    else
        // مصفوفات ثابتة
        var line[]  linesArr  = array.new_line()
        var label[] labelsArr = array.new_label()

        // امسح القديم
        for l in linesArr
            line.delete(l)
        for lb in labelsArr
            label.delete(lb)
        array.clear(linesArr)
        array.clear(labelsArr)

        // ارسم الجديد
        for i = 0 to array.size(_s) - 1
            y  = array.get(_s, i)
            p  = array.get(_p, i)
            iv = array.get(_iv, i)
            alpha   = 90 - int(p * 70)
            bar_col = color.new(_col, alpha)
            bar_len = int(math.max(10, p * 100))
            lineRef  = line.new(bar_index + 3, y, bar_index + bar_len - 12, y, color=bar_col, width=6)
            labelRef = label.new(bar_index + bar_len + 1, y, str.tostring(p*100, "#.##") + "% | IV " + str.tostring(iv*100, "#.##") + "%", style=label.style_none, textcolor=color.white, size=size.small)
            array.push(linesArr, lineRef)
            array.push(labelsArr, labelRef)


{''.join(blocks)}

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
"""
     # ===== labels for highest/lowest (from 240m series)
var label highestLabel = na
var label lowestLabel  = na
if drawhl
    newHigh = ta.highest(h240, prd)
    newLow  = ta.lowest(l240,  prd)
    if na(highestLabel) or label.get_y(highestLabel) != newHigh
        if not na(highestLabel)
            label.delete(highestLabel)
        highestLabel := label.new(bar_index + label_location, newHigh, "Highest PH " + str.tostring(newHigh),color=color.new(color.silver, 0), textcolor=color.black, style=label.style_label_down)
    if na(lowestLabel) or label.get_y(lowestLabel) != newLow
        if not na(lowestLabel)
            label.delete(lowestLabel)
        lowestLabel := label.new(bar_index + label_location, newLow, "Lowest PL " + str.tostring(newLow),color=color.new(color.silver, 0), textcolor=color.black, style=label.style_label_up)
    
return Response(pine, mimetype="text/plain")


# ============================================================
# /all/json endpoint
# ============================================================
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
                    "calls": [{"strike": s, "oi": oi, "iv": iv} for (s, oi, iv) in data["weekly"]["calls"]],
                    "puts":  [{"strike": s, "oi": oi, "iv": iv} for (s, oi, iv) in data["weekly"]["puts"]],
                },
                "monthly": {
                    "calls": [{"strike": s, "oi": oi, "iv": iv} for (s, oi, iv) in data["monthly"]["calls"]],
                    "puts":  [{"strike": s, "oi": oi, "iv": iv} for (s, oi, iv) in data["monthly"]["puts"]],
                },
                "timestamp": data["timestamp"]
            }
    return jsonify({
        "status": "OK",
        "symbols": SYMBOLS,
        "updated": dt.datetime.utcnow().isoformat() + "Z",
        "data": all_data
    })


# ============================================================
# Root Info
# ============================================================
@app.route("/")
def home():
    return jsonify({
        "status": "OK ✅",
        "symbols": SYMBOLS,
        "author": "Bassam GEX PRO v4.6 – SmartCache Edition",
        "interval": "240m ثابت",
        "update": "كل ساعة تلقائيًا",
        "usage": {"all_pine": "/all/pine", "all_json": "/all/json"},
        "cache_items": len(CACHE)
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
