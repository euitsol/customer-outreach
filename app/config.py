"""Environment configuration.

Everything here is a *default*; the delay / limit / retry values can be overridden
live from the panel (Settings page) and are stored in the database.
"""
import hashlib
import os

DATABASE_URL = os.environ["DATABASE_URL"]

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SENDER_NAME = os.environ.get("SENDER_NAME", SMTP_USER)
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", SMTP_USER)

IMAP_HOST = os.environ.get("IMAP_HOST")
IMAP_PORT = int(os.environ.get("IMAP_PORT", 993))
IMAP_USER = os.environ.get("IMAP_USER")
IMAP_PASSWORD = os.environ.get("IMAP_PASSWORD")

TRACK_BASE_URL = os.environ.get("TRACK_BASE_URL", "http://localhost:8000").rstrip("/")

MIN_DELAY_SECONDS = int(os.environ.get("MIN_DELAY_SECONDS", 25))
MAX_DELAY_SECONDS = int(os.environ.get("MAX_DELAY_SECONDS", 75))
MAX_PER_DAY = int(os.environ.get("MAX_PER_DAY", 80))
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", 120))

# Panel login. If ADMIN_PASSWORD is empty the panel is open (not recommended).
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
TIMEZONE = os.environ.get("TIMEZONE", "Europe/Dublin")

# Must be stable across restarts / gunicorn workers. Falls back to a value derived
# from other secrets so sessions keep working without extra configuration.
SECRET_KEY = os.environ.get("SECRET_KEY") or hashlib.sha256(
    f"{ADMIN_PASSWORD}|{DATABASE_URL}|{SMTP_PASSWORD}".encode()
).hexdigest()
