from sqlalchemy import create_engine, text, inspect
from sqlalchemy.orm import sessionmaker, declarative_base
from app.config import DATABASE_URL

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

# (table, column, DDL type) - added to databases created by older versions.
NEW_COLUMNS = [
    ("campaigns", "description", "TEXT"),
    ("campaigns", "status", "VARCHAR(20)"),
    ("campaigns", "is_active", "BOOLEAN DEFAULT FALSE"),
    ("campaigns", "start_at", "TIMESTAMP"),
    ("campaigns", "daily_limit", "INTEGER"),
    ("campaigns", "min_delay", "INTEGER"),
    ("campaigns", "max_delay", "INTEGER"),
    ("campaigns", "track_opens", "BOOLEAN DEFAULT TRUE"),
    ("campaigns", "track_clicks", "BOOLEAN DEFAULT TRUE"),
    ("campaigns", "sender_name", "VARCHAR(200)"),
    ("campaigns", "reply_to", "VARCHAR(300)"),
    ("campaigns", "updated_at", "TIMESTAMP"),
    ("contacts", "phone", "VARCHAR(100)"),
    ("contacts", "notes", "TEXT"),
    ("contacts", "custom", "TEXT"),
    ("contacts", "queued", "BOOLEAN DEFAULT FALSE"),
    ("contacts", "bounced", "BOOLEAN DEFAULT FALSE"),
    ("contacts", "next_due_at", "TIMESTAMP"),
    ("contacts", "last_step_at", "TIMESTAMP"),
    ("contacts", "updated_at", "TIMESTAMP"),
]

BACKFILLS = [
    "UPDATE campaigns SET status = CASE WHEN is_active THEN 'active' ELSE 'paused' END WHERE status IS NULL",
    "UPDATE campaigns SET track_opens = TRUE WHERE track_opens IS NULL",
    "UPDATE campaigns SET track_clicks = TRUE WHERE track_clicks IS NULL",
    "UPDATE contacts SET bounced = FALSE WHERE bounced IS NULL",
]


INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_contacts_campaign_id ON contacts (campaign_id)",
    "CREATE INDEX IF NOT EXISTS ix_contacts_email ON contacts (email)",
    "CREATE INDEX IF NOT EXISTS ix_contacts_next_due_at ON contacts (next_due_at)",
    "CREATE INDEX IF NOT EXISTS ix_events_contact_id ON events (contact_id)",
    "CREATE INDEX IF NOT EXISTS ix_events_type ON events (type)",
    "CREATE INDEX IF NOT EXISTS ix_events_created_at ON events (created_at)",
    "CREATE INDEX IF NOT EXISTS ix_campaigns_status ON campaigns (status)",
]


def _ensure_columns(conn):
    insp = inspect(conn)
    existing_tables = set(insp.get_table_names())
    for table, column, ddl in NEW_COLUMNS:
        if table not in existing_tables:
            continue
        have = {c["name"] for c in insp.get_columns(table)}
        if column not in have:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            have.add(column)


def init_db():
    """Create tables, add missing columns, run the one-off data migration.

    web / worker / reply_checker all call this at start-up, so on Postgres the whole
    thing is serialised with an advisory lock.
    """
    import app.models  # noqa: F401  (register models)
    with engine.connect() as conn:
        is_pg = engine.dialect.name == "postgresql"
        if is_pg:
            conn.execute(text("SELECT pg_advisory_lock(727301)"))
            conn.commit()
        try:
            # new columns on old tables first, then create_all adds brand new tables
            Base.metadata.create_all(bind=conn)
            conn.commit()
            _ensure_columns(conn)
            conn.commit()
            for stmt in INDEXES + BACKFILLS:
                conn.execute(text(stmt))
            conn.commit()
        finally:
            if is_pg:
                conn.execute(text("SELECT pg_advisory_unlock(727301)"))
                conn.commit()

    from app.legacy import migrate_legacy
    with engine.connect() as conn:
        if is_pg:
            conn.execute(text("SELECT pg_advisory_lock(727302)"))
            conn.commit()
        try:
            session = SessionLocal()
            try:
                migrate_legacy(session)
            finally:
                session.close()
        finally:
            if is_pg:
                conn.execute(text("SELECT pg_advisory_unlock(727302)"))
                conn.commit()
