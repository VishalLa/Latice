from __future__ import annotations

from email.message import EmailMessage
import os
import smtplib

from flask import current_app

from core.security import generate_verification_token


class EmailService:
    """Send account verification messages through an SMTP server."""

    def send_verification_email(self, email: str) -> None:
        username = os.environ.get("EMAIL_USER")
        password = os.environ.get("EMAIL_PASS")
        if not username or not password:
            raise RuntimeError("EMAIL_USER and EMAIL_PASS must be configured")

        smtp_host = os.environ.get("EMAIL_SMTP_HOST", "smtp.gmail.com")
        try:
            smtp_port = int(os.environ.get("EMAIL_SMTP_PORT", "587"))
        except ValueError as exc:
            raise ValueError("EMAIL_SMTP_PORT must be an integer") from exc

        verification_endpoint = os.environ.get(
            "EMAIL_VERIFICATION_URL",
            "http://localhost:8000/auth/verify-email",
        )
        token = generate_verification_token(email)
        separator = "&" if "?" in verification_endpoint else "?"
        verification_url = f"{verification_endpoint}{separator}token={token}"

        message = EmailMessage()
        message["Subject"] = "Verify your email address"
        message["From"] = username
        message["To"] = email
        message.set_content(
            "Please verify your email address by opening this link:\n\n"
            f"{verification_url}\n\n"
            "This link expires in one hour. If you did not request this, "
            "you can ignore this email."
        )

        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as smtp:
            smtp.starttls()
            smtp.login(username, password)
            smtp.send_message(message)

        current_app.logger.info("Sent an email verification message")
        