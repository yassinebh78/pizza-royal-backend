import os
import json
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from threading import Lock
from datetime import date

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# Environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_WAITER = os.getenv("CHAT_WAITER")
CHAT_KITCHEN = os.getenv("CHAT_KITCHEN")

# Per‑day order number
order_lock = Lock()
current_day = date.today().isoformat()
today_counter = 0

# Pending orders
pending_lock = Lock()
pending_orders = 0

def get_today_order_number():
    """Returns a monotonically increasing order number that resets at midnight."""
    global current_day, today_counter
    with order_lock:
        today = date.today().isoformat()
        if today != current_day:
            current_day = today
            today_counter = 0
        today_counter += 1
        return today_counter

def calc_wait_time(count: int) -> int:
    """Step‑wise rule: <7 →15 min, 7‑10 →30 min, >10 →45 min."""
    if count < 7:
        return 15
    if count <= 10:
        return 30
    return 45

# ===== Routes =====
@app.route('/')
def index():
    return "Pizza Royal Bot Backend is running."

@app.route('/send-order', methods=['POST'])
def send_order():
    """Receive order from frontend, notify waiter with Accept/Refuse buttons."""
    if not request.is_json:
        return jsonify({"error": "Invalid JSON"}), 400

    data = request.get_json()
    items = data.get('items', [])
    total = float(data.get('total', 0))
    table_number = data.get('table_number')

    if not items or total <= 0 or table_number is None:
        return jsonify({"error": "Missing data"}), 400

    # 1️⃣ Generate order number
    order_no = get_today_order_number()
    
    # 2️⃣ Build order text
    lines = [f"🛒 Commande #{order_no} - Table {table_number}:"]
    for it in items:
        name = it.get('name', 'Inconnu')
        qty = it.get('qty', 0)
        price = float(it.get('price', 0))
        notes = it.get('notes', '')
        line = f"• {name} x{qty} — {price:.1f}DT"
        if notes: line += f" ({notes})"
        lines.append(line)
    lines.extend(["", f"💰 Total: {total:.1f}DT", ""])
    tg_text = "\n".join(lines)  # ← FIXED: single \, not double \\

    # 3️⃣ Send to waiter WITH Accept/Refuse buttons
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ Accepter", "callback_data": f"accept_{order_no}"},
                {"text": "❌ Refuser", "callback_data": f"refuse_{order_no}"}
            ]
        ]
    }
    
    try:
        tg_resp = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_WAITER,
                "text": tg_text,
                "reply_markup": keyboard
            },
            timeout=10
        )
        tg_resp.raise_for_status()
        message_id = tg_resp.json()['result']['message_id']
    except Exception as e:
        app.logger.error(f"Telegram failed: {e}")
        return jsonify({"error": "Telegram notification failed"}), 500

    # 4️⃣ Update pending counter & return JSON
    with pending_lock:
        pending_orders += 1
        wait_min = calc_wait_time(pending_orders)

    return jsonify({
        "status": "ok",
        "telegram_message_id": message_id,
        "order_no": order_no,
        "estimated_wait_min": wait_min
    })

@app.route('/call-waiter', methods=['POST'])
def call_waiter():
    """Frontend‑initiated waiter call."""
    if not request.is_json:
        return jsonify({"error": "Invalid JSON"}), 400
    data = request.get_json()
    table_number = data.get('table_number')
    if table_number is None:
        return jsonify({"error": "Missing table_number"}), 400
    text = f"🔔 La table {table_number} appelle le serveur."
    try:
        tg_resp = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_WAITER, "text": text},
            timeout=10
        )
        tg_resp.raise_for_status()
        tg_data = tg_resp.json()
        if not tg_data.get('ok'):
            raise Exception(f"Telegram error: {tg_data}")
    except Exception as e:
        app.logger.error(f"Failed to send waiter call: {e}")
        return jsonify({"error": "Failed to notify waiter"}), 500
    return jsonify({"status": "ok"})

@app.route('/order-done', methods=['POST'])
def order_done():
    """
    Called by the kitchen when an order has been finished.
    Decrements the pending‑order counter so the next wait‑time estimate stays accurate.
    """
    with pending_lock:
        global pending_orders
        if pending_orders > 0:
            pending_orders -= 1
            app.logger.info(f"Order completed. Pending now: {pending_orders}")
    return jsonify({"status": "ok"})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
