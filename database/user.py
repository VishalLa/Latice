from __future__ import annotations

import enum
from datetime import datetime
from typing import List, Optional

from sqlalchemy import DateTime, String, Integer
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, _uuid
from .security import hash_password, verify_password
from .bank_rec_model import ReconciliationRunModel
from .ledger_tax_models import BillModel, JournalEntryModel, TDSEntryModel, GSTR1RecordModel

class UserRole(str, enum.Enum):
    ADMIN = "admin"   
    USER = "user"     


class UserStatus(str, enum.Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class User(Base):
    __tablename__ = "user"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    username: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    email: Mapped[Optional[str]] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(String(240), nullable=False)

    first_name: Mapped[str] = mapped_column(String(36), nullable=False)
    last_name: Mapped[Optional[str]] = mapped_column(String(36))
    phone_no: Mapped[str] = mapped_column(String(15), nullable=False)
    address: Mapped[Optional[str]] = mapped_column(String(255))
    
    status: Mapped[UserStatus] = mapped_column(
        SAEnum(UserStatus, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=UserStatus.ACTIVE,
        server_default=UserStatus.ACTIVE.value,
    )
    last_login: Mapped[Optional[int]] = mapped_column(Integer)

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
