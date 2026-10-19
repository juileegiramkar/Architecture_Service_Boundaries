"""Inventory Service - owns the stock levels of every item in the store.

    GET /inventory/<item_id>/availability?quantity=N    (Query: is the item available?)
    GET /inventory                                       (Query: list all stock levels)
    GET /health

The availability check always answers 200 OK with "status": "available" or
"status": "unavailable". Being out of stock is a normal answer, not an error.

    400 Bad Request   quantity is not an integer >= 1
    404 Not Found     item_id is not in the catalog

Stock is kept in memory (sample data below). No other service reads or writes
this dictionary directly. They have to ask through the API.
"""
import argparse
import logging
import os
import time

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

# Sample data owned by this service: item_id -> units in stock.
STOCK = {
    "laptop": 10,
    "monitor": 3,
    "mouse": 25,
    "keyboard": 0,
}

log = logging.getLogger("inventory-service")
app = Flask(__name__)
app.json.sort_keys = False  # keep response fields in the order they are written
app.json.compact = False    # pretty-print JSON so curl output is readable
started_at = time.monotonic()


def error(status, code, message):
    return jsonify({"error": code, "message": message}), status


@app.errorhandler(HTTPException)
def http_error(exc):
    # Unknown routes / wrong methods get a JSON body too, instead of Flask's HTML page.
    return error(exc.code, exc.name.upper().replace(" ", "_"), exc.description)


@app.get("/inventory/<item_id>/availability")
def check_availability(item_id):
    raw_quantity = request.args.get("quantity", "1")
    try:
        quantity = int(raw_quantity)
    except ValueError:
        quantity = 0
    if quantity < 1:
        return error(400, "INVALID_QUANTITY", "quantity must be an integer >= 1")

    if item_id not in STOCK:
        log.info("availability check item_id=%s quantity=%d -> 404 item not found", item_id, quantity)
        return error(404, "ITEM_NOT_FOUND", f"Item '{item_id}' does not exist")

    in_stock = STOCK[item_id]
    status = "available" if in_stock >= quantity else "unavailable"
    log.info("availability check item_id=%s quantity=%d in_stock=%d -> %s",
             item_id, quantity, in_stock, status)
    return jsonify({
        "item_id": item_id,
        "requested_quantity": quantity,
        "in_stock": in_stock,
        "status": status,
    }), 200


@app.get("/inventory")
def list_stock():
    return jsonify({"items": STOCK}), 200


@app.get("/health")
def health():
    return jsonify({
        "service": "inventory-service",
        "status": "up",
        "pid": os.getpid(),
        "uptime_seconds": round(time.monotonic() - started_at, 1),
    }), 200


def main():
    parser = argparse.ArgumentParser(description="Inventory Service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, datefmt="%H:%M:%S",
                        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    log.info("Inventory Service on http://%s:%d (pid %d) stock=%s",
             args.host, args.port, os.getpid(), STOCK)
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
