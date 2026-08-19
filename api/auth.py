from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from functools import wraps

from flask import Blueprint, request, jsonify
from flask_jwt_extended import (
    get_jwt_identity,
    jwt_required,
    get_jwt
)

from service import UserService

app = Blueprint("auth", __name__)


_user_service: Optional[UserService] = None

def get_user_service() -> Optional[UserService]:
    global _user_service
    if _user_service is None:
        _user_service = UserService()
    return _user_service


def role_required(role: str):
    def decorator(fn):
        @wraps(fn)
        @jwt_required()
        def wrapper(*args, **kwargs):
            claims = get_jwt()
            
            print(f"Token contains role -> {claims.get('role')}")
            print(f"Route requires role -> {role}")

            if claims.get("role") != role:
                return jsonify({
                    "message": "Forbidden access"
                }), 403

            return fn(*args, **kwargs)
        return wrapper
    return decorator


@app.route('/register', methods=['POST'])
def register_user():
    data = request.json or {}

    required_fields = ("password", "first_name", "phone_no", "email")
    missing_fields = [
        field 
        for field in required_fields 
        if not data.get(field)
    ]
    if missing_fields:
        return jsonify({
            "ok": False,
            "error": f"Missing required fields: {', '.join(missing_fields)}"
        }), 400

    email = data["email"]
    if not UserService.validate_email(email):
        return jsonify({
            "ok": False,
            "error": "Invalid email address"
        }), 400

    result = get_user_service().register(
        password=   data["password"],
        first_name= data["first_name"],
        phone_no=   data["phone_no"],
        email=      email,
        last_name=  data.get("last_name"),
        address=    data.get("address"),
    )

    if not result["ok"]:
        return jsonify(result), 400

    return jsonify(result), 201


@app.route("/login", methods=["POST"])
def login_user():
    data = request.get_json(silent=True) or {}
    email = data.get("email")
    password = data.get("password")

    if not email or not password:
        return jsonify({
            "ok": False,
            "error": "Email and password are required"
        }), 400

    result = get_user_service().login(
        email=email, 
        password=password
    )
    return jsonify(result), 200 if result["ok"] else 401


@app.route("/logout", methods=["POST"])
@jwt_required()
def logout_user():
    claims = get_jwt()
    expires_at = datetime.fromtimestamp(claims["exp"], tz=timezone.utc)
    logged_out = get_user_service().logout(
        jti=claims["jti"],
        expires_at=expires_at,
    )

    return jsonify({
        "ok": logged_out
    }), 200 if logged_out else 500


@app.route("/me", methods=["GET"])
@jwt_required()
def current_user():
    user = get_user_service().get_user(get_jwt_identity())
    if user is None:
        return jsonify({
            "ok": False, 
            "error": "User not found"
        }), 404

    return jsonify({
        "ok": True, 
        "user": user
    }), 200


@app.route("/delete", methods=["DELETE"])
@jwt_required()
def delete_user():
    data = request.get_json(silent=True) or {}
    result = get_user_service().delete_account(
        user_id=get_jwt_identity(),
        password=data.get("password"),
    )

    return jsonify(result), 200 if result["ok"] else 404

