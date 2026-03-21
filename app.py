import os
import json
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from threading import Lock
from datetime import date

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# ===== Environment variables (set in Render) =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_WAITER = os.getenv("CHAT_WAITER")   # waiter chat ID
CHAT_KITCHEN = os.getenv("CHAT_KITCHEN") # kitchen chat ID (if you forward elsewhere)

# ===== Per‑day order number =====
order_lock = Lock()
current_day = date.today().isoformat()
today_counter = 0

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

# ===== Pending orders (for wait‑time estimation) =====
pending_lock = Lock()
pending_orders = 0   # how many orders are currently waiting for preparation

def calc_wait_time(count: int) -> int:
    """Step‑wise rule: <7 →15 min, 7‑10 →30 min, >10 →45 min."""
    if count < 7:
        return 15
    if count <= 10:   # 7 … 10 inclusive
        return 30
    return 45         # > 10

# ===== Routes =====
@app.route('/')
def index():
    return "Pizza Royal Bot Backend is running."

@app.route('/send-order', methods=['POST'])
def send_order():
    """Receive order from frontend, notify waiter, and return order number + wait estimate."""
    if not request.is_json:
        return jsonify({"error": "Invalid JSON"}), 400

    data = request.get_json()
    items = data.get('items', [])
    total = float(data.get('total', 0))
    table_number = data.get('table_number')

    if not items or total <= 0 or table_number is None:
        return jsonify({"error": "Missing items, total, or table_number"}), 400

    # Build Telegram message for waiter/kitchen
    order_no = get_today_order_number()
    lines = [f"🛒 Commande - Table {table_number}:"]
    for it in items:
        name = it.get('name', 'Sans nom')
        qty = it.get('qty', 0)
        price = float(it.get('price', 0))
        notes = it.get('notes', '').strip()
        line_total = qty * price
        line = f"• {name} x{qty} — {price:.1f}DT"
        if notes:
            line += f" ({notes})"
        lines.append(line)
    lines.append("")
    lines.append(f"💰 Total : {total:.1f}DT")
    tg_text = "\n".join(lines)

    # Send to waiter (Telegram)
    try:
        tg_resp = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_WAITER, "text": tg_text},
            timeout=10
        )
        tg_resp.raise_for_status()
        tg_data = tg_resp.json()
        if not tg_data.get('ok'):
            raise Exception(f"Telegram error: {tg_data}")
        message_id = tg_data['result']['message_id']
    except Exception as e:
        app.logger.error(f"Failed to notify waiter: {e}")
        return jsonify({"error": "Failed to notify waiter"}), 500

    # Update pending‑order counter and compute wait time
    with pending_lock:
        pending_orders += 1
        wait_min = calc_wait_time(pending_orders)

    # Respond to frontend
    return jsonify({
        "status": "ok",
        "telegram_message_id": message_id,
        "order_no": order_no,
        "estimated_wait_min": wait_min   # <-- new field for the frontend
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
    if not request.is_json:
        return jsonify({"error": "Invalid JSON"}), 400
    # No need to inspect the payload; we just decrement the counter.
    with pending_lock:
        global pending_orders
        if pending_orders > 0:
            pending_orders -= 1
        # Optional logging for debugging:
        # app.logger.info(f"Order done, pending now: {pending_orders}")
    return jsonify({"status": "ok"})

# ===== Entrypoint =====
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
