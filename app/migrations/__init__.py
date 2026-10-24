"""Versioned schema migrations.

    schema_migrations(version, name, applied_at) records what has run.

* Fresh database  -> create every table from the models, mark all migrations as applied.
* Existing database without the table (created before versioning) -> run every migration from the
  baseline, which knows how to recognise and upgrade each older shape.
* Otherwise        -> run the migrations that are missing, in order, one transaction each.

Then, every start: idempotent extras (Postgres search / JSONB indexes, partial indexes) and default
rows (sales pipeline, email channel account).

Writing a new migration: add `vNNNN_name.py` with VERSION, NAME and `upgrade(conn)`, append it to
MIGRATIONS, and only use the idempotent helpers in `ops` (add_column, create_model_table, ...).
"""
from sqlalchemy import inspect, text

from app.clock import utcnow
from app.migrations import ops, v0001_baseline, v0002_crm

MIGRATIONS = [v0001_baseline, v0002_crm]
LOCK_ID = 727301


def migrate(engine):
    pg = engine.dialect.name == "postgresql"
    with engine.connect() as conn:
        if pg:
            conn.execute(text(f"SELECT pg_advisory_lock({LOCK_ID})"))
            conn.commit()
        try:
            _run(conn)
            post_schema(conn)
            conn.commit()
            ensure_defaults(conn)
            conn.commit()
        finally:
            if pg:
                conn.rollback()
                conn.execute(text(f"SELECT pg_advisory_unlock({LOCK_ID})"))
                conn.commit()


def applied_versions(conn):
    if not ops.has_table(conn, "schema_migrations"):
        return set()
    return {v for (v,) in conn.execute(text("SELECT version FROM schema_migrations"))}


def _stamp(conn, m):
    conn.execute(text("INSERT INTO schema_migrations (version, name, applied_at) VALUES (:v, :n, :t)"),
                 {"v": m.VERSION, "n": m.NAME, "t": utcnow().isoformat(sep=" ")})


def _run(conn):
    from app.db import Base
    existing = set(inspect(conn).get_table_names())
    if "schema_migrations" not in existing:
        conn.execute(text("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, "
                          "name VARCHAR(100) NOT NULL, applied_at TIMESTAMP NOT NULL)"))
        conn.commit()
        if "campaigns" not in existing and "enrollments" not in existing:
            Base.metadata.create_all(conn)
            for m in MIGRATIONS:
                _stamp(conn, m)
            conn.commit()
            return
    done = applied_versions(conn)
    for m in MIGRATIONS:
        if m.VERSION in done:
            continue
        print(f"migration {m.VERSION:04d} {m.NAME}: applying", flush=True)
        try:
            m.upgrade(conn)
            _stamp(conn, m)
            conn.commit()
        except Exception:
            conn.rollback()
            print(f"migration {m.VERSION:04d} {m.NAME}: FAILED, rolled back", flush=True)
            raise
        print(f"migration {m.VERSION:04d} {m.NAME}: done", flush=True)
    # Safety net for tables declared in models after the last migration (no-op normally).
    Base.metadata.create_all(conn, checkfirst=True)
    conn.commit()


# ---------------------------------------------------------------------- idempotent extras

PORTABLE_INDEXES = [
    # The worker's hot path: due enrollments that may still be emailed.
    "CREATE INDEX IF NOT EXISTS ix_enrollments_sendable_due ON enrollments (next_due_at) "
    "WHERE send_flag = TRUE AND replied = FALSE AND bounced = FALSE AND unsubscribed = FALSE "
    "AND needs_verification = FALSE",
    "CREATE INDEX IF NOT EXISTS ix_messages_queued_only ON messages (id) WHERE queued = TRUE",
    "CREATE INDEX IF NOT EXISTS ix_companies_lower_name ON companies (lower(name))",
    "CREATE INDEX IF NOT EXISTS ix_people_lower_name ON people (lower(first_name), lower(last_name))",
    "CREATE INDEX IF NOT EXISTS ix_tasks_open_due ON tasks (due_at) WHERE status = 'open'",
]

POSTGRES_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_people_attributes_gin ON people USING gin (attributes jsonb_path_ops)",
    "CREATE INDEX IF NOT EXISTS ix_companies_attributes_gin ON companies USING gin (attributes jsonb_path_ops)",
    "CREATE INDEX IF NOT EXISTS ix_deals_attributes_gin ON deals USING gin (attributes jsonb_path_ops)",
    "CREATE INDEX IF NOT EXISTS ix_enrollments_variables_gin ON enrollments USING gin (variables jsonb_path_ops)",
]

TRIGRAM_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_people_search_trgm ON people USING gin ("
    "(lower(coalesce(first_name, '') || ' ' || coalesce(last_name, '') || ' ' || coalesce(email, '') "
    "|| ' ' || coalesce(phone, ''))) gin_trgm_ops)",
    "CREATE INDEX IF NOT EXISTS ix_companies_search_trgm ON companies USING gin ("
    "(lower(name || ' ' || coalesce(domain, ''))) gin_trgm_ops)",
]


def post_schema(conn):
    for sql in PORTABLE_INDEXES:
        conn.execute(text(sql))
    if ops.is_pg(conn):
        for sql in POSTGRES_INDEXES:
            conn.execute(text(sql))
        if ops.pg_try(conn, "CREATE EXTENSION IF NOT EXISTS pg_trgm"):
            for sql in TRIGRAM_INDEXES:
                ops.pg_try(conn, sql)


# ---------------------------------------------------------------------- default rows

DEFAULT_STAGES = [("New lead", 10, "open"), ("Contacted", 20, "open"), ("Qualified", 40, "open"),
                  ("Proposal sent", 60, "open"), ("Negotiation", 80, "open"), ("Won", 100, "won"),
                  ("Lost", 0, "lost")]


def ensure_defaults(conn):
    from app import config
    from app.db import Base
    t = Base.metadata.tables
    now = utcnow()
    if not conn.execute(text("SELECT 1 FROM pipelines")).first():
        pid = conn.execute(t["pipelines"].insert().values(
            name="Sales pipeline", is_default=True, position=0, created_at=now)).inserted_primary_key[0]
        for i, (name, prob, kind) in enumerate(DEFAULT_STAGES, start=1):
            conn.execute(t["pipeline_stages"].insert().values(
                pipeline_id=pid, name=name, position=i, probability=prob, kind=kind))
    ch = t["channel_accounts"]
    if not conn.execute(text("SELECT 1 FROM channel_accounts WHERE kind = 'email'")).first():
        conn.execute(ch.insert().values(
            kind="email", name="Email (SMTP)", address=config.SENDER_EMAIL or None, external_id=None,
            config={"smtp_host": config.SMTP_HOST, "imap_host": config.IMAP_HOST or ""},
            status="active" if config.SMTP_HOST else "disabled", is_default=True, created_at=now, updated_at=now))
    if config.WHATSAPP_PHONE_NUMBER_ID and not conn.execute(
            text("SELECT 1 FROM channel_accounts WHERE kind = 'whatsapp' AND external_id = :e"),
            {"e": config.WHATSAPP_PHONE_NUMBER_ID}).first():
        conn.execute(ch.insert().values(
            kind="whatsapp", name="WhatsApp Business", address=config.WHATSAPP_DISPLAY_NUMBER or None,
            external_id=config.WHATSAPP_PHONE_NUMBER_ID,
            config={"business_account_id": config.WHATSAPP_BUSINESS_ACCOUNT_ID,
                    "api_version": config.WHATSAPP_API_VERSION},
            status="active" if config.WHATSAPP_TOKEN else "disabled",
            is_default=not conn.execute(text("SELECT 1 FROM channel_accounts WHERE kind = 'whatsapp'")).first(),
            created_at=now, updated_at=now))
