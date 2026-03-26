from flask import Flask, jsonify, request
import os
import requests
import redis
import json
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone, timedelta
from html import escape

app = Flask(__name__)

UNISAT_API_KEY = os.getenv("UNISAT_API_KEY")
TARGET_RUNE_ID = os.getenv("TARGET_RUNE_ID")
TARGET_RUNE_NAME = os.getenv("TARGET_RUNE_NAME")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
REDIS_URL = os.getenv("REDIS_URL")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

# 你的 Render Web Service 公网地址
WEB_BASE_URL = "https://runes-watch-bot.onrender.com"

SGT = timezone(timedelta(hours=8))

redis_client = None
if REDIS_URL:
    redis_client = redis.from_url(REDIS_URL, decode_responses=True)


# =========================
# 基础工具
# =========================
def get_headers():
    return {
        "Authorization": f"Bearer {UNISAT_API_KEY}",
        "Accept": "application/json"
    }


def format_number(value, decimals=8):
    try:
        d = Decimal(str(value)).quantize(
            Decimal("1." + "0" * decimals),
            rounding=ROUND_DOWN
        )
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


def format_ts(ts):
    try:
        dt = datetime.fromtimestamp(int(ts), tz=SGT)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "未知时间"


def tx_url(txid):
    return f"https://mempool.space/tx/{txid}"


# =========================
# Telegram 主菜单按钮
# =========================
def main_menu_keyboard():
    return {
        "keyboard": [
            ["设置符文"],
            ["添加监控地址"],
            ["我的监控列表"],
            ["删除监控地址"],
            ["钱包明细查询"]
        ],
        "resize_keyboard": True
    }


# =========================
# Redis Key
# =========================
def get_user_key(chat_id):
    return f"user_config:{chat_id}"


def get_last_tx_key(chat_id, address):
    return f"last_pushed_tx:{chat_id}:{address}"


def get_user_state_key(chat_id):
    return f"user_state:{chat_id}"


# =========================
# Redis 用户配置
# =========================
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


def list_all_user_chat_ids():
    if not redis_client:
        return []

    keys = redis_client.keys("user_config:*")
    chat_ids = []
    for key in keys:
        if key.startswith("user_config:"):
            chat_ids.append(key.split("user_config:", 1)[1])
    return chat_ids


# =========================
# Redis 输入状态
# =========================
def load_user_state(chat_id):
    if not redis_client:
        return None

    raw = redis_client.get(get_user_state_key(chat_id))
    if not raw:
        return {}

    return json.loads(raw)


def save_user_state(chat_id, state):
    if not redis_client:
        return False

    redis_client.set(get_user_state_key(chat_id), json.dumps(state))
    return True


def clear_user_state(chat_id):
    if not redis_client:
        return False

    redis_client.delete(get_user_state_key(chat_id))
    return True


# =========================
# Redis 去重
# =========================
def get_last_pushed_tx(chat_id, address):
    if not redis_client:
        return None
    return redis_client.get(get_last_tx_key(chat_id, address))


def save_last_pushed_tx(chat_id, address, txid):
    if not redis_client:
        return False
    redis_client.set(get_last_tx_key(chat_id, address), txid)
    return True


# =========================
# UniSat 数据
# =========================
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


# =========================
# Telegram API
# =========================
def send_telegram_message(text, chat_id=None, parse_mode=None, reply_markup=None):
    if not TELEGRAM_BOT_TOKEN:
        return {"success": False, "error": "TELEGRAM_BOT_TOKEN 缺失"}

    final_chat_id = chat_id or TELEGRAM_CHAT_ID
    if not final_chat_id:
        return {"success": False, "error": "chat_id 缺失"}

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": final_chat_id,
        "text": text,
        "disable_web_page_preview": True
    }

    if parse_mode:
        payload["parse_mode"] = parse_mode

    if reply_markup:
        payload["reply_markup"] = reply_markup

    response = requests.post(url, json=payload, timeout=20)
    data = response.json()

    return {
        "success": data.get("ok", False),
        "telegram_response": data
    }


def set_telegram_webhook():
    if not TELEGRAM_BOT_TOKEN:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN 缺失"}
    if not WEBHOOK_SECRET:
        return {"ok": False, "error": "WEBHOOK_SECRET 缺失"}

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook"
    webhook_url = f"{WEB_BASE_URL}/webhook/{WEBHOOK_SECRET}"

    payload = {
        "url": webhook_url,
        "drop_pending_updates": True
    }

    response = requests.post(url, json=payload, timeout=20)
    return response.json()


def delete_telegram_webhook():
    if not TELEGRAM_BOT_TOKEN:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN 缺失"}

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook"
    payload = {
        "drop_pending_updates": False
    }

    response = requests.post(url, json=payload, timeout=20)
    return response.json()


# =========================
# 文案格式化
# =========================
def format_user_config(config):
    watch_list = config.get("watch_addresses", [])

    if watch_list:
        watch_text = "\n".join([f"- {addr}" for addr in watch_list])
    else:
        watch_text = "（空）"

    return (
        "⚙️ 当前配置\n\n"
        f"符文 ID：{config.get('rune_id')}\n"
        f"符文名称：{config.get('rune_name')}\n"
        f"监控地址：\n{watch_text}"
    )


def format_watch_list(config):
    watch_list = config.get("watch_addresses", [])
    if not watch_list:
        return "📭 当前没有监控地址。"

    lines = [f"{i}. {addr}" for i, addr in enumerate(watch_list, start=1)]
    return "📋 我的监控列表\n\n" + "\n".join(lines)


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
            time_text = format_ts(item.get("timestamp"))
            txid = item.get("txid", "")
            link = f'<a href="{escape(tx_url(txid))}">查看</a>'

            lines.append(
                f"{i}. {direction_cn} {sign}{net_amount}\n"
                f"   时间：{time_text}\n"
                f"   {link}"
            )
        history_text = "\n".join(lines)

    return (
        "📜 地址历史记录\n\n"
        f"地址：{escape(address)}\n"
        f"符文：{escape(TARGET_RUNE_NAME)}\n\n"
        f"{history_text}\n\n"
        f"汇总：\n"
        f"总流入：{format_number(summary.get('total_inflow', '0'))}\n"
        f"总流出：{format_number(summary.get('total_outflow', '0'))}\n"
        f"净持仓：{format_number(summary.get('net_position', '0'))}"
    )


def format_wallet_detail(address):
    try:
        balance_data = get_address_balance_data(address)
        if balance_data.get("code") != 0:
            return f"❌ 查询钱包明细失败：{json.dumps(balance_data, ensure_ascii=False)}", None

        rune_data = balance_data.get("data", {})
        amount_raw = rune_data.get("amount", "0")
        divisibility = int(rune_data.get("divisibility", 0))
        readable_amount = safe_raw_to_readable(amount_raw, divisibility)

        summary_result = get_address_netflow_data(address)
        if not summary_result.get("success"):
            return "❌ 查询钱包明细失败：无法获取历史汇总。", None

        summary = summary_result.get("summary", {})
        history_text = format_history(address, summary_result, limit=3)

        text = (
            "💼 钱包明细查询\n\n"
            f"地址：{address}\n"
            f"符文：{TARGET_RUNE_NAME}\n"
            f"当前余额：{format_number(readable_amount)}\n"
            f"总流入：{format_number(summary.get('total_inflow', '0'))}\n"
            f"总流出：{format_number(summary.get('total_outflow', '0'))}\n"
            f"净持仓：{format_number(summary.get('net_position', '0'))}\n\n"
            f"{history_text}"
        )
        return text, "HTML"
    except Exception as e:
        return f"❌ 查询钱包明细出错：{str(e)}", None


def build_watch_alert_message(address, latest):
    direction = latest.get("direction")
    net_amount = format_number(latest.get("net_readable", "0"))
    time_text = format_ts(latest.get("timestamp"))
    txid = latest.get("txid")

    if direction == "inflow":
        emoji = "🟢"
        direction_cn = "流入"
        sign = "+"
    elif direction == "outflow":
        emoji = "🔴"
        direction_cn = "流出"
        sign = "-"
    else:
        emoji = "⚪"
        direction_cn = "无变化"
        sign = ""

    return (
        f"{emoji} 地址活动提醒\n\n"
        f"1. {direction_cn} {sign}{net_amount}\n"
        f"   时间：{time_text}\n"
        f'   <a href="{escape(tx_url(txid))}">查看</a>'
    )


# =========================
# 输入状态处理
# =========================
def handle_pending_input(chat_id, text):
    state = load_user_state(chat_id)
    action = state.get("action")

    config = load_user_config(chat_id)
    if config is None:
        return {
            "text": "❌ Redis 未连接。",
            "parse_mode": None,
            "reply_markup": main_menu_keyboard()
        }

    if action == "set_rune":
        parts = text.strip().split()
        if len(parts) < 2:
            return {
                "text": "❌ 请输入正确格式：\n符文ID 空格 符文名称\n\n例如：\n900830:2441 BTHACD•ID•FQEE•ODIN",
                "parse_mode": None,
                "reply_markup": main_menu_keyboard()
            }

        rune_id = parts[0]
        rune_name = " ".join(parts[1:])

        config["rune_id"] = rune_id
        config["rune_name"] = rune_name
        save_user_config(chat_id, config)
        clear_user_state(chat_id)

        return {
            "text": (
                "✅ 已设置监控符文\n\n"
                f"符文 ID：{rune_id}\n"
                f"符文名称：{rune_name}"
            ),
            "parse_mode": None,
            "reply_markup": main_menu_keyboard()
        }

    if action == "add_watch":
        address = text.strip()

        if address not in config["watch_addresses"]:
            config["watch_addresses"].append(address)
            save_user_config(chat_id, config)

        clear_user_state(chat_id)

        return {
            "text": (
                "✅ 已添加监控地址\n\n"
                f"地址：{address}"
            ),
            "parse_mode": None,
            "reply_markup": main_menu_keyboard()
        }

    if action == "remove_watch":
        address = text.strip()

        if address in config["watch_addresses"]:
            config["watch_addresses"].remove(address)
            save_user_config(chat_id, config)
            clear_user_state(chat_id)
            return {
                "text": (
                    "✅ 已移除监控地址\n\n"
                    f"地址：{address}"
                ),
                "parse_mode": None,
                "reply_markup": main_menu_keyboard()
            }

        clear_user_state(chat_id)
        return {
            "text": (
                "⚠️ 该地址不在监控列表中\n\n"
                f"地址：{address}"
            ),
            "parse_mode": None,
            "reply_markup": main_menu_keyboard()
        }

    if action == "wallet_detail":
        address = text.strip()
        clear_user_state(chat_id)
        text_out, parse_mode = format_wallet_detail(address)
        return {
            "text": text_out,
            "parse_mode": parse_mode,
            "reply_markup": main_menu_keyboard()
        }

    return None


# =========================
# 统一消息处理
# =========================
def handle_command(chat_id, text):
    config = load_user_config(chat_id)
    if config is None:
        return {
            "text": "❌ Redis 未连接。",
            "parse_mode": None,
            "reply_markup": main_menu_keyboard()
        }

    # 按钮文本
    if text == "设置符文":
        save_user_state(chat_id, {"action": "set_rune"})
        return {
            "text": (
                "请发送要监控的符文，格式如下：\n\n"
                "符文ID 空格 符文名称\n\n"
                "例如：\n"
                "900830:2441 BTHACD•ID•FQEE•ODIN"
            ),
            "parse_mode": None,
            "reply_markup": main_menu_keyboard()
        }

    if text == "添加监控地址":
        save_user_state(chat_id, {"action": "add_watch"})
        return {
            "text": "请直接发送你要添加的监控地址。",
            "parse_mode": None,
            "reply_markup": main_menu_keyboard()
        }

    if text == "我的监控列表":
        clear_user_state(chat_id)
        return {
            "text": format_watch_list(config),
            "parse_mode": None,
            "reply_markup": main_menu_keyboard()
        }

    if text == "删除监控地址":
        save_user_state(chat_id, {"action": "remove_watch"})
        return {
            "text": "请直接发送你要删除的监控地址。",
            "parse_mode": None,
            "reply_markup": main_menu_keyboard()
        }

    if text == "钱包明细查询":
        save_user_state(chat_id, {"action": "wallet_detail"})
        return {
            "text": "请直接发送你要查询的钱包地址。",
            "parse_mode": None,
            "reply_markup": main_menu_keyboard()
        }

    # 非命令，先看是否是等待输入
    if not text.startswith("/"):
        pending_result = handle_pending_input(chat_id, text)
        if pending_result:
            return pending_result

        return {
            "text": "❌ 无法识别你的输入。请点击下方按钮继续操作。",
            "parse_mode": None,
            "reply_markup": main_menu_keyboard()
        }

    # 命令兼容
    command = text.strip().lower()

    if command == "/start":
        clear_user_state(chat_id)
        return {
            "text": "✅ Runes 监控机器人已就绪。\n\n请直接点击下方按钮操作。",
            "parse_mode": None,
            "reply_markup": main_menu_keyboard()
        }

    return {
        "text": "❌ 未知命令。请直接点击下方按钮操作。",
        "parse_mode": None,
        "reply_markup": main_menu_keyboard()
    }


def process_incoming_message(chat_id, text):
    reply = handle_command(str(chat_id), text)
    return send_telegram_message(
        reply["text"],
        chat_id=str(chat_id),
        parse_mode=reply.get("parse_mode"),
        reply_markup=reply.get("reply_markup")
    )


# =========================
# Web 路由
# =========================
@app.route("/")
def home():
    return "Runes Watch Bot is running!"


@app.route("/set-webhook")
def set_webhook_route():
    result = set_telegram_webhook()
    return jsonify(result)


@app.route("/delete-webhook")
def delete_webhook_route():
    result = delete_telegram_webhook()
    return jsonify(result)


@app.route("/webhook/<secret>", methods=["POST"])
def telegram_webhook(secret):
    if not WEBHOOK_SECRET or secret != WEBHOOK_SECRET:
        return jsonify({"success": False, "error": "invalid secret"}), 403

    try:
        update = request.get_json(force=True, silent=True) or {}
        message = update.get("message", {})
        text = message.get("text", "")
        chat = message.get("chat", {})
        chat_id = chat.get("id")

        if text and chat_id:
            process_incoming_message(chat_id, text)

        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/poll-bot")
def poll_bot():
    # 保留兼容，便于迁移期手动排查
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
                send_result = process_incoming_message(chat_id, text)
                processed.append({
                    "update_id": update_id,
                    "chat_id": chat_id,
                    "text": text,
                    "send_success": send_result.get("success", False)
                })

            # webhook 模式下通常不会再用 offset，但保留不影响
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

        message = build_watch_alert_message(address, latest)
        send_result = send_telegram_message(
            message,
            chat_id=str(chat_id),
            parse_mode="HTML"
        )

        if send_result.get("success"):
            save_last_pushed_tx(chat_id, address, latest_txid)
            alerts.append({
                "address": address,
                "status": "pushed",
                "txid": latest_txid,
                "direction": latest.get("direction"),
                "net_amount": format_number(latest.get("net_readable", "0"))
            })
        else:
            alerts.append({
                "address": address,
                "status": "push_failed",
                "txid": latest_txid,
                "direction": latest.get("direction"),
                "net_amount": format_number(latest.get("net_readable", "0"))
            })

    return jsonify({
        "success": True,
        "chat_id": chat_id,
        "alerts": alerts
    })


@app.route("/check-all-users")
def check_all_users():
    if not redis_client:
        return jsonify({"success": False, "error": "REDIS_URL is missing or Redis not connected"}), 500

    chat_ids = list_all_user_chat_ids()
    results = []

    for chat_id in chat_ids:
        config = load_user_config(chat_id)
        watch_addresses = config.get("watch_addresses", []) if config else []
        user_alerts = []

        for address in watch_addresses:
            result = get_address_netflow_data(address)

            if not result.get("success"):
                user_alerts.append({
                    "address": address,
                    "status": "error",
                    "error": result.get("error", "unknown error")
                })
                continue

            netflows = result.get("netflows", [])
            if not netflows:
                user_alerts.append({
                    "address": address,
                    "status": "no_netflow"
                })
                continue

            latest = netflows[0]
            latest_txid = latest.get("txid")
            last_pushed_txid = get_last_pushed_tx(chat_id, address)

            if latest_txid == last_pushed_txid:
                user_alerts.append({
                    "address": address,
                    "status": "already_processed",
                    "txid": latest_txid
                })
                continue

            message = build_watch_alert_message(address, latest)
            send_result = send_telegram_message(
                message,
                chat_id=str(chat_id),
                parse_mode="HTML"
            )

            if send_result.get("success"):
                save_last_pushed_tx(chat_id, address, latest_txid)
                user_alerts.append({
                    "address": address,
                    "status": "pushed",
                    "txid": latest_txid,
                    "direction": latest.get("direction"),
                    "net_amount": format_number(latest.get("net_readable", "0"))
                })
            else:
                user_alerts.append({
                    "address": address,
                    "status": "push_failed",
                    "txid": latest_txid,
                    "direction": latest.get("direction"),
                    "net_amount": format_number(latest.get("net_readable", "0"))
                })

        results.append({
            "chat_id": chat_id,
            "watch_count": len(watch_addresses),
            "alerts": user_alerts
        })

    return jsonify({
        "success": True,
        "user_count": len(chat_ids),
        "results": results
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
