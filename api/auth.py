from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from core.config import settings
from database.session import get_session
from database.user import User

router = APIRouter()


JWT_SECRET_KEY = settings.JWT_SECRET_KEY
JWT_ALGORITHM = settings.ALGORITHM
JWT_ACCESS_TOKEN_EXPIRES = timedelta(hours=1)

_security = HTTPBearer()


def create_access_token(identity) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(identity),
        "iat": now,
        "exp": now + JWT_ACCESS_TOKEN_EXPIRES,
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def get_jwt_identity(credentials: HTTPAuthorizationCredentials = Depends(_security)) -> str:
 
    token = credentials.credentials
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

    identity = payload.get("sub")
    if identity is None:
        raise HTTPException(status_code=401, detail="Invalid token")
    return identity


class RegisterRequest(BaseModel):
    username: str
    email: Optional[str] = None
    password: str


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/register", status_code=201)
def register_user(body: RegisterRequest):
    username = body.username
    email = body.email
    password = body.password

    if not username or not password:
        raise HTTPException(status_code=400, detail="username and password are required")

    with get_session() as session:
        existing = session.query(User).filter(User.username == username).first()
        if existing is not None:
            raise HTTPException(status_code=409, detail="Username already taken")

        user = User(username=username, email=email, password=password)
        session.add(user)
        session.commit()

        return {"message": "User registered successfully", "username": user.username}


@router.post("/login")
def login_user(body: LoginRequest):
    username = body.username
    password = body.password

    if not username or not password:
        raise HTTPException(status_code=400, detail="username and password are required")

    with get_session() as session:
        user = session.query(User).filter(User.username == username).first()
        if user is None or not user.check_password(password):
            raise HTTPException(status_code=401, detail="Invalid username or password")

        access_token = create_access_token(identity=user.id)
        return {"access_token": access_token, "user_id": user.id}


@router.get("/me")
def current_user(current_user_id: str = Depends(get_jwt_identity)):
    with get_session() as session:
        user = session.query(User).filter(User.id == current_user_id).first()
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")

        return {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "created_at": user.created_at.isoformat(),
        }
    