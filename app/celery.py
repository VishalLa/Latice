from __future__ import annotations

from celery import Celery
from kombu import Queue

from core.config import Config

def create_celery(config: Config) -> Celery:
    app = Celery(
        "Lattic",
        broker=config.REDIS_URL,
        backend=config.REDIS_URL,
        include=[
            "tasks.bank_rec",
            "tasks.bill_pipeline",
            "tasks.generate_report_tasks",
        ]
    )
    
    app.conf.timezone = "Asia/Kolkata"
    app.conf.task_queues = (
        Queue("celery")
    )
    app.conf.task_default_queue = "celery"

    return app

celery_app = create_celery(config=Config.from_env())

