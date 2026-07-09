from __future__ import annotations

import enum
from datetime import datetime
from typing import List, Optional

from sqlalchemy import DateTime, String, Column
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, _uuid
from .bank_renc_model import ReconciliationRunModel
from .security import hash_password, verify_password


class UserRole(str, enum.Enum):
    ADMIN = "admin"   
    USER = "user"     

class User(Base):
    __tablename__ = "user"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    username: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    email: Mapped[Optional[str]] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(String(240), nullable=False)

    role: Mapped[UserRole] = mapped_column(
        SAEnum(UserRole, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=UserRole.USER,
        server_default=UserRole.USER.value,
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    reconciliation_runs: Mapped[List["ReconciliationRunModel"]] = relationship(back_populates="user")
    bills: Mapped[List["BillModel"]] = relationship(back_populates="user")
    journal_entries: Mapped[List["JournalEntryModel"]] = relationship(back_populates="user")
    tds_entries: Mapped[List["TDSEntryModel"]] = relationship(back_populates="user")
    gstr1_records: Mapped[List["GSTR1RecordModel"]] = relationship(back_populates="user")

    @property
    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN

    def __repr__(self) -> str:
        return f"<User {self.username!r} email={self.email!r} role={self.role.value}>"
    
    def __init__(self, password=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if password:
            self.set_password(password=password)

    def set_password(self, password: str) -> None:
        self.password_hash = hash_password(password)

    def check_password(self, password: str) -> bool:
        return verify_password(hashed_password=self.password_hash, plain_password=password)
