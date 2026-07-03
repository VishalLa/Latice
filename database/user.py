from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy import DateTime, String, Column
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, _uuid
from .bank_renc_model import BankStatementModel, LedgerFormatModel, ReconciliationRunModel
from .security import hash_password, verify_password


class User(Base):
    __tablename__ = "user"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    username: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    email: Mapped[Optional[str]] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(String(240), nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    # Relationships
    reconciliation_runs: Mapped[List["ReconciliationRunModel"]] = relationship(back_populates="user")
    bank_statements: Mapped[List["BankStatementModel"]] = relationship(back_populates="user")
    ledger_records: Mapped[List["LedgerFormatModel"]] = relationship(back_populates="user")

    def __repr__(self) -> str:
        return f"<User {self.username!r} email={self.email!r}>"
    
    def __init__(self, password=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if password:
            self.set_password(password=password)

    def set_password(self, password: str) -> None:
        self.password_hash = hash_password(password)

    def check_password(self, password: str) -> bool:
        return verify_password(hashed_password=self.password_hash, plain_password=password)
