from urllib.parse import quote

import requests
from flask import current_app


def segment(value):
    """Escape a value for use as one URL path segment."""
    return quote(value, safe="@")


def call(method, path, **kwargs):
    """Call the API and return (status_code, json_body), including error responses."""
    try:
        resp = requests.request(
            method, current_app.config["API_URL"] + path, timeout=5, **kwargs
        )
    except requests.RequestException:
        return 502, {"error": "the scheduling service is unavailable"}
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return 502, {"error": f"unexpected response from the scheduling service ({resp.status_code})"}
