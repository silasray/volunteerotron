"""Bearer tokens for authenticated API calls.

A token is the user's id and token_version, signed with the API's SECRET_KEY
and timestamped, so it can't be forged or altered, and expires after
AUTH_TOKEN_MAX_AGE seconds. Logging out (revoke_tokens) increments the user's
token_version, so every token issued to them before stops working, in all
browsers.
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
    return _serializer().dumps({"uid": str(user.id), "ver": user.token_version})


def revoke_tokens(user):
    """Invalidate every token issued to the user so far. The caller commits."""
    user.token_version = User.token_version + 1  # in SQL, so concurrent revokes both count


def _user_from_request():
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    try:
        # SignatureExpired is a BadSignature, so expiry is covered too.
        data = _serializer().loads(token, max_age=current_app.config["AUTH_TOKEN_MAX_AGE"])
        user_id = uuid.UUID(data["uid"])
        version = data["ver"]
    except (BadSignature, KeyError, TypeError, ValueError):
        return None
    # Look the user up each time, so a deleted user's token stops working.
    user = db.session.get(User, user_id)
    if user is None or user.token_version != version:
        return None
    return user


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
