"""Factories for building API test data directly in the database.

Each returns the saved model. Keep them minimal: required fields with
sensible defaults, everything else passed as keyword arguments.
"""
import pytest

from api.auth import issue_token
from api.models import Event, Organization, User, UserOrganization, db

PASSWORD = "correct horse battery"  # meets User.PASSWORD_MIN_LENGTH


@pytest.fixture
def db_ctx(api_app):
    """An app context, for touching db.session directly in a test."""
    with api_app.app_context():
        yield db.session


@pytest.fixture
def make_user(db_ctx):
    def make(name="alice", password=PASSWORD, **kwargs):
        user = User(name=name, **kwargs)
        user.set_password(password)
        db_ctx.add(user)
        db_ctx.commit()
        return user
    return make


@pytest.fixture
def make_org(db_ctx):
    def make(name="acme", pretty_name="Acme", **kwargs):
        org = Organization(name=name, pretty_name=pretty_name, **kwargs)
        db_ctx.add(org)
        db_ctx.commit()
        return org
    return make


@pytest.fixture
def add_member(db_ctx):
    def add(user, org, is_admin=False):
        membership = UserOrganization(user=user, organization=org, is_admin=is_admin)
        db_ctx.add(membership)
        db_ctx.commit()
        return membership
    return add


@pytest.fixture
def make_event(db_ctx):
    def make(org, name="spring-fair", pretty_name="Spring Fair", **kwargs):
        event = Event(organization=org, name=name, pretty_name=pretty_name, **kwargs)
        db_ctx.add(event)
        db_ctx.commit()
        return event
    return make


@pytest.fixture
def auth(db_ctx):
    """auth(user) -> headers for an authenticated API call as that user."""
    def headers(user):
        return {"Authorization": "Bearer " + issue_token(user)}
    return headers
