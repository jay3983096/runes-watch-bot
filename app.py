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


@app.route("/address-balance/<address>")
def address_balance(address):
    if not UNISAT_API_KEY:
        return jsonify({
            "success": False,
            "error": "UNISAT_API_KEY is missing"
        }), 500

    url = f"https://open-api.unisat.io/v1/indexer/address/{address}/runes/{TARGET_RUNE_ID}/balance"
    headers = {
        "Authorization": f"Bearer {UNISAT_API_KEY}",
        "Accept": "application/json"
    }

    try:
        response = requests.get(url, headers=headers, timeout=20)
        data = response.json()

        if data.get("code") != 0:
            return jsonify({
                "success": False,
                "address": address,
                "target_rune_id": TARGET_RUNE_ID,
                "target_rune_name": TARGET_RUNE_NAME,
                "unisat_response": data
            }), 400

        rune_data = data.get("data", {})
        amount_raw = rune_data.get("amount", "0")
        divisibility = int(rune_data.get("divisibility", 0))

        readable_amount = 0
        try:
            readable_amount = int(amount_raw) / (10 ** divisibility)
        except Exception:
            readable_amount = amount_raw

        return jsonify({
            "success": True,
            "address": address,
            "target_rune_id": TARGET_RUNE_ID,
            "target_rune_name": TARGET_RUNE_NAME,
            "amount_raw": amount_raw,
            "divisibility": divisibility,
            "readable_amount": readable_amount,
            "unisat_response": data
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e),
            "address": address
        }), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
