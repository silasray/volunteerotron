"""Test data shared by the API and integration permission tests.

build_world(session) creates two organizations, each with an event fully set
up, and one user per role:

  acme/spring-fair: the event the permission matrices target. Has a window, an
    enrichment type with a field and an option, and a volunteer signed up for both.
  globex/gala: another org's event, for cross-organization checks.

  outsider (no memberships), member (acme), admin (acme admin),
  other_admin (globex admin only), superuser (no memberships).

It returns a dict of ids (as strings), e.g. w["window"], w["globex_window"],
w["member_id"], and w["tokens"][role], a valid API token per role.
"""
from datetime import datetime, timezone

from sqlalchemy import func, select

from api.auth import issue_token
from api.models import (
    ContentType,
    Enrichment,
    EnrichmentFragmentType,
    EnrichmentType,
    Event,
    Organization,
    User,
    UserOrganization,
    VolunteerInteraction,
    VolunteerOffer,
    VolunteerOfferEnrichment,
    VolunteerWindow,
    VolunteerWindowOffer,
    db,
)

PASSWORD = "correct horse battery"  # meets User.PASSWORD_MIN_LENGTH
ROLES = ("outsider", "member", "admin", "other_admin", "superuser")


def build_world(session):
    def user(name, **kwargs):
        u = User(name=name, **kwargs)
        u.set_password(PASSWORD)
        session.add(u)
        return u

    def event_with_data(org_name, event_name, email):
        org = Organization(name=org_name, pretty_name=org_name.title())
        event = Event(organization=org, name=event_name, pretty_name=event_name.title())
        window = VolunteerWindow(
            event=event,
            start=datetime(2026, 11, 1, 9, tzinfo=timezone.utc),
            end=datetime(2026, 11, 1, 12, tzinfo=timezone.utc),
        )
        etype = EnrichmentType(event=event, name="Shirt size",
                               volunteer_interaction=VolunteerInteraction.SELECT)
        fld = EnrichmentFragmentType(enrichment_type=etype, name="Size",
                                     content_type=ContentType.TEXT)
        option = Enrichment(enrichment_type=etype)
        offer = VolunteerOffer(event=event, email=email, name="Vol")
        signup = VolunteerWindowOffer(volunteer_offer=offer, volunteer_window=window)
        session.add_all([org, event, window, etype, fld, option, offer, signup,
                         VolunteerOfferEnrichment(volunteer_offer=offer, enrichment=option)])
        return {"org": org, "window": window, "etype": etype, "field": fld,
                "enrichment": option, "signup": signup}

    acme = event_with_data("acme", "spring-fair", "vol@example.com")
    globex = event_with_data("globex", "gala", "vol2@example.com")
    users = {
        "outsider": user("outsider"),
        "member": user("member"),
        "admin": user("admin"),
        "other_admin": user("other_admin"),
        "superuser": user("superuser", is_superuser=True),
    }
    session.add_all([
        UserOrganization(user=users["member"], organization=acme["org"]),
        UserOrganization(user=users["admin"], organization=acme["org"], is_admin=True),
        UserOrganization(user=users["other_admin"], organization=globex["org"], is_admin=True),
    ])
    session.commit()

    w = {k: str(v.id) for k, v in acme.items()}
    w |= {"globex_" + k: str(v.id) for k, v in globex.items()}
    w |= {role + "_id": str(u.id) for role, u in users.items()}
    w["tokens"] = {role: issue_token(u) for role, u in users.items()}
    return w


def row_counts(session):
    """Rows per table, to show a denied request changed nothing."""
    session.rollback()  # see the latest committed state
    return {
        table.name: session.scalar(select(func.count()).select_from(table))
        for table in db.metadata.sorted_tables
        if table.name != "rate_limit"  # login counters aren't app data
    }
