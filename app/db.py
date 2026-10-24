from sqlalchemy import JSON, BigInteger, Integer, MetaData, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import DATABASE_URL

engine = create_engine(DATABASE_URL, pool_pre_ping=True)

if engine.dialect.name == "sqlite":
    @event.listens_for(engine, "connect")
    def _sqlite_fk(dbapi_conn, _record):
        # SQLite ignores FOREIGN KEY clauses unless asked; Postgres always enforces them.
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

# Deterministic constraint names, so later migrations can find and alter them.
NAMING = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
}
Base = declarative_base(metadata=MetaData(naming_convention=NAMING))

# JSONB on Postgres (indexable with GIN), plain JSON text elsewhere (SQLite in tests).
JSONType = JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")
JSONDict = MutableDict.as_mutable(JSONType)
JSONList = MutableList.as_mutable(JSONType)
# 64-bit keys for append-only tables that grow without bound; SQLite needs INTEGER for rowid aliasing.
BigID = BigInteger().with_variant(Integer(), "sqlite")


def init_db():
    """Bring the schema up to date (versioned migrations) and make sure default rows exist.

    web / worker / reply_checker / mcp all call this at start-up; the runner serialises
    itself with a Postgres advisory lock.
    """
    import app.models  # noqa: F401  (register models)
    from app.migrations import migrate
    migrate(engine)
