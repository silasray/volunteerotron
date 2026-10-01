"""AWS Lambda entry points (see template.yaml).

web_handler  - behind API Gateway (HTTP API, payload v2); serves every page.
api_handler  - invoked directly by the web function with
               {"internal_request": {...}}, never through API Gateway, so the
               API has no public endpoint. Also runs admin commands:
                 {"command": "migrate"}
                 {"command": "create-user", "name": ..., "password": ..., "superuser": false}
"""
import json

_apps = {}


def _app(name):
    # Created on first use and kept for warm invocations. Each function only
    # builds the app it serves.
    if name not in _apps:
        if name == "web":
            from web import create_app
        else:
            from api import create_app
        _apps[name] = create_app()
    return _apps[name]


_web = None


def web_handler(event, context):
    global _web
    if _web is None:
        from apig_wsgi import make_lambda_handler

        _web = make_lambda_handler(_app("web"))
    return _web(event, context)


def api_handler(event, context):
    if "internal_request" in event:
        return _internal_request(event["internal_request"])
    if "command" in event:
        return _command(event)
    raise ValueError("unsupported event: expected 'internal_request' or 'command'")


def _internal_request(req):
    app = _app("api")
    with app.test_client() as client:
        resp = client.open(
            req["path"],
            method=req["method"],
            headers=req.get("headers") or {},
            data=req.get("body"),
            content_type="application/json" if req.get("body") is not None else None,
        )
    return {"status": resp.status_code, "body": resp.get_data(as_text=True)}


def _command(event):
    app = _app("api")
    command = event["command"]
    if command == "migrate":
        from flask_migrate import upgrade

        with app.app_context():
            upgrade()
        return {"ok": True, "output": "database upgraded to the latest migration"}
    if command == "create-user":
        args = ["create-user", event["name"], "--password", event["password"]]
        if event.get("superuser"):
            args.append("--superuser")
        result = app.test_cli_runner().invoke(args=args)
        # The password never appears in the output.
        return {"ok": result.exit_code == 0, "output": result.output.strip()}
    raise ValueError(f"unknown command {command!r}: expected 'migrate' or 'create-user'")


if __name__ == "__main__":  # pragma: no cover - handy for a quick local check
    print(json.dumps(api_handler({"internal_request": {"method": "GET", "path": "/api/health"}}, None)))
