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
    # CORS(app, expose_headers=["Content-Disposition"])
    CORS(app)
    
    app.config['CACHE_TYPE'] = os.environ.get('CACHE_TYPE')
    app.config['CACHE_REDIS_URL'] = os.environ.get('REDIS_URL')
    app.config['CACHE_DEFAULT_TIMEOUT'] = os.environ.get('CACHE_DEFAULT_TIMEOUT')
    app.config['SQLALCHEMY_DATABASE_URI'] = settings.SQLALCHEMY_SYNC_DATABASE_URI
    app.config['SECRET_KEY'] = settings.SECRET_KEY

    app.register_blueprint(bank_rec_api.app, url_prefix="/api")
    app.register_blueprint(auth.app, url_prefix="/auth")

    app.config["JWT_SECRET_KEY"] = settings.JWT_SECRET_KEY
    _ = JWTManager(app)

    with app.app_context():
        init_db()
            
    return app

