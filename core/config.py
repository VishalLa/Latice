import logging

from typing import Optional

from sqlalchemy import create_engine 
from sqlalchemy_utils import database_exists, create_database
from pydantic_settings import BaseSettings, SettingsConfigDict # type: ignore


class Settings(BaseSettings):

    DEBUG: bool = False

    DATABASE_URL: str
    
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    
    SQLALCHEMY_SYNC_DATABASE_URI: Optional[str] = None
    SQLALCHEMY_ASYNC_DATABASE_URI: Optional[str] = None
    

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True,
        extra="ignore"
    )

    LOG_FILE: str = "debug.log"
    LOG_FORMAT: str = "%(asctime)s %(levelname)s %(name)s %(threadName)s : %(message)s"

    def model_post_init(self, __context):
        if self.DATABASE_URL.startswith("postgres"):
            base_url = self.DATABASE_URL.replace("postgres://", "postgresql://")

            self.SQLALCHEMY_SYNC_DATABASE_URI = base_url.replace(
                "postgresql://", "postgresql+psycopg2://"
            )
            
            self.SQLALCHEMY_ASYNC_DATABASE_URI = base_url.replace(
                "postgresql://", "postgresql+asyncpg://"
            )

        elif self.DATABASE_URL.startswith("sqlite"):
            self.SQLALCHEMY_SYNC_DATABASE_URI = self.DATABASE_URL

            self.SQLALCHEMY_ASYNC_DATABASE_URI = self.DATABASE_URL.replace(
                "sqlite:///", "sqlite+aiosqlite:///"
            )

        else: 
            self.SQLALCHEMY_SYNC_DATABASE_URI = self.DATABASE_URL
            self.SQLALCHEMY_ASYNC_DATABASE_URI = self.DATABASE_URL

settings = Settings()


def ensure_database_exists(sync_uri: str) -> None: 
    """
    Checks if the PostgreSQL (or SQLite) database exists. 
    If not, it creates a new database based on the provided URI.
    """

    if not sync_uri:
        return 
    
    try: 
        engine = create_engine(sync_uri)

        if not database_exists(engine.url):
            create_database(engine.url)
            logging.info(f"Successfully created database at: {engine.url.database}")

    except Exception as e:
        logging.error(f"Failed to check or create database: {e}")

ensure_database_exists(settings.SQLALCHEMY_SYNC_DATABASE_URI)
