#!/bin/bash

echo "Activating virtual environment..."
source .venv/bin/activate

echo "Starting Celery worker in the background..."
celery -A app.celery worker --loglevel=info &

echo "Starting Ollama server in the background"
ollama serve &

echo "Starting main application..."
python3 main.py

echo "Done!"
