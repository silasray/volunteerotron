"""Saving the values grid of an enrichment type from the configure page.

PUT /api/admin/event-config/<org>/<event>/enrichment-types/<id>/values with
{"values": {enrichment_id: {field_id: value}}}: a string for a text field,
{"start": iso, "end": iso} for a calendar field; null or empty clears it.

All-or-nothing: if any value is invalid nothing is saved, and the 400 lists
every problem as {"row", "field", "message"} so the page can point at each cell.
"""
import uuid

import pytest
from sqlalchemy import func, select

from api.models import CalendarEnrichment, TextEnrichment

from .conftest import at, field

CONFIG = "/api/admin/event-config/acme/spring-fair"


@pytest.fixture
def admin(event, make_user, add_member, auth):
    user = make_user("admin")
    add_member(user, event.organization, is_admin=True)
    return auth(user)


@pytest.fixture
def slots(event, make_type, make_option):
    """A type with a text and a calendar field, and two rows."""
    etype = make_type(event, name="Slots", fields=(("Label", "text", False), ("When", "calendar", False)))
    early = make_option(etype, {"Label": "Early", "When": (at(1, 8), at(1, 9))})
    late = make_option(etype, {"Label": "Late"})
    return {"type": etype, "label": str(field(etype, "Label").id), "when": str(field(etype, "When").id),
            "early": str(early.id), "late": str(late.id)}


def put_values(api, admin, etype, values):
    return api.put(f"{CONFIG}/enrichment-types/{etype.id}/values", json={"values": values}, headers=admin)


def grid(api, admin):
    """{enrichment id: {field id: value}} for every row, as the configure page reads it."""
    resp = api.get(f"{CONFIG}/config", headers=admin)
    assert resp.status_code == 200
    return {e["id"]: e["values"] for t in resp.json["enrichment_types"] for e in t["enrichments"]}


def iso(dt):
    return dt.isoformat()


def count(db_ctx, model):
    db_ctx.expire_all()
    return db_ctx.scalar(select(func.count()).select_from(model))


# ---------------------------------------------------------------- saving


def test_sets_updates_and_clears_values(api, admin, slots):
    s = slots
    resp = put_values(api, admin, s["type"], {
        s["early"]: {s["label"]: "  Dawn  ", s["when"]: None},
        s["late"]: {s["label"]: "", s["when"]: {"start": "2026-11-01T18:00:00Z", "end": "2026-11-01T20:00:00+00:00"}},
    })
    assert resp.status_code == 200
    assert resp.json == {"updated": 2}
    assert grid(api, admin) == {
        s["early"]: {s["label"]: {"value": "Dawn"}},
        s["late"]: {s["when"]: {"start": iso(at(1, 18)), "end": iso(at(1, 20))}},
    }


def test_fields_left_out_are_untouched(api, admin, slots):
    s = slots
    before = grid(api, admin)
    assert put_values(api, admin, s["type"], {s["late"]: {s["label"]: "Later"}}).status_code == 200
    after = grid(api, admin)
    assert after[s["early"]] == before[s["early"]]
    assert after[s["late"]] == {s["label"]: {"value": "Later"}}


@pytest.mark.parametrize("empty", [None, {}, {"start": "", "end": ""}, {"start": " ", "end": None}])
def test_empty_calendar_value_clears_it(api, admin, slots, db_ctx, empty):
    s = slots
    assert put_values(api, admin, s["type"], {s["early"]: {s["when"]: empty}}).status_code == 200
    assert count(db_ctx, CalendarEnrichment) == 0


def test_offset_is_kept_as_the_same_instant(api, admin, slots):
    s = slots
    value = {"start": "2026-11-01T09:00:00-05:00", "end": "2026-11-01T10:00:00-05:00"}
    assert put_values(api, admin, s["type"], {s["late"]: {s["when"]: value}}).status_code == 200
    assert grid(api, admin)[s["late"]][s["when"]] == {"start": iso(at(1, 14)), "end": iso(at(1, 15))}


def test_start_equal_to_end_is_allowed(api, admin, slots):
    s = slots
    value = {"start": "2026-11-01T09:00:00Z", "end": "2026-11-01T09:00:00Z"}
    assert put_values(api, admin, s["type"], {s["late"]: {s["when"]: value}}).status_code == 200


def test_hidden_fields_are_editable(api, admin, event, make_type, make_option, db_ctx):
    etype = make_type(event, fields=(("Label", "text", True),))
    option = make_option(etype)
    resp = put_values(api, admin, etype, {str(option.id): {str(field(etype, "Label").id): "Secret"}})
    assert resp.status_code == 200
    assert count(db_ctx, TextEnrichment) == 1


def test_duplicate_fragments_collapse_to_one(api, admin, slots, db_ctx):
    # No constraint stops two values in one cell; a save leaves just one.
    s = slots
    early, label, when = (uuid.UUID(s[k]) for k in ("early", "label", "when"))
    db_ctx.add_all([
        TextEnrichment(enrichment_id=early, enrichment_fragment_type_id=label, value="Extra"),
        CalendarEnrichment(enrichment_id=early, enrichment_fragment_type_id=when, start=at(2), end=at(2, 10)),
    ])
    db_ctx.commit()
    resp = put_values(api, admin, s["type"], {s["early"]: {
        s["label"]: "One", s["when"]: {"start": iso(at(3)), "end": iso(at(3, 10))}}})
    assert resp.status_code == 200
    assert count(db_ctx, TextEnrichment) == 2  # early's one, plus late's
    assert count(db_ctx, CalendarEnrichment) == 1
    assert grid(api, admin)[s["early"]] == {
        s["label"]: {"value": "One"}, s["when"]: {"start": iso(at(3)), "end": iso(at(3, 10))}}


def test_text_at_the_length_limit(api, admin, slots):
    s = slots
    assert put_values(api, admin, s["type"], {s["late"]: {s["label"]: "x" * 255 + "   "}}).status_code == 200
    assert grid(api, admin)[s["late"]][s["label"]] == {"value": "x" * 255}


# ---------------------------------------------------------------- refused


@pytest.mark.parametrize("cell, value, message", [
    ("label", 5, "must be text"),
    ("label", ["a"], "must be text"),
    ("label", "x" * 256, "must be at most 255 characters"),
    ("when", "2026-11-01T09:00:00Z", "needs both a start and an end"),
    ("when", {"start": "2026-11-01T09:00:00Z"}, "end is required"),
    ("when", {"end": "2026-11-01T09:00:00Z"}, "start is required"),
    ("when", {"start": 5, "end": "2026-11-01T09:00:00Z"}, "start and end must be text"),
    ("when", {"start": "2026-11-01T09:00:00Z", "end": 7}, "start and end must be text"),
    ("when", {"start": "", "end": 7}, "start and end must be text"),
    ("when", {"start": ["2026-11-01T09:00:00Z"], "end": None}, "start and end must be text"),
    ("when", {"start": "tomorrow", "end": "2026-11-01T09:00:00Z"}, "start is not a valid date and time"),
    ("when", {"start": "2026-11-01T09:00:00", "end": "2026-11-01T10:00:00Z"},
     "start must include a timezone offset"),
    ("when", {"start": "2026-11-01T10:00:00Z", "end": "2026-11-01T09:00:00Z"}, "start must not be after end"),
])
def test_invalid_value_is_reported_and_nothing_saved(api, admin, slots, cell, value, message):
    s = slots
    before = grid(api, admin)
    resp = put_values(api, admin, s["type"], {
        s["early"]: {s["label"]: "Changed"},  # valid, but must not be saved
        s["late"]: {s[cell]: value},
    })
    assert resp.status_code == 400
    assert resp.json["error"] == "1 value needs fixing; nothing was saved"
    assert resp.json["problems"] == [{"row": s["late"], "field": s[cell], "message": message}]
    assert grid(api, admin) == before


def test_every_problem_is_listed(api, admin, slots, event, make_type, make_option):
    s = slots
    other = make_type(event, name="Other")
    foreign_row = str(make_option(other, {"Label": "x"}).id)
    foreign_field = str(field(other, "Label").id)
    resp = put_values(api, admin, s["type"], {
        s["early"]: {s["label"]: 5, s["when"]: "soon", foreign_field: "x"},
        s["late"]: "not an object",
        foreign_row: {s["label"]: "x"},
        "no-such-row": {},
    })
    assert resp.status_code == 400
    assert resp.json["error"] == "6 values need fixing; nothing was saved"
    gone_row = "this row no longer exists (reload the page)"
    assert sorted(resp.json["problems"], key=lambda p: (p["row"], p["field"] or "")) == sorted([
        {"row": s["early"], "field": s["label"], "message": "must be text"},
        {"row": s["early"], "field": s["when"], "message": "needs both a start and an end"},
        {"row": s["early"], "field": foreign_field, "message": "this field no longer exists (reload the page)"},
        {"row": s["late"], "field": None, "message": gone_row},
        {"row": foreign_row, "field": None, "message": gone_row},
        {"row": "no-such-row", "field": None, "message": gone_row},
    ], key=lambda p: (p["row"], p["field"] or ""))


@pytest.mark.parametrize("body", [{}, {"values": []}, {"values": "x"}, {"values": None}])
def test_values_must_be_an_object(api, admin, slots, body):
    resp = api.put(f"{CONFIG}/enrichment-types/{slots['type'].id}/values", json=body, headers=admin)
    assert resp.status_code == 400
    assert resp.json["error"] == "values must be an object of {enrichment_id: {field_id: value}}"


def test_empty_values_is_a_no_op(api, admin, slots):
    before = grid(api, admin)
    resp = put_values(api, admin, slots["type"], {})
    assert resp.json == {"updated": 0}
    assert grid(api, admin) == before
