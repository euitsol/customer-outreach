import os
import sys
import tempfile

# Must be set before the app package is imported.
_tmp = tempfile.mkdtemp(prefix="outreach-test-")
# SQLite by default; set TEST_DATABASE_URL to an EMPTY Postgres database to run the suite against Postgres.
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{_tmp}/test.db"
os.environ.update(
    SMTP_HOST="smtp.test", SMTP_USER="sender@example.com", SMTP_PASSWORD="x", SENDER_EMAIL="sender@example.com",
    SENDER_NAME="Tester", TRACK_BASE_URL="https://track.example.com", ADMIN_PASSWORD="secret", ADMIN_USER="admin",
    MIN_DELAY_SECONDS="0", MAX_DELAY_SECONDS="0", MAX_PER_DAY="500",
)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
