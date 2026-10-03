"""
Django settings for the Biometric Attendance Management System.

Configuration is read from environment variables (a `.env` file in the
project root is loaded automatically). See `.env.example`.
"""
import os
import sys
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


SECRET_KEY = os.environ.get("SECRET_KEY", "")
if len(SECRET_KEY) < 40:
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured("Set a long random SECRET_KEY in .env (see .env.example).")
DEBUG = env_bool("DEBUG", False)
DESKTOP_MODE = env_bool("BIOATTEND_DESKTOP", False)  # set by the Windows desktop app
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]
CSRF_TRUSTED_ORIGINS = [o.strip() for o in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()]

# Writable data folder (logs, backups work area, cache, simulator files, rclone binary). In Docker: a volume.
VAR_DIR = Path(os.environ.get("VAR_DIR") or BASE_DIR / "var")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "core",
    "employees",
    "devices",
    "attendance",
    "backups",
]

MIDDLEWARE = [
    "core.middleware.HealthCheckMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "backups.middleware.MaintenanceMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "core.firstrun.FirstRunMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django.middleware.csp.ContentSecurityPolicyMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.app_context",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# Database: SQLite by default, or set DATABASE_URL, e.g.
#   postgres://user:pass@localhost:5433/biometric
DATABASE_URL = os.environ.get("DATABASE_URL", "")
if DATABASE_URL:
    DATABASES = {"default": dj_database_url.parse(DATABASE_URL, conn_max_age=60)}
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": Path(os.environ.get("SQLITE_PATH") or BASE_DIR / "db.sqlite3"),
            # The web server and the device worker write concurrently.
            "OPTIONS": {
                "timeout": 30,
                "transaction_mode": "IMMEDIATE",
                "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
            },
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ---------------------------------------------------------------- security
from django.utils.csp import CSP  # noqa: E402

SECURE_CSP = {
    "default-src": [CSP.SELF],
    "script-src": [CSP.SELF, CSP.UNSAFE_INLINE],
    "style-src": [CSP.SELF, CSP.UNSAFE_INLINE],
    "img-src": [CSP.SELF, "data:"],
    "font-src": [CSP.SELF],
    "connect-src": [CSP.SELF],
    "frame-ancestors": [CSP.NONE],
    "object-src": [CSP.NONE],
    "base-uri": [CSP.SELF],
    "form-action": [CSP.SELF],
}
SECURE_REFERRER_POLICY = "same-origin"
SESSION_COOKIE_AGE = 60 * 60 * 12  # 12 hours
SESSION_COOKIE_HTTPONLY = True
# Turn on when the portal is served over HTTPS (reverse proxy with a certificate).
if env_bool("HTTPS", False):
    SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = True
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30

# Login protection: lock a username (per IP) after repeated failures.
LOGIN_MAX_FAILURES = int(os.environ.get("LOGIN_MAX_FAILURES", 5))
LOGIN_LOCK_MINUTES = int(os.environ.get("LOGIN_LOCK_MINUTES", 15))
CACHES = {"default": {"BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
                      "LOCATION": str(VAR_DIR / "cache")}}
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
BACKUP_UPLOAD_MAX_BYTES = 2 * 1024 ** 3

LANGUAGE_CODE = "en-us"
# ZKTeco devices store naive local timestamps; this must match the devices' clock zone.
TIME_ZONE = os.environ.get("TIME_ZONE", "Asia/Kolkata")
USE_I18N = False
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
WHITENOISE_USE_FINDERS = True  # serve static files with DEBUG off, even before collectstatic
# Development server: re-read edited CSS/JS (otherwise a changed file is served with its old size, cut off).
WHITENOISE_AUTOREFRESH = DEBUG or "runserver" in sys.argv
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"
        if not DEBUG
        else "django.contrib.staticfiles.storage.StaticFilesStorage"
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "login"

MESSAGE_TAGS = {40: "danger"}

# Working folder for creating backup files before they are uploaded.
BACKUP_WORK_DIR = Path(os.environ["BACKUP_WORK_DIR"]) if os.environ.get("BACKUP_WORK_DIR") else VAR_DIR / "backups"

# rclone binary: the copy downloaded into var/bin, else one on the PATH.
_rclone_local = VAR_DIR / "bin" / ("rclone.exe" if os.name == "nt" else "rclone")
RCLONE_BINARY = os.environ.get("RCLONE_BINARY") or (str(_rclone_local) if _rclone_local.exists() else "rclone")

# Folder used by simulator devices (virtual devices for testing/training).
SIMULATOR_DIR = Path(os.environ["SIMULATOR_DIR"]) if os.environ.get("SIMULATOR_DIR") else VAR_DIR / "simulators"

# ---------------------------------------------------------------- logging
# LOG_TO_FILE=true (default): rotating files in var/log. Docker: LOG_TO_FILE=false -> console only.
LOG_DIR = VAR_DIR / "log"
LOG_TO_FILE = env_bool("LOG_TO_FILE", True) and "test" not in sys.argv[1:2]
_handlers = {"console": {"class": "logging.StreamHandler", "formatter": "plain"}}
if LOG_TO_FILE:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _handlers["file"] = {"class": "logging.handlers.RotatingFileHandler", "formatter": "plain",
                         "filename": str(LOG_DIR / "bioattend.log"), "maxBytes": 10 * 1024 * 1024, "backupCount": 10}
    _handlers["errors"] = {"class": "logging.handlers.RotatingFileHandler", "formatter": "plain", "level": "ERROR",
                           "filename": str(LOG_DIR / "errors.log"), "maxBytes": 10 * 1024 * 1024, "backupCount": 10}
_targets = list(_handlers)

# Optional e-mail: errors (HTTP 500, failed jobs logged as errors) are mailed to ADMIN_EMAILS.
EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
if EMAIL_HOST:
    EMAIL_PORT = int(os.environ.get("EMAIL_PORT", 587))
    EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
    EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
    EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
    DEFAULT_FROM_EMAIL = SERVER_EMAIL = os.environ.get("EMAIL_FROM", EMAIL_HOST_USER or "bioattend@localhost")
    ADMINS = [("Admin", a.strip()) for a in os.environ.get("ADMIN_EMAILS", "").split(",") if a.strip()]
    if ADMINS:
        _handlers["mail_admins"] = {"class": "django.utils.log.AdminEmailHandler", "level": "ERROR"}
        _targets.append("mail_admins")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(process)d %(name)s: %(message)s"}},
    "handlers": _handlers,
    "root": {"handlers": [h for h in _targets if h != "mail_admins"], "level": "WARNING"},
    "loggers": {
        "django.request": {"handlers": _targets, "level": "ERROR", "propagate": False},
        "devices": {"handlers": _targets, "level": "INFO", "propagate": False},
        "core": {"handlers": _targets, "level": "INFO", "propagate": False},
    },
}
