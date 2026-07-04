import os
import subprocess

import logging
import logging.handlers
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .celery import app as celery_app

from core.config import settings, ensure_database_exists

from dotenv import load_dotenv

load_dotenv()

from api import bank_rec_api
from api import auth

LOG_FORMAT = settings.LOG_FORMAT
LOG_FILE = settings.LOG_FILE


def init_db():
    ensure_database_exists(settings.SQLALCHEMY_SYNC_DATABASE_URI)
    from database.session import create_tables
    create_tables()


def run_ollama():
    try:
        subprocess.run(["ollama", "serve"], check=True)
    except FileNotFoundError:
        logging.error("Failed to start: Ollama is not installed or not found in system PATH.")
    except Exception as e:
        logging.error(f"Ollama server process encountered an error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Content-Disposition"],
    )

    app.state.cache_type = os.environ.get("CACHE_TYPE")
    app.state.cache_redis_url = os.environ.get("REDIS_URL")
    app.state.cache_default_timeout = os.environ.get("CACHE_DEFAULT_TIMEOUT")
    app.state.sqlalchemy_database_uri = settings.SQLALCHEMY_SYNC_DATABASE_URI
    app.state.secret_key = settings.SECRET_KEY

    app.include_router(bank_rec_api.router, prefix="/api")
    app.include_router(auth.router, prefix="/auth")

    return app
