"""Self-service account: password change, preferences, revocation of sessions and tokens."""

import uuid

import pytest
from conftest import PASSWORD, Api


@pytest.fixture()
def account(client):
    admin = Api(client, "admin")
    name = f"acct-{uuid.uuid4().hex[:6]}"
    admin.ok(admin.post("/users", json={"username": name, "email": f"{name}@example.com", "full_name": name, "password": PASSWORD, "roles": ["VIEWER"], "plant_ids": []}), 201)
    return name, admin


def _bearer(client, username, password=PASSWORD):
    r = client.post("/api/v1/auth/token", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"authorization": f"Bearer {r.json()['access_token']}"}


def test_wrong_current_password_is_not_a_session_error(client, account):
    name, _ = account
    me = Api(client, name)
    r = me.post("/auth/password", json={"current_password": "not-my-password", "new_password": "Brand-New-Pass-2026"})
    # 422, not 401: a 401 would make the browser treat the user as signed out
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_CURRENT_PASSWORD", r.text
    assert me.get("/auth/me").status_code == 200


def test_password_change_ends_other_sessions_and_keeps_this_one(client, account):
    name, _ = account
    me = Api(client, name)
    other_browser = Api(client, name)
    token = _bearer(client, name)
    assert client.get("/api/v1/auth/me", headers=token).status_code == 200
    r = me.ok(me.post("/auth/password", json={"current_password": PASSWORD, "new_password": "Brand-New-Pass-2026"}))
    assert r["ok"] and r["csrf_token"]
    me.h = {"x-csrf-token": r["csrf_token"]}
    assert me.get("/auth/me").status_code == 200  # this browser got a fresh session
    assert other_browser.get("/auth/me").status_code == 401
    assert client.get("/api/v1/auth/me", headers=token).status_code == 401
    assert client.post("/api/v1/auth/token", json={"username": name, "password": PASSWORD}).status_code == 401
    fresh = _bearer(client, name, "Brand-New-Pass-2026")
    assert client.get("/api/v1/auth/me", headers=fresh).status_code == 200


def test_sign_out_everywhere_and_deactivation_revoke_tokens(client, account):
    name, admin = account
    me = Api(client, name)
    token = _bearer(client, name)
    me.ok(me.post("/auth/logout-everywhere"))
    assert client.get("/api/v1/auth/me", headers=token).status_code == 401
    assert me.get("/auth/me").status_code == 401
    token = _bearer(client, name)
    uid = next(u["id"] for u in admin.ok(admin.get("/users")) if u["username"] == name)
    admin.ok(admin.patch(f"/users/{uid}", json={"is_active": False}))
    assert client.get("/api/v1/auth/me", headers=token).status_code == 401
    log = admin.ok(admin.get("/audit", params={"entity_id": uid}))
    assert "LOGOUT_EVERYWHERE" in {a["action"] for a in log["items"]}


def test_own_preferences(client, account):
    name, _ = account
    me = Api(client, name)
    out = me.ok(me.patch("/auth/me", json={"locale": "es"}))
    assert out["locale"] == "es"
    assert me.ok(me.get("/auth/me"))["locale"] == "es"
    assert me.patch("/auth/me", json={"locale": "xx"}).status_code == 422
    plant = out["plants"][0]["id"]
    assert me.ok(me.patch("/auth/me", json={"default_plant_id": plant}))["default_plant_id"] == plant
