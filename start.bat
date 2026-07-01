@echo off
echo Activating virtual environment...
call myenv\Scripts\activate.bat

echo Starting Celery worker in a separate window...
start "Celery Worker" cmd /k "celery -A app.celery worker --loglevel=info --pool=solo"

echo Starting Ollama server in a separate window...
start "Ollama Server" cmd /k "ollama serve"

echo Starting main application...
python main.py

echo Done!
