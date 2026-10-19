from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base
from app.config import DATABASE_URL

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

MIGRATIONS = [
    "ALTER TABLE contacts ADD COLUMN IF NOT EXISTS phone VARCHAR(100)",
    "ALTER TABLE contacts ADD COLUMN IF NOT EXISTS notes TEXT",
    "ALTER TABLE contacts ADD COLUMN IF NOT EXISTS queued BOOLEAN DEFAULT FALSE",
    "ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
]

def init_db():
    import app.models  # noqa
    try:
        Base.metadata.create_all(bind=engine)
    except Exception as e:
        print("create_all:", e)
    for stmt in MIGRATIONS:
        try:
            with engine.begin() as conn:
                conn.execute(text(stmt))
        except Exception as e:  # web/worker start together, so a race is harmless
            print("migration skipped:", e)