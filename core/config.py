import os
import logging
from typing import Optional
from urllib.parse import urlparse, urlunparse

from sqlalchemy import create_engine
from sqlalchemy_utils import database_exists, create_database
from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict  # type: ignore
from dotenv import load_dotenv

load_dotenv()


def _default_storage_dir() -> str:
    if os.environ.get("STORAGE_DIR"):
        return os.environ["STORAGE_DIR"]
    if os.environ.get("BILL_UPLOAD_FOLDER"):
        return os.environ["BILL_UPLOAD_FOLDER"]
    if os.path.exists("/.dockerenv") or os.path.isdir("/data"):
        return "/data/uploads"
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "storage")


def _default_ollama_url() -> str:
    host = "ollama" if os.path.exists("/.dockerenv") or os.path.isdir("/data") else "127.0.0.1"
    return f"http://{host}:11434"


def _is_container_runtime() -> bool:
    return os.path.exists("/.dockerenv") or os.path.isdir("/data")


class Config(BaseSettings):

    DEBUG: bool = False

    DATABASE_URL: Optional[str] = None
    POSTGRES_USER: Optional[str] = None
    POSTGRES_PASSWORD: Optional[str] = None
    POSTGRES_HOST: Optional[str] = None
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: Optional[str] = None

    OLLAMA_NAME: str = "phi3:latest"
    OLLAMA_URL: str = os.environ.get("OLLAMA_URL", _default_ollama_url())

    SECRET_KEY: str
    JWT_SECRET_KEY: str
    ALGORITHM: str = "HS256"

    SQLALCHEMY_SYNC_DATABASE_URI: Optional[str] = None
    SQLALCHEMY_ASYNC_DATABASE_URI: Optional[str] = None

    BILL_UPLOAD_FOLDER: str = "/data/uploads"
    STORAGE_DIR: str = _default_storage_dir()

    # celery queue
    QUEUE_DISPATCH: str = "queue_dispatch"        # run_reconciliation_pipeline
    QUEUE_PREPROCESS: str = "queue_preprocess"    # process_pre_data
    QUEUE_RECONCILE: str = "queue_reconcile"      # run_matching
    QUEUE_POSTPROCESS: str = "queue_postprocess"  # finalize_reconciliation
    QUEUE_BILL_LEDGER: str = "queue_bil_ledger"

    LOG_FILE: str = "debug.log"
    LOG_FORMAT: str = "%(asctime)s %(levelname)s %(name)s %(threadName)s : %(message)s"

    SCHEDULER_API_ENABLED: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True,
        extra="ignore"
    )

    def _replace_host(self, url: str, host: str, port: Optional[int] = None) -> str:
        parsed = urlparse(url)
        username = parsed.username or ""
        password = parsed.password or ""
        auth = ""

        if username:
            auth = username
            if password:
                auth += f":{password}"

        host_port = host if port is None else f"{host}:{port}"
        netloc = f"{auth}@{host_port}" if auth else host_port
        return urlunparse(parsed._replace(netloc=netloc))

    def _normalize_postgres_url(self, url: str) -> str:
        url = url.replace("postgres://", "postgresql://", 1)

        if self.POSTGRES_HOST:
            url = self._replace_host(url, self.POSTGRES_HOST, self.POSTGRES_PORT)

        return url

    def _build_postgres_url(self) -> Optional[str]:
        if self.DATABASE_URL:
            if self.DATABASE_URL.startswith(("postgres://", "postgresql://", "postgresql+")):
                return self._normalize_postgres_url(self.DATABASE_URL)

        if self.POSTGRES_USER and self.POSTGRES_HOST and self.POSTGRES_DB:
            credentials = self.POSTGRES_USER
            if self.POSTGRES_PASSWORD:
                credentials += f":{self.POSTGRES_PASSWORD}"

            return (
                f"postgresql://{credentials}@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}"
                f"/{self.POSTGRES_DB}"
            )

        return None

    def model_post_init(self, __context):
        os.makedirs(self.STORAGE_DIR, exist_ok=True)

        if self.OLLAMA_URL:
            parsed_ollama = urlparse(self.OLLAMA_URL)
            hostname = parsed_ollama.hostname
            if hostname in {"0.0.0.0", "127.0.0.1", "localhost", "ollama_server", "ollama"}:
                host = "ollama" if _is_container_runtime() else "127.0.0.1"
                port = parsed_ollama.port or 11434
                self.OLLAMA_URL = urlunparse(parsed_ollama._replace(netloc=f"{host}:{port}"))

        postgres_url = self._build_postgres_url()
        if postgres_url:
            self.SQLALCHEMY_SYNC_DATABASE_URI = postgres_url.replace(
                "postgresql://", "postgresql+psycopg2://", 1
            )
            self.SQLALCHEMY_ASYNC_DATABASE_URI = postgres_url.replace(
                "postgresql://", "postgresql+asyncpg://", 1
            )
            return

        if self.DATABASE_URL and self.DATABASE_URL.startswith("sqlite"):
            self.SQLALCHEMY_SYNC_DATABASE_URI = self.DATABASE_URL
            self.SQLALCHEMY_ASYNC_DATABASE_URI = self.DATABASE_URL.replace(
                "sqlite:///", "sqlite+aiosqlite:///", 1
            )
            logging.warning("SQLite DATABASE_URL is configured. Postgres is preferred.")
            return

        raise ValueError(
            "PostgreSQL configuration is required. Set DATABASE_URL to a postgres URL "
            "or provide POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_HOST, POSTGRES_PORT and POSTGRES_DB."
        )

    @classmethod
    def from_env(cls) -> "Settings":
        try:
            return cls()
        except ValidationError as exc:
            missing = [
                err["loc"][0]
                for err in exc.errors()
                if err.get("type") == "missing" and err.get("loc")
            ]

            if missing:
                raise ValueError(
                    "Missing required environment variable(s): "
                    f"{', '.join(str(m) for m in missing)}. "
                    "Set them in your shell environment or in a .env file."
                ) from exc

            raise ValueError(f"Invalid environment configuration: {exc}") from exc


# settings = Settings.from_env()


def ensure_database_exists(sync_uri: str) -> None:
    """
    Checks if the PostgreSQL database exists. If not, it creates the database.
    SQLite and other non-PostgreSQL URIs are left untouched.
    """

    if not sync_uri:
        return

    if not sync_uri.startswith(("postgres://", "postgresql://", "postgresql+")):
        return

    try:
        engine = create_engine(sync_uri)

        if not database_exists(engine.url):
            create_database(engine.url)
            logging.info(f"Successfully created database at: {engine.url.database}")

    except Exception as e:
        logging.error(f"Failed to check or create database: {e}")

# ensure_database_exists(settings.SQLALCHEMY_SYNC_DATABASE_URI)

