import os

from app import create_app
from core.config import Config
from manage_db import initialize_database


if __name__ == "__main__":
    config = Config.from_env()

    initialize_database()
    app = create_app()

    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        debug=config.DEBUG,   # set DEBUG=true in your local .env to enable
        use_reloader=False,
    )
