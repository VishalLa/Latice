from app import create_app
from core.config import Config
from database import DatabaseManager


def initialize_database() -> None:
    """Create the SQLAlchemy schema before the HTTP server accepts requests."""
    config = Config.from_env()
    database_url = config.SQLALCHEMY_SYNC_DATABASE_URI or config.DATABASE_URL
    if not database_url:
        raise RuntimeError("DATABASE_URL is required to initialize the database")

    database = DatabaseManager(
        db_url=database_url,
        echo=config.DEBUG,
        pool_workers=1,
    )
    try:
        database.init_db()
    finally:
        database.dispose()

if __name__ == '__main__':
    initialize_database()
    app = create_app()
    app.run(host='0.0.0.0', port=8000, debug=True, use_reloader=False)  

