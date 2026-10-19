"""Order Service - creates and tracks orders.

    POST /orders           (Command: create an order)
                           body: {"customer_id": "c-101", "item_id": "laptop", "quantity": 2}
    GET  /orders           (Query: list orders)
    GET  /orders/<id>      (Query: track one order)
    GET  /health

Before an order is created, the Order Service asks the Inventory Service
(GET {inventory-url}/inventory/<item_id>/availability?quantity=N) with a
timeout (default 2s). The answer is mapped to an HTTP status:

    201 Created              Inventory said "available" -> order created
    400 Bad Request          request body is invalid
    404 Not Found            Inventory does not know the item
    409 Conflict             Inventory said "unavailable" -> order not created
    502 Bad Gateway          Inventory answered with something unexpected
    503 Service Unavailable  Inventory could not be reached (service stopped / connection refused)
    504 Gateway Timeout      Inventory accepted the connection but did not answer within the timeout

A failed dependency never crashes this process. The request gets an error
response and the service keeps serving the next request.

After an order is created, an OrderCreated event is published to the
Notification Service in a background thread (fire-and-forget). If the
Notification Service is down, the order is still created.
"""
import argparse
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

import requests
from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

log = logging.getLogger("order-service")
app = Flask(__name__)
app.json.sort_keys = False  # keep response fields in the order they are written
app.json.compact = False    # pretty-print JSON so curl output is readable

inventory_url = "http://127.0.0.1:8001"
notification_url = "http://127.0.0.1:8002"
timeout_seconds = 2.0
started_at = time.monotonic()

# Data owned by this service. Only this process reads or writes it.
orders = {}
lock = threading.Lock()
next_order_number = 1
stats = {"orders_created": 0, "orders_rejected": 0, "inventory_failures": 0}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def error(status, code, message, **extra):
    return jsonify({"error": code, "message": message, **extra}), status


def count(stat):
    with lock:
        stats[stat] += 1


@app.errorhandler(HTTPException)
def http_error(exc):
    # Unknown routes / wrong methods get a JSON body too, instead of Flask's HTML page.
    return error(exc.code, exc.name.upper().replace(" ", "_"), exc.description)


@app.errorhandler(Exception)
def unexpected_error(exc):
    # Last line of defense: log it and answer 500, the process keeps running.
    log.exception("unexpected error while handling %s %s", request.method, request.path)
    return error(500, "INTERNAL_ERROR", "Unexpected error in the Order Service")


def validate(body):
    if not isinstance(body, dict):
        return "Request body must be a JSON object"
    for field in ("customer_id", "item_id"):
        value = body.get(field)
        if not isinstance(value, str) or not value.strip():
            return f"{field} must be a non-empty string"
    quantity = body.get("quantity")
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 1:
        return "quantity must be an integer >= 1"
    return None


@app.post("/orders")
def create_order():
    global next_order_number
    body = request.get_json(silent=True)
    problem = validate(body)
    if problem:
        return error(400, "INVALID_REQUEST", problem)
    customer_id, item_id, quantity = body["customer_id"], body["item_id"], body["quantity"]
    log.info("POST /orders customer_id=%s item_id=%s quantity=%d", customer_id, item_id, quantity)

    # Order Service -> Inventory Service: ask whether the item is available.
    url = f"{inventory_url}/inventory/{quote(item_id, safe='')}/availability"
    try:
        reply = requests.get(url, params={"quantity": quantity}, timeout=timeout_seconds)
    except requests.exceptions.ReadTimeout:
        # Connected, but no answer in time: Inventory is up but too slow.
        count("inventory_failures")
        log.warning("Inventory Service did not answer within %.1fs -> 504, order not created", timeout_seconds)
        return error(504, "INVENTORY_SERVICE_TIMEOUT",
                     "Order not created: the Inventory Service did not respond in time. "
                     "Please try again later.")
    except requests.exceptions.RequestException as exc:
        # Could not connect at all (connection refused, or no connection within the
        # timeout): Inventory is down. On Windows a refused localhost connection takes
        # about 2s to fail, so this can arrive as ConnectTimeout or ConnectionError.
        count("inventory_failures")
        log.warning("Inventory Service unreachable at %s (%s) -> 503, order not created",
                    inventory_url, type(exc).__name__)
        response, status = error(503, "INVENTORY_SERVICE_UNAVAILABLE",
                                 "Order not created: the Inventory Service is unavailable, so item "
                                 "availability could not be checked. Please try again later.")
        response.headers["Retry-After"] = "10"
        return response, status

    try:
        answer = reply.json()
    except ValueError:
        answer = {}

    if reply.status_code == 404:
        count("orders_rejected")
        log.info("Inventory: item %s not found -> 404", item_id)
        return error(404, "ITEM_NOT_FOUND", f"Order not created: item '{item_id}' does not exist")
    if reply.status_code == 400:
        count("orders_rejected")
        return error(400, "INVALID_REQUEST", answer.get("message", "Inventory rejected the request"))
    if reply.status_code != 200 or answer.get("status") not in ("available", "unavailable"):
        count("inventory_failures")
        log.warning("unexpected answer from Inventory: HTTP %d %s -> 502", reply.status_code, answer)
        return error(502, "INVENTORY_BAD_RESPONSE",
                     "Order not created: the Inventory Service returned an unexpected response")

    if answer["status"] == "unavailable":
        count("orders_rejected")
        log.info("Inventory: %s unavailable (in_stock=%s, requested=%d) -> 409, order not created",
                 item_id, answer.get("in_stock"), quantity)
        return error(409, "ITEM_UNAVAILABLE",
                     f"Order not created: only {answer.get('in_stock')} unit(s) of '{item_id}' "
                     f"in stock, {quantity} requested",
                     inventory={"status": "unavailable", "in_stock": answer.get("in_stock")})

    with lock:
        order_id = f"ORD-{next_order_number:04d}"
        next_order_number += 1
        order = {
            "order_id": order_id,
            "customer_id": customer_id,
            "item_id": item_id,
            "quantity": quantity,
            "status": "CREATED",
            "created_at": now(),
        }
        orders[order_id] = order
        stats["orders_created"] += 1
    log.info("Inventory: %s available (in_stock=%s) -> 201, created %s",
             item_id, answer.get("in_stock"), order_id)

    publish_order_created(order)
    return jsonify({
        "message": "Order created",
        "order": order,
        "inventory": {"status": "available", "in_stock": answer.get("in_stock")},
    }), 201, {"Location": f"/orders/{order_id}"}


def publish_order_created(order):
    """Publish the OrderCreated event without making the client wait for it."""
    if not notification_url:
        return
    event = {
        "event_id": str(uuid.uuid4()),
        "event_type": "OrderCreated",
        "occurred_at": order["created_at"],
        "data": {key: order[key] for key in ("order_id", "customer_id", "item_id", "quantity")},
    }
    threading.Thread(target=send_event, args=(event,), daemon=True).start()


def send_event(event):
    order_id = event["data"]["order_id"]
    try:
        requests.post(f"{notification_url}/events", json=event, timeout=timeout_seconds).raise_for_status()
        log.info("published OrderCreated event for %s", order_id)
    except requests.exceptions.RequestException as exc:
        # The order already exists. A lost notification must not undo or fail it.
        log.warning("OrderCreated event for %s not delivered (%s); order is unaffected",
                    order_id, type(exc).__name__)


@app.get("/orders")
def list_orders():
    with lock:
        return jsonify({"orders": list(orders.values())}), 200


@app.get("/orders/<order_id>")
def get_order(order_id):
    with lock:
        order = orders.get(order_id)
    if order is None:
        return error(404, "ORDER_NOT_FOUND", f"Order '{order_id}' does not exist")
    return jsonify(order), 200


@app.get("/health")
def health():
    with lock:
        counters = dict(stats)
    return jsonify({
        "service": "order-service",
        "status": "up",
        "pid": os.getpid(),
        "uptime_seconds": round(time.monotonic() - started_at, 1),
        **counters,
    }), 200


def main():
    global inventory_url, notification_url, timeout_seconds
    parser = argparse.ArgumentParser(description="Order Service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--inventory-url", default=os.environ.get("INVENTORY_URL", inventory_url))
    parser.add_argument("--notification-url", default=os.environ.get("NOTIFICATION_URL", notification_url),
                        help='where OrderCreated events are sent; "" turns events off')
    parser.add_argument("--timeout", type=float, default=timeout_seconds,
                        help="seconds to wait for the Inventory Service")
    args = parser.parse_args()
    inventory_url = args.inventory_url.rstrip("/")
    notification_url = args.notification_url.rstrip("/")
    timeout_seconds = args.timeout

    logging.basicConfig(level=logging.INFO, datefmt="%H:%M:%S",
                        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    log.info("Order Service on http://%s:%d (pid %d) inventory=%s notification=%s timeout=%.1fs",
             args.host, args.port, os.getpid(), inventory_url, notification_url or "off", timeout_seconds)
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
