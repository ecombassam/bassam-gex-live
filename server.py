from flask import Flask, jsonify
import requests
import os
from datetime import datetime

app = Flask(__name__)

API_KEY = os.getenv("POLYGON_API_KEY")
BASE_URL = "https://api.polygon.io/v3/reference/options/contracts"

def get_gamma_data(symbol="AAPL"):
    url = f"{BASE_URL}?underlying_ticker={symbol}&expired=false&limit=100&apiKey={API_KEY}"
    response = requests.get(url)
    data = response.json()

    if "results" not in data:
        return []

    contracts = data["results"]
    gamma_data = []

    for c in contracts:
        greeks = c.get("greeks", {})
        gamma = greeks.get("gamma")
        strike = c.get("strike_price")
        if gamma is not None and strike is not None:
            gamma_data.append({
                "strike": strike,
                "gamma": gamma,
                "delta": greeks.get("delta"),
                "theta": greeks.get("theta"),
                "vega": greeks.get("vega")
            })

    gamma_data = sorted(gamma_data, key=lambda x: x["strike"])
    return gamma_data

@app.route('/')
def home():
    return jsonify({
        "message": "✅ Bassam GEX Live API is running",
        "usage": "Use /AAPL or /TSLA or /SPY to fetch latest Gamma data"
    })

@app.route('/<symbol>')
def symbol_data(symbol):
    gamma_data = get_gamma_data(symbol.upper())

    if not gamma_data:
        return jsonify({"error": "No gamma data"})

    # استخراج أقرب 5 فوق السعر و5 تحته
    try:
        last_price = float(gamma_data[len(gamma_data)//2]["strike"])
    except:
        last_price = 0

    top5 = gamma_data[-5:]
    bottom5 = gamma_data[:5]
    selected = bottom5 + top5

    # تحويلها إلى كود PineScript تلقائي
    strikes = ",".join([str(round(x["strike"], 2)) for x in selected])
    gammas = ",".join([str(round(x["gamma"], 8)) for x in selected])

    pine_code = f"""// Updated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
indicator("Bassam GEX Live – {symbol.upper()}", overlay=true, max_lines_count=500, max_labels_count=300)
strikes = array.from({strikes})
gammas  = array.from({gammas})
for i = 0 to array.size(strikes)-1
    strike = array.get(strikes, i)
    gamma  = array.get(gammas, i)
    colorLine = gamma > 0 ? color.new(color.lime, 0) : color.new(color.red, 0)
    line.new(bar_index - 50, strike, bar_index + 50, strike, color=colorLine, width=2)
"""

    return jsonify({
        "symbol": symbol.upper(),
        "count": len(selected),
        "pine_code": pine_code,
        "strikes": selected
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
