#!/usr/bin/env bash
# Runs the whole demo and saves the curl output and service logs to evidence/.
#
#   bash scripts/demo.sh
#
# 1. start Inventory, Order and Notification as three separate processes
# 2. query Inventory directly, then place orders (available, unavailable, bad input)
# 3. failure test: stop the Inventory Service and submit another order
#    -> Order Service answers 503 and keeps running (same pid, health still "up")
# 4. restart Inventory -> the same Order Service process creates orders again
#
# Works in Git Bash on Windows, and on macOS/Linux. Needs the .venv from the README.
set -u
cd "$(dirname "$0")/.."
export PYTHONUNBUFFERED=1
export PATH="$PWD/.venv/Scripts:$PWD/.venv/bin:$PATH"
DIR=evidence
rm -rf "$DIR" && mkdir -p "$DIR"

start() {  # LOGFILE CMD... -> prints the background pid
  local log=$1; shift
  "$@" > "$log" 2>&1 &
  echo $!
}
port_open() { (echo > "/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
wait_port() {
  for _ in $(seq 1 100); do port_open "$1" && return 0; sleep 0.2; done
  echo "port $1 never opened" >&2; exit 1
}
stop() {  # PID PORT
  if [[ -r /proc/$1/winpid ]]; then  # Git Bash: kill the venv launcher and the interpreter under it
    taskkill //F //T //PID "$(cat "/proc/$1/winpid")" > /dev/null 2>&1
  else
    kill "$1" 2>/dev/null
  fi
  for _ in $(seq 1 50); do port_open "$2" || return 0; sleep 0.1; done
}
run() {  # reads one command from stdin, shows it and its output, and appends both to $OUT
  local cmd; cmd=$(cat)
  printf '$ %s\n' "$cmd" | tee -a "$OUT"
  bash -c "$cmd" 2>&1 | tee -a "$OUT"
  printf '\n\n' | tee -a "$OUT"
}
order() {  # ITEM QUANTITY [extra curl args]
  run <<CMD
curl -s -i ${3:+$3 }-X POST http://127.0.0.1:8000/orders -H "Content-Type: application/json" -d '{"customer_id": "c-101", "item_id": "$1", "quantity": $2}'
CMD
}
note() { printf '# %s\n' "$*" | tee -a "$OUT"; }

for port in 8000 8001 8002; do
  port_open $port && { echo "port $port is already in use, stop that process first" >&2; exit 1; }
done

INV=""; ORD=""; NOT=""
trap '[[ -n $INV ]] && stop "$INV" 8001; [[ -n $ORD ]] && stop "$ORD" 8000; [[ -n $NOT ]] && stop "$NOT" 8002' EXIT

echo "### 1. Start the three services as independent processes"
OUT=$DIR/01_services_running.txt
INV=$(start "$DIR/inventory_service.log" python inventory_service/app.py)
NOT=$(start "$DIR/notification_service.log" python notification_service/app.py)
ORD=$(start "$DIR/order_service.log" python order_service/app.py)
wait_port 8001; wait_port 8002; wait_port 8000
note "Each service is its own OS process with its own pid and port"
run <<< 'curl -s http://127.0.0.1:8001/health'
run <<< 'curl -s http://127.0.0.1:8000/health'
run <<< 'curl -s http://127.0.0.1:8002/health'

echo "### 2. Query: ask Inventory directly whether an item is available"
OUT=$DIR/02_inventory_query.txt
run <<< 'curl -s -i "http://127.0.0.1:8001/inventory/laptop/availability?quantity=2"'
run <<< 'curl -s -i "http://127.0.0.1:8001/inventory/keyboard/availability?quantity=1"'

echo "### 3. Command: create an order (item available)"
OUT=$DIR/03_successful_order.txt
order laptop 2
sleep 1  # give the background OrderCreated event a moment to reach Notification
note "Track the order that was just created"
run <<< 'curl -s http://127.0.0.1:8000/orders/ORD-0001'
note "Event: Notification Service received OrderCreated and notified the customer"
run <<< 'curl -s http://127.0.0.1:8002/notifications'

echo "### 4. Orders that Inventory rejects, and bad input"
OUT=$DIR/04_rejected_orders.txt
note "keyboard has 0 units in stock -> Inventory says unavailable -> 409"
order keyboard 1
note "monitor has 3 units, 5 requested -> unavailable -> 409"
order monitor 5
note "item that does not exist -> 404"
order tablet 1
note "invalid quantity -> 400 (rejected before calling Inventory)"
order laptop 0

echo "### 5. Failure test: stop the Inventory Service, then submit another order"
OUT=$DIR/05_failure_test.txt
note "Order Service before the failure (note the pid)"
run <<< 'curl -s http://127.0.0.1:8000/health'
stop "$INV" 8001; INV=""
note "Inventory Service stopped. Nothing is listening on port 8001 any more:"
run <<< 'curl -sS http://127.0.0.1:8001/health; echo "curl exit code: $?"'
note "Submit another order while Inventory is down"
order laptop 1 "-w '\ntime_total=%{time_total}s\n'"
note "Order Service is still running: same pid, status up, inventory_failures counted"
run <<< 'curl -s http://127.0.0.1:8000/health'
note "and it still serves its own data (the order created before the failure)"
run <<< 'curl -s http://127.0.0.1:8000/orders'

echo "### 6. Recovery: restart Inventory, the same Order Service process works again"
OUT=$DIR/06_recovery.txt
INV=$(start "$DIR/inventory_service_restarted.log" python inventory_service/app.py)
wait_port 8001
order laptop 1
run <<< 'curl -s http://127.0.0.1:8000/health'

echo "Saved to $DIR/"
