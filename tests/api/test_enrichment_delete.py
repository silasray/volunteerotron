"""Deleting options (enrichments), enrichment types and fields from the configure page.

  DELETE .../enrichments/<id>       409 while volunteers chose it, unless
                                    {"cascade": true} (drop their choice) or
                                    {"reassign_to": <id>} (move it to another
                                    option of the same type)
  DELETE .../enrichment-types/<id>  409 while any of its options is chosen, unless cascade
  DELETE .../fields/<id>            409 while any row has a value in it, unless cascade;
                                    volunteers link to rows, so no choice is affected
None of these is a volunteer change, so offers' updated_on stays put.
"""
import uuid

import pytest
from sqlalchemy import func, select

from api.models import (
    CalendarEnrichment,
    Enrichment,
    EnrichmentFragmentType,
    EnrichmentType,
    TextEnrichment,
    VolunteerOffer,
    VolunteerOfferEnrichment,
)

from .conftest import at

BASE = "/api/acme/spring-fair"
CONFIG = "/api/admin/event-config/acme/spring-fair"


@pytest.fixture
def admin(event, make_user, add_member, auth):
    user = make_user("admin")
    add_member(user, event.organization, is_admin=True)
    return auth(user)


class Ref:
    """Just an id: a deleted model instance can't be read once the session expires."""

    def __init__(self, obj):
        self.id = obj.id


@pytest.fixture
def roles(event, make_type, make_option):
    etype = make_type(event, name="Roles", interaction="multiselect")
    return Ref(etype), [Ref(make_option(etype, {"Label": v})) for v in ("Gate", "Kitchen", "Parking")]


def choose(api, email, *options):
    resp = api.put(f"{BASE}/offers/{email}", json={"name": "Vol", "enrichment_ids": [str(o.id) for o in options]})
    assert resp.status_code in (200, 201)


def chosen(db_ctx, email):
    db_ctx.expire_all()
    return sorted(str(e) for e in db_ctx.scalars(
        select(VolunteerOfferEnrichment.enrichment_id).join(VolunteerOffer).filter(VolunteerOffer.email == email)))


def ids(*objs):
    return sorted(str(o.id) for o in objs)


def count(db_ctx, model):
    db_ctx.expire_all()
    return db_ctx.scalar(select(func.count()).select_from(model))


def exists(db_ctx, model, ref):
    db_ctx.expire_all()
    return db_ctx.get(model, ref.id) is not None


def delete_option(api, admin, option, body=None):
    return api.delete(f"{CONFIG}/enrichments/{option.id}", json=body, headers=admin)


# ---------------------------------------------------------------- options


def test_unchosen_option_deletes_with_its_values(api, admin, roles, db_ctx):
    _, (gate, _, _) = roles
    resp = delete_option(api, admin, gate)
    assert resp.status_code == 200
    assert resp.json == {"deleted": True, "moved": 0}
    assert not exists(db_ctx, Enrichment, gate)
    assert count(db_ctx, TextEnrichment) == 2  # the other options' labels


def test_chosen_option_needs_a_decision(api, admin, roles, db_ctx):
    _, (gate, kitchen, _) = roles
    choose(api, "a@example.com", gate)
    choose(api, "b@example.com", gate, kitchen)
    resp = delete_option(api, admin, gate)
    assert resp.status_code == 409
    assert resp.json["linked_count"] == 2
    assert exists(db_ctx, Enrichment, gate)
    assert chosen(db_ctx, "b@example.com") == ids(gate, kitchen)


def test_cascade_drops_the_choice_only(api, admin, roles, db_ctx):
    _, (gate, kitchen, _) = roles
    choose(api, "a@example.com", gate, kitchen)
    resp = delete_option(api, admin, gate, {"cascade": True})
    assert resp.json == {"deleted": True, "moved": 0}
    assert chosen(db_ctx, "a@example.com") == ids(kitchen)


def test_reassign_moves_the_choice(api, admin, roles, db_ctx):
    _, (gate, kitchen, parking) = roles
    choose(api, "a@example.com", gate)
    choose(api, "b@example.com", gate, parking)
    resp = delete_option(api, admin, gate, {"reassign_to": str(kitchen.id)})
    assert resp.status_code == 200
    assert resp.json == {"deleted": True, "moved": 2}
    assert chosen(db_ctx, "a@example.com") == ids(kitchen)
    assert chosen(db_ctx, "b@example.com") == ids(kitchen, parking)
    assert not exists(db_ctx, Enrichment, gate)


# The dropped link must be deleted once; a second delete only shows as a warning.
@pytest.mark.filterwarnings("error::sqlalchemy.exc.SAWarning")
def test_reassign_onto_an_existing_choice_drops_the_duplicate(api, admin, roles, db_ctx):
    _, (gate, kitchen, _) = roles
    choose(api, "a@example.com", gate, kitchen)
    resp = delete_option(api, admin, gate, {"reassign_to": str(kitchen.id)})
    assert resp.json == {"deleted": True, "moved": 0}
    assert chosen(db_ctx, "a@example.com") == ids(kitchen)


def test_reassign_within_single_choice_keeps_one_link(api, admin, event, make_type, make_option, db_ctx):
    shirt = make_type(event, name="Shirt", interaction="select")
    small, large = make_option(shirt, {"Label": "S"}), make_option(shirt, {"Label": "L"})
    choose(api, "a@example.com", small)
    choose(api, "b@example.com", large)
    assert delete_option(api, admin, small, {"reassign_to": str(large.id)}).status_code == 200
    assert chosen(db_ctx, "a@example.com") == ids(large)
    assert chosen(db_ctx, "b@example.com") == ids(large)


@pytest.mark.parametrize("target", ["same", "other type", "other event", "unknown", "not a uuid"])
def test_bad_reassign_target_changes_nothing(api, admin, roles, event, make_type, make_option,
                                             make_org, make_event, db_ctx, target):
    _, (gate, _, _) = roles
    choose(api, "a@example.com", gate)
    if target == "same":
        target_id, status = str(gate.id), 400
    elif target == "other type":
        target_id, status = str(make_option(make_type(event, name="Diet"), {"Label": "Vegan"}).id), 400
    elif target == "other event":
        other = make_event(make_org("globex", "Globex"), name="gala")
        target_id, status = str(make_option(make_type(other, name="Roles"), {"Label": "Gate"}).id), 404
    elif target == "unknown":
        target_id, status = str(uuid.uuid4()), 404
    else:
        target_id, status = "nope", 400
    resp = delete_option(api, admin, gate, {"reassign_to": target_id})
    assert resp.status_code == status
    assert exists(db_ctx, Enrichment, gate)
    assert chosen(db_ctx, "a@example.com") == ids(gate)


# ---------------------------------------------------------------- enrichment types


def delete_type(api, admin, etype, body=None):
    return api.delete(f"{CONFIG}/enrichment-types/{etype.id}", json=body, headers=admin)


def test_unchosen_type_deletes_with_fields_options_and_values(api, admin, event, make_type, make_option, db_ctx):
    etype = make_type(event, name="Slots", fields=(("Label", "text", False), ("When", "calendar", True)))
    make_option(etype, {"Label": "Early", "When": (at(1, 8), at(1, 9))})
    make_option(etype, {"Label": "Late"})
    etype = Ref(etype)
    resp = delete_type(api, admin, etype)
    assert resp.status_code == 200
    assert resp.json == {"deleted": True, "removed": 0}
    assert not exists(db_ctx, EnrichmentType, etype)
    for model in (Enrichment, EnrichmentFragmentType, TextEnrichment, CalendarEnrichment):
        assert count(db_ctx, model) == 0, model.__name__


def test_chosen_type_needs_cascade(api, admin, roles, db_ctx):
    etype, (gate, kitchen, parking) = roles
    choose(api, "a@example.com", gate, kitchen)
    choose(api, "b@example.com", parking)
    resp = delete_type(api, admin, etype)
    assert resp.status_code == 409
    assert resp.json["linked_count"] == 3
    assert exists(db_ctx, EnrichmentType, etype)
    # reassign isn't an option for a whole type
    assert delete_type(api, admin, etype, {"reassign_to": str(gate.id)}).status_code == 409


def test_type_cascade_drops_only_that_types_choices(api, admin, roles, event, make_type, make_option, db_ctx):
    etype, (gate, _, _) = roles
    vegan = make_option(make_type(event, name="Diet"), {"Label": "Vegan"})
    choose(api, "a@example.com", gate, vegan)
    resp = delete_type(api, admin, etype, {"cascade": True})
    assert resp.json == {"deleted": True, "removed": 1}
    assert chosen(db_ctx, "a@example.com") == ids(vegan)
    assert count(db_ctx, VolunteerOffer) == 1


# ---------------------------------------------------------------- fields


def delete_field(api, admin, fld, body=None):
    return api.delete(f"{CONFIG}/fields/{fld.id}", json=body, headers=admin)


@pytest.fixture
def slots(event, make_type, make_option):
    etype = make_type(event, name="Slots", fields=(("Label", "text", False), ("When", "calendar", False)))
    options = [make_option(etype, {"Label": "Early", "When": (at(1, 8), at(1, 9))}),
               make_option(etype, {"Label": "Late"})]
    fields = {f.name: Ref(f) for f in etype.fragment_types}
    return fields, [Ref(o) for o in options]


def test_empty_field_deletes_outright(api, admin, event, make_type, db_ctx):
    etype = make_type(event, fields=(("Label", "text", False), ("Notes", "text", False)))
    notes = Ref(next(f for f in etype.fragment_types if f.name == "Notes"))
    resp = delete_field(api, admin, notes)
    assert resp.json == {"deleted": True, "removed": 0}
    assert not exists(db_ctx, EnrichmentFragmentType, notes)


@pytest.mark.parametrize("name, rows, model", [("Label", 2, TextEnrichment), ("When", 1, CalendarEnrichment)])
def test_field_with_values_needs_cascade(api, admin, slots, db_ctx, name, rows, model):
    fields, _ = slots
    resp = delete_field(api, admin, fields[name])
    assert resp.status_code == 409
    assert resp.json["linked_count"] == rows
    before = count(db_ctx, model)
    assert before == rows

    resp = delete_field(api, admin, fields[name], {"cascade": True})
    assert resp.json == {"deleted": True, "removed": rows}
    assert count(db_ctx, model) == 0
    assert not exists(db_ctx, EnrichmentFragmentType, fields[name])


def test_field_cascade_leaves_choices_and_other_values(api, admin, slots, db_ctx):
    fields, (early, late) = slots
    choose(api, "a@example.com", early)
    assert delete_field(api, admin, fields["When"], {"cascade": True}).status_code == 200
    assert chosen(db_ctx, "a@example.com") == ids(early)
    assert count(db_ctx, TextEnrichment) == 2
    assert exists(db_ctx, Enrichment, early) and exists(db_ctx, Enrichment, late)


def test_field_names_are_unique_per_type_ignoring_case(api, admin, event, make_type, db_ctx):
    etype = make_type(event, fields=(("Label", "text", False),))
    other = make_type(event, name="Other", fields=())

    def add(t, name):
        return api.post(f"{CONFIG}/enrichment-types/{t.id}/fields",
                        json={"name": name, "content_type": "text"}, headers=admin)

    resp = add(etype, "  label ")
    assert resp.status_code == 409
    assert resp.json["error"] == "this enrichment type already has a field named 'label'"
    assert add(other, "Label").status_code == 201
    assert count(db_ctx, EnrichmentFragmentType) == 2


# ---------------------------------------------------------------- not a volunteer change


@pytest.mark.parametrize("action", ["option cascade", "option reassign", "type cascade"])
def test_deletes_leave_offer_updated_on(api, admin, roles, db_ctx, action):
    etype, (gate, kitchen, _) = roles
    choose(api, "a@example.com", gate)
    db_ctx.expire_all()
    offer = db_ctx.scalar(select(VolunteerOffer))
    offer_id, updated_on = offer.id, offer.updated_on
    if action == "option cascade":
        resp = delete_option(api, admin, gate, {"cascade": True})
    elif action == "option reassign":
        resp = delete_option(api, admin, gate, {"reassign_to": str(kitchen.id)})
    else:
        resp = delete_type(api, admin, etype, {"cascade": True})
    assert resp.status_code == 200
    db_ctx.expire_all()
    assert db_ctx.get(VolunteerOffer, offer_id).updated_on == updated_on
