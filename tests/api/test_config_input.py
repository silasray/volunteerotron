"""Input checks on the configure page's endpoints (api/event_config.py): request
bodies, names, enum values, date-times and ids. Every refusal is a 400 with a
message the page can show, and changes nothing.
"""
import pytest
from sqlalchemy import func, select

from api.models import EnrichmentFragmentType, EnrichmentType, TextEnrichment, VolunteerWindow

from .conftest import field

CONFIG = "/api/admin/event-config/acme/spring-fair"


@pytest.fixture
def admin(event, make_user, add_member, auth):
    user = make_user("admin")
    add_member(user, event.organization, is_admin=True)
    return auth(user)


def count(db_ctx, model):
    db_ctx.expire_all()
    return db_ctx.scalar(select(func.count()).select_from(model))


# ---------------------------------------------------------------- bodies


@pytest.mark.parametrize("data, content_type", [
    ("{not json", "application/json"),
    ("[1, 2]", "application/json"),
    ('"text"', "application/json"),
])
def test_body_must_be_a_json_object(api, admin, data, content_type, db_ctx):
    resp = api.post(f"{CONFIG}/enrichment-types", data=data, content_type=content_type, headers=admin)
    assert (resp.status_code, resp.json) == (400, {"error": "a JSON object body is required"})
    assert count(db_ctx, EnrichmentType) == 0


def test_empty_body_counts_as_no_options(api, admin, event, make_window, db_ctx):
    # A DELETE with no body at all is the plain case: nothing to cascade or reassign.
    window = make_window(event)
    assert api.delete(f"{CONFIG}/windows/{window.id}", headers=admin).status_code == 200
    assert count(db_ctx, VolunteerWindow) == 0


# ---------------------------------------------------------------- windows


@pytest.mark.parametrize("body, message", [
    ({"end": "2026-11-01T10:00:00Z"}, "start is required"),
    ({"start": "  ", "end": "2026-11-01T10:00:00Z"}, "start is required"),
    ({"start": 900, "end": "2026-11-01T10:00:00Z"}, "start is required"),
    ({"start": "2026-11-01T09:00:00Z"}, "end is required"),
    ({"start": "Nov 1 9am", "end": "2026-11-01T10:00:00Z"}, "start is not a valid date and time"),
    ({"start": "2026-11-01T09:00:00", "end": "2026-11-01T10:00:00Z"}, "start must include a timezone offset"),
    ({"start": "2026-11-01T09:00:00Z", "end": "2026-11-01T10:00:00"}, "end must include a timezone offset"),
    ({"start": "2026-11-01T11:00:00Z", "end": "2026-11-01T10:00:00Z"}, "start must not be after end"),
])
def test_window_times_are_checked(api, admin, db_ctx, body, message):
    resp = api.post(f"{CONFIG}/windows", json=body, headers=admin)
    assert (resp.status_code, resp.json) == (400, {"error": message})
    assert count(db_ctx, VolunteerWindow) == 0


def test_window_times_accept_surrounding_spaces(api, admin):
    resp = api.post(f"{CONFIG}/windows", json={"start": " 2026-11-01T09:00:00Z ", "end": "2026-11-01T09:00:00Z"},
                    headers=admin)
    assert resp.status_code == 201  # zero length is allowed


# ---------------------------------------------------------------- enrichment types and fields


@pytest.mark.parametrize("body, message", [
    ({"volunteer_interaction": "select"}, "name is required"),
    ({"name": " ", "volunteer_interaction": "select"}, "name is required"),
    ({"name": "x" * 256, "volunteer_interaction": "select"}, "name must be at most 255 characters"),
    ({"name": "Shirt"}, "volunteer interaction must be one of: multiselect, select"),
    ({"name": "Shirt", "volunteer_interaction": "SELECT"}, "volunteer interaction must be one of: multiselect, select"),
])
def test_enrichment_type_input(api, admin, db_ctx, body, message):
    resp = api.post(f"{CONFIG}/enrichment-types", json=body, headers=admin)
    assert (resp.status_code, resp.json) == (400, {"error": message})
    assert count(db_ctx, EnrichmentType) == 0


def test_interaction_change_is_checked(api, admin, event, make_type):
    etype = make_type(event)
    resp = api.patch(f"{CONFIG}/enrichment-types/{etype.id}", json={"volunteer_interaction": "some"}, headers=admin)
    assert (resp.status_code, resp.json) == (400, {"error": "volunteer interaction must be one of: multiselect, select"})


@pytest.mark.parametrize("body, message", [
    ({"content_type": "text"}, "name is required"),
    ({"name": "Size"}, "content type must be one of: text, calendar"),
    ({"name": "Size", "content_type": "number"}, "content type must be one of: text, calendar"),
])
def test_field_input(api, admin, event, make_type, db_ctx, body, message):
    etype = make_type(event, fields=())
    resp = api.post(f"{CONFIG}/enrichment-types/{etype.id}/fields", json=body, headers=admin)
    assert (resp.status_code, resp.json) == (400, {"error": message})
    assert count(db_ctx, EnrichmentFragmentType) == 0


@pytest.mark.parametrize("body", [{}, {"hidden": "true"}, {"hidden": 1}, {"hidden": None}])
def test_hidden_must_be_boolean(api, admin, event, make_type, db_ctx, body):
    etype = make_type(event)
    resp = api.patch(f"{CONFIG}/fields/{field(etype, 'Label').id}", json=body, headers=admin)
    assert (resp.status_code, resp.json) == (400, {"error": "hidden must be true or false"})
    db_ctx.expire_all()
    assert db_ctx.get(EnrichmentFragmentType, field(etype, "Label").id).hidden is False


def test_null_text_value_clears_it(api, admin, event, make_type, make_option, db_ctx):
    etype = make_type(event)
    option = make_option(etype, {"Label": "Gate"})
    resp = api.put(f"{CONFIG}/enrichment-types/{etype.id}/values",
                   json={"values": {str(option.id): {str(field(etype, "Label").id): None}}}, headers=admin)
    assert resp.status_code == 200
    assert count(db_ctx, TextEnrichment) == 0


# ---------------------------------------------------------------- ids


@pytest.mark.parametrize("path", [
    "/windows/not-a-uuid", "/enrichment-types/not-a-uuid", "/fields/not-a-uuid", "/enrichments/not-a-uuid",
])
def test_malformed_ids_in_the_path_are_404(api, admin, path):
    assert api.delete(CONFIG + path, json={"cascade": True}, headers=admin).status_code == 404


@pytest.mark.parametrize("reassign_to", ["nope", 5, "", None, ["x"]])
def test_malformed_reassign_target(api, admin, event, make_window, reassign_to):
    first, second = make_window(event, day=1), make_window(event, day=2)
    api.put("/api/acme/spring-fair/offers/v@example.com", json={"window_ids": [str(first.id)]})
    resp = api.delete(f"{CONFIG}/windows/{first.id}", json={"reassign_to": reassign_to}, headers=admin)
    # A falsy target is no target: the conflict is reported instead.
    assert resp.status_code == (409 if not reassign_to else 400)
