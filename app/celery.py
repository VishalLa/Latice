import os
from dotenv import load_dotenv
from celery import Celery
from kombu import Queue

load_dotenv()

app = Celery(
    'xyz',
    broker=os.environ.get("REDIS_URL"),  # where tasks are stored
    backend=os.environ.get("REDIS_URL"),  # where results are stored
    include=[
        'tasks.bank_rec_task'
    ]
)

app.conf.timezone = 'Asia/Kolkata'

app.conf.task_queues = (
    Queue('queue_dispatch'),
    Queue('queue_preprocess'),
    Queue('queue_reconcile'),
    Queue('queue_postprocess'),
    Queue('queue_bil_ledger'),
    Queue('celery'),  # default queue, used by get_data_from_db / generate_report_from_db
)
app.conf.task_default_queue = 'celery'
