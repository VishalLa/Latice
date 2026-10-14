FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    STORAGE_DIR=/data/uploads

RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY req.txt .
RUN pip install -r req.txt && pip install "gunicorn>=22"

COPY . .

RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /data/uploads /home/appuser/.paddleocr \
    && chown -R appuser:appuser /app /data /home/appuser

USER appuser
EXPOSE 8000

CMD ["gunicorn", "wsgi:app", "-c", "gunicorn.conf.py"]
