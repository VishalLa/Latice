import os
from passlib.context import CryptContext

from itsdangerous import URLSafeTimedSerializer
from flask import current_app

pwd_context = CryptContext(
    schemes=["bcrypt_sha256"],
    deprecated="auto"
)

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


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