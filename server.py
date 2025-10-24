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

    headers = {
        "Accept": "application/json",
        "User-Agent": "Bassam-GEX-Live/1.0"
    }

    try:
        r = requests.get(url, params=params, headers=headers, timeout=30)
        data = r.json()
    except Exception as e:
        return jsonify({"error": f"Request failed: {str(e)}", "symbol": symbol})

    # لو الرد فارغ أو خطأ
    if not data or "results" not in data:
        return jsonify({"error": "Invalid response from Polygon", "data": data, "symbol": symbol})

    results = data.get("results", [])
    if len(results) == 0:
        return jsonify({"error": "Polygon returned empty results", "params": params})

    # السعر الحالي
    current_price = results[0].get("underlying_asset", {}).get("price", 0)

    valid = []
    for opt in results:
        greeks = opt.get("greeks") or {}
        gamma = greeks.get("gamma", 0)
        strike = opt["details"].get("strike_price", 0)
        if gamma and gamma != 0:
            valid.append({
                "strike": strike,
                "gamma": gamma,
                "delta": greeks.get("delta"),
                "theta": greeks.get("theta"),
                "vega": greeks.get("vega")
            })

    if not valid:
        return jsonify({"error": "No valid gamma data found", "count": len(results)})

    # ترتيب الاسترايكات وتقسيمها
    valid.sort(key=lambda x: x["strike"])
    below = [v for v in valid if v["strike"] < current_price][-5:]
    above = [v for v in valid if v["strike"] > current_price][:5]

    return jsonify({
        "symbol": symbol,
        "price": current_price,
        "below": below,
        "above": above,
        "total_contracts": len(valid)
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
