from __future__ import annotations

import sys
from typing import Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from core.config import Config
from database.session import DatabaseManager
from database.user import User, UserRole


def promote(identifier: str) -> bool:
	"""Promote a user identified by email address or user ID."""
	config = Config.from_env()
	manager = DatabaseManager(
		db_url=config.SQLALCHEMY_SYNC_DATABASE_URI,
		pool_workers=config.POOL_WORKERS or 1,
	)

	def _promote(session: Session) -> Optional[tuple[str, str]]:
		user = session.query(User).filter(
			or_(User.email == identifier, User.id == identifier)
		).first()
		if user is None:
			return None

		user.role = UserRole.ADMIN
		return user.id, user.email or ""

	try:
		promoted = manager.run(_promote)
	finally:
		manager.dispose()

	if promoted is None:
		print(f"No user found with email or id={identifier!r}")
		return False

	user_id, email = promoted
	print(f"User {email or user_id!r} is now an ADMIN.")
	return True


if __name__ == "__main__":
	if len(sys.argv) != 2:
		print("Usage: python prompte_admin.py <email-or-user-id>")
		sys.exit(1)

	sys.exit(0 if promote(sys.argv[1]) else 1)
