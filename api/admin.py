"""Endpoints for signed-in admin users. Every route requires a bearer token.

Permissions come from organization membership (UserOrganization): a member of
an organization can see and manage all of its events, and an organization
admin (is_admin) can also configure them and create new ones. User management
is for superusers only.
"""
from functools import wraps

from flask import Blueprint, g, jsonify, request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .auth import require_user
from .errors import ApiError
from .models import Event, Organization, User, UserOrganization, db, url_name_problem

bp = Blueprint("api_admin", __name__)


def _text_field(data, key, label):
    value = data.get(key)
    value = value.strip() if isinstance(value, str) else ""
    if not value:
        raise ApiError(f"{label} is required")
    if len(value) > 255:
        raise ApiError(f"{label} must be at most 255 characters")
    return value


@bp.get("/me")
@require_user
def me():
    """The signed-in user, their organizations and each organization's events."""
    organizations = []
    for membership in sorted(g.user.memberships, key=lambda m: m.organization.pretty_name):
        org = membership.organization
        events = sorted(org.events, key=lambda e: e.pretty_name)
        organizations.append({
            **org.to_dict(),
            "is_admin": membership.is_admin,
            "events": [{"name": e.name, "pretty_name": e.pretty_name} for e in events],
        })
    return jsonify(user=g.user.to_dict(), organizations=organizations)


@bp.post("/<organization>/events")
@require_user
def create_event(organization):
    """Create an event. Body: {"name": ..., "pretty_name": ...}.

    Only admins of the organization may create one. Non-members get 404, so the
    response doesn't reveal which organizations exist; members who aren't
    admins already know it exists and get 403.
    """
    membership = db.session.scalar(
        select(UserOrganization)
        .join(Organization)
        .filter(UserOrganization.user_id == g.user.id, Organization.name == organization)
    )
    if membership is None:
        raise ApiError("organization not found", 404)
    if not membership.is_admin:
        raise ApiError("only organization admins can create events", 403)

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError("a JSON object body is required")
    name = _text_field(data, "name", "name")
    pretty_name = _text_field(data, "pretty_name", "pretty name")
    if problem := url_name_problem(name):
        raise ApiError(problem)

    org = membership.organization
    if db.session.scalar(select(Event).filter_by(organization_id=org.id, name=name)):
        raise ApiError(f"an event named {name!r} already exists in this organization", 409)
    event = Event(name=name, pretty_name=pretty_name, organization=org)
    db.session.add(event)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise ApiError(f"an event named {name!r} already exists in this organization", 409)
    return jsonify(event.to_dict()), 201


def require_superuser(view):
    """Like require_user, but non-superusers get 404 so the routes stay hidden."""

    @wraps(view)
    @require_user
    def wrapped(*args, **kwargs):
        if not g.user.is_superuser:
            raise ApiError("not found", 404)
        return view(*args, **kwargs)

    return wrapped


def _password_field(data):
    password = data.get("password")
    if problem := User.password_problem(password):
        raise ApiError(problem)
    return password


def _json_body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError("a JSON object body is required")
    return data


def _user_or_404(user_id):
    user = db.session.get(User, user_id)
    if user is None:
        raise ApiError("user not found", 404)
    return user


@bp.get("/users")
@require_superuser
def list_users():
    users = db.session.scalars(select(User).order_by(User.name))
    return jsonify([u.to_dict() for u in users])


@bp.post("/users")
@require_superuser
def create_user():
    """Body: {"name": ..., "password": ...}. The password is hashed here."""
    data = _json_body()
    name = _text_field(data, "name", "name")
    password = _password_field(data)
    if db.session.scalar(select(User).filter_by(name=name)):
        raise ApiError(f"a user named {name!r} already exists", 409)
    user = User(name=name)
    user.set_password(password)
    db.session.add(user)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise ApiError(f"a user named {name!r} already exists", 409)
    return jsonify(user.to_dict()), 201


@bp.patch("/users/<uuid:user_id>")
@require_superuser
def update_user(user_id):
    """Body: {"password": ...} and/or {"is_superuser": true|false}.

    You can't change your own superuser status (so there's always at least the
    acting superuser left), but you can change your own password.
    """
    user = _user_or_404(user_id)
    data = _json_body()
    if "password" not in data and "is_superuser" not in data:
        raise ApiError("nothing to update: send password and/or is_superuser")
    if "is_superuser" in data:
        if not isinstance(data["is_superuser"], bool):
            raise ApiError("is_superuser must be true or false")
        if user.id == g.user.id:
            raise ApiError("you can't change your own superuser status")
        user.is_superuser = data["is_superuser"]
    if "password" in data:
        user.set_password(_password_field(data))
    db.session.commit()
    return jsonify(user.to_dict())


@bp.delete("/users/<uuid:user_id>")
@require_superuser
def delete_user(user_id):
    """Delete a user and their organization memberships. Not yourself."""
    user = _user_or_404(user_id)
    if user.id == g.user.id:
        raise ApiError("you can't delete yourself")
    for membership in list(user.memberships):
        db.session.delete(membership)
    db.session.delete(user)
    db.session.commit()
    return "", 204


def _org_dict(org):
    members = sorted(org.memberships, key=lambda m: m.user.name)
    return {
        **org.to_dict(),
        "members": [{"user_id": str(m.user_id), "is_admin": m.is_admin} for m in members],
    }


def _org_or_404(org_id):
    org = db.session.get(Organization, org_id)
    if org is None:
        raise ApiError("organization not found", 404)
    return org


@bp.get("/organizations")
@require_superuser
def list_organizations():
    orgs = db.session.scalars(select(Organization).order_by(Organization.name))
    return jsonify([_org_dict(o) for o in orgs])


@bp.post("/organizations")
@require_superuser
def create_organization():
    """Body: {"name": ..., "pretty_name": ...}. The name is used in URLs."""
    data = _json_body()
    name = _text_field(data, "name", "name")
    pretty_name = _text_field(data, "pretty_name", "pretty name")
    if problem := url_name_problem(name):
        raise ApiError(problem)
    if db.session.scalar(select(Organization).filter_by(name=name)):
        raise ApiError(f"an organization named {name!r} already exists", 409)
    org = Organization(name=name, pretty_name=pretty_name)
    db.session.add(org)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise ApiError(f"an organization named {name!r} already exists", 409)
    return jsonify(_org_dict(org)), 201


@bp.patch("/organizations/<uuid:org_id>")
@require_superuser
def update_organization(org_id):
    """Body: {"pretty_name": ...}."""
    org = _org_or_404(org_id)
    org.pretty_name = _text_field(_json_body(), "pretty_name", "pretty name")
    db.session.commit()
    return jsonify(_org_dict(org))


@bp.put("/organizations/<uuid:org_id>/members/<uuid:user_id>")
@require_superuser
def set_membership(org_id, user_id):
    """Body: {"is_admin": true|false}. Adds the user to the org if needed."""
    org = _org_or_404(org_id)
    user = _user_or_404(user_id)
    is_admin = _json_body().get("is_admin", False)
    if not isinstance(is_admin, bool):
        raise ApiError("is_admin must be true or false")
    membership = db.session.scalar(
        select(UserOrganization).filter_by(organization_id=org.id, user_id=user.id)
    )
    created = membership is None
    if created:
        membership = UserOrganization(organization=org, user=user)
        db.session.add(membership)
    membership.is_admin = is_admin
    try:
        db.session.commit()
    except IntegrityError:
        # Added concurrently by someone else; apply the admin status to theirs.
        db.session.rollback()
        membership = db.session.scalar(
            select(UserOrganization).filter_by(organization_id=org.id, user_id=user.id)
        )
        membership.is_admin = is_admin
        db.session.commit()
        created = False
    return jsonify(_org_dict(org)), 201 if created else 200


@bp.delete("/organizations/<uuid:org_id>/members/<uuid:user_id>")
@require_superuser
def remove_membership(org_id, user_id):
    org = _org_or_404(org_id)
    membership = db.session.scalar(
        select(UserOrganization).filter_by(organization_id=org.id, user_id=user_id)
    )
    if membership is None:
        raise ApiError("that user isn't a member of this organization", 404)
    db.session.delete(membership)
    db.session.commit()
    return "", 204
