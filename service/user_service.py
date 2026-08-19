from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, Dict, Tuple, Optional

from flask_jwt_extended import create_access_token
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from database import DatabaseManager, User, RevokedTokenModel

from .helper import _log_db_errors
from ._base import Base


class UserService:
    def __init__(self, db_manager: Optional[DatabaseManager] = None) -> None:
        base = Base(db_manager)
        self.db_manager = base.get_manager


    @staticmethod
    def validate_email(email: str) -> bool:
        if not isinstance(email, str) or len(email) > 254:
            return False

        return bool(
            re.fullmatch(
                r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+"
                r"@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+",
                email,
            )
        )
        
        
    @staticmethod
    def scope_owner_id(
        user: User,
        requested_user_id: Optional[str] = None
    ) -> Tuple[Optional[str], Optional[tuple]]:
        if user is None:
            return None, ({
                "error": "User not found"
            }, 404)
            
        if not user.is_admin:
            return user.id, None
        
        return (requested_user_id or None), None
        
        
    @staticmethod
    def _serialize_user(user: User) -> Dict[str, Any]:
        return {
            "id":         user.id,
            "email":      user.email,
            "first_name": user.first_name,
            "last_name":  user.last_name,
            "phone_no":   user.phone_no,
            "address":    user.address,
            "status":     user.status.value,
            "role":       user.role.value,
            "created_at": user.created_at.isoformat() if user.created_at else None,
        }
        
    @_log_db_errors("registering user")
    def register(
        self,
        password: str,
        first_name: str,
        phone_no: str,
        email: Optional[str] = None,
        last_name: Optional[str] = None,
        address: Optional[str] = None,
    ) -> Dict[str, Any]:
        def _op(session: Session) -> Dict[str, Any]:
            existing = session.query(User).filter(
                User.email == email
            ).first()
            
            if existing is not None:
                return {
                    "ok": False,
                    "error": "Email already exisits"
                }
                
            user = User(
                password=password,
                email=email,
                first_name=first_name,
                last_name=last_name,
                phone_no=phone_no,
                address=address,
            )
            session.add(user)
            try:
                session.flush()
            except IntegrityError:
                session.rollback()
                return {
                    "ok": False,
                    "error": "Email already exisits"
                }
                
            return {
                "ok": True,
                "user": self._serialize_user(user=user)
            }
        
        return self.db_manager.run(_op)
    
    
    @_log_db_errors("logging in user")
    def login(
        self, 
        email: str,
        password: str
    ) -> Dict[str, Any]:
        def _op(session: Session):
            user = session.query(User).filter(
                User.email == email
            ).first()
            if user is None or not user.check_password(password=password):
                return {
                    "ok": False,
                    "error": "Invalid email or password"
                }
                
            user.last_login = int(datetime.now(timezone.utc).timestamp())
            return {
                "ok": True,
                "user": self._serialize_user(user=user)
            }
            
        result = self.db_manager.run(_op)
        if not result["ok"]:
            return result
        
        result["access_token"] = create_access_token(identity=result["user"]["id"])
        result["role"] = result["user"]["role"].value
        return result
    
    
    @_log_db_errors("logging out user")
    def logout(
        self,
        jti: str,
        expires_at: datetime
    ) -> bool:
        def _op(session: Session) -> bool:
            if session.get(RevokedTokenModel, jti) is not None:
                return True
            session.add(RevokedTokenModel(
                jti=jti,
                expires_at=expires_at
            ))
            return True
        
        return self.db_manager.run(_op)
    
    
    @_log_db_errors("checking token revocation")
    def is_token_revoked(self, jti: str) -> bool:
        def _op(session: Session) -> bool:
            return session.get(RevokedTokenModel, jti) is not None
        
        return self.db_manager.run(_op)
    
    
    @_log_db_errors("fetching currect user")
    def get_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        def _op(session: Session) -> Optional[Dict[str, Any]]:
            user = session.query(User).filter(
                User.id == user_id
            ).first()
            
            return self._serialize_user(user) if user is not None else None
        
        return self.db_manager.run(_op)
    
    
    @_log_db_errors("deleting user account")
    def delete_account(
        self,
        user_id: str,
        password: Optional[str] = None
    ) -> Dict[str, Any]:
        def _op(session: Session) -> Dict[str, Any]:
            user = session.query(User).filter(
                User.id == user_id
            ).first()
            
            if user is None:
                return {
                    "ok": False,
                    "error": "User not found"
                }
                
            if password is not None and not user.check_password(password=password):
                return {
                    "ok": False,
                    "error": "Incorrect password"
                }
                
            session.delete(user)
            return {"ok": True}
        
        return self.db_manager.run(_op)
        
