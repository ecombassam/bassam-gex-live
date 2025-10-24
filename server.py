from flask import Flask, jsonify
import os, requests

app = Flask(__name__)

API_KEY = os.getenv("POLYGON_KEY")
BASE = "https://api.polygon.io"

@app.get("/<symbol>")
def gex(symbol):
    symbol = symbol.upper()
    url = f"{BASE}/v3/snapshot/options/{symbol}"
    params = {
        "greeks": "true",
        "limit": 1000,
        "expiration_date": "2025-10-24",
        "apiKey": API_KEY
    }

    r = requests.get(url, params=params, timeout=30)
    data = r.json()
    results = data.get("results", [])

    if not results:
        return jsonify({"error": "No options data", "symbol": symbol})

    # استخراج السعر الحالي
    current_price = results[0].get("underlying_asset", {}).get("price", 0)

    # فلترة العقود ذات قيم gamma غير صفرية
    valid = []
    for opt in results:
        strike = opt["details"].get("strike_price")
        greeks = opt.get("greeks") or {}
        gamma = greeks.get("gamma", 0)
        if gamma and gamma != 0:
            valid.append({
                "strike": strike,
                "gamma": gamma,
                "delta": greeks.get("delta"),
                "theta": greeks.get("theta"),
                "vega": greeks.get("vega")
            })

    if not valid:
        return jsonify({"error": "No valid gamma data", "symbol": symbol})

    # ترتيب الاسترايكات حسب السعر
    valid.sort(key=lambda x: x["strike"])

    # تقسيمها لأعلى وأسفل السعر الحالي
    below = [v for v in valid if v["strike"] < current_price]
    above = [v for v in valid if v["strike"] > current_price]

    # اختيار أقرب 5 فوق و5 تحت
    below = below[-5:] if len(below) > 5 else below
    above = above[:5] if len(above) > 5 else above

    return jsonify({
        "symbol": symbol,
        "price": current_price,
        "below": below,
        "above": above
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
