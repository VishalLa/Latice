import logging
import os
from datetime import timedelta

from flask import Flask, jsonify
from flask_cors import CORS
from flask_jwt_extended import JWTManager
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from api.v1 import (
    auth_app,
    bank_rec_app,
    bills_app,
    ledger_app,
    pipeline_app,
    report_app,
)

from core.config import Config

logger = logging.getLogger(__name__)


def _configure_logging(debug: bool) -> None:
    default_level = "DEBUG" if debug else "INFO"
    level_name = os.environ.get("LOG_LEVEL", default_level).upper()
    logging.basicConfig(
        level=getattr(logging, level_name, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _validate_secrets(config: Config) -> None:
    """Refuse to boot in production with missing or weak secrets."""
    if config.DEBUG:
        return
    for name in ("SECRET_KEY", "JWT_SECRET_KEY"):
        value = getattr(config, name, None) or ""
        if len(value) < 32:
            raise RuntimeError(
                f"{name} must be set to a random value of at least 32 characters"
            )
    if config.SECRET_KEY == config.JWT_SECRET_KEY:
        raise RuntimeError("SECRET_KEY and JWT_SECRET_KEY must be different")


def _configure_cors(app: Flask, debug: bool) -> None:
    configured_origins = os.environ.get("CORS_ALLOWED_ORIGINS")
    if configured_origins is None:
        return  # CORS stays disabled: same-origin requests only

    allowed_origins = [
        origin.strip()
        for origin in configured_origins.split(",")
        if origin.strip()
    ]
    if not allowed_origins:
        raise ValueError(
            "CORS_ALLOWED_ORIGINS must contain '*' or at least one origin"
        )
    if "*" in allowed_origins and not debug:
        logger.warning(
            "CORS_ALLOWED_ORIGINS='*' allows every origin; "
            "use an explicit origin list in production"
        )

    CORS(app, origins=allowed_origins, max_age=600)


def _register_error_handlers(app: Flask) -> None:
    @app.errorhandler(HTTPException)
    def handle_http_error(error: HTTPException):
        return jsonify(error=error.name, message=error.description), error.code

    @app.errorhandler(Exception)
    def handle_unexpected_error(error: Exception):
        # Full traceback goes to the logs; clients never see it.
        app.logger.exception("Unhandled exception")
        return jsonify(error="Internal Server Error"), 500


def create_app() -> Flask:
    config = Config.from_env()
    _configure_logging(config.DEBUG)
    _validate_secrets(config)

    app = Flask(__name__)

    # Trust one proxy hop (Caddy) for X-Forwarded-For/Proto/Host.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    access_minutes = int(os.environ.get("JWT_ACCESS_TOKEN_MINUTES", "60"))
    max_upload_mb = int(os.environ.get("MAX_UPLOAD_MB", "50"))

    app.config["SECRET_KEY"] = config.SECRET_KEY
    app.config["JWT_SECRET_KEY"] = config.JWT_SECRET_KEY
    app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(minutes=access_minutes)
    app.config["MAX_CONTENT_LENGTH"] = max_upload_mb * 1024 * 1024
    JWTManager(app)

    _configure_cors(app, config.DEBUG)
    _register_error_handlers(app)

    # Health endpoints (used by Docker healthcheck / load balancers)
    @app.get("/")
    def root():
        return {"status": "running"}, 200

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}, 200

    # v1 apis
    app.register_blueprint(auth_app,     url_prefix="/auth")
    app.register_blueprint(pipeline_app, url_prefix="/api/pipeline")
    app.register_blueprint(bank_rec_app, url_prefix="/api/bank-rec")
    app.register_blueprint(bills_app,     url_prefix="/api/bills")
    app.register_blueprint(ledger_app,   url_prefix="/api/ledger")
    app.register_blueprint(report_app,   url_prefix="/api")

    return app
