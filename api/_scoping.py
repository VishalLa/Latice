from __future__ import annotations

from typing import Optional, Tuple

from flask import jsonify
from flask_jwt_extended import get_jwt_identity
from sqlalchemy.orm import Session

from database.user import User


def current_user(session: Session) -> Optional[User]:
    user_id = get_jwt_identity()
    if not user_id:
        return None
    return session.query(User).filter(User.id == user_id).first()


def scope_owner_id(user: User, requested_user_id: Optional[str]) -> Tuple[Optional[str], Optional[tuple]]:
    if user is None:
        return None, (jsonify({"error": "User not found"}), 404)

    if not user.is_admin:
        return user.id, None

    return (requested_user_id or None), None
