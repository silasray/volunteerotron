"""web.api_client.call: how the web tier reaches the API.

Locally it's HTTP to API_URL; deployed (API_FUNCTION_NAME set) it invokes the
API Lambda directly. Either way call() returns (status, json_body), and any
transport failure becomes UNAVAILABLE (502) rather than an exception.
"""
import io
import json
import sys
import types

import pytest
import requests

from web import api_client
from web.api_client import UNAVAILABLE, call


@pytest.fixture
def in_request(web_app):
    """A web request from the browser at 203.0.113.7."""
    with web_app.test_request_context(environ_base={"REMOTE_ADDR": "203.0.113.7"}):
        yield web_app


class Response:
    def __init__(self, status_code, text):
        self.status_code, self.text = status_code, text


@pytest.fixture
def http(monkeypatch):
    """Replaces requests.request; .reply sets what it returns, .calls records each call."""
    class Http:
        reply = Response(200, "{}")
        calls = []

        def __call__(self, method, url, **kwargs):
            self.calls.append({"method": method, "url": url, **kwargs})
            if isinstance(self.reply, Exception):
                raise self.reply
            return self.reply

    fake = Http()
    fake.calls = []
    monkeypatch.setattr(requests, "request", fake)
    return fake


# ---------------------------------------------------------------- over HTTP (local)


def test_http_call_sends_token_client_ip_and_body(in_request, http):
    http.reply = Response(201, '{"id": "x"}')
    assert call("PUT", "/acme/fair/offers/v@example.com", token="tok", json={"name": "V"}) == (201, {"id": "x"})
    assert http.calls == [{
        "method": "PUT",
        "url": "http://api.test/api/acme/fair/offers/v@example.com",
        "headers": {"Authorization": "Bearer tok", "X-Client-IP": "203.0.113.7"},
        "json": {"name": "V"},
        "timeout": 5,
    }]


def test_no_token_no_authorization_header(in_request, http):
    call("GET", "/health")
    assert "Authorization" not in http.calls[0]["headers"]


def test_outside_a_request_no_client_ip(web_app, http):
    with web_app.app_context():
        call("GET", "/health")
    assert http.calls[0]["headers"] == {}


@pytest.mark.parametrize("error", [requests.ConnectionError("refused"), requests.Timeout("slow")])
def test_http_failure_is_unavailable(in_request, http, error):
    http.reply = error
    assert call("GET", "/health") == UNAVAILABLE


def test_no_content_is_an_empty_body(in_request, http):
    http.reply = Response(204, "")
    assert call("POST", "/auth/logout", token="tok") == (204, {})


@pytest.mark.parametrize("status", [200, 500])
def test_non_json_reply_is_a_502(in_request, http, status):
    http.reply = Response(status, "<html>Bad gateway</html>")
    assert call("GET", "/health") == (502, {"error": f"unexpected response from the scheduling service ({status})"})


def test_error_statuses_pass_through(in_request, http):
    http.reply = Response(409, '{"error": "taken", "linked_count": 2}')
    assert call("DELETE", "/x") == (409, {"error": "taken", "linked_count": 2})


# ---------------------------------------------------------------- invoking the API Lambda (deployed)


class LambdaClient:
    """Stands in for boto3's Lambda client."""

    def __init__(self):
        self.invocations = []
        self.reply = {"status": 200, "body": "{}"}
        self.error = None
        self.function_error = None

    def invoke(self, FunctionName, Payload):
        self.invocations.append({"function": FunctionName, "payload": json.loads(Payload)})
        if self.error:
            raise self.error
        if self.function_error:
            return {"FunctionError": "Unhandled", "Payload": io.BytesIO(json.dumps(self.function_error).encode())}
        return {"StatusCode": 200, "Payload": io.BytesIO(json.dumps(self.reply).encode())}


@pytest.fixture
def lambda_client(monkeypatch, web_app, http):
    web_app.config["API_FUNCTION_NAME"] = "scheduler-ApiFunction"
    client = LambdaClient()
    monkeypatch.setattr(api_client, "_lambda_client", lambda: client)
    return client


def test_invoke_sends_an_internal_request(in_request, lambda_client, http):
    lambda_client.reply = {"status": 201, "body": '{"id": "x"}'}
    assert call("PUT", "/acme/fair/offers/v@example.com", token="tok", json={"name": "V"}) == (201, {"id": "x"})
    assert lambda_client.invocations == [{
        "function": "scheduler-ApiFunction",
        "payload": {"internal_request": {
            "method": "PUT",
            "path": "/api/acme/fair/offers/v@example.com",  # the API's own path, /api included
            "headers": {"Authorization": "Bearer tok", "X-Client-IP": "203.0.113.7"},
            "body": json.dumps({"name": "V"}),
        }},
    }]
    assert http.calls == []  # never HTTP when deployed


def test_invoke_without_body_sends_null(in_request, lambda_client):
    call("GET", "/health")
    assert lambda_client.invocations[0]["payload"]["internal_request"]["body"] is None


def test_invoke_relays_api_errors(in_request, lambda_client):
    lambda_client.reply = {"status": 404, "body": '{"error": "event not found"}'}
    assert call("GET", "/x") == (404, {"error": "event not found"})


def test_invoke_failure_is_unavailable_and_logged(in_request, lambda_client, caplog):
    lambda_client.error = RuntimeError("TooManyRequestsException")
    assert call("GET", "/health") == UNAVAILABLE
    assert "API invoke failed" in caplog.text


def test_function_error_is_unavailable_and_logged(in_request, lambda_client, caplog):
    lambda_client.function_error = {"errorMessage": "boom", "errorType": "OperationalError"}
    assert call("GET", "/health") == UNAVAILABLE
    assert "API function error" in caplog.text and "boom" in caplog.text


def test_lambda_client_never_retries(monkeypatch):
    """A retried invoke could repeat a write, so retries are off."""
    created = []
    boto3 = types.ModuleType("boto3")
    boto3.client = lambda service, **kwargs: created.append({"service": service, **kwargs}) or "client"
    config_module = types.ModuleType("botocore.config")
    config_module.Config = lambda **kwargs: kwargs
    monkeypatch.setitem(sys.modules, "boto3", boto3)
    monkeypatch.setitem(sys.modules, "botocore", types.ModuleType("botocore"))
    monkeypatch.setitem(sys.modules, "botocore.config", config_module)
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    api_client._lambda_client.cache_clear()
    try:
        assert api_client._lambda_client() == "client"
        assert api_client._lambda_client() == "client"  # built once per instance
    finally:
        api_client._lambda_client.cache_clear()
    assert len(created) == 1
    assert created[0]["service"] == "lambda"
    assert created[0]["region_name"] == "us-east-1"
    assert created[0]["config"]["retries"] == {"max_attempts": 1}
