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

# --- WhatsApp through Twilio. Leave empty to disable the channel.
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")                 # also verifies webhook signatures
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "")           # e.g. whatsapp:+14155238886
TWILIO_DAILY_LIMIT = int(os.environ.get("TWILIO_DAILY_LIMIT", 250))        # WhatsApp campaign sends per day (Meta tier)
TWILIO_COOLDOWN_DAYS = int(os.environ.get("TWILIO_COOLDOWN_DAYS", 7))        # days between messages from different WhatsApp campaigns (0 = off)
TWILIO_MESSAGING_SERVICE_SID = os.environ.get("TWILIO_MESSAGING_SERVICE_SID", "")  # optional, instead of FROM

TWILIO_LOOKUP_DAILY_LIMIT = int(os.environ.get("TWILIO_LOOKUP_DAILY_LIMIT", 50))  # Lookup v2 number checks per day (0 = off); editable in Settings
TWILIO_LOOKUP_PAID = os.environ.get("TWILIO_LOOKUP_PAID", "0").lower() in ("1", "true", "yes")  # add line type intelligence (~$0.008 per lookup)

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
