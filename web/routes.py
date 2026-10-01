from flask import Blueprint, abort, jsonify, render_template, request

from . import api_client
from .api_client import segment

bp = Blueprint("main", __name__)


@bp.route("/")
def index():
    return render_template("index.html")


@bp.get("/<organization>/<event_name>/volunteer")
def volunteer(organization, event_name):
    status, form = api_client.call(
        "GET", f"/{segment(organization)}/{segment(event_name)}/volunteer-form"
    )
    if status == 404:
        abort(404)
    if status != 200:
        abort(502)
    return render_template("volunteer.html", form=form)


# The browser only talks to this tier; these relay Load/Submit to the API.
@bp.get("/<organization>/<event_name>/volunteer/offer")
def load_offer(organization, event_name):
    email = request.args.get("email", "").strip()
    if not email:
        return jsonify(error="email is required"), 400
    status, body = api_client.call(
        "GET", f"/{segment(organization)}/{segment(event_name)}/offers/{segment(email)}"
    )
    return jsonify(body), status


@bp.put("/<organization>/<event_name>/volunteer/offer")
def save_offer(organization, event_name):
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip()
    if not email:
        return jsonify(error="email is required"), 400
    status, body = api_client.call(
        "PUT",
        f"/{segment(organization)}/{segment(event_name)}/offers/{segment(email)}",
        json={
            "name": data.get("name", ""),
            "window_ids": data.get("window_ids", []),
            "enrichment_ids": data.get("enrichment_ids", []),
        },
    )
    return jsonify(body), status
