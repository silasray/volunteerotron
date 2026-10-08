"""Switching an enrichment type between multi-select and single-choice.

A single-choice type can't hold an offer with several of its options chosen, so
switching to single-choice is refused (409) while any offer has more than one.
"""
import pytest
from sqlalchemy import select

from api.models import EnrichmentType, VolunteerInteraction, VolunteerOfferEnrichment

BASE = "/api/acme/spring-fair"
CONFIG = "/api/admin/event-config/acme/spring-fair"


@pytest.fixture
def admin(event, make_user, add_member, auth):
    user = make_user("admin")
    add_member(user, event.organization, is_admin=True)
    return auth(user)


@pytest.fixture
def roles(event, make_type, make_option):
    etype = make_type(event, name="Roles", interaction="multiselect")
    return etype, [make_option(etype, {"Label": v}) for v in ("Gate", "Kitchen", "Parking")]


def choose(api, email, *options):
    resp = api.put(f"{BASE}/offers/{email}",
                   json={"name": email.split("@")[0], "enrichment_ids": [str(o.id) for o in options]})
    assert resp.status_code in (200, 201)


def switch(api, admin, etype, interaction):
    return api.patch(f"{CONFIG}/enrichment-types/{etype.id}",
                     json={"volunteer_interaction": interaction}, headers=admin)


def state(db_ctx, etype):
    db_ctx.expire_all()
    links = sorted(str(e) for e in db_ctx.scalars(select(VolunteerOfferEnrichment.enrichment_id)))
    return db_ctx.get(EnrichmentType, etype.id).volunteer_interaction, links


def test_switch_allowed_when_every_offer_has_at_most_one(api, admin, roles, db_ctx):
    etype, (gate, kitchen, _) = roles
    choose(api, "one@example.com", gate)
    choose(api, "two@example.com", kitchen)
    choose(api, "three@example.com")
    resp = switch(api, admin, etype, "select")
    assert resp.status_code == 200
    assert state(db_ctx, etype)[0] is VolunteerInteraction.SELECT


def test_switch_refused_while_an_offer_has_several(api, admin, roles, db_ctx):
    etype, (gate, kitchen, parking) = roles
    choose(api, "one@example.com", gate, kitchen)
    choose(api, "two@example.com", gate, kitchen, parking)
    choose(api, "three@example.com", parking)
    before = state(db_ctx, etype)

    resp = switch(api, admin, etype, "select")
    assert resp.status_code == 409
    assert resp.json["error"].startswith("2 volunteer(s) chose more than one option")
    assert state(db_ctx, etype) == before  # nothing changed


def test_switch_allowed_once_resolved(api, admin, roles, db_ctx):
    etype, (gate, kitchen, _) = roles
    choose(api, "one@example.com", gate, kitchen)
    assert switch(api, admin, etype, "select").status_code == 409
    choose(api, "one@example.com", kitchen)
    assert switch(api, admin, etype, "select").status_code == 200


def test_switch_back_to_multiselect_always_allowed(api, admin, event, make_type, make_option):
    etype = make_type(event, name="Shirt", interaction="select")
    choose(api, "one@example.com", make_option(etype, {"Label": "M"}))
    assert switch(api, admin, etype, "multiselect").status_code == 200


def test_hidden_type_counts_too(api, admin, roles, db_ctx):
    """Choices stay on offers while a type is hidden, so they still block the switch."""
    etype, (gate, kitchen, _) = roles
    choose(api, "one@example.com", gate, kitchen)
    field_id = etype.fragment_types[0].id
    api.patch(f"{CONFIG}/fields/{field_id}", json={"hidden": True}, headers=admin)
    assert switch(api, admin, etype, "select").status_code == 409


@pytest.mark.xfail(strict=True, reason=(
    "TODO: switching multi-select to single-choice while offers have several "
    "options chosen needs a resolution workflow. Today the admin gets only a count "
    "('2 volunteer(s) chose more than one option...'): no warning naming who is "
    "affected and no way to resolve it from the admin view (pick which option each "
    "keeps, contact them, etc). The assertion below is a placeholder (the refusal "
    "identifies the affected volunteers); replace it with the real design and drop "
    "this marker."))
def test_switch_refusal_supports_resolution(api, admin, roles):
    etype, (gate, kitchen, parking) = roles
    choose(api, "one@example.com", gate, kitchen)
    choose(api, "two@example.com", parking)
    resp = switch(api, admin, etype, "select")
    body = resp.get_data(as_text=True)
    assert "one@example.com" in body
    assert "two@example.com" not in body
