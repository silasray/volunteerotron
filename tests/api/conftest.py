"""Factories for building API test data directly in the database.

Each returns the saved model. Keep them minimal: required fields with
sensible defaults, everything else passed as keyword arguments.
"""
from datetime import datetime, timedelta, timezone

import pytest

from api.auth import issue_token
from api.models import (
    CalendarEnrichment,
    ContentType,
    Enrichment,
    EnrichmentFragmentType,
    EnrichmentType,
    Event,
    Organization,
    TextEnrichment,
    User,
    UserOrganization,
    VolunteerInteraction,
    VolunteerWindow,
    db,
)

from tests.world import PASSWORD  # noqa: F401  (tests import it from here too)


@pytest.fixture
def db_ctx(api_app):
    """A database session owned by the test, inside an app context.

    Separate from db.session: test-client requests reuse this app context and
    close db.session when they finish, which would detach the test's objects.
    Objects keep their loaded values across commits; call expire_all() before
    reading what a request changed.
    """
    from sqlalchemy.orm import Session

    from api.models import db

    with api_app.app_context():
        session = Session(db.engine, expire_on_commit=False)
        yield session
        session.close()


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


def at(day, hour=9):
    """A UTC datetime in November 2026, for windows and calendar values."""
    return datetime(2026, 11, day, hour, tzinfo=timezone.utc)


@pytest.fixture
def event(make_org, make_event):
    """acme/spring-fair, for tests that need one event."""
    return make_event(make_org())


@pytest.fixture
def make_window(db_ctx):
    def make(event, day=1, hours=3):
        window = VolunteerWindow(event=event, start=at(day), end=at(day) + timedelta(hours=hours))
        db_ctx.add(window)
        db_ctx.commit()
        return window
    return make


@pytest.fixture
def make_type(db_ctx):
    """make_type(event, name, fields=[(name, "text"|"calendar", hidden), ...],
    interaction="multiselect") -> the EnrichmentType; fields in .fragment_types."""
    def make(event, name="Shifts", fields=(("Label", "text", False),), interaction="multiselect"):
        etype = EnrichmentType(event=event, name=name,
                               volunteer_interaction=VolunteerInteraction(interaction))
        db_ctx.add(etype)
        for field_name, kind, hidden in fields:
            db_ctx.add(EnrichmentFragmentType(enrichment_type=etype, name=field_name,
                                              content_type=ContentType(kind), hidden=hidden))
        db_ctx.commit()
        return etype
    return make


def field(etype, name):
    return next(f for f in etype.fragment_types if f.name == name)


@pytest.fixture
def make_option(db_ctx):
    """make_option(etype, {field name: "text" or (start, end)}) -> the Enrichment.
    Fields left out get no value."""
    def make(etype, values=None):
        option = Enrichment(enrichment_type=etype)
        db_ctx.add(option)
        for name, value in (values or {}).items():
            fld = field(etype, name)
            if fld.content_type is ContentType.TEXT:
                db_ctx.add(TextEnrichment(enrichment=option, enrichment_fragment_type=fld, value=value))
            else:
                db_ctx.add(CalendarEnrichment(enrichment=option, enrichment_fragment_type=fld,
                                              start=value[0], end=value[1]))
        db_ctx.commit()
        return option
    return make
