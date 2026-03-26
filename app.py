from flask import Flask, jsonify, request
import os
import requests
import redis
import json

app = Flask(__name__)

UNISAT_API_KEY = os.getenv("UNISAT_API_KEY")
TARGET_RUNE_ID = os.getenv("TARGET_RUNE_ID")
TARGET_RUNE_NAME = os.getenv("TARGET_RUNE_NAME")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
REDIS_URL = os.getenv("REDIS_URL")

redis_client = None
if REDIS_URL:
    redis_client = redis.from_url(REDIS_URL, decode_responses=True)


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


def get_address_balance_data(address):
    url = f"https://open-api.unisat.io/v1/indexer/address/{address}/runes/{TARGET_RUNE_ID}/balance"
    response = requests.get(url, headers=get_headers(), timeout=20)
    return response.json()


def get_address_netflow_data(address):
    data = fetch_rune_events()

    if data.get("code") != 0:
        return {
            "success": False,
            "error": "Failed to fetch rune events",
            "unisat_response": data
        }

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

    return {
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
    }


def send_telegram_message(text, chat_id=None):
    if not TELEGRAM_BOT_TOKEN:
        return {"success": False, "error": "TELEGRAM_BOT_TOKEN is missing"}

    final_chat_id = chat_id or TELEGRAM_CHAT_ID
    if not final_chat_id:
        return {"success": False, "error": "chat_id is missing"}

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": final_chat_id,
        "text": text
    }

    response = requests.post(url, json=payload, timeout=20)
    data = response.json()

    return {
        "success": data.get("ok", False),
        "telegram_response": data
    }


def get_user_key(chat_id):
    return f"user_config:{chat_id}"


def get_bot_offset_key():
    return "bot_update_offset"


def load_user_config(chat_id):
    if not redis_client:
        return None

    raw = redis_client.get(get_user_key(chat_id))
    if not raw:
        return {
            "chat_id": str(chat_id),
            "rune_id": None,
            "rune_name": None,
            "watch_addresses": []
        }

    return json.loads(raw)


def save_user_config(chat_id, config):
    if not redis_client:
        return False

    redis_client.set(get_user_key(chat_id), json.dumps(config))
    return True


def get_bot_offset():
    if not redis_client:
        return None

    offset = redis_client.get(get_bot_offset_key())
    if not offset:
        return None

    try:
        return int(offset)
    except Exception:
        return None


def save_bot_offset(offset):
    if not redis_client:
        return False

    redis_client.set(get_bot_offset_key(), str(offset))
    return True


def get_updates_from_telegram():
    if not TELEGRAM_BOT_TOKEN:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN is missing"}

    offset = get_bot_offset()
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"

    params = {}
    if offset is not None:
        params["offset"] = offset

    response = requests.get(url, params=params, timeout=20)
    return response.json()


def format_user_config(config):
    watch_list = config.get("watch_addresses", [])

    if watch_list:
        watch_text = "\n".join([f"- {addr}" for addr in watch_list])
    else:
        watch_text = "(empty)"

    return (
        "📋 Your current config\n\n"
        f"Chat ID: {config.get('chat_id')}\n"
        f"Rune ID: {config.get('rune_id')}\n"
        f"Rune Name: {config.get('rune_name')}\n"
        f"Watch Addresses:\n{watch_text}"
    )


def handle_command(chat_id, text):
    config = load_user_config(chat_id)
    if config is None:
        return "❌ Redis is not connected."

    parts = text.strip().split()

    if not parts:
        return "❌ Empty command."

    command = parts[0].lower()

    if command == "/start":
        return (
            "✅ Runes Watch Bot is ready.\n\n"
            "Available commands:\n"
            "/start\n"
            "/setrune <rune_id> <rune_name>\n"
            "/addwatch <address>\n"
            "/myconfig\n"
            "/balance <address>\n"
            "/summary <address>"
        )

    if command == "/setrune":
        if len(parts) < 3:
            return "❌ Usage: /setrune <rune_id> <rune_name>"

        rune_id = parts[1]
        rune_name = " ".join(parts[2:])

        config["rune_id"] = rune_id
        config["rune_name"] = rune_name
        save_user_config(chat_id, config)

        return (
            "✅ Rune has been set\n\n"
            f"Rune ID: {rune_id}\n"
            f"Rune Name: {rune_name}"
        )

    if command == "/addwatch":
        if len(parts) < 2:
            return "❌ Usage: /addwatch <address>"

        address = parts[1]

        if address not in config["watch_addresses"]:
            config["watch_addresses"].append(address)
            save_user_config(chat_id, config)

        return (
            "✅ Watch address added\n\n"
            f"Address: {address}"
        )

    if command == "/myconfig":
        return format_user_config(config)

    if command == "/balance":
        if len(parts) < 2:
            return "❌ Usage: /balance <address>"

        address = parts[1]

        try:
            data = get_address_balance_data(address)

            if data.get("code") != 0:
                return f"❌ Balance fetch failed: {json.dumps(data, ensure_ascii=False)}"

            rune_data = data.get("data", {})
            amount_raw = rune_data.get("amount", "0")
            divisibility = int(rune_data.get("divisibility", 0))
            readable_amount = safe_raw_to_readable(amount_raw, divisibility)

            return (
                "💰 Address Balance\n\n"
                f"Address: {address}\n"
                f"Rune: {TARGET_RUNE_NAME}\n"
                f"Balance: {readable_amount}"
            )
        except Exception as e:
            return f"❌ Error fetching balance: {str(e)}"

    if command == "/summary":
        if len(parts) < 2:
            return "❌ Usage: /summary <address>"

        address = parts[1]

        try:
            result = get_address_netflow_data(address)
            if not result.get("success"):
                return "❌ Failed to fetch summary."

            summary = result.get("summary", {})

            return (
                "📊 Address Summary\n\n"
                f"Address: {address}\n"
                f"Rune: {TARGET_RUNE_NAME}\n"
                f"Total Inflow: {summary.get('total_inflow')}\n"
                f"Total Outflow: {summary.get('total_outflow')}\n"
                f"Net Position: {summary.get('net_position')}"
            )
        except Exception as e:
            return f"❌ Error fetching summary: {str(e)}"

    return "❌ Unknown command. Try /start"


@app.route("/")
def home():
    return "Runes Watch Bot is running!"


@app.route("/poll-bot")
def poll_bot():
    if not redis_client:
        return jsonify({"success": False, "error": "REDIS_URL is missing or Redis not connected"}), 500

    if not TELEGRAM_BOT_TOKEN:
        return jsonify({"success": False, "error": "TELEGRAM_BOT_TOKEN is missing"}), 500

    try:
        data = get_updates_from_telegram()

        if not data.get("ok"):
            return jsonify({
                "success": False,
                "telegram_response": data
            }), 400

        results = data.get("result", [])
        processed = []

        for item in results:
            update_id = item.get("update_id")
            message = item.get("message", {})
            text = message.get("text", "")
            chat = message.get("chat", {})
            chat_id = chat.get("id")

            if text and chat_id:
                reply_text = handle_command(str(chat_id), text)
                send_result = send_telegram_message(reply_text, chat_id=str(chat_id))

                processed.append({
                    "update_id": update_id,
                    "chat_id": chat_id,
                    "text": text,
                    "reply_text": reply_text,
                    "send_success": send_result.get("success", False)
                })

            if update_id is not None:
                save_bot_offset(int(update_id) + 1)

        return jsonify({
            "success": True,
            "processed_count": len(processed),
            "processed": processed
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/get-config/<chat_id>")
def get_config(chat_id):
    if not redis_client:
        return jsonify({"success": False, "error": "REDIS_URL is missing or Redis not connected"}), 500

    config = load_user_config(chat_id)
    return jsonify({"success": True, "config": config})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
