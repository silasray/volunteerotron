"""Flows where an admin changes field visibility after volunteers have chosen
options: choices must survive hiding, come back when shown, and never be
changed by the admin's visibility changes themselves.

A type is offered to volunteers while at least one of its fields is visible
(see test_fragment_visibility.py for the rules on a single state).
"""
import itertools

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from api.models import VolunteerOffer, VolunteerOfferEnrichment

from .conftest import at, field

BASE = "/api/acme/spring-fair"
CONFIG = "/api/admin/event-config/acme/spring-fair"
VOL = "vol@example.com"


@pytest.fixture
def admin(event, make_user, add_member, auth):
    user = make_user("admin")
    add_member(user, event.organization, is_admin=True)
    return auth(user)


@pytest.fixture
def set_hidden(api, admin):
    def set_(etype, name, hidden):
        resp = api.patch(f"{CONFIG}/fields/{field(etype, name).id}", json={"hidden": hidden},
                         headers=admin)
        assert resp.status_code == 200
    return set_


def save(api, *options, email=VOL, name="Vol", windows=()):
    return api.put(f"{BASE}/offers/{email}", json={
        "name": name,
        "window_ids": [str(w.id) for w in windows],
        "enrichment_ids": [str(o.id) for o in options],
    })


def loaded(api, email=VOL):
    """The choices the volunteer sees when loading their offer."""
    resp = api.get(f"{BASE}/offers/{email}")
    assert resp.status_code == 200
    return set(resp.json["enrichment_ids"])


def offered(api, etype):
    """The option ids the form offers for the type, or None if it isn't offered."""
    t = next((t for t in api.get(f"{BASE}/volunteer-form").json["enrichment_types"]
              if t["id"] == str(etype.id)), None)
    return None if t is None else {o["id"]: o["fragments"] for o in t["enrichments"]}


def stored(db_ctx, email=VOL):
    """The choices actually on the volunteer's offer."""
    db_ctx.expire_all()
    return {str(e) for e in db_ctx.scalars(
        select(VolunteerOfferEnrichment.enrichment_id)
        .join(VolunteerOffer).filter(VolunteerOffer.email == email))}


def ids(*options):
    return {str(o.id) for o in options}


def updated_on(db_ctx, email=VOL):
    db_ctx.expire_all()
    return db_ctx.scalar(select(VolunteerOffer.updated_on).filter_by(email=email))


# ---------------------------------------------------------------- hide, then show again


def test_hidden_choices_survive_and_return(api, event, make_type, make_option, make_window,
                                           set_hidden, db_ctx):
    roles = make_type(event, name="Roles")
    shirts = make_type(event, name="Shirts")
    gate, kitchen, spare = (make_option(roles, {"Label": v}) for v in ("Gate", "Kitchen", "Spare"))
    medium = make_option(shirts, {"Label": "M"})
    window = make_window(event)
    assert save(api, gate, kitchen, medium).status_code == 201

    set_hidden(roles, "Label", True)
    assert offered(api, roles) is None
    assert loaded(api) == ids(medium)
    assert stored(db_ctx) == ids(gate, kitchen, medium)

    # The volunteer keeps using the page: other changes save, hidden choices stay.
    assert save(api, windows=[window], name="Renamed").status_code == 200
    assert stored(db_ctx) == ids(gate, kitchen)

    set_hidden(roles, "Label", False)
    assert loaded(api) == ids(gate, kitchen)
    assert offered(api, roles).keys() == ids(gate, kitchen, spare)

    # Once shown again they're ordinary choices: the volunteer can change them.
    assert save(api, gate, spare, windows=[window]).status_code == 200
    assert stored(db_ctx) == ids(gate, spare)
    assert loaded(api) == ids(gate, spare)


def test_repeated_hide_show_cycles(api, event, make_type, make_option, set_hidden, db_ctx):
    roles = make_type(event, name="Roles")
    gate = make_option(roles, {"Label": "Gate"})
    save(api, gate)
    for _ in range(3):
        set_hidden(roles, "Label", True)
        assert loaded(api) == set()
        save(api)
        set_hidden(roles, "Label", False)
        assert loaded(api) == ids(gate)
    assert stored(db_ctx) == ids(gate)


# ---------------------------------------------------------------- multi-field types, a field at a time

FIELDS = (("Code", "text"), ("Label", "text"), ("When", "calendar"))


@pytest.mark.parametrize("order", list(itertools.permutations([n for n, _ in FIELDS])),
                         ids=lambda order: ">".join(order))
def test_hiding_fields_one_at_a_time(api, event, make_type, make_option, set_hidden, db_ctx, order):
    """The choice stays visible while any field is visible, is hidden only when
    the last one is, and comes back as soon as any one is shown again."""
    etype = make_type(event, fields=[(n, k, False) for n, k in FIELDS])
    chosen = make_option(etype, {"Code": "G1", "Label": "Gate", "When": (at(3), at(3, 12))})
    make_option(etype, {"Label": "Kitchen"})  # an option never chosen
    save(api, chosen)

    for i, name in enumerate(order):
        set_hidden(etype, name, True)
        last = i == len(order) - 1
        step = f"after hiding {'>'.join(order[:i + 1])}"
        assert (offered(api, etype) is None) == last, step
        assert loaded(api) == (set() if last else ids(chosen)), step
        # The volunteer resubmits exactly what their page shows.
        assert save(api, *([] if last else [chosen])).status_code == 200, step
        assert stored(db_ctx) == ids(chosen), step

    for name in reversed(order):
        set_hidden(etype, name, False)
        step = f"after showing {name}"
        assert loaded(api) == ids(chosen), step
        assert save(api, chosen).status_code == 200, step
    assert stored(db_ctx) == ids(chosen)


def test_choice_whose_visible_values_are_all_hidden(api, event, make_type, make_option,
                                                    set_hidden, db_ctx):
    """An option with values only in the field that gets hidden stays chosen and
    listed, with no details, while the type is still offered."""
    etype = make_type(event, fields=(("Label", "text", False), ("Code", "text", False)))
    code_only = make_option(etype, {"Code": "G1"})
    save(api, code_only)
    set_hidden(etype, "Code", True)
    assert offered(api, etype)[str(code_only.id)] == []
    assert loaded(api) == ids(code_only)
    assert save(api, code_only).status_code == 200
    assert stored(db_ctx) == ids(code_only)


# ---------------------------------------------------------------- single-choice types


def test_single_choice_switch_after_reshow(api, event, make_type, make_option, set_hidden, db_ctx):
    shirts = make_type(event, name="Shirt", interaction="select")
    small, large = make_option(shirts, {"Label": "S"}), make_option(shirts, {"Label": "L"})
    save(api, small)
    set_hidden(shirts, "Label", True)
    save(api)
    set_hidden(shirts, "Label", False)
    assert loaded(api) == ids(small)

    assert save(api, small, large).status_code == 400  # still at most one
    assert stored(db_ctx) == ids(small)
    assert save(api, large).status_code == 200
    assert stored(db_ctx) == ids(large)


@pytest.mark.xfail(strict=True, reason=(
    "TODO: no code path may give one offer two links to options of a single-choice "
    "type. Only the volunteer save checks this today; the models don't, so staff "
    "links (or any future path) can create several. Once shown, the volunteer then "
    "sees them all chosen, can't save them, and saving one silently drops the rest. "
    "Enforce it where every path goes through (model/database) and drop this marker."))
@pytest.mark.parametrize("commits", ["together", "one at a time"])
def test_single_choice_type_never_gets_two_links(event, make_type, make_option, db_ctx, commits):
    team = make_type(event, name="Team", interaction="select", fields=(("Label", "text", True),))
    a, b = make_option(team, {"Label": "A"}), make_option(team, {"Label": "B"})
    offer = VolunteerOffer(event=event, email=VOL, name="Vol")
    db_ctx.add(offer)
    db_ctx.add(VolunteerOfferEnrichment(volunteer_offer=offer, enrichment=a))
    if commits == "one at a time":
        db_ctx.commit()
    db_ctx.add(VolunteerOfferEnrichment(volunteer_offer=offer, enrichment=b))
    with pytest.raises((IntegrityError, ValueError)):
        db_ctx.commit()


# ---------------------------------------------------------------- a page opened before the change


def test_stale_page_cannot_add_now_hidden_option(api, event, make_type, make_option,
                                                 set_hidden, db_ctx):
    """A volunteer whose page still shows a type an admin has since hidden can't
    add a choice from it by submitting; their existing choices are unaffected."""
    roles = make_type(event, name="Roles")
    gate, kitchen = make_option(roles, {"Label": "Gate"}), make_option(roles, {"Label": "Kitchen"})
    save(api, gate)
    set_hidden(roles, "Label", True)
    save(api, gate, kitchen, name="New name")
    assert stored(db_ctx) == ids(gate)


@pytest.mark.xfail(strict=True, reason=(
    "TODO: messaging and workflow for a stale page need reworking. Today the whole "
    "save is refused with 400 'enrichments not selectable at this event: [<uuid>]': "
    "raw ids a volunteer can't act on, and their other edits are lost. The "
    "assertions below are a placeholder for the reworked behaviour; update them "
    "with the real design and drop this marker."))
def test_stale_page_submission_is_handled_for_the_volunteer(api, event, make_type, make_option,
                                                            set_hidden, db_ctx):
    roles = make_type(event, name="Roles")
    gate, kitchen = make_option(roles, {"Label": "Gate"}), make_option(roles, {"Label": "Kitchen"})
    save(api, gate)
    set_hidden(roles, "Label", True)

    resp = save(api, gate, kitchen, name="New name")
    assert str(kitchen.id) not in resp.get_data(as_text=True)  # no raw ids in what they're told
    db_ctx.expire_all()
    assert db_ctx.scalar(select(VolunteerOffer.name)) == "New name"  # other edits not lost


# ---------------------------------------------------------------- several volunteers; the offer itself


def test_each_volunteer_keeps_their_own_choices(api, event, make_type, make_option,
                                                set_hidden, db_ctx):
    roles = make_type(event, name="Roles")
    gate, kitchen = make_option(roles, {"Label": "Gate"}), make_option(roles, {"Label": "Kitchen"})
    save(api, gate, email="one@example.com")
    save(api, kitchen, email="two@example.com")
    save(api, email="three@example.com")

    set_hidden(roles, "Label", True)
    for email in ("one@example.com", "two@example.com", "three@example.com"):
        assert loaded(api, email) == set()
        save(api, email=email)
    set_hidden(roles, "Label", False)

    assert loaded(api, "one@example.com") == ids(gate)
    assert loaded(api, "two@example.com") == ids(kitchen)
    assert loaded(api, "three@example.com") == set()


def test_visibility_changes_leave_offer_untouched(api, event, make_type, make_option,
                                                  set_hidden, db_ctx):
    """Hiding and showing are admin actions: they never move the volunteer's
    updated_on, and neither does a volunteer save that changes nothing."""
    etype = make_type(event, fields=(("Label", "text", False), ("Code", "text", False)))
    option = make_option(etype, {"Label": "Gate", "Code": "G1"})
    save(api, option)
    before = updated_on(db_ctx)

    for name, hidden in (("Code", True), ("Label", True), ("Label", False), ("Code", False)):
        set_hidden(etype, name, hidden)
        assert updated_on(db_ctx) == before
    save(api, option)
    assert updated_on(db_ctx) == before
