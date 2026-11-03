# ruff: noqa: F811  (pytest fixtures imported from test_flow)
"""The API key (app/security.py) and the health checks."""
import pytest

from app import security
from app.config import settings
from tests.test_flow import client, h  # noqa: F401 (fixtures)

KEY = "k-" + "x" * 30


@pytest.fixture
def locked(monkeypatch):
    monkeypatch.setattr(settings, "api_key", KEY)


def test_without_a_key_setting_the_api_is_open(client, users):
    assert client.get("/api/v1/users").status_code == 200


def test_with_a_key_every_api_call_needs_it(client, users, locked):
    assert client.get("/api/v1/users").status_code == 401
    assert client.get("/api/v1/users", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/api/v1/users", headers={"X-API-Key": KEY}).status_code == 200
    assert client.get("/api/v1/users", headers={"Authorization": f"Bearer {KEY}"}).status_code == 200
    r = client.post("/api/v1/invoices/1/approve", headers=h(users["priya"]))     # a user id is not enough
    assert r.status_code == 401 and "X-API-Key" in r.json()["detail"]
    assert client.get("/openapi.json").status_code == 401


def test_callers_with_their_own_proof_are_let_through(client, locked):
    """Twilio and Composio sign their requests; they never reach a 401 from the key check."""
    assert client.get("/healthz").status_code == 200
    assert client.post("/api/v1/webhook/whatsapp", data={}).status_code == 403          # Twilio signature check
    assert client.post("/webhooks/composio", content=b"{}").status_code != 401
    assert security.is_open("/api/v1/users/3/gmail/callback")
    assert not security.is_open("/api/v1/users/3/gmail/connect")
    assert not security.is_open("/api/v1/webhookx") and not security.is_open("/healthz/x")


def test_the_websocket_needs_the_key_too(client, locked):
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/api/v1/not-a-webhook"):
        pass


def test_health_checks(client, monkeypatch):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ok", "database": True}

    def broken(*a, **kw):
        raise ConnectionError("database down")
    from sqlalchemy.orm import Session
    monkeypatch.setattr(Session, "execute", broken)
    r = client.get("/readyz")
    assert r.status_code == 503 and r.json()["database"] is False
