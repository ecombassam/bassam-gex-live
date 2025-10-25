# server.py — Bassam OI[Pro] v4.0 – Weekly + Monthly + IV (SmartMode)
import os, json, datetime as dt, requests
from flask import Flask, jsonify, Response

app = Flask(__name__)
POLY_KEY  = (os.environ.get("POLYGON_API_KEY") or "").strip()
BASE_SNAP = "https://api.polygon.io/v3/snapshot/options"
TODAY     = dt.date.today

# ─────────────────────────────
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

# ─────────────────────────────
def fetch_all(symbol):
    """يجلب جميع صفحات snapshot (حد 50 في الصفحة)"""
    url = f"{BASE_SNAP}/{symbol.upper()}"
    cursor, all_rows = None, []
    for _ in range(10):  # حد أقصى 10 صفحات احتياطاً
        params = {"limit": 50}
        if cursor: params["cursor"] = cursor
        status, j = _get(url, params)
        if status != 200 or j.get("status") != "OK":
            break
        rows = j.get("results") or []
        all_rows.extend(rows)
        cursor = j.get("next_url")
        if not cursor:
            break
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
    """أقرب جمعة قادمة (weekly)."""
    for d in expiries:
        try:
            y, m, dd = map(int, d.split("-"))
            if dt.date(y, m, dd).weekday() == 4:  # Friday
                return d
        except Exception:
            continue
    return expiries[0] if expiries else None

def nearest_monthly(expiries):
    """
    أقرب شهري معقول:
    - آخر يوم جمعة من الشهر (إن وُجد)، وإلا آخر يوم من الشهر ضمن القائمة،
      وإلا fallback لآخر تاريخ متاح في القائمة.
    """
    if not expiries: return None
    # جمّع تواريخ الشهر الأول القادم
    first = expiries[0]
    y, m, _ = map(int, first.split("-"))
    # كل التواريخ لذلك الشهر
    month_list = [d for d in expiries if d.startswith(f"{y:04d}-{m:02d}-")]
    # أحضر آخر جمعة في هذا الشهر
    last_friday = None
    for d in month_list:
        Y, M, D = map(int, d.split("-"))
        if dt.date(Y, M, D).weekday() == 4:
            last_friday = d
    if last_friday:
        return last_friday
    # إن لم يوجد جمعة، خذ آخر يوم من ذلك الشهر في القائمة
    return month_list[-1] if month_list else expiries[-1]

def analyze_oi_iv(rows, expiry, per_side_limit, split_by_price=True):
    """
    يُرجع أعلى OI لكل من Calls و Puts (مع IV).
      - split_by_price=True: Calls فوق السعر الحالي، Puts تحت السعر الحالي.
      - per_side_limit: أسبوعي=3، شهري=6.
    """
    rows = [r for r in rows if r.get("details", {}).get("expiration_date") == expiry]
    if not rows:
        return None, [], []

    # السعر الحالي
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
        if ctype == "call":
            calls.append((strike, oi, iv))
        elif ctype == "put":
            puts.append((strike, oi, iv))

    if split_by_price and isinstance(price, (int, float)):
        calls = [(s, oi, iv) for (s, oi, iv) in calls if s >= price]  # فوق السعر
        puts  = [(s, oi, iv) for (s, oi, iv) in puts  if s <= price]  # تحت السعر

    top_calls = sorted(calls, key=lambda x: x[1], reverse=True)[:per_side_limit]
    top_puts  = sorted(puts,  key=lambda x: x[1], reverse=True)[:per_side_limit]
    return price, top_calls, top_puts

def normalize_for_pine(data):
    """تحويل [(strike, oi, iv)] -> strikes[], pct[], iv[]  بحيث pct = oi / max_oi"""
    if not data:
        return [], [], []
    base = max(oi for _, oi, _ in data) or 1.0
    strikes = [round(s, 2) for (s, _, _) in data]
    pcts    = [round((oi / base), 4) for (_, oi, _) in data]
    ivs     = [round(iv, 4) for (_, _, iv) in data]  # 0..1
    return strikes, pcts, ivs

# ─────────────────────────────
@app.route("/<symbol>/json")
def json_route(symbol):
    if not POLY_KEY:
        return _err("Missing POLYGON_API_KEY", 401, sym=symbol)

    rows = fetch_all(symbol)
    expiries = list_future_expiries(rows)
    if not expiries:
        return _err("No upcoming expiries found", 404, {"why": "empty list"}, symbol)

    exp_w = nearest_weekly(expiries)
    exp_m = nearest_monthly(expiries)

    # أسبوعي: 3 لكل جانب
    _, w_calls, w_puts = analyze_oi_iv(rows, exp_w, per_side_limit=3, split_by_price=True) if exp_w else (None, [], [])
    # شهري: 6 لكل جانب
    _, m_calls, m_puts = analyze_oi_iv(rows, exp_m, per_side_limit=6, split_by_price=True) if exp_m else (None, [], [])

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

# ─────────────────────────────
@app.route("/<symbol>/pine")
def pine_route(symbol):
    if not POLY_KEY:
        return _err("Missing POLYGON_API_KEY", 401, sym=symbol)

    rows = fetch_all(symbol)
    expiries = list_future_expiries(rows)
    if not expiries:
        return _err("No upcoming expiries found", 404, {"why": "empty list"}, symbol)

    exp_w = nearest_weekly(expiries)
    exp_m = nearest_monthly(expiries)

    # أسبوعي
    _, w_calls, w_puts = analyze_oi_iv(rows, exp_w, per_side_limit=3, split_by_price=True) if exp_w else (None, [], [])
    # شهري
    _, m_calls, m_puts = analyze_oi_iv(rows, exp_m, per_side_limit=6, split_by_price=True) if exp_m else (None, [], [])

    # إلى مصفوفات Pine: strikes[], pcts[], ivs[]
    wc_s, wc_p, wc_iv = normalize_for_pine(w_calls)
    wp_s, wp_p, wp_iv = normalize_for_pine(w_puts)
    mc_s, mc_p, mc_iv = normalize_for_pine(m_calls)
    mp_s, mp_p, mp_iv = normalize_for_pine(m_puts)

    title = f"Bassam OI[Pro] • v4.0 SmartMode | {symbol.upper()}"
    pine = f"""//@version=5
indicator("{title}", overlay=true, max_lines_count=500, max_labels_count=500)

// === User Mode: Weekly OR Monthly (exclusive) ===
mode = input.string("Weekly", "Expiry Mode", options=["Weekly","Monthly"], group="Settings")

// Weekly data (server normalized)
weekly_calls_strikes = array.from({', '.join(map(str, wc_s))})
weekly_calls_pct     = array.from({', '.join(map(str, wc_p))})
weekly_calls_iv      = array.from({', '.join(map(str, wc_iv))})

weekly_puts_strikes  = array.from({', '.join(map(str, wp_s))})
weekly_puts_pct      = array.from({', '.join(map(str, wp_p))})
weekly_puts_iv       = array.from({', '.join(map(str, wp_iv))})

// Monthly data (server normalized)
monthly_calls_strikes = array.from({', '.join(map(str, mc_s))})
monthly_calls_pct     = array.from({', '.join(map(str, mc_p))})
monthly_calls_iv      = array.from({', '.join(map(str, mc_iv))})

monthly_puts_strikes  = array.from({', '.join(map(str, mp_s))})
monthly_puts_pct      = array.from({', '.join(map(str, mp_p))})
monthly_puts_iv       = array.from({', '.join(map(str, mp_iv))})

// Draw one side
draw_side(_strikes, _pcts, _ivs, _base_col) =>
    for i = 0 to array.size(_strikes) - 1
        y  = array.get(_strikes, i)
        p  = array.get(_pcts, i)    // 0..1
        iv = array.get(_ivs, i)     // 0..1
        alpha   = 90 - int(p * 70)  // تدرّج حسب القوة
        bar_col = color.new(_base_col, alpha)
        bar_len = int(math.max(10, p * 120))
        line.new(bar_index - 5, y, bar_index + bar_len - 5, y, color=bar_col, width=6)
        // نص بدون خلفية: "82% | IV 24%"
        label.new(bar_index + bar_len + 1, y,
                  str.format("{0}% | IV {1}%", int(p*100), int(iv*100)),
                  style=label.style_none, textcolor=color.white, size=size.small)

if barstate.islast
    if mode == "Weekly"
        draw_side(weekly_calls_strikes, weekly_calls_pct, weekly_calls_iv, color.lime)
        draw_side(weekly_puts_strikes,  weekly_puts_pct,  weekly_puts_iv,  color.red)
    if mode == "Monthly"
        draw_side(monthly_calls_strikes, monthly_calls_pct, monthly_calls_iv, color.new(color.green, 0))
        draw_side(monthly_puts_strikes,  monthly_puts_pct,  monthly_puts_iv,  color.new(color.purple, 0))
"""
    return Response(pine, mimetype="text/plain")

# ─────────────────────────────
@app.route("/")
def home():
    return jsonify({
        "status": "OK ✅",
        "usage": {
            "json": "/AAPL/json",
            "pine": "/AAPL/pine"
        },
        "notes": [
            "Weekly: أقرب جمعة قادمة، يعرض أعلى 3 فوق/تحت السعر (Calls فوق، Puts تحت)، مع IV.",
            "Monthly: أقرب شهري منطقي (آخر جمعة أو آخر يوم من الشهر)، يعرض أعلى 6/6، مع IV.",
            "المؤشر داخل TradingView يختار Weekly أو Monthly حصريًا."
        ],
        "author": "Bassam OI[Pro] v4.0 – SmartMode + IV%"
    })

# ─────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
