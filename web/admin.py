import hmac
import secrets

from flask import (
    Blueprint, abort, flash, g, jsonify, redirect, render_template, request, session, url_for,
)

from . import api_client
from .api_client import segment

bp = Blueprint("admin", __name__, url_prefix="/admin")

# Login rate limits (per name and per client IP) are enforced by the API; the
# web tier forwards the client IP with each call (see api_client).
TOO_MANY = "Too many sign-in attempts. Please wait a few minutes and try again."


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def _check_csrf():
    sent = request.form.get("csrf", "")
    if not hmac.compare_digest(sent, session.get("csrf", "")):
        abort(400)


# ---- forms submitted in the background (static/admin_forms.js) ----
# Those ask for JSON so a failure can be shown in a dialog without reloading
# the page and losing what was typed. Without the script, the same routes
# behave as ordinary form posts (redirect, or re-render with the error).


def _wants_json():
    return request.accept_mimetypes.best == "application/json"


def _json_error(message, status):
    return jsonify(ok=False, error=message), status if 400 <= status < 500 else 400


@bp.after_request
def _redirects_to_json(response):
    """For background posts, turn a route's redirect into JSON: an error if the
    route flashed one, otherwise where to go to show the result."""
    if request.method != "POST" or not _wants_json() or response.status_code not in (301, 302, 303):
        return response
    flashes = session.get("_flashes", [])
    errors = [message for category, message in flashes if category == "error"]
    if errors:
        session["_flashes"] = [(c, m) for c, m in flashes if c != "error"]
        failed = jsonify(ok=False, error=" ".join(errors))
        failed.status_code = 400  # hooks must return a response object, not a tuple
        return failed
    return jsonify(ok=True, reload=response.location)


EXPIRED = "Your session has expired. Please sign in again."


def _remember_page():
    """On a page load, note the page so signing in again returns to it."""
    if request.method == "GET":
        query = request.query_string.decode()
        session["next"] = request.path + ("?" + query if query else "")


def _after_login_url(next_url):
    """Where to go after signing in: the remembered admin page, if any.

    Only paths in this site's admin area are followed. The page checks access
    as usual, so one this user can't see still 404s.
    """
    admin = url_for("admin.index")  # "/admin", below any script root
    if (
        isinstance(next_url, str)
        and (next_url == admin or next_url.startswith(admin + "/") or next_url.startswith(admin + "?"))
        and "\\" not in next_url
    ):
        return next_url
    return admin


def _end_expired_session():
    """Sign out after the API rejected the token, remembering why, so admin
    pages send this browser to the login page instead of a 404."""
    session.clear()
    session.permanent = True
    session["expired"] = True
    _remember_page()


def _me():
    """The signed-in user's details and organizations from the API, or None.

    Fetched once per request. An expired or rejected token ends the session.
    """
    if "me" not in g:
        g.me = None
        token = session.get("api_token")
        if token:
            status, body = api_client.call("GET", "/admin/me", token=token)
            if status == 200:
                g.me = body
            elif status == 401:
                _end_expired_session()
            else:
                abort(502)
    return g.me


def _require_me():
    # Admin pages 404 rather than redirect, so they don't reveal that they
    # exist, except to a browser whose sign-in just timed out.
    me = _me()
    if me is None:
        if session.get("expired") and request.method == "GET":
            _remember_page()
            abort(redirect(url_for("admin.index")))
        abort(404)
    return me


def _find_org(me, org_name):
    return next((o for o in me["organizations"] if o["name"] == org_name), None)


def _render(template, me, org_name=None, status=200, **context):
    """Render an admin page with the side navigation.

    The active organization is, in order: the page's own organization, ?org=,
    the last one chosen this session, or the first one. It's remembered in the
    session.
    """
    orgs = me["organizations"]
    active = (
        _find_org(me, org_name or request.args.get("org") or session.get("active_org"))
        or (orgs[0] if orgs else None)
    )
    if active:
        session["active_org"] = active["name"]
    return render_template(
        template, me=me, orgs=orgs, active_org=active, csrf=csrf_token(), **context
    ), status


@bp.get("", strict_slashes=False)  # /admin/ too
def index():
    me = _me()
    if me is None:
        return render_template("admin_login.html", csrf=csrf_token(), name="", error=None,
                               notice=EXPIRED if session.get("expired") else None)
    return _render("admin.html", me)


def _require_superuser():
    me = _require_me()
    if not me["user"]["is_superuser"]:
        abort(404)
    return me


def _admin_api(method, path, **kwargs):
    """Call a superuser API endpoint; ends the session or 502s like the pages do."""
    status, body = api_client.call(method, "/admin" + path, token=session["api_token"], **kwargs)
    if status == 401:
        _end_expired_session()
        abort(redirect(url_for("admin.index")))
    if status >= 500:
        abort(502)
    return status, body


def _users_api(method, path="", **kwargs):
    return _admin_api(method, "/users" + path, **kwargs)


def _users_page(me, selected_id=None, status=200, **context):
    list_status, users = _users_api("GET")
    if list_status != 200:
        abort(404)
    selected = next((u for u in users if u["id"] == (selected_id or request.args.get("user"))), None)
    return _render(
        "admin_users.html", me, status=status, users=users,
        selected=selected or (users[0] if users else None), **context,
    )


def _error_text(body, fallback):
    error = body.get("error") or fallback
    return error[:1].upper() + error[1:]


def _back_to(user_id):
    return redirect(url_for("admin.users", user=user_id))


@bp.get("/users")
def users():
    return _users_page(_require_superuser())


@bp.post("/users")
def create_user():
    _check_csrf()
    me = _require_superuser()
    form = {"name": request.form.get("name", "").strip(), "password": request.form.get("password", "")}
    status, body = _users_api("POST", json=form)
    if status == 201:
        flash(f"Created user {body['name']}.")
        return _back_to(body["id"])
    if status == 404:
        abort(404)
    if _wants_json():
        return _json_error(_error_text(body, "Could not create the user."), status)
    # Keep what was typed, including a generated password, so it isn't lost.
    return _users_page(me, status=status, create_error=_error_text(body, "Could not create the user."),
                       create_form=form)


def _update_user(user_id, payload, success, error_key):
    _check_csrf()
    me = _require_superuser()
    status, body = _users_api("PATCH", f"/{segment(user_id)}", json=payload)
    if status == 200:
        flash(success.format(name=body["name"]))
        return _back_to(user_id)
    if status == 404:
        abort(404)
    if _wants_json():
        return _json_error(_error_text(body, "Could not update the user."), status)
    return _users_page(me, selected_id=user_id, status=status,
                       **{error_key: _error_text(body, "Could not update the user.")},
                       manage_password=payload.get("password", ""))


@bp.post("/users/<user_id>/password")
def change_password(user_id):
    return _update_user(user_id, {"password": request.form.get("password", "")},
                        "Changed the password for {name}.", "password_error")


@bp.post("/users/<user_id>/superuser")
def set_superuser(user_id):
    is_superuser = request.form.get("is_superuser") == "on"
    return _update_user(
        user_id, {"is_superuser": is_superuser},
        "{name} is " + ("now" if is_superuser else "no longer") + " a super user.",
        "superuser_error",
    )


@bp.post("/users/<user_id>/delete")
def delete_user(user_id):
    _check_csrf()
    me = _require_superuser()
    name = request.form.get("name", "the user")
    status, body = _users_api("DELETE", f"/{segment(user_id)}")
    if status == 204:
        flash(f"Deleted user {name}.")
        return redirect(url_for("admin.users"))
    if status == 404:
        abort(404)
    return _users_page(me, selected_id=user_id, status=status,
                       delete_error=_error_text(body, "Could not delete the user."))


def _orgs_page(me, selected_id=None, status=200, **context):
    """The org management page. The managed org is ?manage=, else the last one
    managed this session, else the first. It's separate from the nav's ?org=."""
    orgs_status, orgs = _admin_api("GET", "/organizations")
    users_status, users = _admin_api("GET", "/users")
    if orgs_status != 200 or users_status != 200:
        abort(404)
    wanted = selected_id or request.args.get("manage") or session.get("manage_org_id")
    selected = next((o for o in orgs if o["id"] == wanted), None) or (orgs[0] if orgs else None)
    if selected:
        session["manage_org_id"] = selected["id"]
        members = {m["user_id"]: m for m in selected["members"]}
        for user in users:
            user["membership"] = members.get(user["id"])
    return _render(
        "admin_orgs.html", me, status=status, managed_orgs=orgs, selected=selected,
        users=users, q=request.values.get("q", ""), **context,
    )


def _back_to_org(org_id, user_id=None):
    """Back to the managed org, keeping the user filter and scroll position."""
    url = url_for("admin.organizations", manage=org_id, q=request.form.get("q") or None)
    return redirect(url + (f"#user-{user_id}" if user_id else ""))


@bp.get("/organizations")
def organizations():
    return _orgs_page(_require_superuser())


@bp.post("/organizations")
def create_organization():
    _check_csrf()
    me = _require_superuser()
    form = {
        "name": request.form.get("name", "").strip(),
        "pretty_name": request.form.get("pretty_name", "").strip(),
    }
    status, body = _admin_api("POST", "/organizations", json=form)
    if status == 201:
        flash(f"Created organization {body['pretty_name']}.")
        return _back_to_org(body["id"])
    if status == 404:
        abort(404)
    if _wants_json():
        return _json_error(_error_text(body, "Could not create the organization."), status)
    return _orgs_page(me, status=status, create_error=_error_text(body, "Could not create the organization."),
                      create_form=form)


@bp.post("/organizations/<org_id>/pretty-name")
def rename_organization(org_id):
    _check_csrf()
    me = _require_superuser()
    pretty_name = request.form.get("pretty_name", "").strip()
    status, body = _admin_api("PATCH", f"/organizations/{segment(org_id)}", json={"pretty_name": pretty_name})
    if status == 200:
        flash(f"Changed the pretty name to {body['pretty_name']}.")
        return _back_to_org(org_id)
    if status == 404:
        abort(404)
    if _wants_json():
        return _json_error(_error_text(body, "Could not change the pretty name."), status)
    return _orgs_page(me, selected_id=org_id, status=status,
                      pretty_name_error=_error_text(body, "Could not change the pretty name."),
                      pretty_name_value=pretty_name)


MEMBER_ACTIONS = {
    # action: (API method, is_admin, message)
    "add": ("PUT", False, "Added {user} to {org}."),
    "make-admin": ("PUT", True, "{user} is now an admin of {org}."),
    "remove-admin": ("PUT", False, "{user} is no longer an admin of {org}."),
    "remove": ("DELETE", None, "Removed {user} from {org}."),
}


@bp.post("/organizations/<org_id>/members/<user_id>")
def change_membership(org_id, user_id):
    _check_csrf()
    _require_superuser()
    action = MEMBER_ACTIONS.get(request.form.get("action", ""))
    if action is None:
        abort(400)
    method, is_admin, message = action
    path = f"/organizations/{segment(org_id)}/members/{segment(user_id)}"
    status, body = _admin_api(method, path, **({"json": {"is_admin": is_admin}} if method == "PUT" else {}))
    if status in (200, 201, 204):
        flash(message.format(user=request.form.get("user_name", "The user"),
                             org=request.form.get("org_name", "the organization")))
    elif status == 404 and method == "DELETE":
        # Already removed (e.g. in another tab): nothing to do.
        flash(f"{request.form.get('user_name', 'The user')} wasn't a member.")
    else:
        abort(404)
    return _back_to_org(org_id, user_id)


def _event_or_404(me, org_name, event_name, need_admin=False):
    org = _find_org(me, org_name)
    if org is None or (need_admin and not org["is_admin"]):
        abort(404)
    event = next((e for e in org["events"] if e["name"] == event_name), None)
    if event is None:
        abort(404)
    return org, event


def _manage_api(method, org_name, event_name, path, json=None):
    status, body = api_client.call(
        method, f"/admin/event-manage/{segment(org_name)}/{segment(event_name)}{path}",
        token=session["api_token"], json=json,
    )
    if status == 401:
        _end_expired_session()
        abort(redirect(url_for("admin.index")))
    if status == 404:
        abort(404)
    if status >= 500:
        abort(502)
    return status, body


@bp.get("/<org_name>/<event_name>/manage")
def manage_event(org_name, event_name):
    me = _require_me()
    _event_or_404(me, org_name, event_name)
    _, data = _manage_api("GET", org_name, event_name, "/windows")
    return _render("admin_manage.html", me, org_name=org_name, manage=data)


RESPONSE_ACTIONS = {
    # form value: (API value, message)
    "accepted": ("accepted", "Accepted {name}."),
    "denied": ("denied", "Denied {name}."),
    "pending": (None, "{name} is back to pending."),
}


@bp.post("/<org_name>/<event_name>/manage/signups/<signup_id>/response")
def set_signup_response(org_name, event_name, signup_id):
    _check_csrf()
    me = _require_me()
    _event_or_404(me, org_name, event_name)
    action = RESPONSE_ACTIONS.get(request.form.get("response", ""))
    if action is None:
        abort(400)
    value, message = action
    status, body = _manage_api("PUT", org_name, event_name, f"/signups/{segment(signup_id)}/response",
                               json={"response": value})
    if status == 200:
        volunteer = body["volunteer"]
        flash(message.format(name=volunteer["name"] or volunteer["email"]))
    else:
        flash(_error_text(body, "Couldn't change that response."), "error")
    window_id = request.form.get("window_id", "")
    url = url_for("admin.manage_event", org_name=org_name, event_name=event_name)
    return redirect(url + (f"#window-{window_id}" if window_id else ""))


@bp.get("/<org_name>/<event_name>/configure")
def configure_event(org_name, event_name):
    me = _require_me()
    _event_or_404(me, org_name, event_name, need_admin=True)
    status, config = _config_api("GET", org_name, event_name, "/config")
    if status != 200:
        abort(404)
    return _render("admin_configure.html", me, org_name=org_name, config=config,
                   open_types=set(request.args.getlist("open")))


# ---- configure page actions: each calls the API, then returns to the page ----


def _config_api(method, org_name, event_name, path, json=None):
    status, body = api_client.call(
        method, f"/admin/event-config/{segment(org_name)}/{segment(event_name)}{path}",
        token=session["api_token"], json=json,
    )
    if status == 401:
        _end_expired_session()
        abort(redirect(url_for("admin.index")))
    if status == 404:
        abort(404)
    if status >= 500:
        abort(502)
    return status, body


def _config_action(org_name, event_name):
    """Common start for configure actions: CSRF and org-admin check."""
    _check_csrf()
    me = _require_me()
    _event_or_404(me, org_name, event_name, need_admin=True)


def _back_to_config(org_name, event_name, anchor="", open_type=None):
    url = url_for("admin.configure_event", org_name=org_name, event_name=event_name,
                  open=open_type or None)
    return redirect(url + (f"#{anchor}" if anchor else ""))


def _report(status, body, success, ok=(200, 201)):
    if status in ok:
        flash(success)
    else:
        flash(_error_text(body, "That didn't work."), "error")


def _delete_choice():
    """Body for a delete that may have linked records (see the delete dialog)."""
    mode = request.form.get("mode", "")
    if mode == "cascade":
        return {"cascade": True}
    if mode == "reassign" and request.form.get("reassign_to"):
        return {"reassign_to": request.form["reassign_to"]}
    return {}


C = "/<org_name>/<event_name>/configure"


@bp.post(C + "/windows")
def add_window(org_name, event_name):
    _config_action(org_name, event_name)
    start, end = request.form.get("start_iso", ""), request.form.get("end_iso", "")
    if not start or not end:
        flash("Enter a start and an end.", "error")
    else:
        status, body = _config_api("POST", org_name, event_name, "/windows",
                                   json={"start": start, "end": end})
        _report(status, body, "Added the window.")
    return _back_to_config(org_name, event_name, "windows")


@bp.post(C + "/windows/<window_id>/delete")
def delete_window(org_name, event_name, window_id):
    _config_action(org_name, event_name)
    status, body = _config_api("DELETE", org_name, event_name, f"/windows/{segment(window_id)}",
                               json=_delete_choice())
    moved = body.get("moved") if status == 200 else 0
    _report(status, body, "Deleted the window" + (f" and moved {moved} offer(s)." if moved else "."))
    return _back_to_config(org_name, event_name, "windows")


@bp.post(C + "/enrichment-types")
def add_enrichment_type(org_name, event_name):
    _config_action(org_name, event_name)
    status, body = _config_api("POST", org_name, event_name, "/enrichment-types", json={
        "name": request.form.get("name", ""),
        "volunteer_interaction": request.form.get("volunteer_interaction", ""),
    })
    _report(status, body, f"Added {body.get('name', 'the enrichment')}.")
    new_id = body.get("id") if status == 201 else None
    return _back_to_config(org_name, event_name, f"type-{new_id}" if new_id else "enrichments", new_id)


@bp.post(C + "/enrichment-types/<type_id>/interaction")
def set_interaction(org_name, event_name, type_id):
    _config_action(org_name, event_name)
    status, body = _config_api("PATCH", org_name, event_name, f"/enrichment-types/{segment(type_id)}",
                               json={"volunteer_interaction": request.form.get("volunteer_interaction", "")})
    _report(status, body, f"Updated {body.get('name', 'the enrichment')}.")
    return _back_to_config(org_name, event_name, f"type-{type_id}", request.form.get("open") or None)


@bp.post(C + "/enrichment-types/<type_id>/delete")
def delete_enrichment_type(org_name, event_name, type_id):
    _config_action(org_name, event_name)
    status, body = _config_api("DELETE", org_name, event_name, f"/enrichment-types/{segment(type_id)}",
                               json=_delete_choice())
    _report(status, body, "Deleted the enrichment.")
    return _back_to_config(org_name, event_name, "enrichments")


@bp.post(C + "/enrichment-types/<type_id>/fields")
def add_field(org_name, event_name, type_id):
    _config_action(org_name, event_name)
    status, body = _config_api("POST", org_name, event_name, f"/enrichment-types/{segment(type_id)}/fields", json={
        "name": request.form.get("name", ""), "content_type": request.form.get("content_type", ""),
    })
    _report(status, body, f"Added the field {body.get('name', '')}.")
    return _back_to_config(org_name, event_name, f"type-{type_id}", type_id)


@bp.post(C + "/fields/<field_id>/hidden")
def set_field_hidden(org_name, event_name, field_id):
    _config_action(org_name, event_name)
    hidden = request.form.get("hidden") == "1"
    status, body = _config_api("PATCH", org_name, event_name, f"/fields/{segment(field_id)}",
                               json={"hidden": hidden})
    _report(status, body, f"{body.get('name', 'The field')} is now {'hidden from' if hidden else 'shown to'} volunteers.")
    type_id = request.form.get("type_id", "")
    return _back_to_config(org_name, event_name, f"type-{type_id}", type_id or None)


@bp.post(C + "/fields/<field_id>/delete")
def delete_field(org_name, event_name, field_id):
    _config_action(org_name, event_name)
    status, body = _config_api("DELETE", org_name, event_name, f"/fields/{segment(field_id)}",
                               json=_delete_choice())
    removed = body.get("removed") if status == 200 else 0
    _report(status, body, "Deleted the column" + (f" and its values in {removed} row(s)." if removed else "."))
    type_id = request.form.get("type_id", "")
    return _back_to_config(org_name, event_name, f"type-{type_id}", type_id or None)


@bp.post(C + "/enrichment-types/<type_id>/rows")
def add_row(org_name, event_name, type_id):
    _config_action(org_name, event_name)
    status, body = _config_api("POST", org_name, event_name, f"/enrichment-types/{segment(type_id)}/enrichments")
    _report(status, body, "Added a row. Fill it in, then Update Values.")
    return _back_to_config(org_name, event_name, f"type-{type_id}", type_id)


@bp.post(C + "/enrichment-types/<type_id>/values")
def update_values(org_name, event_name, type_id):
    """Form fields: t|<row>|<field> for text; c|<row>|<field>|start / |end for
    calendar values (UTC ISO, filled in by the page's script)."""
    _config_action(org_name, event_name)
    values = {}
    for key, value in request.form.items():
        parts = key.split("|")
        if parts[0] == "t" and len(parts) == 3:
            values.setdefault(parts[1], {})[parts[2]] = value
        elif parts[0] == "c" and len(parts) == 4 and parts[3] in ("start", "end"):
            values.setdefault(parts[1], {}).setdefault(parts[2], {})[parts[3]] = value
    status, body = _config_api("PUT", org_name, event_name, f"/enrichment-types/{segment(type_id)}/values",
                               json={"values": values})
    if request.accept_mimetypes.best == "application/json":
        # The page's script saves in the background, so on failure it can show
        # what's wrong without reloading away the user's edits.
        if status == 200:
            flash("Saved the values.")
            return jsonify(ok=True, reload=url_for(
                "admin.configure_event", org_name=org_name, event_name=event_name, open=type_id
            ) + f"#type-{type_id}")
        return jsonify(ok=False, error=_error_text(body, "Couldn't save the values."),
                       problems=body.get("problems", [])), status
    _report(status, body, "Saved the values.")
    return _back_to_config(org_name, event_name, f"type-{type_id}", type_id)


@bp.post(C + "/enrichments/<enrichment_id>/delete")
def delete_enrichment(org_name, event_name, enrichment_id):
    _config_action(org_name, event_name)
    status, body = _config_api("DELETE", org_name, event_name, f"/enrichments/{segment(enrichment_id)}",
                               json=_delete_choice())
    moved = body.get("moved") if status == 200 else 0
    _report(status, body, "Deleted the row" + (f" and moved {moved} choice(s)." if moved else "."))
    type_id = request.form.get("type_id", "")
    return _back_to_config(org_name, event_name, f"type-{type_id}", type_id or None)


@bp.post("/events")
def create_event():
    _check_csrf()
    me = _require_me()
    org_name = request.form.get("organization", "")
    org = _find_org(me, org_name)
    if org is None or not org["is_admin"]:
        abort(404)
    form = {
        "name": request.form.get("name", "").strip(),
        "pretty_name": request.form.get("pretty_name", "").strip(),
    }
    status, body = api_client.call(
        "POST", f"/admin/{segment(org_name)}/events", token=session["api_token"], json=form
    )
    if status == 201:
        # Creators are org admins (required above), so they can always configure.
        return redirect(url_for(
            "admin.configure_event", org_name=org_name, event_name=body["name"]
        ))
    if status == 401:
        _end_expired_session()
        return redirect(url_for("admin.index"))
    if status in (403, 404):
        # Membership or admin rights changed since the page was loaded.
        abort(404)
    error = body.get("error", "Could not create the event.") if status in (400, 409) else (
        "The scheduling service is unavailable. Please try again."
    )
    if _wants_json():
        return _json_error(error[:1].upper() + error[1:], status)
    # Re-show the page with the dialog open, the error and what was typed.
    return _render("admin.html", me, org_name=org_name, status=status if status < 500 else 502,
                   new_event_error=error[:1].upper() + error[1:], new_event_form=form)


@bp.post("/login")
def login():
    _check_csrf()
    name = request.form.get("name", "").strip()
    password = request.form.get("password", "")
    if not name or not password:
        error, status = "Enter your name and password.", 400
    else:
        # The plain password goes only to the API, which does all hashing.
        status, body = api_client.call(
            "POST", "/auth/login", json={"name": name, "password": password}
        )
        if status == 200:
            next_url = _after_login_url(session.get("next"))
            # New session on login so a pre-login session can't be fixed onto the user.
            session.clear()
            session.permanent = True
            session["user"] = {"id": body["user"]["id"], "name": body["user"]["name"]}
            session["api_token"] = body["token"]
            return redirect(next_url)
        if status in (400, 401):
            error = "Invalid name or password."
        elif status == 429:
            error = TOO_MANY
        else:
            error = "Sign-in is unavailable right now. Please try again."
    return render_template(
        "admin_login.html", csrf=csrf_token(), name=name, error=error
    ), status


@bp.post("/logout")
def logout():
    _check_csrf()
    session.clear()
    return redirect(url_for("admin.index"))
