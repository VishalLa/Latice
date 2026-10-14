from werkzeug.middleware.proxy_fix import ProxyFix

from . import create_app

app = create_app()

# Trust exactly one proxy hop (Caddy) for X-Forwarded-* headers.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


# Liveness probe. (Move into create_app() if you prefer.)
@app.get("/healthz")
def healthz():
    return {"status": "ok"}, 200
