"""Bearer tokens for authenticated API calls.

A token is the user's id signed with the API's SECRET_KEY and timestamped, so
it can't be forged or altered, and expires after AUTH_TOKEN_MAX_AGE seconds.
Nothing is stored server-side, so a token stays valid until it expires even
after the web session that holds it is signed out.
"""
import uuid
from functools import wraps

from flask import current_app, g, request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from .errors import ApiError
from .models import User, db


def _serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="api-auth-token")


def issue_token(user):
    return _serializer().dumps({"uid": str(user.id)})


def _user_from_request():
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    try:
        # SignatureExpired is a BadSignature, so expiry is covered too.
        data = _serializer().loads(token, max_age=current_app.config["AUTH_TOKEN_MAX_AGE"])
        user_id = uuid.UUID(data["uid"])
    except (BadSignature, KeyError, TypeError, ValueError):
        return None
    # Look the user up each time, so a deleted user's token stops working.
    return db.session.get(User, user_id)


def require_user(view):
    """Reject the request with 401 unless it carries a valid token; sets g.user."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        user = _user_from_request()
        if user is None:
            raise ApiError("authentication required", 401)
        g.user = user
        return view(*args, **kwargs)

    return wrapped
