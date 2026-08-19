from flask import Flask
from flask_jwt_extended import JWTManager

from api import auth
from api import bank_rec_api
from api import ledger_api
from api import pipeline_api
from api import report_api

from core.config import Config


def create_app() -> Flask:
    config = Config.from_env()
    app = Flask(__name__)
    
    app.config["SECRET_KEY"] = config.SECRET_KEY
    app.config["JWT_SECRET_KEY"] = config.JWT_SECRET_KEY
    JWTManager(app)

    app.register_blueprint(auth.app,         url_prefix="/auth")
    app.register_blueprint(pipeline_api.app, url_prefix="/api/pipeline")
    app.register_blueprint(bank_rec_api.app, url_prefix="/api/bank-rec")
    app.register_blueprint(ledger_api.app,   url_prefix="/api/ledger")
    app.register_blueprint(report_api.app,   url_prefix="/api")
    
    return app
