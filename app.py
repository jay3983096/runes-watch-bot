from flask import Flask, jsonify
import os
import requests

app = Flask(__name__)

UNISAT_API_KEY = os.getenv("UNISAT_API_KEY")
TARGET_RUNE_ID = os.getenv("TARGET_RUNE_ID")
TARGET_RUNE_NAME = os.getenv("TARGET_RUNE_NAME")


@app.route("/")
def home():
    return "Runes Watch Bot is running!"


@app.route("/test-rune")
def test_rune():
    if not UNISAT_API_KEY:
        return jsonify({
            "success": False,
            "error": "UNISAT_API_KEY is missing"
        }), 500

    url = "https://open-api.unisat.io/v1/indexer/runes/event"
    headers = {
        "Authorization": f"Bearer {UNISAT_API_KEY}",
        "Accept": "application/json"
    }
    params = {
        "rune": TARGET_RUNE_NAME
    }

    try:
        response = requests.get(url, headers=headers, params=params, timeout=20)
        data = response.json()

        return jsonify({
            "success": True,
            "target_rune_id": TARGET_RUNE_ID,
            "target_rune_name": TARGET_RUNE_NAME,
            "unisat_response": data
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
