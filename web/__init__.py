import os
from datetime import timedelta

from flask import Flask


def create_app():
    app = Flask(__name__)
    app.config.from_mapping(
        # Signs the session cookie. Set a long random SECRET_KEY outside development.
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev"),
        # How the web tier reaches the API: by invoking its Lambda function when
        # API_FUNCTION_NAME is set (deployed), otherwise over HTTP at API_URL.
        API_FUNCTION_NAME=os.environ.get("API_FUNCTION_NAME") or None,
        API_URL=os.environ.get("API_URL", "http://127.0.0.1:5001/api"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # Set SESSION_COOKIE_SECURE=1 when served over HTTPS.
        SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    )

    from . import admin, routes
    app.register_blueprint(routes.bp)
    app.register_blueprint(admin.bp)

    return app
