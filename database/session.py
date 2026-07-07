from contextlib import contextmanager
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, scoped_session
from core.config import settings
from .base import Base

engine = create_engine(
    settings.SQLALCHEMY_SYNC_DATABASE_URI,
    echo=False,
    pool_pre_ping=True  
)

session_factory = sessionmaker(bind=engine)
# Thread-Local "singleton" (The Scoped Session)
db_session = scoped_session(session_factory)


def _ensure_reconciliation_run_columns() -> None:
    """Add newer reconciliation-run columns to already-existing PostgreSQL tables."""
    if engine.dialect.name != "postgresql":
        return

    try:
        inspector = inspect(engine)
        with engine.begin() as conn:
            columns = {c["name"] for c in inspector.get_columns("reconciliation_run")}
            if "task_id" not in columns:
                conn.execute(text("ALTER TABLE reconciliation_run ADD COLUMN task_id VARCHAR(128)"))
    except Exception as exc:
        print(f"Warning: could not add reconciliation_run.task_id column: {exc}")


def _ensure_run_scoped_constraints() -> None:
    """Upgrade existing database schemas to use run-scoped uniqueness when needed."""
    if engine.dialect.name != "postgresql":
        return

    try:
        inspector = inspect(engine)
        with engine.begin() as conn:
            for table_name, constraint_name, columns in [
                ("ledger_format", "uq_ledger_format_ledger_id", ["run_id", "ledger_id"]),
                ("bank_statement", "uq_bank_statement_row", ["run_id", "row_index", "bank_name", "template_version"]),
            ]:
                if table_name not in inspector.get_table_names():
                    continue
                existing_constraints = {c["name"] for c in inspector.get_unique_constraints(table_name)}
                if constraint_name in existing_constraints:
                    conn.execute(text(f'ALTER TABLE {table_name} DROP CONSTRAINT IF EXISTS {constraint_name}'))
                if constraint_name not in {c["name"] for c in inspector.get_unique_constraints(table_name)}:
                    conn.execute(text(
                        f'ALTER TABLE {table_name} ADD CONSTRAINT {constraint_name} UNIQUE ({", ".join(columns)})'
                    ))
    except Exception as exc:
        print(f"Warning: could not update run-scoped constraints: {exc}")


def create_tables() -> None:
    """Create all database tables for imported ORM models."""
    # Import model modules so their classes register with Base.metadata.
    import database.user  # noqa: F401
    import database.bank_renc_model  # noqa: F401

    Base.metadata.create_all(engine)
    _ensure_reconciliation_run_columns()
    _ensure_run_scoped_constraints()


@contextmanager
def get_session():
    session = db_session()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
        db_session.remove()


def get_db():
    session = db_session()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
        db_session.remove()
