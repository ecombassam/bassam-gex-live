# ===========================================================
# Bassam GEX – Σ CUMULATIVE  |  Final Replica of Options GEX [Lite]
# ===========================================================
import os, json, datetime, math
from flask import Flask, jsonify, Response
import requests
from urllib.parse import urlencode

POLY_KEY = os.environ.get("POLYGON_API_KEY") or os.environ.get("POLYGON_KEY") or ""
POLY_KEY = POLY_KEY.strip()
print("✅ Polygon Key Loaded:", POLY_KEY[:6] + "..." if POLY_KEY else "❌ EMPTY")

BASE = "https://api.polygon.io/v3/snapshot/options"
EXP_BASE = "https://api.polygon.io/v3/reference/options/expirations"
app = Flask(__name__)

# ---------- جلب أقرب تاريخ Expiry ----------
def get_next_expiry(symbol: str):
    if not POLY_KEY:
        return None
    url = f"{EXP_BASE}?ticker={symbol.upper()}"
    headers = {"Authorization": f"Bearer {POLY_KEY}"}
    try:
        r = requests.get(url, headers=headers, timeout=15)
        j = r.json()
        exps = j.get("results", [])
        today = datetime.date.today()
        for e in exps:
            try:
                d = datetime.date.fromisoformat(e)
                if d >= today:
                    diff = (d - today).days
                    label = "1st Weekly" if diff <= 7 else (
                        "2nd Weekly" if diff <= 14 else (
                        "3rd Weekly" if diff <= 21 else (
                        "4th Weekly" if diff <= 28 else "Optimal Monthly")))
                    return {"date": e, "label": label}
            except: continue
    except Exception as e:
        print("❌ Expiry fetch error:", e)
    return {"date": None, "label": "Unknown"}

# ---------- تحميل بيانات الخيارات ----------
def get_chain(symbol: str, expiry=None):
    if not POLY_KEY:
        return {"error": "Missing POLYGON_API_KEY env"}, []
    params = {"greeks": "true"}
    if expiry and expiry.get("date"):
        params["expiration_date"] = expiry["date"]
    url = f"{BASE}/{symbol.upper()}"
    headers = {"Authorization": f"Bearer {POLY_KEY}"}
    r = requests.get(url, headers=headers, timeout=30)
    j = r.json()
    if r.status_code != 200 or j.get("status") != "OK":
        return {"status": "ERROR", "data": j}, []
    return None, j.get("results", [])

# ---------- حساب Σ CUMULATIVE Gamma ----------
def cumulative_gamma_by_strike(items):
    underlying = next(
        (it.get("underlying_asset", {}).get("price") for it in items
         if isinstance(it.get("underlying_asset", {}).get("price"), (int,float))), None)
    buckets = {}
    for it in items:
        det, g = it.get("details", {}), it.get("greeks", {})
        t, strike, gamma = det.get("contract_type"), det.get("strike_price"), g.get("gamma")
        if isinstance(strike,(int,float)) and isinstance(gamma,(int,float)):
            buckets.setdefault(strike, {"call":0,"put":0})
            buckets[strike][t] += gamma
    rows=[{"strike":k,"cum":v["call"]-v["put"]} for k,v in buckets.items()]
    rows.sort(key=lambda x:x["strike"])
    return underlying, rows

# ---------- اختيار الجدران الأقوى ----------
def pick_walls(rows, price, depth=3):
    pos=[r for r in rows if r["cum"]>0]; neg=[r for r in rows if r["cum"]<0]
    pos.sort(key=lambda r:r["cum"],reverse=True); neg.sort(key=lambda r:r["cum"])
    return pos[:depth],neg[:depth]

# ---------- إنشاء كود Pine ----------
def make_pine(symbol, price, expiry, pos, neg):
    exp_date=expiry["date"] or "na"; exp_label=expiry["label"]
    strong_pos=max((r["cum"] for r in pos),default=0)
    strong_neg=max((abs(r["cum"]) for r in neg),default=0)

    def bar_len(pct): return max(10,int(round(pct*120)))
    def emit(side,color):
        lines=[]
        for i,r in enumerate(side,1):
            pct=(r["cum"]/strong_pos) if color=="lime" else (abs(r["cum"])/strong_neg)
            pct=max(0,min(1,pct))
            L=bar_len(pct); val=int(round(pct*100))
            lines.append(f"""
// {('CALL' if color=='lime' else 'PUT')} wall #{i}
line.new(bar_index,{r["strike"]},bar_index-1000,{r["strike"]},extend=extend.left,color=color.new(color.{color},0),width=1)
if barstate.islast
    var line l{i}{color[0].upper()}=na
    if not na(l{i}{color[0].upper()})
        line.delete(l{i}{color[0].upper()})
    l{i}{color[0].upper()}:=line.new(bar_index,{r["strike"]},bar_index+{L},{r["strike"]},color=color.new(color.{color},0),width=6)
    label.new(bar_index+{L}+2,{r["strike"]},"{val}%",textcolor=color.white,color=color.new(color.{color},0),style=label.style_label_left,size=size.large)
""")
        return "\n".join(lines)

    return f"""//@version=5
indicator("Bassam GEX – Σ CUMULATIVE | {symbol} | {exp_label} | Exp {exp_date}",
 overlay=true, max_lines_count=500, max_labels_count=500)

// 🧠 GEX SETTINGS
grpGex="{{ GEX SETTINGS }}"
calcMode=input.string("Σ CUMULATIVE","GEX calculation method:",options=["Σ CUMULATIVE"],group=grpGex)
selHighlight=input.bool(true,"Enable selection highlight",group=grpGex)

// 🎨 DESIGN SETTINGS
grpDesign="{{ DESIGN SETTINGS }}"
callDepth=input.int(3,"Depth",minval=1,maxval=10,group=grpDesign,inline="CALL")
callColor=input.color(color.new(color.lime,0),"CALL color",group=grpDesign,inline="CALL")
hvlEnabled=input.bool(true,"HVL enabled",group=grpDesign,inline="HVL")

// 💠 DISPLAY SETTINGS
grpDisp="{{ DISPLAY SETTINGS }}"
fontSize=input.string("L","Font Size",options=["S","M","L"],group=grpDisp,inline="F")
labelOff=input.int(30,"And label offset",group=grpDisp,inline="F")
enStrk=input.bool(true,"Enable strikes",group=grpDisp)
enLines=input.bool(true,"Lines",group=grpDisp)
enBars=input.bool(true,"Bars",group=grpDisp)
enPct=input.bool(true,"% with info",group=grpDisp)
extSel=input.string("←","If Lines enabled, then line Extension",
 options=["←","→","↔","none"],group=grpDisp)

{emit(pos,'lime')}
{emit(neg,'red')}
"""

# ---------- الواجهات ----------
@app.route("/")
def root(): return jsonify({"ok":True,"usage":"/AAPL/pine"}),200

@app.route("/<symbol>/pine")
def pine(symbol):
    expiry=get_next_expiry(symbol)
    err,items=get_chain(symbol,expiry)
    if err: return Response(json.dumps(err),mimetype="text/plain"),502
    price,rows=cumulative_gamma_by_strike(items)
    pos,neg=pick_walls(rows,price)
    pine_code=make_pine(symbol.upper(),price,expiry,pos,neg)
    header=f"// Generated from Polygon snapshot | Symbol={symbol.upper()} | Exp={expiry['date']} | Price={round(price,2) if price else 'na'}\n"
    return Response(header+pine_code,mimetype="text/plain")

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",8000)))
