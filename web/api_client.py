import json as _json  # call() takes a json= argument, like requests
from functools import lru_cache
from urllib.parse import quote

import requests
from flask import current_app, has_request_context, request

UNAVAILABLE = (502, {"error": "the scheduling service is unavailable"})

# Header the API reads the browser's IP from, for per-IP rate limits. The API
# trusts it only where it can't be called directly (see TRUSTED_CLIENT_IP_HEADER).
CLIENT_IP_HEADER = "X-Client-IP"


def segment(value):
    """Escape a value for use as one URL path segment."""
    return quote(value, safe="@")


def call(method, path, token=None, json=None):
    """Call the API and return (status_code, json_body), including error responses.

    path: below the API root, e.g. "/auth/login".
    token: the signed-in user's API token, sent as a bearer token.
    """
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    if has_request_context() and request.remote_addr:
        headers[CLIENT_IP_HEADER] = request.remote_addr
    if current_app.config["API_FUNCTION_NAME"]:
        status, text = _invoke(method, "/api" + path, headers, json)
    else:
        status, text = _http(method, current_app.config["API_URL"] + path, headers, json)
    if status is None:
        return UNAVAILABLE
    if status == 204:
        return 204, {}
    try:
        return status, _json.loads(text)
    except ValueError:
        return 502, {"error": f"unexpected response from the scheduling service ({status})"}


def _http(method, url, headers, body):
    try:
        resp = requests.request(method, url, headers=headers, json=body, timeout=5)
    except requests.RequestException:
        return None, None
    return resp.status_code, resp.text


@lru_cache(maxsize=1)
def _lambda_client():
    import os

    import boto3  # provided by the Lambda runtime
    from botocore.config import Config

    # No automatic retries: a retried invoke could repeat a write.
    return boto3.client(
        "lambda",
        region_name=os.environ.get("AWS_REGION"),  # set by Lambda
        config=Config(retries={"max_attempts": 1}, read_timeout=30, connect_timeout=5),
    )


def _invoke(method, path, headers, body):
    """Call the API Lambda directly (see lambda_handlers.api_handler).

    Access is controlled by IAM, so the API needs no public endpoint.
    """
    payload = {"internal_request": {
        "method": method,
        "path": path,
        "headers": headers,
        "body": None if body is None else _json.dumps(body),
    }}
    try:
        resp = _lambda_client().invoke(
            FunctionName=current_app.config["API_FUNCTION_NAME"],
            Payload=_json.dumps(payload).encode(),
        )
    except Exception:  # botocore errors: throttling, network, permissions
        current_app.logger.exception("API invoke failed")
        return None, None
    if resp.get("FunctionError"):
        current_app.logger.error("API function error: %s", resp["Payload"].read()[:2000])
        return None, None
    result = _json.loads(resp["Payload"].read())
    return result["status"], result["body"]
