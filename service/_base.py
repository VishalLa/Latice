from __future__ import annotations

from typing import Optional

from database import DatabaseManager
from core.config import Config

class Base:
    def __init__(
        self, 
        db_manager: Optional[DatabaseManager] = None
    ) -> None:
        self.config = Config.from_env()
        if db_manager:
            self.db_manager = db_manager
        else:
            self.db_manager = DatabaseManager(
                db_url=self.config.DATABASE_URL,
                echo=True,
                pool_workers=self.config.POOL_WORKERS,
            )
    
    @property
    def get_manager(self) -> DatabaseManager:
        return self.db_manager
    
    @property
    def get_config(self) -> Config:
        return self.config
    
