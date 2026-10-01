import hmac
import secrets

from flask import Blueprint, abort, redirect, render_template, request, session, url_for

from . import api_client

bp = Blueprint("admin", __name__, url_prefix="/admin")


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def _check_csrf():
    sent = request.form.get("csrf", "")
    if not hmac.compare_digest(sent, session.get("csrf", "")):
        abort(400)


def current_user():
    return session.get("user")


@bp.get("")
def index():
    if current_user() is None:
        return render_template("admin_login.html", csrf=csrf_token(), name="", error=None)
    return render_template("admin.html", csrf=csrf_token(), user=current_user())


@bp.post("/login")
def login():
    _check_csrf()
    name = request.form.get("name", "").strip()
    password = request.form.get("password", "")
    if not name or not password:
        error, status = "Enter your name and password.", 400
    else:
        # The plain password goes only to the API, which does all hashing.
        status, body = api_client.call(
            "POST", "/auth/login", json={"name": name, "password": password}
        )
        if status == 200:
            # New session on login so a pre-login session can't be fixed onto the user.
            session.clear()
            session.permanent = True
            session["user"] = {"id": body["user"]["id"], "name": body["user"]["name"]}
            return redirect(url_for("admin.index"))
        error = "Invalid name or password." if status in (400, 401) else (
            "Sign-in is unavailable right now. Please try again."
        )
    return render_template(
        "admin_login.html", csrf=csrf_token(), name=name, error=error
    ), status


@bp.post("/logout")
def logout():
    _check_csrf()
    session.clear()
    return redirect(url_for("admin.index"))
