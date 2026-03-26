from flask import Flask, jsonify, request
import os
import requests
import redis
import json
from decimal import Decimal, ROUND_DOWN

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


def format_number(value, decimals=8):
    try:
        d = Decimal(str(value)).quantize(Decimal("1." + "0" * decimals), rounding=ROUND_DOWN)
        s = format(d, "f").rstrip("0").rstrip(".")
        return s if s else "0"
    except Exception:
        return str(value)


def safe_raw_to_readable(amount_raw, divisibility):
    try:
        raw = Decimal(str(amount_raw))
        divisor = Decimal(10) ** int(divisibility)
        return raw / divisor
    except Exception:
        return Decimal("0")


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
            "error": "获取符文事件失败",
            "unisat_response": data
        }

    detail_list = data.get("data", {}).get("detail", [])
    tx_map = {}

    for item in detail_list:
        if item.get("address") != address:
            continue

        txid = item.get("txid")
        event_type = item.get("type")
        amount_raw_int = int(item.get("amount", "0"))
        divisibility = int(item.get("divisibility", 0))
        readable_amount = safe_raw_to_readable(amount_raw_int, divisibility)

        if txid not in tx_map:
            tx_map[txid] = {
                "txid": txid,
                "height": item.get("height"),
                "timestamp": item.get("timestamp"),
                "total_receive_raw": 0,
                "total_send_raw": 0,
                "total_receive": Decimal("0"),
                "total_send": Decimal("0"),
                "divisibility": divisibility,
                "rune_id": item.get("runeId"),
                "spaced_rune": item.get("spacedRune")
            }

        if event_type == "receive":
            tx_map[txid]["total_receive_raw"] += amount_raw_int
            tx_map[txid]["total_receive"] += readable_amount
        elif event_type == "send":
            tx_map[txid]["total_send_raw"] += amount_raw_int
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
            "total_receive": str(row["total_receive"]),
            "total_send": str(row["total_send"]),
            "net_readable": str(net_readable),
            "direction": direction,
            "rune_id": row["rune_id"],
            "spaced_rune": row["spaced_rune"]
        })

    results.sort(key=lambda x: x["timestamp"], reverse=True)

    total_inflow = Decimal("0")
    total_outflow = Decimal("0")
    net_position = Decimal("0")

    for x in results:
        net_val = Decimal(str(x["net_readable"]))
        if net_val > 0:
            total_inflow += net_val
        elif net_val < 0:
            total_outflow += abs(net_val)
        net_position += net_val

    return {
        "success": True,
        "address": address,
        "target_rune_id": TARGET_RUNE_ID,
        "target_rune_name": TARGET_RUNE_NAME,
        "count": len(results),
        "summary": {
            "total_inflow": str(total_inflow),
            "total_outflow": str(total_outflow),
            "net_position": str(net_position)
        },
        "netflows": results
    }


def send_telegram_message(text, chat_id=None):
    if not TELEGRAM_BOT_TOKEN:
        return {"success": False, "error": "TELEGRAM_BOT_TOKEN 缺失"}

    final_chat_id = chat_id or TELEGRAM_CHAT_ID
    if not final_chat_id:
        return {"success": False, "error": "chat_id 缺失"}

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


def get_last_tx_key(chat_id, address):
    return f"last_pushed_tx:{chat_id}:{address}"


def get_last_pushed_tx(chat_id, address):
    if not redis_client:
        return None
    return redis_client.get(get_last_tx_key(chat_id, address))


def save_last_pushed_tx(chat_id, address, txid):
    if not redis_client:
        return False
    redis_client.set(get_last_tx_key(chat_id, address), txid)
    return True


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
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN 缺失"}

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
        watch_text = "（空）"

    return (
        "📋 当前配置\n\n"
        f"聊天 ID：{config.get('chat_id')}\n"
        f"符文 ID：{config.get('rune_id')}\n"
        f"符文名称：{config.get('rune_name')}\n"
        f"监控地址：\n{watch_text}"
    )


def format_history(address, result, limit=5):
    netflows = result.get("netflows", [])[:limit]
    summary = result.get("summary", {})

    if not netflows:
        history_text = "暂无历史记录"
    else:
        lines = []
        for i, item in enumerate(netflows, start=1):
            direction = item.get("direction")
            if direction == "inflow":
                direction_cn = "流入"
                sign = "+"
            elif direction == "outflow":
                direction_cn = "流出"
                sign = "-"
            else:
                direction_cn = "无变化"
                sign = ""

            net_amount = format_number(item.get("net_readable", "0"))
            txid_short = item.get("txid", "")[:12] + "..."
            lines.append(
                f"{i}. {direction_cn} {sign}{net_amount}\n"
                f"   交易：{txid_short}"
            )
        history_text = "\n".join(lines)

    return (
        "📜 地址历史记录\n\n"
        f"地址：{address}\n"
        f"符文：{TARGET_RUNE_NAME}\n\n"
        f"{history_text}\n\n"
        f"汇总：\n"
        f"总流入：{format_number(summary.get('total_inflow', '0'))}\n"
        f"总流出：{format_number(summary.get('total_outflow', '0'))}\n"
        f"净持仓：{format_number(summary.get('net_position', '0'))}"
    )


def handle_command(chat_id, text):
    config = load_user_config(chat_id)
    if config is None:
        return "❌ Redis 未连接。"

    parts = text.strip().split()

    if not parts:
        return "❌ 空命令。"

    command = parts[0].lower()

    if command == "/start":
        return (
            "✅ Runes 监控机器人已就绪。\n\n"
            "可用命令：\n"
            "/start\n"
            "/setrune <符文ID> <符文名称>\n"
            "/addwatch <地址>\n"
            "/myconfig\n"
            "/balance <地址>\n"
            "/summary <地址>\n"
            "/history <地址>"
        )

    if command == "/setrune":
        if len(parts) < 3:
            return "❌ 用法：/setrune <符文ID> <符文名称>"

        rune_id = parts[1]
        rune_name = " ".join(parts[2:])

        config["rune_id"] = rune_id
        config["rune_name"] = rune_name
        save_user_config(chat_id, config)

        return (
            "✅ 已设置监控符文\n\n"
            f"符文 ID：{rune_id}\n"
            f"符文名称：{rune_name}"
        )

    if command == "/addwatch":
        if len(parts) < 2:
            return "❌ 用法：/addwatch <地址>"

        address = parts[1]

        if address not in config["watch_addresses"]:
            config["watch_addresses"].append(address)
            save_user_config(chat_id, config)

        return (
            "✅ 已添加监控地址\n\n"
            f"地址：{address}"
        )

    if command == "/myconfig":
        return format_user_config(config)

    if command == "/balance":
        if len(parts) < 2:
            return "❌ 用法：/balance <地址>"

        address = parts[1]

        try:
            data = get_address_balance_data(address)

            if data.get("code") != 0:
                return f"❌ 查询余额失败：{json.dumps(data, ensure_ascii=False)}"

            rune_data = data.get("data", {})
            amount_raw = rune_data.get("amount", "0")
            divisibility = int(rune_data.get("divisibility", 0))
            readable_amount = safe_raw_to_readable(amount_raw, divisibility)

            return (
                "💰 地址余额\n\n"
                f"地址：{address}\n"
                f"符文：{TARGET_RUNE_NAME}\n"
                f"余额：{format_number(readable_amount)}"
            )
        except Exception as e:
            return f"❌ 查询余额出错：{str(e)}"

    if command == "/summary":
        if len(parts) < 2:
            return "❌ 用法：/summary <地址>"

        address = parts[1]

        try:
            result = get_address_netflow_data(address)
            if not result.get("success"):
                return "❌ 查询汇总失败。"

            summary = result.get("summary", {})

            return (
                "📊 地址汇总\n\n"
                f"地址：{address}\n"
                f"符文：{TARGET_RUNE_NAME}\n"
                f"总流入：{format_number(summary.get('total_inflow', '0'))}\n"
                f"总流出：{format_number(summary.get('total_outflow', '0'))}\n"
                f"净持仓：{format_number(summary.get('net_position', '0'))}"
            )
        except Exception as e:
            return f"❌ 查询汇总出错：{str(e)}"

    if command == "/history":
        if len(parts) < 2:
            return "❌ 用法：/history <地址>"

        address = parts[1]

        try:
            result = get_address_netflow_data(address)
            if not result.get("success"):
                return "❌ 查询历史失败。"

            return format_history(address, result, limit=5)
        except Exception as e:
            return f"❌ 查询历史出错：{str(e)}"

    return "❌ 未知命令，请先使用 /start 查看帮助。"


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


@app.route("/check-watches/<chat_id>")
def check_watches(chat_id):
    if not redis_client:
        return jsonify({"success": False, "error": "REDIS_URL is missing or Redis not connected"}), 500

    config = load_user_config(chat_id)
    if config is None:
        return jsonify({"success": False, "error": "User config not found"}), 404

    watch_addresses = config.get("watch_addresses", [])
    if not watch_addresses:
        return jsonify({
            "success": True,
            "message": "No watch addresses configured",
            "alerts": []
        })

    alerts = []

    for address in watch_addresses:
        result = get_address_netflow_data(address)

        if not result.get("success"):
            alerts.append({
                "address": address,
                "status": "error",
                "error": result.get("error", "unknown error")
            })
            continue

        netflows = result.get("netflows", [])
        if not netflows:
            alerts.append({
                "address": address,
                "status": "no_netflow"
            })
            continue

        latest = netflows[0]
        latest_txid = latest.get("txid")
        last_pushed_txid = get_last_pushed_tx(chat_id, address)

        if latest_txid == last_pushed_txid:
            alerts.append({
                "address": address,
                "status": "already_processed",
                "txid": latest_txid
            })
            continue

        direction = latest.get("direction")
        net_amount = format_number(latest.get("net_readable", "0"))

        if direction == "inflow":
            emoji = "🟢"
            direction_cn = "流入"
        elif direction == "outflow":
            emoji = "🔴"
            direction_cn = "流出"
        else:
            emoji = "⚪"
            direction_cn = "无变化"

        message = (
            f"{emoji} 地址活动提醒\n\n"
            f"地址：{address}\n"
            f"符文：{TARGET_RUNE_NAME}\n"
            f"方向：{direction_cn}\n"
            f"净变化：{net_amount}\n"
            f"交易：{latest_txid}"
        )

        send_result = send_telegram_message(message, chat_id=str(chat_id))

        if send_result.get("success"):
            save_last_pushed_tx(chat_id, address, latest_txid)
            alerts.append({
                "address": address,
                "status": "pushed",
                "txid": latest_txid,
                "direction": direction,
                "net_amount": net_amount
            })
        else:
            alerts.append({
                "address": address,
                "status": "push_failed",
                "txid": latest_txid,
                "direction": direction,
                "net_amount": net_amount
            })

    return jsonify({
        "success": True,
        "chat_id": chat_id,
        "alerts": alerts
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
