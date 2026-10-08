"""Switching a type to single-choice from the configure page while a volunteer
has several of its options chosen: the admin is shown the API's refusal.
(The resolution workflow is a TODO: see tests/api/test_enrichment_interaction.py.)"""
import pytest

from api.auth import issue_token
from api.models import (
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
)
from tests.world import PASSWORD

CSRF = "test-csrf"


@pytest.fixture
def crowded(stack, db_ctx):
    org = Organization(name="acme", pretty_name="Acme")
    event = Event(organization=org, name="spring-fair", pretty_name="Spring Fair")
    roles = EnrichmentType(event=event, name="Roles", volunteer_interaction=VolunteerInteraction.MULTISELECT)
    label = EnrichmentFragmentType(enrichment_type=roles, name="Label", content_type=ContentType.TEXT)
    options = [Enrichment(enrichment_type=roles) for _ in range(2)]
    admin = User(name="admin")
    admin.set_password(PASSWORD)
    db_ctx.add_all([org, event, roles, label, admin, *options,
                    *(TextEnrichment(enrichment=o, enrichment_fragment_type=label, value=v)
                      for o, v in zip(options, ("Gate", "Kitchen"))),
                    UserOrganization(user=admin, organization=org, is_admin=True)])
    db_ctx.commit()
    resp = stack.put("/acme/spring-fair/volunteer/offer", json={
        "email": "vol@example.com", "enrichment_ids": [str(o.id) for o in options]})
    assert resp.status_code == 201
    with stack.session_transaction() as session:
        session["csrf"] = CSRF
        session["api_token"] = issue_token(admin)
        session["user"] = {"id": str(admin.id), "name": "admin"}
    return f"/admin/acme/spring-fair/configure/enrichment-types/{roles.id}/interaction"


def test_form_post_flashes_the_refusal(stack, crowded):
    resp = stack.post(crowded, data={"csrf": CSRF, "volunteer_interaction": "select"})
    assert resp.status_code == 302
    with stack.session_transaction() as session:
        errors = [m for c, m in session["_flashes"] if c == "error"]
    assert errors == ["1 volunteer(s) chose more than one option of this type; "
                      "it can't become single-choice until they have at most one"]


def test_background_post_returns_the_refusal(stack, crowded):
    resp = stack.post(crowded, data={"csrf": CSRF, "volunteer_interaction": "select"},
                      headers={"Accept": "application/json"})
    assert resp.status_code == 400
    assert resp.json["ok"] is False
    assert "chose more than one option" in resp.json["error"]
