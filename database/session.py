from contextlib import contextmanager
from sqlalchemy import create_engine 
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
    # Import model modules so their classes register with Base.metadata.
    import database.user  # noqa: F401
    import database.bank_renc_model  # noqa: F401
    import database.period_model  # noqa: F401

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