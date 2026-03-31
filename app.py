import os
import json
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from threading import Lock
from datetime import date
from datetime import datetime, timedelta
from math import isfinite

# ...

pending_orders = {}
pending_lock = Lock()

# Daily order counter
current_day = date.today().isoformat()
today_counter = 0


app = Flask(__name__)


def parse_allowed_origins():
    raw_value = os.getenv("ALLOWED_ORIGINS", "*").strip()
    if raw_value == "*":
        return "*"
    return [origin.strip() for origin in raw_value.split(",") if origin.strip()]


# Keep CORS configurable so deployment can be tightened without code changes.
CORS(app, resources={r"/*": {"origins": parse_allowed_origins()}})

# --- Configuration from environment variables ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_WAITER = os.getenv("CHAT_WAITER")   # waiter chat id
CHAT_KITCHEN = os.getenv("CHAT_KITCHEN") # kitchen chat id

if not all([BOT_TOKEN, CHAT_WAITER, CHAT_KITCHEN]):
    raise RuntimeError("Missing required env vars: BOT_TOKEN, CHAT_WAITER, CHAT_KITCHEN")

BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

# In‑memory store for pending orders
pending_orders = {}
pending_lock = Lock()
order_statuses = {}


def is_valid_table_number(value):
    table = str(value).strip()
    if not table or len(table) > 10:
        return False
    return table.isdigit()


def parse_positive_number(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(parsed) or parsed < 0:
        return None
    return parsed


def validate_order_items(items):
    if not isinstance(items, list) or not items:
        return None, "Order must include at least one item."

    validated_items = []
    computed_total = 0.0

    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            return None, f"Item #{index} is invalid."

        name = str(item.get("name", "")).strip()
        if not name or len(name) > 120:
            return None, f"Item #{index} has an invalid name."

        qty = item.get("qty")
        if not isinstance(qty, int) or qty <= 0 or qty > 50:
            return None, f"Item #{index} has an invalid quantity."

        price = parse_positive_number(item.get("price"))
        if price is None:
            return None, f"Item #{index} has an invalid price."

        note = str(item.get("note", "")).strip()
        if len(note) > 300:
            return None, f"Item #{index} note is too long."

        raw_supplements = item.get("supplements", [])
        if raw_supplements is None:
            raw_supplements = []
        if not isinstance(raw_supplements, list):
            return None, f"Item #{index} supplements are invalid."

        validated_supplements = []
        supplements_total = 0.0
        for supplement_index, supplement in enumerate(raw_supplements, start=1):
            if not isinstance(supplement, dict):
                return None, f"Item #{index} supplement #{supplement_index} is invalid."

            supplement_name = str(supplement.get("name", "")).strip()
            if not supplement_name or len(supplement_name) > 80:
                return None, f"Item #{index} supplement #{supplement_index} has an invalid name."

            supplement_price = parse_positive_number(supplement.get("price"))
            if supplement_price is None:
                return None, f"Item #{index} supplement #{supplement_index} has an invalid price."

            validated_supplements.append({
                "name": supplement_name,
                "price": supplement_price
            })
            supplements_total += supplement_price

        line_total = (price * qty) + supplements_total
        computed_total += line_total

        validated_items.append({
            "name": name,
            "qty": qty,
            "price": price,
            "note": note,
            "supplements": validated_supplements,
            "line_total": round(line_total, 2)
        })

    return {
        "items": validated_items,
        "total": round(computed_total, 2)
    }, None


def next_order_number():
    """Return today's next order number and reset counter if day changed."""
    global current_day, today_counter
    today_str = date.today().isoformat()
    if today_str != current_day:
        current_day = today_str
        today_counter = 0
    today_counter += 1
    return today_counter


def build_waiting_time_keyboard(order_no):
    return {
        "inline_keyboard": [
            [
                {"text": "15min", "callback_data": f"wait_15_{order_no}"},
                {"text": "30min", "callback_data": f"wait_30_{order_no}"},
                {"text": "45min", "callback_data": f"wait_45_{order_no}"}
            ]
        ]
    }


def save_order_status(order_no, **updates):
    order_no = int(order_no)
    current_status = order_statuses.get(order_no, {})
    current_status.update(updates)
    order_statuses[order_no] = current_status
    return current_status

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


@app.route("/send-order", methods=["POST"])
def receive_order():
    if not request.is_json:
        return jsonify({"error": "JSON required"}), 400

    data = request.get_json()
    table_number = str(data.get("table", "")).strip()
    notes = str(data.get("notes", "")).strip()

    if not is_valid_table_number(table_number):
        return jsonify({"error": "Invalid table number"}), 400

    validated_order, validation_error = validate_order_items(data.get("items", []))
    if validation_error:
        return jsonify({"error": validation_error}), 400

    items = validated_order["items"]
    total = validated_order["total"]

    # NEW: get today's order number
    order_no = next_order_number()

    lines = [f"Commande #{order_no} - Table {table_number}:"]
    for item in items:
        supplement_text = ""
        if item["supplements"]:
            supplement_names = ", ".join(supplement["name"] for supplement in item["supplements"])
            supplement_text = f" (+ {supplement_names})"
        lines.append(f"- {item['name']}{supplement_text} x{item['qty']} - {item['line_total']:.1f}DT")
        if item["note"]:
            lines.append(f"  Note: {item['note']}")
    lines.append(f"\nTotal: {total:.1f}DT")
    if notes:
        lines.append(f"\nNotes: {notes}")
    text = "\n".join(lines)

    keyboard = {
        "inline_keyboard": [
            [
                {"text": "Accept", "callback_data": f"accept_{order_no}"},
                {"text": "Refuse", "callback_data": f"refuse_{order_no}"}
            ]
        ]
    }

    tg_resp = tg_send_message(CHAT_WAITER, text, reply_markup=keyboard)
    message_id = tg_resp["result"]["message_id"]

    with pending_lock:
        pending_orders[message_id] = {
            "order_no": order_no,
            "table": table_number,
            "items": items,
            "total": total,
            "notes": notes,
            "waiter_chat_id": CHAT_WAITER,
            "original_text": text,
            "status": "received",
            "waiting_time_minutes": None
        }
        save_order_status(
            order_no,
            table=table_number,
            status="received",
            waiting_time_minutes=None,
            total=total,
            updated_at=datetime.utcnow().isoformat() + "Z"
        )

    return jsonify({
        "status": "ok",
        "telegram_message_id": message_id,
        "order_no": order_no,
        "total": total
    })

    # Build message text (visible to waiter)
    lines = [f"🛒 Commande #{order_no} - Table {table_number}:"]
    for it in items:
        lines.append(f"• {it['name']} x{it['qty']} — {it['price']:.1f}DT")
    lines.append(f"\n💰 Total : {total:.1f}DT")
    if notes:
        lines.append(f"\n📝 Notes : {notes}")
    text = "\n".join(lines)

    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ Accepter", "callback_data": f"accept_{order_no}"},
                {"text": "❌ Refuser",  "callback_data": f"refuse_{order_no}"}
            ]
        ]
    }

    tg_resp = tg_send_message(CHAT_WAITER, text, reply_markup=keyboard)
    message_id = tg_resp["result"]["message_id"]

    with pending_lock:
        pending_orders[message_id] = {
            "order_no": order_no,              # store it
            "table": table_number,
            "items": items,
            "total": total,
            "notes": notes,
            "waiter_chat_id": CHAT_WAITER,
            "original_text": text
        }

    # Include order_no so client can see it
    return jsonify({
        "status": "ok",
        "telegram_message_id": message_id,
        "order_no": order_no
    })



@app.route("/call-waiter", methods=["POST"])
def receive_call_waiter():
    """Called by the website when customer presses the bell."""
    if not request.is_json:
        return jsonify({"error": "JSON required"}), 400

    data = request.get_json()
    table_number = str(data.get("table", "")).strip()
    if not is_valid_table_number(table_number):
        return jsonify({"error": "Invalid table number"}), 400

    tg_send_message(CHAT_WAITER, f"Table {table_number} is calling the waiter.")
    return jsonify({"status": "ok"})

    table_number = data.get("table", "?")
    text = f"🔔 La table {table_number} appelle le serveur."

    tg_send_message(CHAT_WAITER, text)
    return jsonify({"status": "ok"})


@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    update = request.get_json(force=True)
    if not update:
        return jsonify({"ok": False})

    if "callback_query" not in update:
        return jsonify({"ok": True})

    cq = update["callback_query"]
    data = cq["data"]              # e.g. "accept_5"
    message_id = cq["message"]["message_id"]
    chat_id = cq["message"]["chat"]["id"]

    parts = data.split("_")
    action = parts[0]
    order_no = parts[-1] if len(parts) > 1 else "?"

    with pending_lock:
        order = pending_orders.get(message_id)

    if order and action == "accept" and order.get("status") == "received":
        kitchen_text = (
            f"Commande #{order['order_no']} acceptee "
            f"(Table {order['table']}):\n{order['original_text']}"
        )
        tg_send_message(CHAT_KITCHEN, kitchen_text)

        with pending_lock:
            order["status"] = "accepted"
            save_order_status(
                order["order_no"],
                table=order["table"],
                status="accepted",
                waiting_time_minutes=None,
                total=order["total"],
                updated_at=datetime.utcnow().isoformat() + "Z"
            )

        tg_edit_message_reply_markup(chat_id, message_id, reply_markup=build_waiting_time_keyboard(order["order_no"]))
        requests.post(f"{BASE_URL}/answerCallbackQuery", json={
            "callback_query_id": cq["id"],
            "text": f"Commande #{order['order_no']} envoyee en cuisine. Choisissez un delai.",
            "show_alert": False
        })
        return jsonify({"ok": True})

    if order and action == "wait":
        if len(parts) != 3:
            requests.post(f"{BASE_URL}/answerCallbackQuery", json={
                "callback_query_id": cq["id"],
                "text": "Delai invalide.",
                "show_alert": True
            })
            return jsonify({"ok": True})

        wait_minutes = parts[1]
        if order.get("status") != "accepted":
            requests.post(f"{BASE_URL}/answerCallbackQuery", json={
                "callback_query_id": cq["id"],
                "text": "Veuillez accepter la commande avant de choisir un delai.",
                "show_alert": True
            })
            return jsonify({"ok": True})

        if wait_minutes not in {"15", "30", "45"}:
            requests.post(f"{BASE_URL}/answerCallbackQuery", json={
                "callback_query_id": cq["id"],
                "text": "Delai invalide.",
                "show_alert": True
            })
            return jsonify({"ok": True})

        estimated_ready_at = (datetime.utcnow() + timedelta(minutes=int(wait_minutes))).isoformat() + "Z"
        with pending_lock:
            order["status"] = "preparing"
            order["waiting_time_minutes"] = int(wait_minutes)
            pending_orders.pop(message_id, None)
            save_order_status(
                order["order_no"],
                table=order["table"],
                status="preparing",
                waiting_time_minutes=int(wait_minutes),
                estimated_ready_at=estimated_ready_at,
                total=order["total"],
                updated_at=datetime.utcnow().isoformat() + "Z"
            )

        tg_edit_message_reply_markup(chat_id, message_id, reply_markup={"inline_keyboard": []})
        requests.post(f"{BASE_URL}/answerCallbackQuery", json={
            "callback_query_id": cq["id"],
            "text": f"Delai de {wait_minutes} minutes enregistre.",
            "show_alert": False
        })
        return jsonify({"ok": True})

    if order and action == "accept":
        requests.post(f"{BASE_URL}/answerCallbackQuery", json={
            "callback_query_id": cq["id"],
            "text": "Cette commande ne peut plus etre acceptee.",
            "show_alert": True
        })
        return jsonify({"ok": True})

    if order and action == "refuse":
        with pending_lock:
            pending_orders.pop(message_id, None)
            save_order_status(
                order["order_no"],
                table=order["table"],
                status="refused",
                waiting_time_minutes=None,
                total=order["total"],
                updated_at=datetime.utcnow().isoformat() + "Z"
            )

        tg_edit_message_reply_markup(chat_id, message_id, reply_markup={"inline_keyboard": []})
        requests.post(f"{BASE_URL}/answerCallbackQuery", json={
            "callback_query_id": cq["id"],
            "text": f"Commande #{order['order_no']} refusee.",
            "show_alert": True
        })
        return jsonify({"ok": True})

    if not order:
        requests.post(f"{BASE_URL}/answerCallbackQuery", json={
            "callback_query_id": cq["id"],
            "text": "Cette commande a déjà été traitée.",
            "show_alert": True
        })
        return jsonify({"ok": True})

    if action == "accept":
        kitchen_text = (
            f"✅ Commande #{order['order_no']} acceptée "
            f"(Table {order['table']}):\n{order['original_text']}"
        )
        tg_send_message(CHAT_KITCHEN, kitchen_text)

        tg_edit_message_reply_markup(chat_id, message_id, reply_markup={"inline_keyboard": []})
        requests.post(f"{BASE_URL}/answerCallbackQuery", json={
            "callback_query_id": cq["id"],
            "text": f"Commande #{order['order_no']} envoyée en cuisine.",
            "show_alert": False
        })

    elif action == "refuse":
        tg_edit_message_reply_markup(chat_id, message_id, reply_markup={"inline_keyboard": []})
        requests.post(f"{BASE_URL}/answerCallbackQuery", json={
            "callback_query_id": cq["id"],
            "text": f"Commande #{order['order_no']} refusée.",
            "show_alert": True
        })

    return jsonify({"ok": True})

@app.route("/order-status/<int:order_no>", methods=["GET"])
def order_status(order_no):
    with pending_lock:
        order = order_statuses.get(order_no)

    if not order:
        return jsonify({"error": "Order not found"}), 404

    return jsonify(order)


@app.route("/")
def index():
    return "Pizza Royal Bot Backend is running."


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
