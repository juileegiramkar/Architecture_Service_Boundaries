"""Notification Service - notifies users when something happens to their order.

    POST /events           receives events, e.g. OrderCreated (Event)
    GET  /notifications    (Query: list the notifications that were "sent")
    GET  /health

Sending is simulated: the notification is logged and kept in memory instead
of sending a real email/SMS. The Order Service does not wait for this service,
so an order is created even if this service is down.
"""
import argparse
import logging
import os
import threading
import time
from datetime import datetime, timezone

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

log = logging.getLogger("notification-service")
app = Flask(__name__)
app.json.sort_keys = False  # keep response fields in the order they are written
app.json.compact = False    # pretty-print JSON so curl output is readable
started_at = time.monotonic()

# Data owned by this service. Only this process reads or writes it.
notifications = []
seen_event_ids = set()
lock = threading.Lock()


def error(status, code, message):
    return jsonify({"error": code, "message": message}), status


@app.errorhandler(HTTPException)
def http_error(exc):
    # Unknown routes / wrong methods get a JSON body too, instead of Flask's HTML page.
    return error(exc.code, exc.name.upper().replace(" ", "_"), exc.description)


@app.post("/events")
def receive_event():
    event = request.get_json(silent=True)
    if not isinstance(event, dict) or not isinstance(event.get("data"), dict):
        return error(400, "INVALID_EVENT", "Event must be a JSON object with a data object")
    if event.get("event_type") != "OrderCreated":
        # Events this service does not care about are accepted and ignored.
        return jsonify({"accepted": True, "handled": False}), 202

    data = event["data"]
    with lock:
        if event.get("event_id") in seen_event_ids:  # the same event delivered twice
            return jsonify({"accepted": True, "handled": False, "reason": "duplicate"}), 202
        seen_event_ids.add(event.get("event_id"))
        notification = {
            "notification_id": f"NTF-{len(notifications) + 1:04d}",
            "customer_id": data.get("customer_id"),
            "order_id": data.get("order_id"),
            "channel": "email (simulated)",
            "message": f"Your order {data.get('order_id')} for {data.get('quantity')} x "
                       f"{data.get('item_id')} has been created.",
            "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        notifications.append(notification)
    log.info("OrderCreated %s -> notified customer %s: %s",
             data.get("order_id"), data.get("customer_id"), notification["message"])
    return jsonify({"accepted": True, "handled": True,
                    "notification_id": notification["notification_id"]}), 202


@app.get("/notifications")
def list_notifications():
    with lock:
        return jsonify({"notifications": list(notifications)}), 200


@app.get("/health")
def health():
    return jsonify({
        "service": "notification-service",
        "status": "up",
        "pid": os.getpid(),
        "uptime_seconds": round(time.monotonic() - started_at, 1),
    }), 200


def main():
    parser = argparse.ArgumentParser(description="Notification Service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8002)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, datefmt="%H:%M:%S",
                        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    log.info("Notification Service on http://%s:%d (pid %d)", args.host, args.port, os.getpid())
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
