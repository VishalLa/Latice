from flask import Blueprint, request, jsonify, send_file
from flask_jwt_extended import (
    create_access_token,
    get_jwt_identity,
    jwt_required,
)
from database.session import get_session
from database.user import User

app = Blueprint("auth", __name__)

@app.route('/register', methods=['POST'])
def register_user():
    data = request.json or {}
    username = data.get('username')
    email = data.get('email')
    password = data.get('password')

    if not username or not password:
        return jsonify({"error": "username and password are required"}), 400

    with get_session() as session:
        existing = session.query(User).filter(User.username == username).first()
        if existing is not None:
            return jsonify({"error": "Username already taken"}), 409

        user = User(username=username, email=email, password=password)
        session.add(user)
        session.commit()

        return jsonify({"message": "User registered successfully", "username": user.username}), 201


@app.route('/login', methods=['POST'])
def login_user():
    data = request.json or {}
    username = data.get('username')
    password = data.get('password')

    if not username or not password:
        return jsonify({"error": "username and password are required"}), 400

    with get_session() as session:
        user = session.query(User).filter(User.username == username).first()
        if user is None or not user.check_password(password):
            return jsonify({"error": "Invalid username or password"}), 401

        access_token = create_access_token(identity=user.id)
        return jsonify({"access_token": access_token, "user_id": user.id}), 200


@app.route('/me', methods=['GET'])
@jwt_required()
def current_user():
    current_user_id = get_jwt_identity()
    with get_session() as session:
        user = session.query(User).filter(User.id == current_user_id).first()
        if user is None:
            return jsonify({"error": "User not found"}), 404

        return jsonify({
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "created_at": user.created_at.isoformat(),
        }), 200
    