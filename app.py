from flask import Flask, jsonify
import os
import requests

app = Flask(__name__)

UNISAT_API_KEY = os.getenv("UNISAT_API_KEY")
TARGET_RUNE_ID = os.getenv("TARGET_RUNE_ID")
TARGET_RUNE_NAME = os.getenv("TARGET_RUNE_NAME")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def get_headers():
    return {
        "Authorization": f"Bearer {UNISAT_API_KEY}",
        "Accept": "application/json"
    }


def safe_raw_to_readable(amount_raw, divisibility):
    try:
        return int(amount_raw) / (10 ** int(divisibility))
    except Exception:
        return amount_raw


def fetch_rune_events():
    url = "https://open-api.unisat.io/v1/indexer/runes/event"
    params = {"rune": TARGET_RUNE_NAME}
    response = requests.get(url, headers=get_headers(), params=params, timeout=20)
    return response.json()


def send_telegram_message(text):
    if not TELEGRAM_BOT_TOKEN:
        return {"success": False, "error": "TELEGRAM_BOT_TOKEN is missing"}

    if not TELEGRAM_CHAT_ID:
        return {"success": False, "error": "TELEGRAM_CHAT_ID is missing"}

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text
    }

    response = requests.post(url, json=payload, timeout=20)
    data = response.json()

    return {
        "success": data.get("ok", False),
        "telegram_response": data
    }


@app.route("/")
def home():
    return "Runes Watch Bot is running!"


@app.route("/test-rune")
def test_rune():
    if not UNISAT_API_KEY:
        return jsonify({"success": False, "error": "UNISAT_API_KEY is missing"}), 500

    try:
        data = fetch_rune_events()
        return jsonify({
            "success": True,
            "target_rune_id": TARGET_RUNE_ID,
            "target_rune_name": TARGET_RUNE_NAME,
            "unisat_response": data
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/address-balance/<address>")
def address_balance(address):
    if not UNISAT_API_KEY:
        return jsonify({"success": False, "error": "UNISAT_API_KEY is missing"}), 500

    url = f"https://open-api.unisat.io/v1/indexer/address/{address}/runes/{TARGET_RUNE_ID}/balance"

    try:
        response = requests.get(url, headers=get_headers(), timeout=20)
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
        readable_amount = safe_raw_to_readable(amount_raw, divisibility)

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
        return jsonify({"success": False, "error": str(e), "address": address}), 500


@app.route("/address-events/<address>")
def address_events(address):
    if not UNISAT_API_KEY:
        return jsonify({"success": False, "error": "UNISAT_API_KEY is missing"}), 500

    try:
        data = fetch_rune_events()

        if data.get("code") != 0:
            return jsonify({
                "success": False,
                "address": address,
                "target_rune_id": TARGET_RUNE_ID,
                "target_rune_name": TARGET_RUNE_NAME,
                "unisat_response": data
            }), 400

        detail_list = data.get("data", {}).get("detail", [])

        matched_events = []
        for item in detail_list:
            if item.get("address") == address:
                amount_raw = item.get("amount", "0")
                divisibility = int(item.get("divisibility", 0))
                readable_amount = safe_raw_to_readable(amount_raw, divisibility)

                matched_events.append({
                    "txid": item.get("txid"),
                    "type": item.get("type"),
                    "amount_raw": amount_raw,
                    "readable_amount": readable_amount,
                    "height": item.get("height"),
                    "timestamp": item.get("timestamp"),
                    "rune_id": item.get("runeId"),
                    "spaced_rune": item.get("spacedRune")
                })

        return jsonify({
            "success": True,
            "address": address,
            "target_rune_id": TARGET_RUNE_ID,
            "target_rune_name": TARGET_RUNE_NAME,
            "count": len(matched_events),
            "events": matched_events
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "address": address}), 500


@app.route("/address-netflows/<address>")
def address_netflows(address):
    if not UNISAT_API_KEY:
        return jsonify({"success": False, "error": "UNISAT_API_KEY is missing"}), 500

    try:
        data = fetch_rune_events()

        if data.get("code") != 0:
            return jsonify({
                "success": False,
                "address": address,
                "target_rune_id": TARGET_RUNE_ID,
                "target_rune_name": TARGET_RUNE_NAME,
                "unisat_response": data
            }), 400

        detail_list = data.get("data", {}).get("detail", [])
        tx_map = {}

        for item in detail_list:
            if item.get("address") != address:
                continue

            txid = item.get("txid")
            event_type = item.get("type")
            amount_raw = int(item.get("amount", "0"))
            divisibility = int(item.get("divisibility", 0))
            readable_amount = safe_raw_to_readable(amount_raw, divisibility)

            if txid not in tx_map:
                tx_map[txid] = {
                    "txid": txid,
                    "height": item.get("height"),
                    "timestamp": item.get("timestamp"),
                    "total_receive_raw": 0,
                    "total_send_raw": 0,
                    "total_receive": 0,
                    "total_send": 0,
                    "divisibility": divisibility,
                    "rune_id": item.get("runeId"),
                    "spaced_rune": item.get("spacedRune")
                }

            if event_type == "receive":
                tx_map[txid]["total_receive_raw"] += amount_raw
                tx_map[txid]["total_receive"] += readable_amount
            elif event_type == "send":
                tx_map[txid]["total_send_raw"] += amount_raw
                tx_map[txid]["total_send"] += readable_amount

        results = []
        for txid, row in tx_map.items():
            net_raw = row["total_receive_raw"] - row["total_send_raw"]
            net_readable = row["total_receive"] - row["total_send"]

            if net_raw > 0:
                direction = "inflow"
            elif net_raw < 0:
                direction = "outflow"
            else:
                direction = "neutral"

            results.append({
                "txid": txid,
                "height": row["height"],
                "timestamp": row["timestamp"],
                "total_receive_raw": str(row["total_receive_raw"]),
                "total_send_raw": str(row["total_send_raw"]),
                "net_raw": str(net_raw),
                "total_receive": row["total_receive"],
                "total_send": row["total_send"],
                "net_readable": net_readable,
                "direction": direction,
                "rune_id": row["rune_id"],
                "spaced_rune": row["spaced_rune"]
            })

        results.sort(key=lambda x: x["timestamp"], reverse=True)

        total_inflow = sum(x["net_readable"] for x in results if x["net_readable"] > 0)
        total_outflow = sum(abs(x["net_readable"]) for x in results if x["net_readable"] < 0)
        net_position = sum(x["net_readable"] for x in results)

        return jsonify({
            "success": True,
            "address": address,
            "target_rune_id": TARGET_RUNE_ID,
            "target_rune_name": TARGET_RUNE_NAME,
            "count": len(results),
            "summary": {
                "total_inflow": total_inflow,
                "total_outflow": total_outflow,
                "net_position": net_position
            },
            "netflows": results
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "address": address}), 500


@app.route("/get-updates")
def get_updates():
    if not TELEGRAM_BOT_TOKEN:
        return jsonify({"success": False, "error": "TELEGRAM_BOT_TOKEN is missing"}), 500

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"

    try:
        response = requests.get(url, timeout=20)
        data = response.json()

        return jsonify({
            "success": True,
            "telegram_response": data
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/send-test-message")
def send_test_message():
    text = (
        "✅ Test message from Runes Watch Bot\n\n"
        f"Rune: {TARGET_RUNE_NAME}\n"
        f"Rune ID: {TARGET_RUNE_ID}\n"
        "Status: Telegram push is working."
    )

    result = send_telegram_message(text)
    return jsonify(result)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
