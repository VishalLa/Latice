"""
Shared scoping helper for the Ledger/Journal/TDS/GSTR-1/Bills API routes.

Every route in those blueprints needs the same thing: figure out who's
asking (via JWT), and whether they should see only their own data or
every user's data (UserRole.ADMIN). Centralised here so that logic can't
drift between blueprints.
"""
from __future__ import annotations

from typing import Optional, Tuple

from flask import jsonify
from flask_jwt_extended import get_jwt_identity
from sqlalchemy.orm import Session

from database.user import User


def current_user(session: Session) -> Optional[User]:
    """Looks up the User row for the current JWT identity. None if the
    token's subject no longer exists (deleted user)."""
    user_id = get_jwt_identity()
    if not user_id:
        return None
    return session.query(User).filter(User.id == user_id).first()


def scope_owner_id(user: User, requested_user_id: Optional[str]) -> Tuple[Optional[str], Optional[tuple]]:
    """
    Decides which user_id a query should be filtered by.

      - Regular user: always scoped to their own id. `requested_user_id`
        is ignored (a non-admin can never read someone else's data by
        passing ?user_id=... on the query string).
      - Admin, no requested_user_id: None -> caller should NOT filter by
        user_id at all (sees every user's rows).
      - Admin, with requested_user_id: scoped to that specific user (lets
        an admin drill into one user's data).

    Returns (owner_id_or_None, error_response_or_None). If the second
    element is not None, the caller should return it immediately.
    """
    if user is None:
        return None, (jsonify({"error": "User not found"}), 404)

    if not user.is_admin:
        return user.id, None

    return (requested_user_id or None), None
