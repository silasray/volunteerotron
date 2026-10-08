"""Which enrichment fields (fragment types) and values volunteers see, and what
they may choose.

Rules (api/routes.py, _visible_fragment_types and friends):
  - a hidden field, and every option's value in it, is left out of the
    volunteer form entirely: not listed, not in any option, not used to order
  - an enrichment type is offered only while at least one field is visible
  - only options of offered types can be chosen; links to options of types
    that aren't offered (staff-only) survive the volunteer's saves untouched
  - an offered option with no values in visible fields is still listed, with
    no fragments (the page labels it "(no details)")

The matrix builds types with one to three fields of each content type mix and
every combination of hidden flags, and checks the form and saving against them.
"""
import itertools

import pytest
from sqlalchemy import select

from api.models import VolunteerOffer, VolunteerOfferEnrichment

from .conftest import at, field

BASE = "/api/acme/spring-fair"
EMAIL = "vol@example.com"

# Field names by position, chosen so sorting by name differs from creation order.
NAMES = ("Zeta", "Alpha", "Mid")

LAYOUTS = [
    ("text",),
    ("calendar",),
    ("text", "text"),
    ("calendar", "calendar"),
    ("text", "calendar"),
    ("calendar", "text"),
    ("text", "text", "text"),
    ("text", "text", "calendar"),
    ("text", "calendar", "calendar"),
    ("calendar", "calendar", "calendar"),
]


def _value(i, kind):
    """A distinct value per field position, recognisable in a response body."""
    if kind == "text":
        return f"value-of-{NAMES[i]}"
    return (at(10 + i), at(10 + i, 17))


def _expected_fragment(fld, value):
    if isinstance(value, str):
        return {"fragment_type_id": str(fld.id), "kind": "text", "value": value}
    return {"fragment_type_id": str(fld.id), "kind": "calendar",
            "start": value[0].isoformat(), "end": value[1].isoformat()}


def _form(api):
    resp = api.get(f"{BASE}/volunteer-form")
    assert resp.status_code == 200
    return resp


def _type_in(form_json, etype):
    return next((t for t in form_json["enrichment_types"] if t["id"] == str(etype.id)), None)


def _put(api, *options, windows=()):
    return api.put(f"{BASE}/offers/{EMAIL}", json={
        "name": "Vol",
        "window_ids": [str(w.id) for w in windows],
        "enrichment_ids": [str(o.id) for o in options],
    })


CASES = [
    pytest.param(layout, hidden, id="+".join(
        f"{kind}{'(hidden)' if h else ''}" for kind, h in zip(layout, hidden)))
    for layout in LAYOUTS
    for hidden in itertools.product((False, True), repeat=len(layout))
]


@pytest.mark.parametrize("layout, hidden", CASES)
def test_visibility_matrix(api, event, make_type, make_option, layout, hidden):
    names = NAMES[:len(layout)]
    etype = make_type(event, fields=[(n, k, h) for n, k, h in zip(names, layout, hidden)])
    values = {n: _value(i, k) for i, (n, k) in enumerate(zip(names, layout))}
    full = make_option(etype, values)                   # a value in every field
    partial = make_option(etype, {names[0]: values[names[0]]})  # only the first field
    visible = [n for n, h in zip(names, hidden) if not h]

    resp = _form(api)
    offered = _type_in(resp.json, etype)
    body = resp.get_data(as_text=True)

    # Nothing about a hidden field reaches the volunteer.
    for n, h in zip(names, hidden):
        if h:
            fld = field(etype, n)
            assert str(fld.id) not in body
            assert n not in body
            v = values[n]
            assert (v if isinstance(v, str) else v[0].isoformat()) not in body

    if not visible:
        assert offered is None
        assert _put(api, full).status_code == 400
        assert _put(api, partial).status_code == 400
        assert _put(api).status_code == 201
        return

    # Visible fields, sorted by name, with their content types.
    assert [ft["name"] for ft in offered["fragment_types"]] == sorted(visible)
    for ft in offered["fragment_types"]:
        assert ft["hidden"] is False
        assert ft["content_type"] == layout[names.index(ft["name"])]

    options = {o["id"]: o["fragments"] for o in offered["enrichments"]}
    assert options.keys() == {str(full.id), str(partial.id)}
    assert options[str(full.id)] == [
        _expected_fragment(field(etype, n), values[n]) for n in sorted(visible)]
    # The partial option keeps its one value if that field is visible; otherwise
    # it's listed with no details.
    assert options[str(partial.id)] == (
        [_expected_fragment(field(etype, names[0]), values[names[0]])] if names[0] in visible else [])

    assert _put(api, full, partial).status_code == 201


# ---------------------------------------------------------------- types offered or not


def test_type_without_fields_is_not_offered(api, event, make_type, make_option):
    etype = make_type(event, fields=())
    option = make_option(etype)
    assert _type_in(_form(api).json, etype) is None
    assert _put(api, option).status_code == 400


def test_visible_type_without_options_is_offered_empty(api, event, make_type):
    etype = make_type(event)
    offered = _type_in(_form(api).json, etype)
    assert offered["enrichments"] == []


def test_only_offered_types_listed_by_name(api, event, make_type):
    make_type(event, name="b-type")
    make_type(event, name="a-hidden", fields=(("Label", "text", True),))
    make_type(event, name="a-type")
    names = [t["name"] for t in _form(api).json["enrichment_types"]]
    assert names == ["a-type", "b-type"]


# ---------------------------------------------------------------- ordering ignores hidden values


def test_option_order_uses_visible_values_only(api, event, make_type, make_option, db_ctx):
    """Ordering by a hidden value would leak it through the order, so hiding a
    field must re-sort the options by what's left."""
    etype = make_type(event, fields=(("Code", "text", False), ("Label", "text", False)))
    first_by_code = make_option(etype, {"Code": "1", "Label": "b"})
    first_by_label = make_option(etype, {"Code": "2", "Label": "a"})

    def order():
        return [o["id"] for o in _type_in(_form(api).json, etype)["enrichments"]]

    assert order() == [str(first_by_code.id), str(first_by_label.id)]
    field(etype, "Code").hidden = True
    db_ctx.commit()
    assert order() == [str(first_by_label.id), str(first_by_code.id)]


def test_options_order_by_calendar_start(api, event, make_type, make_option):
    etype = make_type(event, fields=(("When", "calendar", False),))
    later = make_option(etype, {"When": (at(5), at(5, 12))})
    sooner = make_option(etype, {"When": (at(2), at(2, 12))})
    blank = make_option(etype)
    ids = [o["id"] for o in _type_in(_form(api).json, etype)["enrichments"]]
    assert ids == [str(blank.id), str(sooner.id), str(later.id)]


# ---------------------------------------------------------------- changing visibility


@pytest.fixture
def admin_headers(event, make_user, add_member, auth):
    user = make_user("admin")
    add_member(user, event.organization, is_admin=True)
    return auth(user)


def _set_hidden(api, headers, fld, hidden):
    resp = api.patch(f"/api/admin/event-config/acme/spring-fair/fields/{fld.id}",
                     json={"hidden": hidden}, headers=headers)
    assert resp.status_code == 200


def test_hiding_and_showing_takes_effect_at_once(api, event, make_type, make_option, admin_headers):
    etype = make_type(event, fields=(("Label", "text", False), ("Code", "text", False)))
    option = make_option(etype, {"Label": "Gate", "Code": "G1"})

    _set_hidden(api, admin_headers, field(etype, "Code"), True)
    offered = _type_in(_form(api).json, etype)
    assert [ft["name"] for ft in offered["fragment_types"]] == ["Label"]
    assert "G1" not in _form(api).get_data(as_text=True)

    _set_hidden(api, admin_headers, field(etype, "Label"), True)
    assert _type_in(_form(api).json, etype) is None
    assert _put(api, option).status_code == 400

    _set_hidden(api, admin_headers, field(etype, "Code"), False)
    offered = _type_in(_form(api).json, etype)
    assert [ft["name"] for ft in offered["fragment_types"]] == ["Code"]
    assert _put(api, option).status_code == 201


# ---------------------------------------------------------------- staff-only links survive saves


def _links(db_ctx):
    db_ctx.expire_all()
    return {str(e) for e in db_ctx.scalars(select(VolunteerOfferEnrichment.enrichment_id))}


def test_links_to_hidden_type_survive_volunteer_saves(
        api, event, make_type, make_option, make_window, admin_headers, db_ctx):
    shirts = make_type(event, name="Shirt")
    staff = make_type(event, name="Team")
    medium = make_option(shirts, {"Label": "M"})
    gate = make_option(staff, {"Label": "Gate crew"})
    window = make_window(event)
    assert _put(api, medium, gate).status_code == 201

    _set_hidden(api, admin_headers, field(staff, "Label"), True)  # Team becomes staff-only
    db_ctx.expire_all()
    updated_on = db_ctx.scalar(select(VolunteerOffer)).updated_on

    # The volunteer's page can't show Team, so its saves never include gate.
    assert _put(api, medium).status_code == 200
    assert _links(db_ctx) == {str(medium.id), str(gate.id)}
    assert db_ctx.scalar(select(VolunteerOffer)).updated_on == updated_on  # nothing changed

    assert _put(api, windows=[window]).status_code == 200  # drops medium, keeps gate
    assert _links(db_ctx) == {str(gate.id)}


def test_volunteer_cannot_add_hidden_type_option(api, event, make_type, make_option, db_ctx):
    staff = make_type(event, name="Team", fields=(("Label", "text", True),))
    gate = make_option(staff, {"Label": "Gate crew"})
    assert _put(api, gate).status_code == 400
    assert _links(db_ctx) == set()


def test_partly_hidden_type_stays_choosable(api, event, make_type, make_option, admin_headers, db_ctx):
    etype = make_type(event, fields=(("Label", "text", False), ("Code", "text", False)))
    option = make_option(etype, {"Label": "Gate", "Code": "G1"})
    _put(api, option)
    _set_hidden(api, admin_headers, field(etype, "Code"), True)
    assert _put(api).status_code == 200  # unchoosing still works
    assert _links(db_ctx) == set()
    assert _put(api, option).status_code == 200


def test_single_choice_limit_ignores_hidden_types(api, event, make_type, make_option, db_ctx):
    """Staff may link several options of a hidden single-choice type; the
    volunteer's save neither trips the limit nor drops them."""
    staff = make_type(event, name="Team", fields=(("Label", "text", True),), interaction="select")
    a, b = make_option(staff, {"Label": "A"}), make_option(staff, {"Label": "B"})
    assert _put(api).status_code == 201
    offer = db_ctx.scalar(select(VolunteerOffer))
    db_ctx.add_all([VolunteerOfferEnrichment(volunteer_offer=offer, enrichment=a),
                    VolunteerOfferEnrichment(volunteer_offer=offer, enrichment=b)])
    db_ctx.commit()
    assert _put(api).status_code == 200
    assert _links(db_ctx) == {str(a.id), str(b.id)}
