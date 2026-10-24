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
# Country assumed for phone numbers written without an international prefix (ISO 3166 code).
DEFAULT_PHONE_REGION = os.environ.get("DEFAULT_PHONE_REGION", "IE").upper()
DEFAULT_CURRENCY = os.environ.get("DEFAULT_CURRENCY", "EUR").upper()

# --- WhatsApp Business (Meta Cloud API). Leave empty to disable the channel.
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_BUSINESS_ACCOUNT_ID = os.environ.get("WHATSAPP_BUSINESS_ACCOUNT_ID", "")
WHATSAPP_VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")   # you choose it; Meta echoes it back
WHATSAPP_APP_SECRET = os.environ.get("WHATSAPP_APP_SECRET", "")       # signs webhook payloads
WHATSAPP_API_VERSION = os.environ.get("WHATSAPP_API_VERSION", "v21.0")
WHATSAPP_DISPLAY_NUMBER = os.environ.get("WHATSAPP_DISPLAY_NUMBER", "")

# --- MCP server (Claude). The HTTP transport requires an API key created in Settings > API keys.
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", 8092))
MCP_ALLOWED_HOSTS = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

# Must be stable across restarts / gunicorn workers. Falls back to a value derived
# from other secrets so sessions keep working without extra configuration.
SECRET_KEY = os.environ.get("SECRET_KEY") or hashlib.sha256(
    f"{ADMIN_PASSWORD}|{DATABASE_URL}|{SMTP_PASSWORD}".encode()
).hexdigest()
