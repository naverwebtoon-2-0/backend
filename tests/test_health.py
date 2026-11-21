from unittest.mock import patch


def test_healthz(client):
    res = client.get("/healthz")
    assert res.status_code == 200
    assert res.json() == {"status": "ok", "revision": "local"}
    assert res.headers["cache-control"] == "no-store"
    assert "x-request-id" in res.headers


def test_readyz_ok(client):
    res = client.get("/readyz")
    assert res.status_code == 200


def test_readyz_db_down(client):
    with patch("app.main.get_engine", side_effect=RuntimeError("db down")):
        res = client.get("/readyz")
    assert res.status_code == 503


def test_unknown_route_uses_error_format(client):
    res = client.get("/v1/nope")
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "NOT_FOUND"


def test_request_id_is_echoed(client):
    res = client.get("/healthz", headers={"X-Request-ID": "abc-123"})
    assert res.headers["x-request-id"] == "abc-123"
