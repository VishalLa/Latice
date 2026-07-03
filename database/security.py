import os
import random
import hashlib 
from passlib.context import CryptContext

from itsdangerous import URLSafeTimedSerializer
from flask import current_app

pwd_context = CryptContext(
    schemes=["bcrypt"],
    deprecated="auto"
)

def _hash_per_bcrypt(password: str) -> str: 
    return hashlib.sha256(password.encode("utf-8")).hexdigest()

def hash_password(password: str) -> str: 
    safe_password = _hash_per_bcrypt(password)
    return pwd_context.hash(safe_password)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    safe_password = _hash_per_bcrypt(plain_password)
    return pwd_context.verify(safe_password, hashed_password)


def generate_verification_token(email: str) -> str: 
    serializer = URLSafeTimedSerializer(current_app.config["JWT_SECRET_KEY"])
    return serializer.dumps(email, salt=os.environ.get("EMAIL_OTP_SALT"))

def verify_token(token: str, expiration_seconds: int = 3600):
    serializer = URLSafeTimedSerializer(current_app.config["JWT_SECRET_KEY"])

    try:
        email = serializer.loads(token, salt=os.environ.get("EMAIL_OTP_SALT"), max_age=expiration_seconds)
        return email
    except Exception:
        return None