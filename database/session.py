from sqlalchemy import create_engine 
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession 

from core.config import settings 

sync_engine = create_engine(
    settings.SQLALCHEMY_SYNC_DATABASE_URI,
    echo=True
)

async_engine = create_async_engine(
    settings.SQLALCHEMY_ASYNC_DATABASE_URI,
    echo=True  
)


SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=sync_engine
)

AsyncSessionLocal = sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False
)

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session

