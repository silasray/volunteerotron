"""The public volunteer page and the routes that relay loading and saving an
offer to the API (web/routes.py). The API is stubbed (fake_api)."""
import json

import pytest

PATH = "/acme/spring-fair/volunteer"
FORM = {
    "organization": {"id": "o1", "name": "acme", "pretty_name": "Acme"},
    "event": {"id": "e1", "name": "spring-fair", "pretty_name": "Spring Fair"},
    "windows": [],
    "enrichment_types": [],
}


def test_page_renders_the_form(web, fake_api):
    fake_api.on("GET", "/acme/spring-fair/volunteer-form", 200, FORM)
    resp = web.get(PATH)
    assert resp.status_code == 200
    assert b"Volunteer for Spring Fair" in resp.data
    assert fake_api.calls[0]["token"] is None


@pytest.mark.parametrize("api_status, expected", [(404, 404), (500, 502), (502, 502), (400, 502)])
def test_page_errors(web, fake_api, api_status, expected):
    fake_api.on("GET", "/acme/spring-fair/volunteer-form", api_status, {"error": "x"})
    assert web.get(PATH).status_code == expected


def test_load_relays_status_and_body(web, fake_api):
    fake_api.on("GET", "/acme/spring-fair/offers/vol@example.com", 404, {"error": "no offer"})
    resp = web.get(PATH + "/offer?email=%20vol@example.com%20")
    assert (resp.status_code, resp.json) == (404, {"error": "no offer"})


def test_load_requires_email(web, fake_api):
    assert web.get(PATH + "/offer").status_code == 400
    assert web.get(PATH + "/offer?email=%20").status_code == 400
    assert fake_api.calls == []


def test_email_stays_one_path_segment(web, fake_api):
    fake_api.on("GET", "/acme/spring-fair/offers/a%2Fb%3Fc@example.com", 404, {})
    web.get(PATH + "/offer?email=a/b%3Fc@example.com")
    assert fake_api.calls[0]["path"] == "/acme/spring-fair/offers/a%2Fb%3Fc@example.com"


def test_save_forwards_only_the_offer_fields(web, fake_api):
    fake_api.on("PUT", "/acme/spring-fair/offers/vol@example.com", 201, {"id": "x"})
    resp = web.put(PATH + "/offer", json={
        "email": " vol@example.com ", "name": "Vol", "window_ids": ["w1"], "enrichment_ids": ["e1"],
        "is_superuser": True, "event_id": "elsewhere",
    })
    assert (resp.status_code, resp.json) == (201, {"id": "x"})
    assert fake_api.calls[0]["json"] == {"name": "Vol", "window_ids": ["w1"], "enrichment_ids": ["e1"]}
    assert fake_api.calls[0]["token"] is None


def test_save_fills_in_missing_fields(web, fake_api):
    fake_api.on("PUT", "/acme/spring-fair/offers/vol@example.com", 201, {})
    web.put(PATH + "/offer", json={"email": "vol@example.com"})
    assert fake_api.calls[0]["json"] == {"name": "", "window_ids": [], "enrichment_ids": []}


def test_save_relays_api_errors(web, fake_api):
    error = {"error": "choose at most one option for 'Shirt'"}
    fake_api.on("PUT", "/acme/spring-fair/offers/vol@example.com", 400, error)
    resp = web.put(PATH + "/offer", json={"email": "vol@example.com"})
    assert (resp.status_code, resp.json) == (400, error)


@pytest.mark.parametrize("body", [{}, {"email": ""}, {"email": "   "}, None])
def test_save_requires_email(web, fake_api, body):
    resp = web.put(PATH + "/offer", data=json.dumps(body), content_type="application/json")
    assert resp.status_code == 400
    assert fake_api.calls == []
