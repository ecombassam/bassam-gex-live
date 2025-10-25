# server.py — Bassam OI[Pro] v4.0 – SmartMode + IV% (Final Fix)
import os, json, datetime as dt, requests
from flask import Flask, jsonify, Response

app = Flask(__name__)
POLY_KEY  = (os.environ.get("POLYGON_API_KEY") or "").strip()
BASE_SNAP = "https://api.polygon.io/v3/snapshot/options"
TODAY     = dt.date.today


#─────────────────────────────
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
    try: return r.status_code, r.json()
    except Exception: return r.status_code, {"error": "Invalid JSON"}

#─────────────────────────────
def fetch_all(symbol):
    """يجلب جميع صفحات snapshot (حد 50 في الصفحة)"""
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
        cursor = cursor.split("cursor=")[-1]
    return all_rows

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
    if last_friday: return last_friday
    return month_list[-1] if month_list else expiries[-1]

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
        puts  = [(s, oi, iv) for (s, oi, iv) in puts  if s <= price]

    top_calls = sorted(calls, key=lambda x: x[1], reverse=True)[:per_side_limit]
    top_puts  = sorted(puts,  key=lambda x: x[1], reverse=True)[:per_side_limit]
    return price, top_calls, top_puts

def normalize_for_pine(data):
    if not data: return [], [], []
    base = max(oi for _, oi, _ in data) or 1.0
    strikes = [round(s, 2) for (s, _, _) in data]
    pcts    = [round((oi / base), 4) for (_, oi, _) in data]
    ivs     = [round(iv, 4) for (_, _, iv) in data]
    return strikes, pcts, ivs

#─────────────────────────────
@app.route("/<symbol>/json")
def json_route(symbol):
    if not POLY_KEY: return _err("Missing POLYGON_API_KEY", 401, sym=symbol)
    rows = fetch_all(symbol)
    expiries = list_future_expiries(rows)
    if not expiries: return _err("No upcoming expiries found", 404, {"why": "empty list"}, symbol)

    exp_w = nearest_weekly(expiries)
    exp_m = nearest_monthly(expiries)

    _, w_calls, w_puts = analyze_oi_iv(rows, exp_w, per_side_limit=3) if exp_w else (None, [], [])
    _, m_calls, m_puts = analyze_oi_iv(rows, exp_m, per_side_limit=6) if exp_m else (None, [], [])

    return jsonify({
        "symbol": symbol.upper(),
        "weekly": {
            "expiry": exp_w,
            "call_walls": [{"strike": s, "oi": oi, "iv": iv} for (s, oi, iv) in w_calls],
            "put_walls":  [{"strike": s, "oi": oi, "iv": iv} for (s, oi, iv) in w_puts]
        },
        "monthly": {
            "expiry": exp_m,
            "call_walls": [{"strike": s, "oi": oi, "iv": iv} for (s, oi, iv) in m_calls],
            "put_walls":  [{"strike": s, "oi": oi, "iv": iv} for (s, oi, iv) in m_puts]
        }
    })

#─────────────────────────────
@app.route("/<symbol>/pine")
def pine_route(symbol):
    if not POLY_KEY: return _err("Missing POLYGON_API_KEY", 401, sym=symbol)
    rows = fetch_all(symbol)
    expiries = list_future_expiries(rows)
    if not expiries: return _err("No upcoming expiries found", 404, {"why": "empty list"}, symbol)

    exp_w = nearest_weekly(expiries)
    exp_m = nearest_monthly(expiries)

    _, w_calls, w_puts = analyze_oi_iv(rows, exp_w, per_side_limit=3) if exp_w else (None, [], [])
    _, m_calls, m_puts = analyze_oi_iv(rows, exp_m, per_side_limit=6) if exp_m else (None, [], [])

    wc_s, wc_p, wc_iv = normalize_for_pine(w_calls)
    wp_s, wp_p, wp_iv = normalize_for_pine(w_puts)
    mc_s, mc_p, mc_iv = normalize_for_pine(m_calls)
    mp_s, mp_p, mp_iv = normalize_for_pine(m_puts)

    title = f"Bassam OI[Pro] • v4.0 SmartMode | {symbol.upper()}"
    pine = f"""//@version=5
indicator("{title}", overlay=true, max_lines_count=500, max_labels_count=500)
mode = input.string("Weekly", "Expiry Mode", options=["Weekly","Monthly"], group="Settings")

weekly_calls_strikes = array.from({', '.join(map(str, wc_s))})
weekly_calls_pct     = array.from({', '.join(map(str, wc_p))})
weekly_calls_iv      = array.from({', '.join(map(str, wc_iv))})
weekly_puts_strikes  = array.from({', '.join(map(str, wp_s))})
weekly_puts_pct      = array.from({', '.join(map(str, wp_p))})
weekly_puts_iv       = array.from({', '.join(map(str, wp_iv))})

monthly_calls_strikes = array.from({', '.join(map(str, mc_s))})
monthly_calls_pct     = array.from({', '.join(map(str, mc_p))})
monthly_calls_iv      = array.from({', '.join(map(str, mc_iv))})
monthly_puts_strikes  = array.from({', '.join(map(str, mp_s))})
monthly_puts_pct      = array.from({', '.join(map(str, mp_p))})
monthly_puts_iv       = array.from({', '.join(map(str, mp_iv))})

draw_side(_strikes, _pcts, _ivs, _base_col) =>
    for i = 0 to array.size(_strikes) - 1
        y  = array.get(_strikes, i)
        p  = array.get(_pcts, i)
        iv = array.get(_ivs, i)
        alpha   = 90 - int(p * 70)
        bar_col = color.new(_base_col, alpha)
        bar_len = int(math.max(10, p * 50))
        line.new(bar_index + 3, y, bar_index + bar_len - 12, y, color=bar_col, width=6)
        label.new(
     bar_index + bar_len + 1, y,
     str.tostring(p*100, "#.##") + "%  |  IV " + str.tostring(iv*100, "#.##") + "%",
     style = label.style_none,
     textcolor = color.white,
     size = size.small)

if barstate.islast
    if mode == "Weekly"
        draw_side(weekly_calls_strikes, weekly_calls_pct, weekly_calls_iv, color.lime)
        draw_side(weekly_puts_strikes,  weekly_puts_pct,  weekly_puts_iv,  color.red)
    if mode == "Monthly"
        draw_side(monthly_calls_strikes, monthly_calls_pct, monthly_calls_iv, color.new(color.green, 0))
        draw_side(monthly_puts_strikes,  monthly_puts_pct,  monthly_puts_iv,  color.new(#b02727, 0))
        // ========== ask group support ==========
rb             = input.int(10,  "Period for Pivot Points", minval=10)
prd            = input.int(284, "Loopback Period", minval=100, maxval=500)
nump           = input.int(2,   "S/R strength", minval=1)
ChannelW       = input.int(10,  "Channel Width %", minval=5)
label_location = input.int(10,  "Label Location +-")
linestyle      = input.string("Dashed","Line Style", options=["Solid","Dotted","Dashed"])
LineColor      = input.color(color.new(color.blue,20), "Line Color")
drawhl         = input.bool(true, "Draw Highest/Lowest Pivots in Period")
showpp         = input.bool(true,"Show Pivot Points")

ph = ta.pivothigh(high, rb, rb)
pl = ta.pivotlow(low,  rb, rb)

plotshape(showpp and not na(ph), title="PH", text="انعكاس", style=shape.labeldown, color=color.new(color.white,100), textcolor=color.red,  location=location.abovebar, offset=-rb)
plotshape(showpp and not na(pl), title="PL", text="انعكاس", style=shape.labelup,   color=color.new(color.white,100), textcolor=color.lime, location=location.belowbar, offset=-rb)
sr_levels  = array.new_float(21, na)
prdhighest = ta.highest(high, prd)
prdlowest  = ta.lowest(low,  prd)
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
        if na(close[x]) or countpp > 40
            break
        if not na(ph[x]) or not na(pl[x])
            highestph := math.max(highestph, nz(ph[x], prdlowest), nz(pl[x], prdlowest))
            lowestpl  := math.min(lowestpl,  nz(ph[x], prdhighest), nz(pl[x], prdhighest))
            countpp += 1
            if array.get(aas, countpp)
                upl = (not na(ph[x]) ? high[x + rb] : low[x + rb]) + cwidth
                dnl = (not na(ph[x]) ? high[x + rb] : low[x + rb]) - cwidth
                tmp = array.new_bool(41, true)
                cnt = 0
                tpoint = 0
                for xx = 0 to prd
                    if na(close[xx]) or cnt > 40
                        break
                    if not na(ph[xx]) or not na(pl[xx])
                        chg = false
                        cnt += 1
                        if array.get(aas, cnt)
                            if not na(ph[xx]) and high[xx + rb] <= upl and high[xx + rb] >= dnl
                                tpoint += 1
                                chg := true
                            if not na(pl[xx]) and low[xx + rb] <= upl and low[xx + rb] >= dnl
                                tpoint += 1
                                chg := true
                        if chg and cnt < 41
                            array.set(tmp, cnt, false)
                if tpoint >= nump
                    for g = 0 to 40
                        if not array.get(tmp, g)
                            array.set(aas, g, false)
                    if not na(ph[x]) and countpp < 21
                        array.set(sr_levels, countpp, high[x + rb])
                    if not na(pl[x]) and countpp < 21
                        array.set(sr_levels, countpp, low[x + rb])

style = linestyle == "Solid" ? line.style_solid : linestyle == "Dotted" ? line.style_dotted : line.style_dashed
for x = 0 to array.size(sr_levels) - 1
    lvl = array.get(sr_levels, x)
    if not na(lvl)
        col = lvl < close ? color.new(color.lime, 0) : color.new(color.red, 0)
        array.set(sr_lines, x, line.new(bar_index - 1, lvl, bar_index, lvl, color=col, width=1, style=style, extend=extend.both))

// ✅ عرض اللابلز مرة واحدة لكل تحديث وليس لكل شمعة
var label highestLabel = na
var label lowestLabel  = na

if drawhl
    // حذف اللابلز القديمة فقط إذا تغيّر أعلى أو أدنى Pivot فعلاً
    newHigh = ta.highest(high, prd)
    newLow  = ta.lowest(low,  prd)

    if na(highestLabel) or label.get_y(highestLabel) != newHigh
        if not na(highestLabel)
            label.delete(highestLabel)
        highestLabel := label.new( x = bar_index + label_location, y = newHigh + (syminfo.mintick * 50), text = "Highest PH " + str.tostring(newHigh), color = color.new(color.silver, 0), textcolor = color.black, style = label.style_label_down)

    if na(lowestLabel) or label.get_y(lowestLabel) != newLow
        if not na(lowestLabel)
            label.delete(lowestLabel)
        lowestLabel := label.new( x = bar_index + label_location, y = newLow - (syminfo.mintick * 50), text = "Lowest PL " + str.tostring(newLow), color = color.new(color.silver, 0), textcolor = color.black, style = label.style_label_up )"""
    return Response(pine, mimetype="text/plain")

#─────────────────────────────
@app.route("/")
def home():
    return jsonify({
        "status": "OK ✅",
        "usage": {"json": "/AAPL/json", "pine": "/AAPL/pine"},
        "author": "Bassam OI[Pro] v4.0 – SmartMode + IV%",
        "notes": [
            "Weekly: أقرب جمعة قادمة (3 مستويات لكل جانب)",
            "Monthly: آخر جمعة أو آخر يوم من الشهر (6 مستويات لكل جانب)",
            "يعرض النسبة و IV% الحقيقيين مع تدرج لون حسب قوة OI"
        ]
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
