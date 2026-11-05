"""Several sending mailboxes.

* New table `mailboxes` (SMTP + optional IMAP per mailbox). The mailbox configured in .env becomes a row
  flagged `from_env` at start-up (see `ensure_defaults`), so existing deployments keep sending as before.
* `messages.mailbox_id`: which mailbox sent the email (follow-ups stay on it; per-mailbox daily limits).
* `campaigns.mailbox_ids`: the mailboxes a campaign rotates through (empty = the default mailbox).
"""
from app.db import Base, JSONType
from app.migrations import ops

VERSION = 3
NAME = "mailboxes"


def upgrade(conn):
    md = Base.metadata
    ops.create_model_table(conn, md.tables["mailboxes"])
    pg = ops.is_pg(conn)
    ops.add_column(conn, "campaigns", "mailbox_ids", ops.type_ddl(conn, JSONType))
    ops.add_column(conn, "messages", "mailbox_id",
                   "INTEGER" if pg else "INTEGER REFERENCES mailboxes (id) ON DELETE SET NULL")
    if pg:
        ops.pg_replace_fk(conn, "messages", "mailbox_id", "mailboxes", "SET NULL")
