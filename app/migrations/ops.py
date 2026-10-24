"""Small, dialect-aware DDL helpers for migrations (Postgres in production, SQLite in tests).

Every helper is idempotent so a migration that died half-way on a non-transactional backend can
simply be run again.
"""
from sqlalchemy import inspect, text


def is_pg(conn):
    return conn.dialect.name == "postgresql"


def tables(conn):
    return set(inspect(conn).get_table_names())


def has_table(conn, name):
    return name in tables(conn)


def columns(conn, table):
    return {c["name"] for c in inspect(conn).get_columns(table)}


def has_column(conn, table, column):
    return has_table(conn, table) and column in columns(conn, table)


def type_ddl(conn, sa_type):
    return sa_type.compile(dialect=conn.dialect)


def add_column(conn, table, column, ddl):
    if has_table(conn, table) and not has_column(conn, table, column):
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
        return True
    return False


def drop_column(conn, table, column):
    if has_column(conn, table, column):
        conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
        return True
    return False


def rename_table(conn, old, new):
    if has_table(conn, old) and not has_table(conn, new):
        conn.execute(text(f"ALTER TABLE {old} RENAME TO {new}"))
        return True
    return False


def rename_column(conn, table, old, new):
    if has_column(conn, table, old) and not has_column(conn, table, new):
        conn.execute(text(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}"))
        return True
    return False


def index_names(conn, table):
    if not has_table(conn, table):
        return set()
    return {i["name"] for i in inspect(conn).get_indexes(table)}


def drop_index(conn, name, table=None):
    # Postgres scopes index names per schema, SQLite per database: plain DROP INDEX works for both.
    conn.execute(text(f"DROP INDEX IF EXISTS {name}"))


def create_index(conn, sql):
    """`sql` must use CREATE [UNIQUE] INDEX IF NOT EXISTS."""
    conn.execute(text(sql))


def create_model_table(conn, table):
    table.create(conn, checkfirst=True)


def create_model_indexes(conn, table):
    """Create the indexes declared on a model table that don't exist yet."""
    have = index_names(conn, table.name)
    for idx in table.indexes:
        if idx.name not in have:
            idx.create(conn)


def foreign_keys(conn, table):
    return inspect(conn).get_foreign_keys(table) if has_table(conn, table) else []


def pg_replace_fk(conn, table, column, ref_table, ondelete, ref_column="id"):
    """Postgres only: (re)create the FK on table.column with the wanted ON DELETE rule."""
    for fk in foreign_keys(conn, table):
        if fk["constrained_columns"] == [column] and fk.get("name"):
            conn.execute(text(f'ALTER TABLE {table} DROP CONSTRAINT "{fk["name"]}"'))
    name = f"fk_{table}_{column}_{ref_table}"
    conn.execute(text(
        f'ALTER TABLE {table} ADD CONSTRAINT "{name}" FOREIGN KEY ({column}) '
        f"REFERENCES {ref_table} ({ref_column}) ON DELETE {ondelete}"))


def pg_constraint_exists(conn, name):
    return conn.execute(text("SELECT 1 FROM pg_constraint WHERE conname = :n"), {"n": name}).first() is not None


def pg_rename_constraint(conn, table, old, new):
    if pg_constraint_exists(conn, old) and not pg_constraint_exists(conn, new):
        conn.execute(text(f'ALTER TABLE {table} RENAME CONSTRAINT "{old}" TO "{new}"'))


def pg_rename_sequence(conn, old, new):
    exists = conn.execute(text("SELECT 1 FROM pg_class WHERE relkind = 'S' AND relname = :n"), {"n": old}).first()
    taken = conn.execute(text("SELECT 1 FROM pg_class WHERE relkind = 'S' AND relname = :n"), {"n": new}).first()
    if exists and not taken:
        conn.execute(text(f'ALTER SEQUENCE "{old}" RENAME TO "{new}"'))


def pg_try(conn, sql):
    """Run a statement that may legitimately fail (missing privilege / extension) without aborting
    the surrounding transaction."""
    sp = conn.begin_nested()
    try:
        conn.execute(text(sql))
        sp.commit()
        return True
    except Exception as e:  # noqa: BLE001
        sp.rollback()
        print(f"migration: skipped optional statement ({e.__class__.__name__}): {sql[:80]}", flush=True)
        return False
