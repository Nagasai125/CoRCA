#!/bin/bash
# usage: CORCA_E2E_PORT=8040 e2e/restart.sh [fresh]
set -euo pipefail
cd "$(dirname "$0")/.."
port=${CORCA_E2E_PORT:-8000}
base="http://127.0.0.1:$port"
state="$PWD/e2e/state/corca.db"
if [[ -f e2e/server.pid ]]; then
  pid=$(cat e2e/server.pid)
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    command_line=$(ps -p "$pid" -o args=)
    if [[ "$command_line" != *backend.api* || "$command_line" != *"$state"* ]]; then
      echo "PID file does not identify this test server; refusing to stop it." >&2
      exit 1
    fi
    kill "$pid"
    for _ in $(seq 20); do
      if ! kill -0 "$pid" 2>/dev/null; then break; fi
      sleep 0.25
    done
  fi
fi
if curl --max-time 2 -fsS "$base/healthz" >/dev/null 2>&1; then
  echo "Port $port already serves an application. Choose a free CORCA_E2E_PORT." >&2
  exit 1
fi
if [[ ${1:-} == fresh ]]; then rm -rf e2e/state; fi
mkdir -p e2e/state
nohup "${PY:-.venv/bin/python}" -m backend.api --host 127.0.0.1 --port "$port" --db "$state" </dev/null >>e2e/server.log 2>&1 &
pid=$!
echo "$pid" > e2e/server.pid
for _ in $(seq 40); do
  if ! kill -0 "$pid" 2>/dev/null; then echo "Test server failed; inspect e2e/server.log." >&2; exit 1; fi
  if curl --max-time 2 -fsS "$base/readyz" >/dev/null 2>&1; then echo "Test server ready on port $port"; exit 0; fi
  sleep 0.25
done
echo "Test server did not become ready; inspect e2e/server.log." >&2
exit 1
