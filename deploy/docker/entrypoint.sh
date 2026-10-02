#!/bin/sh
# Container entry point:  web | worker | <any command>
set -e
umask 077
case "$1" in
  web)
    python manage.py migrate --noinput
    exec gunicorn -c deploy/gunicorn.conf.py config.wsgi:application
    ;;
  worker)
    until python manage.py migrate --check >/dev/null 2>&1; do
      echo "worker: waiting for the database to be ready…"; sleep 5
    done
    exec python manage.py run_worker
    ;;
  *)
    exec "$@"
    ;;
esac
