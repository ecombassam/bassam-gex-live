# bassam_gex_live.py
from flask import Flask, jsonify
import os, requests, math

API_KEY = os.getenv("POLYGON_KEY")  # مفتاح Polygon
BASE = "https://api.polygon.io"
app = Flask(__name__)

@app.get("/<symbol>")
def gex(symbol):
    symbol = symbol.upper()
    url = f"{BASE}/v3/snapshot/options/{symbol}"
    params = {"greeks": "true", "apiKey": API_KEY, "limit": 1000}
    r = requests.get(url, params=params, timeout=30)
    data = r.json()
    results = data.get("results", [])

    if not results:
        return jsonify({"error": "No options data", "symbol": symbol})

    contracts = []
    underlying_price = None

    for opt in results:
        details = opt.get("details") or {}
        greeks = opt.get("greeks") or {}
        underlying = opt.get("underlying_asset") or {}

        gamma = greeks.get("gamma")
        theta = greeks.get("theta") or 0
        oi = opt.get("open_interest") or 0
        strike = details.get("strike_price")
        ctype = details.get("contract_type")

        # تجاهل القيم الفارغة
        if gamma in (None, 0) or not strike:
            continue

        # التقاط سعر الأصل لمرة واحدة
        if not underlying_price and isinstance(underlying.get("price"), (int, float)):
            underlying_price = underlying["price"]

        # قوة العقد (Gamma × Open Interest)
        strength = abs(gamma) * (oi + 1)

        contracts.append({
            "strike": float(strike),
            "gamma": float(gamma),
            "theta": float(theta),
            "oi": int(oi),
            "type": ctype,
            "strength": strength
        })

    if not contracts:
        return jsonify({"error": "No valid gamma contracts", "symbol": symbol})

    if not underlying_price:
        # fallback لو ما جاب السعر
        underlying_price = sorted(contracts, key=lambda x: x["strike"])[len(contracts)//2]["strike"]

    # فصل فوق السعر وتحته
    above = [c for c in contracts if c["strike"] > underlying_price]
    below = [c for c in contracts if c["strike"] < underlying_price]

    # ترتيب حسب القوة التنازلية
    above.sort(key=lambda x: x["strength"], reverse=True)
    below.sort(key=lambda x: x["strength"], reverse=True)

    # أخذ 5 فوق و5 تحت فقط
    top_above = above[:5]
    top_below = below[:5]

    final = sorted(top_above + top_below, key=lambda x: x["strike"])

    return jsonify({
        "symbol": symbol,
        "underlying_price": round(underlying_price, 2),
        "above_count": len(top_above),
        "below_count": len(top_below),
        "strikes": [c["strike"] for c in final],
        "gammas": [c["gamma"] for c in final],
        "contracts": final
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
