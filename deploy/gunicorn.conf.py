"""Gunicorn settings for BioAttend (web part only – the device worker runs separately: manage.py run_worker).

    .venv/bin/gunicorn -c deploy/gunicorn.conf.py config.wsgi:application
"""
import multiprocessing
import os

# Behind Caddy/nginx keep 127.0.0.1; without a proxy use 0.0.0.0:8090 (GUNICORN_BIND).
bind = os.environ.get("GUNICORN_BIND", "127.0.0.1:8090")
workers = int(os.environ.get("WEB_CONCURRENCY", min(4, multiprocessing.cpu_count() * 2 + 1)))
threads = int(os.environ.get("GUNICORN_THREADS", 2))
timeout = 120             # large Excel exports / backup downloads
graceful_timeout = 30
keepalive = 5
max_requests = 2000       # recycle workers now and then
max_requests_jitter = 200
forwarded_allow_ips = os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1")
accesslog = os.environ.get("GUNICORN_ACCESS_LOG", "-")
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")
umask = 0o077
