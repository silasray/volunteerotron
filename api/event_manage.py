"""Endpoints behind an event's manage page: reviewing and answering offers.

Any member of the event's organization may use them; others get 404.
Enrichments come only from enrichment_providers; nothing here knows how any
column is computed.
"""
from flask import Blueprint, g, jsonify
from sqlalchemy import select

from .auth import require_user
from .enrichment_providers import ManageContext, compute
from .errors import ApiError
from .event_config import _body
from .models import (
    Event,
    Organization,
    UserOrganization,
    VolunteerOfferResponse,
    VolunteerWindow,
    VolunteerWindowOffer,
    db,
)

bp = Blueprint("api_event_manage", __name__)

# The one mapping between VolunteerOfferResponse.accepted (the database) and
# the response values the API and front end use. The manage endpoint sends this
# list so the page builds its status filters and sort options from it.
RESPONSE_VALUES = [
    {"value": "accepted", "accepted": True, "label": "Accepted"},
    {"value": "denied", "accepted": False, "label": "Denied"},
]
RESPONSES = {r["value"]: r["accepted"] for r in RESPONSE_VALUES} | {None: None}
_NAME_FOR = {r["accepted"]: r["value"] for r in RESPONSE_VALUES}


def _event_for_member(org_name, event_name):
    event = db.session.scalar(
        select(Event)
        .join(Organization)
        .join(UserOrganization, UserOrganization.organization_id == Organization.id)
        .filter(Organization.name == org_name, Event.name == event_name,
                UserOrganization.user_id == g.user.id)
    )
    if event is None:
        raise ApiError("event not found", 404)
    return event


def _response_name(signup):
    if signup.response is None:
        return None
    return _NAME_FOR[signup.response.accepted]


def _signup_dict(signup, values):
    offer = signup.volunteer_offer
    return {
        "id": str(signup.id),
        "volunteer": {"name": offer.name, "email": offer.email},
        "offered_on": offer.created_on.isoformat(),
        "response": _response_name(signup),
        "responded_on": signup.response.updated_on.isoformat() if signup.response else None,
        "values": values,
    }


@bp.get("/<org>/<event_name>/windows")
@require_user
def windows(org, event_name):
    """Windows by start time, each with its active sign-ups (in the order
    volunteers signed up) and every enrichment column's value for each."""
    event = _event_for_member(org, event_name)
    ctx = ManageContext(event)
    columns, values = compute(ctx)
    by_window = {w.id: [] for w in ctx.windows}
    for signup in sorted(ctx.signups, key=lambda s: (s.volunteer_offer.created_on, s.volunteer_offer.email)):
        by_window[signup.volunteer_window_id].append(signup)
    return jsonify(
        organization=event.organization.to_dict(),
        event=event.to_dict(),
        columns=[c.to_dict() for c in columns],
        # Possible sign-up responses; a sign-up with none is pending.
        responses=[{"value": r["value"], "label": r["label"]} for r in RESPONSE_VALUES],
        windows=[
            {
                "id": str(w.id),
                "start": w.start.isoformat(),
                "end": w.end.isoformat(),
                "signups": [_signup_dict(s, values.get(s.id, {})) for s in by_window[w.id]],
            }
            for w in ctx.windows
        ],
    )


@bp.put("/<org>/<event_name>/signups/<uuid:signup_id>/response")
@require_user
def set_response(org, event_name, signup_id):
    """Body: {"response": "accepted" | "denied" | null}; null returns the
    sign-up to pending. Admin decisions live in VolunteerOfferResponse and
    never change the volunteer's own offer (or its updated_on)."""
    event = _event_for_member(org, event_name)
    signup = db.session.get(VolunteerWindowOffer, signup_id)
    if signup is None or db.session.get(VolunteerWindow, signup.volunteer_window_id).event_id != event.id:
        raise ApiError("sign-up not found", 404)
    if signup.cancelled:
        raise ApiError("this volunteer has withdrawn from the window", 409)
    data = _body()
    if "response" not in data or data["response"] not in RESPONSES:
        raise ApiError('response must be "accepted", "denied" or null')
    accepted = RESPONSES[data["response"]]

    if accepted is None:
        if signup.response is not None:
            db.session.delete(signup.response)
    elif signup.response is None:
        db.session.add(VolunteerOfferResponse(volunteer_window_offer=signup, accepted=accepted))
    else:
        signup.response.accepted = accepted
    db.session.commit()
    db.session.refresh(signup)
    return jsonify(id=str(signup.id), response=_response_name(signup),
                   volunteer={"name": signup.volunteer_offer.name, "email": signup.volunteer_offer.email})
