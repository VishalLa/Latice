import os 
from dotenv import load_dotenv
from celery import Celery
from celery.schedules import crontab

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
