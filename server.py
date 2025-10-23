from flask import Flask, jsonify
import requests

app = Flask(__name__)

POLYGON_API_KEY = "_Atf0iAjKp1rrpNpOOzrwpsiqv4yNmYo"

@app.route("/")
def home():
    return "✅ Bassam GEX Live API is running!"

@app.route("/<symbol>")
def get_data(symbol):
    url = f"https://api.polygon.io/v3/snapshot/options/{symbol.upper()}?greeks=true&apiKey={POLYGON_API_KEY}"
    r = requests.get(url)
    data = r.json()

    if "results" not in data:
        return jsonify({"error": "No results"}), 400

    results = data["results"]
    if not results:
        return jsonify({"error": "Empty results"}), 400

    underlying = results[0].get("underlying_asset", {}).get("price", 0)
    filtered = [o for o in results if "greeks" in o and o["greeks"] and o.get("details")]

    strikes = sorted([o["details"]["strike_price"] for o in filtered])
    gammas = [o["greeks"]["gamma"] for o in filtered if "greeks" in o and o["greeks"]]

    if not strikes or not gammas:
        return jsonify({"error": "No gamma data"}), 400

    # تحديد 5 فوق و5 تحت السعر الحالي
    mid_index = min(range(len(strikes)), key=lambda i: abs(strikes[i] - underlying))
    start = max(0, mid_index - 5)
    end = min(len(strikes), mid_index + 6)

    selected_strikes = strikes[start:end]
    selected_gammas = gammas[start:end]

    pine = f"""//@version=5
indicator("Bassam GEX Live – {symbol.upper()}", overlay=true, max_lines_count=500, max_labels_count=300)
strikes = array.from({','.join(map(str, selected_strikes))})
gammas  = array.from({','.join(map(str, selected_gammas))})
for i = 0 to array.size(strikes)-1
    strike = array.get(strikes, i)
    gamma  = array.get(gammas, i)
    colorLine = gamma > 0 ? color.new(color.lime, 0) : color.new(color.red, 0)
    line.new(bar_index - 50, strike, bar_index + 50, strike, color=colorLine, width=2)
"""

    return pine, 200, {"Content-Type": "text/plain; charset=utf-8"}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
