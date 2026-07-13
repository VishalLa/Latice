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


def create_tables() -> None:
    """Create all database tables for imported ORM models."""
    import database.user  
    import database.bank_renc_model  
    import database.period_model  
    import database.ledger_tax_models  

    Base.metadata.create_all(engine)


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
