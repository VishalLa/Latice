import os 
import subprocess

import logging 
import logging.handlers

from flask import Flask 
from flask_jwt_extended import JWTManager
from flask_cors import CORS
from .celery import app as celery_app

from core.config import settings, ensure_database_exists

from api import bank_rec_api
from api import auth
from api import journal_api
from api import ledger_api
from api import tds_api
from api import gstr1_api
from api import bills_api

from dotenv import load_dotenv

load_dotenv()

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

def create_app():
    app = Flask(__name__)
    allowed_origins = [
        origin.strip()
        for origin in os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",")
        if origin.strip()
    ]
    if allowed_origins:
        CORS(app, origins=allowed_origins, expose_headers=["Content-Disposition"], supports_credentials=True)
    else:
        logging.warning(
            "CORS_ALLOWED_ORIGINS is not set — allowing all origins. "
            "Set it in your .env before deploying so only your frontend's domain can call this API."
        )
        CORS(app)
    
    app.config['CACHE_TYPE'] = os.environ.get('CACHE_TYPE')
    app.config['CACHE_REDIS_URL'] = os.environ.get('REDIS_URL')
    app.config['CACHE_DEFAULT_TIMEOUT'] = os.environ.get('CACHE_DEFAULT_TIMEOUT')
    app.config['SQLALCHEMY_DATABASE_URI'] = settings.SQLALCHEMY_SYNC_DATABASE_URI
    app.config['SECRET_KEY'] = settings.SECRET_KEY

    app.register_blueprint(bank_rec_api.app, url_prefix="/api")
    app.register_blueprint(auth.app, url_prefix="/auth")
    app.register_blueprint(journal_api.app, url_prefix="/api/journal")
    app.register_blueprint(ledger_api.app, url_prefix="/api/ledger")
    app.register_blueprint(tds_api.app, url_prefix="/api/tds")
    app.register_blueprint(gstr1_api.app, url_prefix="/api/gstr1")
    app.register_blueprint(bills_api.app, url_prefix="/api/bills")

    app.config["JWT_SECRET_KEY"] = settings.JWT_SECRET_KEY
    _ = JWTManager(app)

    with app.app_context():
        init_db()
            
    return app

