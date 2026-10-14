from core.config import Config
from database import DatabaseManager


def initialize_database() -> None:
    config = Config.from_env()
    database_url = config.SQLALCHEMY_SYNC_DATABASE_URI or config.DATABASE_URL
    if not database_url:
        raise RuntimeError("DATABASE_URL is required to initialize the database")

    database = DatabaseManager(db_url=database_url, echo=False, pool_workers=1)
    try:
        database.init_db()
    finally:
        database.dispose()


if __name__ == "__main__":
    initialize_database()
    print("Database schema is up to date.")
