def test_health(api):
    resp = api.get("/api/health")
    assert resp.status_code == 200
    assert resp.json == {"status": "ok"}
