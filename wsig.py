"""Production WSGI entrypoint:  gunicorn wsgi:app -c gunicorn.conf.py"""
from app import create_app

app = create_app()
