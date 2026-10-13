import os

from flask import Flask
from flask_cors import CORS
from flask_jwt_extended import JWTManager

from api.v1 import (
    auth_app,
    bank_rec_app,
    ledger_app,
    pipeline_app,
    report_app
)

from core.config import Config


def create_app() -> Flask:
    config = Config.from_env()
    app = Flask(__name__)
    
    app.config["SECRET_KEY"] = config.SECRET_KEY
    app.config["JWT_SECRET_KEY"] = config.JWT_SECRET_KEY
    JWTManager(app)

    configured_origins = os.environ.get("CORS_ALLOWED_ORIGINS")
    if configured_origins is not None:
        allowed_origins = [
            origin.strip()
            for origin in configured_origins.split(",")
            if origin.strip()
        ]
        
        if not allowed_origins:
            raise ValueError("CORS_ALLOWED_ORIGINS must contain '*' or at least one origin")
        
        CORS(app, origins=allowed_origins)

    # v1 apis
    app.register_blueprint(auth_app,     url_prefix="/auth")
    app.register_blueprint(pipeline_app, url_prefix="/api/pipeline")
    app.register_blueprint(bank_rec_app, url_prefix="/api/bank-rec")
    app.register_blueprint(ledger_app,   url_prefix="/api/ledger")
    app.register_blueprint(report_app,   url_prefix="/api")
    
    return app
