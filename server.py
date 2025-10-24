# server.py (v1.5 live, no caching)
import os, json, math, datetime as dt
from flask import Flask, jsonify, Response, request
import requests
from urllib.parse import urlencode

app = Flask(__name__)
POLY_KEY = (os.environ.get("POLYGON_API_KEY") or os.environ.get("POLYGON_KEY") or "").strip()
BASE_SNAP = "https://api.polygon.io/v3/snapshot/options"
TODAY = dt.date.today

def _err(msg, http=502, data=None, sym=None):
    body = {"error": msg}
    if data is not None: body["data"] = data
    if sym: body["symbol"] = sym.upper()
    return Response(json.dumps(body, ensure_ascii=False), status=http, mimetype="application/json")

def _get(url, params=None):
    params = params or {}
    # نرسل المفتاح بطريقتين لضمان القبول
    params["apiKey"] = POLY_KEY
    headers = {"Authorization": f"Bearer {POLY_KEY}"} if POLY_KEY else {}
    r = requests.get(url, params=params, headers=headers, timeout=30)
    try:
        j = r.json()
    except Exception:
        return r.status_code, {"raw": r.text}
    return r.status_code, j

def _need_key():
    return (not POLY_KEY)

# ---------- 1) إيجاد أقرب Weekly ----------
def find_first_weekly_date(symbol:str):
    """
    نجيب أول صفحة Snapshot ثم نختار أقرب expiration_date >= اليوم.
    (هذا يعطي عمليًا 1st weekly لمعظم الرموز. لو ودك منطق أدق لاحقًا نضيف استثناء الشهري.)
    """
    url = f"{BASE_SNAP}/{symbol.upper()}"
    status, j = _get(url, {"greeks":"true", "limit":100})
    if status!=200 or j.get("status")!="OK": return None, j
    rows = j.get("results") or []
    if not rows: return None, {"why":"no snapshot rows"}
    today = TODAY().isoformat()
    expiries = set()
    for it in rows:
        d = it.get("details",{}).get("expiration_date")
        if d and d>=today: expiries.add(d)
    if not expiries: return None, {"why":"no future expiries"}
    first = sorted(expiries)[0]
    return first, None

# ---------- 2) جلب السلسلة للاكسباير المختار ----------
def fetch_chain(symbol:str, expiration_date:str):
    url = f"{BASE_SNAP}/{symbol.upper()}"
    all_items, cursor = [], None
    pages = 0
    while pages<8:
        params = {"greeks":"true", "expiration_date": expiration_date}
        if cursor:
            # next_url من بوليغون يحتوي cursor=...
            return_code, j = _get(f"{url}", {"cursor": cursor})
        else:
            return_code, j = _get(url, params)
        if return_code!=200 or j.get("status")!="OK":
            return None, j
        items = j.get("results") or []
        all_items.extend(items)
        next_url = j.get("next_url")
        if not next_url: break
        cursor = next_url.split("cursor=")[-1]
        pages += 1
    return all_items, None

# ---------- 3) حساب Σ CUMULATIVE لكل Strike ----------
def cumulative_gamma(items):
    under_price = None
    for it in items:
        p = it.get("underlying_asset",{}).get("price")
        if isinstance(p,(int,float)) and p>0:
            under_price = p
            break
    buckets = {}  # strike -> {call,put}
    for it in items:
        det = it.get("details",{})
        g   = it.get("greeks",{})
        t   = det.get("contract_type")
        strike = det.get("strike_price")
        gamma  = g.get("gamma")
        if not (isinstance(strike,(int,float)) and isinstance(gamma,(int,float))): continue
        d = buckets.setdefault(strike, {"call":0.0,"put":0.0})
        if t=="call": d["call"] += gamma
        elif t=="put": d["put"] += gamma
    rows = []
    for k,v in buckets.items():
        rows.append({
            "strike": float(k),
            "cum": float(v["call"] - v["put"]),
            "call": float(v["call"]),
            "put":  float(v["put"]),
        })
    rows.sort(key=lambda r: r["strike"])
    return under_price, rows

def pick_walls(rows, price, around_pct=0.35, depth=3, extra=4):
    # فلترة حول السعر
    flt = rows
    if isinstance(price,(int,float)) and price>0:
        lo, hi = price*(1-around_pct), price*(1+around_pct)
        flt = [r for r in rows if lo<=r["strike"]<=hi]
    pos = [r for r in flt if r["cum"]>0]
    neg = [r for r in flt if r["cum"]<0]
    pos.sort(key=lambda r:r["cum"], reverse=True)
    neg.sort(key=lambda r:r["cum"])  # الأكثر سلبًا أولًا
    return pos[:depth+extra], neg[:depth+extra]  # نرجع الكل، والـ Pine يرسم 3 أساسية ويظهر % للباقي لو حاب

def _bar_len(pct, max_len=120):
    pct = max(0.0, min(1.0, pct))
    return max(10, int(round(pct*max_len)))

# ---------- 4) توليد Pine مطابق لإعدادات GEX Lite ----------
def make_pine(symbol, expiry, price, pos, neg, depth=3, add_levels=4):
    def norm(arr, take, sign):
        # نحسب نسبة كل مستوى لأقوى مستوى من نفس النوع
        base = (arr[0]["cum"] if arr else 0.0)
        base = abs(base)
        out  = []
        for i, r in enumerate(arr[:take], 1):
            val = r["cum"]
            pct = (abs(val)/base) if base>0 else 0.0
            out.append({"i":i, "strike":r["strike"], "pct":pct})
        return out

    calls = norm(pos, depth+add_levels, +1)
    puts  = norm(neg, depth+add_levels, -1)

    def emit(side, color_expr, label_side):
        lines=[]
        for i, r in enumerate(side, 1):
            s  = r["strike"]
            pc = r["pct"]
            L  = _bar_len(pc)
            txt = f'{int(round(pc*100))}%'
            lines.append(f"""
// {'CALL' if 'lime' in color_expr else 'PUT'} wall #{i}
line.new(bar_index, {s}, bar_index-1000, {s}, extend=extend.left, color={color_expr}, width=1)
var line bar{i}{'C' if 'lime' in color_expr else 'P'} = na
if barstate.islast
    if not na(bar{i}{'C' if 'lime' in color_expr else 'P'}): line.delete(bar{i}{'C' if 'lime' in color_expr else 'P'})
    bar{i}{'C' if 'lime' in color_expr else 'P'} := line.new(bar_index, {s}, bar_index + {L}, {s}, color={color_expr}, width=6)
    label.new(bar_index + {L} + 2, {s}, "{txt}", textcolor=color.white, color=color.new({color_expr}, 0), style=label.style_label_left, size=size.large)
""")
        return "".join(lines)

    title = f"Bassam GEX – Σ CUMULATIVE | {symbol} | 1st Weekly | Exp {expiry}"
    pine = f"""// Generated from Polygon snapshot | Symbol={symbol} | Exp={expiry} | Price={round(price,2) if price else 'na'}
//@version=5
indicator("{title}", overlay=true, max_lines_count=500, max_labels_count=500)

// GEX Lite visual: Lines + Bars + % with info | extension ←
enStrk = input.bool(true, "Enable strikes")
enLines= input.bool(true, "Lines")
enBars = input.bool(true, "Bars")
enPct  = input.bool(true, "% with info")
extSel = input.string("←", "If Lines enabled, then line Extension", options=["←","→","↔","none"])
callDepth = input.int(3, "CALL Depth", minval=1, maxval=10, inline="C")
putDepth  = input.int(3, "PUT Depth",  minval=1, maxval=10, inline="P")

{emit(calls, "color.new(color.lime, 0)", "right")}
{emit(puts,  "color.new(color.red,  0)", "right")}
"""
    return pine

# ------------------- Routes -------------------
@app.route("/")
def home():
    return jsonify({"ok": True, "usage": "/SYMBOL/pine  (PineScript)  |  /SYMBOL/json  (summary)"}), 200

@app.route("/<symbol>/pine")
def pine_route(symbol):
    if _need_key(): return _err("Missing POLYGON_API_KEY", 401, sym=symbol)
    # نحدد أقرب weekly
    exp, e = find_first_weekly_date(symbol)
    if e: return _err("Failed to detect nearest weekly expiry", 502, e, symbol)
    items, e2 = fetch_chain(symbol, exp)
    if e2: return _err("Invalid response from Polygon", 502, e2, symbol)
    price, rows = cumulative_gamma(items)
    pos, neg    = pick_walls(rows, price, around_pct=0.35, depth=3, extra=4)
    pine_code   = make_pine(symbol.upper(), exp, price, pos, neg, depth=3, add_levels=4)
    return Response(pine_code, mimetype="text/plain")

@app.route("/<symbol>/json")
def json_route(symbol):
    if _need_key(): return _err("Missing POLYGON_API_KEY", 401, sym=symbol)
    exp, e = find_first_weekly_date(symbol)
    if e: return _err("Failed to detect nearest weekly expiry", 502, e, symbol)
    items, e2 = fetch_chain(symbol, exp)
    if e2: return _err("Invalid response from Polygon", 502, e2, symbol)
    price, rows = cumulative_gamma(items)
    pos, neg    = pick_walls(rows, price, around_pct=0.35, depth=3, extra=4)
    return jsonify({
        "symbol": symbol.upper(),
        "expiry": exp,
        "price": round(price,2) if price else None,
        "call_walls": [{"strike":r["strike"], "cum":r["cum"]} for r in pos[:3]],
        "put_walls":  [{"strike":r["strike"], "cum":r["cum"]} for r in neg[:3]],
        "total_levels": len(rows)
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
