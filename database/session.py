from contextlib import contextmanager
from sqlalchemy import create_engine 
from sqlalchemy.orm import sessionmaker, scoped_session
from core.config import settings

engine = create_engine(
    settings.SQLALCHEMY_SYNC_DATABASE_URI,
    echo=False,
    pool_pre_ping=True  
)

session_factory = sessionmaker(bind=engine)
# Thread-Local "singleton" (The Scoped Session)
db_session = scoped_session(session_factory)


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