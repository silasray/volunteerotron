import re
import secrets
import uuid

from flask import Blueprint, current_app, g, jsonify, request
from flask_limiter import Limiter, RateLimitExceeded
from flask_limiter.util import get_remote_address
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from .auth import issue_token, require_user, revoke_tokens
from .errors import ApiError
from .models import (
    Event,
    Organization,
    User,
    VolunteerInteraction,
    VolunteerOffer,
    VolunteerOfferEnrichment,
    VolunteerWindow,
    VolunteerWindowOffer,
    db,
)

bp = Blueprint("api", __name__)


def client_ip():
    """The browser's IP: from the web tier's header where that's trusted
    (TRUSTED_CLIENT_IP_HEADER), otherwise the connecting address."""
    header = current_app.config["TRUSTED_CLIENT_IP_HEADER"]
    if header and (forwarded := request.headers.get(header)):
        return forwarded
    return get_remote_address()


# Initialised in create_app. Only routes with an explicit @limiter.limit are limited.
limiter = Limiter(key_func=client_ip)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@bp.errorhandler(RateLimitExceeded)
def handle_rate_limited(err):
    return jsonify(error="too many attempts; try again later"), 429


@bp.get("/health")
def health():
    return jsonify(status="ok")


# Checked when the name doesn't exist, so an unknown name costs the same scrypt
# work as a wrong password and response timing doesn't reveal which names exist.
_DUMMY_HASH = generate_password_hash(secrets.token_urlsafe(), method=User.PASSWORD_HASH_METHOD)


def _login_name_key():
    data = request.get_json(silent=True)
    name = data.get("name") if isinstance(data, dict) else None
    return "login-name:" + (name.strip().lower() if isinstance(name, str) else "")


# Two limits. Per account name, counting only failed attempts, so a user who
# signs in successfully is never slowed down: protects one account from
# guessing spread across many IPs. Per client IP, counting every attempt:
# stops one client trying many names.
@bp.post("/auth/login")
@limiter.limit(
    lambda: current_app.config["LOGIN_RATE_LIMIT"],
    key_func=_login_name_key,
    deduct_when=lambda response: response.status_code == 401,
)
@limiter.limit(lambda: current_app.config["LOGIN_IP_RATE_LIMIT"], key_func=client_ip)
def login():
    """Verify credentials. Body: {"name": ..., "password": ...}.

    Returns the user (never the hash) and a bearer token for authenticated
    calls on success. Unknown name and wrong password get the same 401 so the
    response doesn't reveal which names exist.
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError("a JSON object body is required")
    name, password = data.get("name"), data.get("password")
    if not isinstance(name, str) or not isinstance(password, str) or not name or not password:
        raise ApiError("name and password are required")

    user = db.session.scalar(select(User).filter_by(name=name))
    if user is None:
        check_password_hash(_DUMMY_HASH, password)
        raise ApiError("invalid name or password", 401)
    if not user.check_password(password):
        raise ApiError("invalid name or password", 401)
    return jsonify(user=user.to_dict(), token=issue_token(user))


@bp.post("/auth/logout")
@require_user
def logout():
    """Revoke the caller's tokens: this one and any other the user holds."""
    revoke_tokens(g.user)
    db.session.commit()
    return "", 204


def _get_event(organization_name, event_name):
    event = db.session.scalar(
        select(Event)
        .join(Organization)
        .filter(Organization.name == organization_name, Event.name == event_name)
    )
    if event is None:
        raise ApiError(f"event {event_name!r} not found in {organization_name!r}", 404)
    return event


def _normalize_email(email):
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise ApiError("a valid email is required")
    return email


def _parse_ids(value, field):
    if not isinstance(value, list):
        raise ApiError(f"{field} must be a list of ids")
    try:
        return {uuid.UUID(str(v)) for v in value}
    except ValueError:
        raise ApiError(f"{field} contains an invalid id")


def _visible_fragment_types(enrichment_type):
    return sorted(
        (ft for ft in enrichment_type.fragment_types if not ft.hidden), key=lambda ft: ft.name
    )


def _selectable_enrichment_types(event):
    """Types a volunteer can choose from: those with at least one visible fragment type."""
    return sorted(
        (et for et in event.enrichment_types if _visible_fragment_types(et)),
        key=lambda et: et.name,
    )


def _enrichment_form(enrichment, visible_fragment_types):
    names = {ft.id: ft.name for ft in visible_fragment_types}
    fragments = [
        {"fragment_type_id": str(f.enrichment_fragment_type_id), "kind": "text", "value": f.value}
        for f in enrichment.text_fragments
        if f.enrichment_fragment_type_id in names
    ] + [
        {
            "fragment_type_id": str(f.enrichment_fragment_type_id),
            "kind": "calendar",
            "start": f.start.isoformat(),
            "end": f.end.isoformat(),
        }
        for f in enrichment.calendar_fragments
        if f.enrichment_fragment_type_id in names
    ]
    fragments.sort(
        key=lambda f: (names[uuid.UUID(f["fragment_type_id"])], f.get("value") or f["start"])
    )
    return {"id": str(enrichment.id), "fragments": fragments}


def _enrichment_type_form(enrichment_type):
    visible = _visible_fragment_types(enrichment_type)
    enrichments = [_enrichment_form(en, visible) for en in enrichment_type.enrichments]
    # No ordering column exists yet, so order options by their visible values.
    enrichments.sort(key=lambda en: [f.get("value") or f["start"] for f in en["fragments"]])
    return {
        **enrichment_type.to_dict(),
        "fragment_types": [ft.to_dict() for ft in visible],
        "enrichments": enrichments,
    }


@bp.get("/<organization>/<event_name>/volunteer-form")
def volunteer_form(organization, event_name):
    event = _get_event(organization, event_name)
    windows = db.session.scalars(
        select(VolunteerWindow)
        .filter_by(event_id=event.id)
        .order_by(VolunteerWindow.start, VolunteerWindow.end)
    )
    return jsonify(
        organization=event.organization.to_dict(),
        event=event.to_dict(),
        windows=[w.to_dict() for w in windows],
        enrichment_types=[_enrichment_type_form(et) for et in _selectable_enrichment_types(event)],
    )


def _find_offer(event, email):
    return db.session.scalar(select(VolunteerOffer).filter_by(event_id=event.id, email=email))


def _offer_dict(offer):
    return {
        **offer.to_dict(),
        "window_ids": sorted(
            str(wo.volunteer_window_id) for wo in offer.window_offers if not wo.cancelled
        ),
        "enrichment_ids": sorted(str(oe.enrichment_id) for oe in offer.offer_enrichments),
    }


@bp.get("/<organization>/<event_name>/offers/<email>")
def get_offer(organization, event_name, email):
    event = _get_event(organization, event_name)
    offer = _find_offer(event, _normalize_email(email))
    if offer is None:
        raise ApiError("no offer for that email at this event", 404)
    return jsonify(_offer_dict(offer))


@bp.put("/<organization>/<event_name>/offers/<email>")
def save_offer(organization, event_name, email):
    """Create or replace a volunteer's selections for an event.

    Body: {"name": "...", "window_ids": [...], "enrichment_ids": [...]} listing
    everything selected. The email in the URL identifies the volunteer; name is
    optional (missing or blank saves ""). Unselected windows are cancelled, not deleted, keeping their
    calendar UID. Links to enrichment types the volunteer can't see are left untouched.
    """
    event = _get_event(organization, event_name)
    email = _normalize_email(email)
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError("a JSON object body is required")
    name = data.get("name") or ""
    if not isinstance(name, str):
        raise ApiError("name must be a string")
    name = name.strip()
    if len(name) > 255:
        raise ApiError("name must be at most 255 characters")
    window_ids = _parse_ids(data.get("window_ids", []), "window_ids")
    enrichment_ids = _parse_ids(data.get("enrichment_ids", []), "enrichment_ids")

    windows = {w.id: w for w in event.windows}
    if unknown := window_ids - windows.keys():
        raise ApiError(f"windows not in this event: {sorted(map(str, unknown))}")

    selectable = _selectable_enrichment_types(event)
    type_of = {en.id: et for et in selectable for en in et.enrichments}
    if unknown := enrichment_ids - type_of.keys():
        raise ApiError(f"enrichments not selectable at this event: {sorted(map(str, unknown))}")
    for et in selectable:
        if et.volunteer_interaction is VolunteerInteraction.SELECT:
            if sum(1 for i in enrichment_ids if type_of[i] is et) > 1:
                raise ApiError(f"choose at most one option for {et.name!r}")

    offer = _find_offer(event, email)
    created = offer is None
    if created:
        offer = VolunteerOffer(email=email, name=name, event=event)
        db.session.add(offer)
    else:
        # Assigning an unchanged value doesn't dirty the row, so updated_on only
        # moves when the name really changes.
        offer.name = name

    changed = False
    existing = {wo.volunteer_window_id: wo for wo in offer.window_offers}
    for window_id, window_offer in existing.items():
        selected = window_id in window_ids
        if window_offer.cancelled == selected:
            window_offer.cancelled = not selected
            window_offer.sequence = VolunteerWindowOffer.sequence + 1
            changed = True
    for window_id in window_ids - existing.keys():
        db.session.add(VolunteerWindowOffer(volunteer_offer=offer, volunteer_window=windows[window_id]))
        changed = True

    linked = {oe.enrichment_id: oe for oe in offer.offer_enrichments}
    for enrichment_id, offer_enrichment in linked.items():
        if enrichment_id in type_of and enrichment_id not in enrichment_ids:
            db.session.delete(offer_enrichment)
            changed = True
    for enrichment_id in enrichment_ids - linked.keys():
        db.session.add(VolunteerOfferEnrichment(volunteer_offer=offer, enrichment_id=enrichment_id))
        changed = True

    # Selections live in child rows, so the offer row itself isn't dirtied by them.
    if changed and not created:
        offer.touch()

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise ApiError("this sign-up was changed at the same time; please try again", 409)
    return jsonify(_offer_dict(offer)), 201 if created else 200
