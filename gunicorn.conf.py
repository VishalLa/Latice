import multiprocessing
import os

bind = "0.0.0.0:8000"

workers = int(os.getenv("WEB_CONCURRENCY", min(multiprocessing.cpu_count() * 2 + 1, 9)))
worker_class = "gthread"
threads = int(os.getenv("GUNICORN_THREADS", "2"))

timeout = 120 
graceful_timeout = 30
keepalive = 5
max_requests = 1000
max_requests_jitter = 100
worker_tmp_dir = "/dev/shm"

forwarded_allow_ips = "*"

accesslog = "-"
errorlog = "-"
loglevel = os.getenv("LOG_LEVEL", "info")
