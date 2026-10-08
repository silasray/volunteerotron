"""Hiding and showing a field after a volunteer chose an option, through both
tiers: the admin uses the configure page's form, the volunteer the public page's
load and save relays."""
from sqlalchemy import select

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
    VolunteerOfferEnrichment,
)
from tests.world import PASSWORD

CSRF = "test-csrf"
OFFER = "/acme/spring-fair/volunteer/offer"
VOL = "vol@example.com"


def test_choice_survives_hide_and_returns_on_show(stack, db_ctx):
    org = Organization(name="acme", pretty_name="Acme")
    event = Event(organization=org, name="spring-fair", pretty_name="Spring Fair")
    roles = EnrichmentType(event=event, name="Roles", volunteer_interaction=VolunteerInteraction.MULTISELECT)
    label = EnrichmentFragmentType(enrichment_type=roles, name="Label", content_type=ContentType.TEXT)
    gate = Enrichment(enrichment_type=roles)
    admin = User(name="admin")
    admin.set_password(PASSWORD)
    db_ctx.add_all([
        org, event, roles, label, gate, admin,
        TextEnrichment(enrichment=gate, enrichment_fragment_type=label, value="Gate"),
        UserOrganization(user=admin, organization=org, is_admin=True),
    ])
    db_ctx.commit()
    with stack.session_transaction() as session:
        session["csrf"] = CSRF
        session["api_token"] = issue_token(admin)
        session["user"] = {"id": str(admin.id), "name": "admin"}

    def set_hidden(hidden):
        resp = stack.post(f"/admin/acme/spring-fair/configure/fields/{label.id}/hidden",
                          data={"csrf": CSRF, "hidden": "1" if hidden else "0"})
        assert resp.status_code == 302
        with stack.session_transaction() as session:
            assert not [m for c, m in session.pop("_flashes", []) if c == "error"]

    def volunteer_sees():
        resp = stack.get(f"{OFFER}?email={VOL}")
        assert resp.status_code == 200
        return resp.json["enrichment_ids"]

    def stored():
        db_ctx.expire_all()
        return [str(e) for e in db_ctx.scalars(select(VolunteerOfferEnrichment.enrichment_id))]

    def form_has_roles():
        page = stack.get("/acme/spring-fair/volunteer")
        return b"Roles" in page.data

    assert stack.put(OFFER, json={"email": VOL, "name": "Vol", "enrichment_ids": [str(gate.id)]}).status_code == 201
    assert form_has_roles()

    set_hidden(True)
    assert not form_has_roles()
    assert volunteer_sees() == []
    assert stack.put(OFFER, json={"email": VOL, "name": "Vol", "enrichment_ids": []}).status_code == 200
    assert stored() == [str(gate.id)]

    set_hidden(False)
    assert form_has_roles()
    assert volunteer_sees() == [str(gate.id)]
