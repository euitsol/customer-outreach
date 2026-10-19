import os

DATABASE_URL = os.environ["DATABASE_URL"]

SMTP_HOST = os.environ["SMTP_HOST"]
SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
SMTP_USER = os.environ["SMTP_USER"]
SMTP_PASSWORD = os.environ["SMTP_PASSWORD"]
SENDER_NAME = os.environ.get("SENDER_NAME", SMTP_USER)
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", SMTP_USER)

IMAP_HOST = os.environ.get("IMAP_HOST")
IMAP_PORT = int(os.environ.get("IMAP_PORT", 993))
IMAP_USER = os.environ.get("IMAP_USER")
IMAP_PASSWORD = os.environ.get("IMAP_PASSWORD")

TRACK_BASE_URL = os.environ["TRACK_BASE_URL"].rstrip("/")

MIN_DELAY_SECONDS = int(os.environ.get("MIN_DELAY_SECONDS", 25))
MAX_DELAY_SECONDS = int(os.environ.get("MAX_DELAY_SECONDS", 75))
MAX_PER_DAY = int(os.environ.get("MAX_PER_DAY", 80))
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", 120))