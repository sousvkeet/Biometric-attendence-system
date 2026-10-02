#!/usr/bin/env bash
# Starts the web portal and the background device-sync worker together.
# Usage: ./run.sh            (development server on http://0.0.0.0:8090)
#        PORT=9000 ./run.sh
#        ./run.sh prod        (gunicorn)
set -euo pipefail
cd "$(dirname "$0")"
PY=.venv/bin/python
PORT="${PORT:-8090}"

# Refuse to start if the port is taken (otherwise the worker would start and the website would not).
if (echo > "/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
  echo "Port $PORT is already in use."
  if pgrep -f "manage.py (runserver 0.0.0.0:$PORT|run_worker)" >/dev/null || pgrep -f "gunicorn.*:$PORT" >/dev/null; then
    echo "BioAttend already seems to be running: open http://localhost:$PORT"
    echo "(pm2 users: check with 'pm2 status')"
  else
    echo "Another program is using it. Start on a different port, e.g.:  PORT=8091 ./run.sh"
  fi
  exit 1
fi

$PY manage.py migrate --noinput >/dev/null

$PY manage.py run_worker &
WORKER_PID=$!
trap 'kill $WORKER_PID 2>/dev/null || true' EXIT INT TERM

echo "BioAttend: open http://localhost:$PORT  (Ctrl+C to stop)"
if [ "${1:-}" = "prod" ]; then
  $PY manage.py collectstatic --noinput >/dev/null
  GUNICORN_BIND="0.0.0.0:$PORT" .venv/bin/gunicorn -c deploy/gunicorn.conf.py config.wsgi:application
else
  $PY manage.py runserver "0.0.0.0:$PORT"
fi
