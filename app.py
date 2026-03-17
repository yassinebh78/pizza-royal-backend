from flask_cors import CORS
import os
import json
import requests
from flask import Flask, request, jsonify
from threading import Lock

app = Flask(__name__)

# --- Configuration (set as env vars in Render) ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_WAITER = os.getenv("CHAT_WAITER")
CHAT_KITCHEN = os.getenv("CHAT_KITCHEN")
if not all([BOT_TOKEN, CHAT_WAITER, CHAT_KITCHEN]):
    raise RuntimeError("Missing required env vars: BOT_TOKEN, CHAT_WAITER, CHAT_KITCHEN")

BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

pending_orders = {}
pending_lock = Lock()

def tg_send_message(chat_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    resp = requests.post(f"{BASE_URL}/sendMessage", json=payload)
    resp.raise_for_status()
    return resp.json()

def tg_edit_message_reply_markup(chat_id, message_id, reply_markup):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "reply_markup": json.dumps(reply_markup)
    }
    resp = requests.post(f"{BASE_URL}/editMessageReplyMarkup", json=payload)
    resp.raise_for_status()
    return resp.json()
    @app.route("/call-waiter", methods=["POST"])
    
def receive_call_waiter():
    """Called by the website when a customer calls the waiter."""
    if not request.is_json:
        return jsonify({"error": "JSON required"}), 400

    data = request.get_json()
    table_number = data.get("table", "?")
    text = f"🔔 La table {table_number} appelle le serveur."

    # Send to waiter chat
    tg_send_message(CHAT_WAITER, text)
    return jsonify({"status": "ok"})

@app.route("/send-order", methods=["POST"])
def receive_order():
    if not request.is_json:
        return jsonify({"error": "JSON required"}), 400

    data = request.get_json()
    table_number = data.get("table", "?")
    items = data.get("items", [])
    total = data.get("total", 0)

    lines = [f"🛒 Commande - Table {table_number}:"]
    for it in items:
        lines.append(f"• {it['name']} x{it['qty']} — {it['price']:.1f}DT")
    lines.append(f"\n💰 Total : {total:.1f}DT")
    text = "\n".join(lines)

    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ Accepter", "callback_data": "accept_"},
                {"text": "❌ Refuser",  "callback_data": "refuse_"}
            ]
        ]
    }

    tg_resp = tg_send_message(CHAT_WAITER, text, reply_markup=keyboard)
    message_id = tg_resp["result"]["message_id"]

    with pending_lock:
        pending_orders[message_id] = {
            "table": table_number,
            "items": items,
            "total": total,
            "waiter_chat_id": CHAT_WAITER,
            "original_text": text
        }

    return jsonify({"status": "ok", "telegram_message_id": message_id})

@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    update = request.get_json(force=True)
    if not update:
        return jsonify({"ok": False})

    if "callback_query" not in update:
        return jsonify({"ok": True})

    cq = update["callback_query"]
    data = cq["data"]
    message_id = cq["message"]["message_id"]
    chat_id = cq["message"]["chat"]["id"]

    action = data.split("_")[0]

    with pending_lock:
        order = pending_orders.pop(message_id, None)

    if not order:
        requests.post(f"{BASE_URL}/answerCallbackQuery", json={
            "callback_query_id": cq["id"],
            "text": "Cette commande a déjà été traitée.",
            "show_alert": True
        })
        return jsonify({"ok": True})

    if action == "accept":
        kitchen_text = f"✅ Commande acceptée (Table {order['table']}):\n{order['original_text']}"
        tg_send_message(CHAT_KITCHEN, kitchen_text)
        tg_edit_message_reply_markup(chat_id, message_id, reply_markup={"inline_keyboard": []})
        requests.post(f"{BASE_URL}/answerCallbackQuery", json={
            "callback_query_id": cq["id"],
            "text": "Commande acceptée et envoyée en cuisine.",
            "show_alert": False
        })

    elif action == "refuse":
        tg_edit_message_reply_markup(chat_id, message_id, reply_markup={"inline_keyboard": []})
        requests.post(f"{BASE_URL}/answerCallbackQuery", json={
            "callback_query_id": cq["id"],
            "text": "Commande refusée.",
            "show_alert": True
        })

    return jsonify({"ok": True})

@app.route("/")
def index():
    return "Pizza Royal Bot Backend is running."

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
