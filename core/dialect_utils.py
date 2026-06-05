"""Cross-dialect helpers for ad-hoc table introspection.

Startup migrations in `core/database.py` historically called
`PRAGMA table_info(t)` and `SELECT name FROM sqlite_master ...` to
look up column names and table existence. Both queries are
SQLite-specific — on Postgres they raise syntax errors. Use the
helpers in this module to keep that code working on both backends.

They wrap `sqlalchemy.inspect()`, which dispatches to the dialect's
native introspection (PRAGMA on SQLite, information_schema on
Postgres) so the caller doesn't have to know which backend is in use.
"""
from sqlalchemy import inspect


def table_columns(conn, table_name: str) -> list[str]:
    """Return the column names of `table_name` on the given connection.

    Drop-in replacement for `[r[1] for r in conn.execute(text("PRAGMA
    table_info(t)"))]`. Works on SQLite and Postgres (and any other
    SQLAlchemy-supported dialect).
    """
    return [c["name"] for c in inspect(conn).get_columns(table_name)]


def table_exists(conn, table_name: str) -> bool:
    """Return True if `table_name` exists on the given connection.

    Avoids `SELECT name FROM sqlite_master ...` (Postgres has no
    sqlite_master). Uses SQLAlchemy's dialect-aware introspection.
    """
    return inspect(conn).has_table(table_name)
