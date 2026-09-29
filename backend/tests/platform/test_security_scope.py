"""Security boundaries: plant isolation inside a tenant (events, runs), delegation of rights (no
privilege escalation), OIDC account linking, outbound network policy, connector SQL guard."""

import uuid

import pytest
from conftest import PASSWORD, Api


def _session(api):
    from monxuplan.core.db import new_session

    return new_session(uuid.UUID(api.ok(api.get("/auth/me"))["tenant_id"]), "tests")


@pytest.fixture(scope="module")
def plants(client):
    admin = Api(client, "admin")
    ps = {p["code"]: p for p in admin.ok(admin.get("/plants"))}
    return ps["SEV"], ps["QRO"]


@pytest.fixture(scope="module")
def resources(client, plants):
    from sqlalchemy import select

    from monxuplan.models import Resource

    admin = Api(client, "admin")
    sev, qro = plants
    with _session(admin) as s:
        r_sev = s.scalar(select(Resource).where(Resource.plant_id == uuid.UUID(sev["id"]), Resource.kind == "MACHINE").order_by(Resource.code))
        r_qro = s.scalar(select(Resource).where(Resource.plant_id == uuid.UUID(qro["id"])).order_by(Resource.code))
        return (str(r_sev.id), r_sev.code), (str(r_qro.id), r_qro.code)


def _user(client, username, roles, plant_ids):
    admin = Api(client, "admin")
    r = admin.post("/users", json={"username": username, "email": f"{username}@example.com", "full_name": username, "password": PASSWORD, "roles": roles, "plant_ids": plant_ids})
    if r.status_code != 409:
        admin.ok(r, 201)
    return Api(client, username)


@pytest.fixture(scope="module")
def sev_supervisor(client, plants):
    return _user(client, "sev-supervisor", ["SUPERVISOR"], [plants[0]["id"]])


@pytest.fixture(scope="module")
def sev_admin(client, plants):
    return _user(client, "sev-admin", ["COMPANY_ADMIN"], [plants[0]["id"]])


# ------------------------------------------------------------------ plant isolation
def test_events_cannot_touch_another_plant(sev_supervisor, resources, plants):
    (sev_id, sev_code), (qro_id, qro_code) = resources
    ev = lambda t, p: sev_supervisor.post("/events", json={"type": t, "payload": p})  # noqa: E731
    assert ev("MachineDown", {"resource": qro_id, "reason": "test"}).status_code == 403
    assert ev("MaintenanceCreated", {"resource": qro_id, "start": "2030-01-01T00:00:00Z", "end": "2030-01-01T01:00:00Z"}).status_code == 403
    assert ev("MachineDown", {"resource": qro_code, "reason": "test"}).status_code in (403, 404)  # not even resolvable by code
    assert ev("InventoryChanged", {"item": "RM-S355-D40", "plant": plants[1]["code"], "on_hand": 1}).status_code in (403, 404)
    ok = ev("MachineAvailable", {"resource": sev_id})
    assert ok.status_code in (200, 201, 202), ok.text


def test_runs_of_another_plant_are_not_visible(sev_supervisor, plants):
    qro_live = plants[1]["live_scenario_id"]
    assert sev_supervisor.get("/planning/runs", params={"scenario_id": qro_live}).status_code == 403


# ------------------------------------------------------------------ delegation
def test_plant_admin_cannot_escalate(sev_admin, plants):
    sev, qro = plants
    body = {"email": "x@example.com", "full_name": "x", "password": PASSWORD}
    # all-plant scope, another plant, a higher role
    assert sev_admin.post("/users", json={**body, "username": "esc-1", "roles": ["PLANNER"], "plant_ids": []}).status_code == 403
    assert sev_admin.post("/users", json={**body, "username": "esc-2", "roles": ["PLANNER"], "plant_ids": [qro["id"]]}).status_code == 403
    assert sev_admin.post("/users", json={**body, "username": "esc-3", "roles": ["SUPER_ADMIN"], "plant_ids": [sev["id"]]}).status_code == 403
    # within its own rights: fine
    ok = sev_admin.post("/users", json={**body, "username": f"sev-planner-{uuid.uuid4().hex[:4]}", "roles": ["PLANNER"], "plant_ids": [sev["id"]]})
    assert ok.status_code == 201, ok.text
    # API keys are tenant-wide: not for a plant-scoped admin
    assert sev_admin.post("/api-keys", json={"name": "k", "role_code": "INTEGRATION_SERVICE"}).status_code == 403


def test_cannot_take_over_a_more_privileged_account(client, sev_admin):
    admin = Api(client, "admin")
    users = {u["username"]: u for u in admin.ok(admin.get("/users"))}
    target = users["admin"]
    assert sev_admin.post(f"/users/{target['id']}/password", json={"new_password": "Another-Pass-2026!"}).status_code == 403
    assert sev_admin.patch(f"/users/{target['id']}", json={"is_active": False}).status_code == 403


def test_api_key_role_cannot_exceed_creator(client):
    admin = Api(client, "admin")  # a company admin: no tenant:manage, not super admin
    assert admin.post("/api-keys", json={"name": "super", "role_code": "SUPER_ADMIN"}).status_code == 403
    assert admin.post("/api-keys", json={"name": "mes", "role_code": "INTEGRATION_SERVICE"}).status_code == 201


# ------------------------------------------------------------------ OIDC
def test_oidc_never_links_by_unverified_email(client, monkeypatch):
    from monxuplan.core import config
    from monxuplan.services import auth

    admin = Api(client, "admin")
    email = admin.ok(admin.get("/auth/me"))["email"]
    with _session(admin) as s:
        monkeypatch.setattr(auth, "verify_oidc", lambda t: {"sub": "attacker-sub", "email": email, "email_verified": False})
        assert auth.user_from_oidc(s, "tok") is None
        monkeypatch.setattr(auth, "verify_oidc", lambda t: {"sub": "attacker-sub", "email": email, "email_verified": True})
        assert auth.user_from_oidc(s, "tok") is None  # linking by e-mail is off by default
        import dataclasses

        linked = dataclasses.replace(config.get_settings(), oidc_link_by_email=True)
        monkeypatch.setattr(config, "get_settings", lambda: linked)
        u = auth.user_from_oidc(s, "tok")
        assert u is not None and u.external_subject == "attacker-sub"
        # once bound, another subject with the same e-mail gets nothing
        monkeypatch.setattr(auth, "verify_oidc", lambda t: {"sub": "someone-else", "email": email, "email_verified": True})
        assert auth.user_from_oidc(s, "tok") is None
        s.rollback()


# ------------------------------------------------------------------ outbound network policy
@pytest.mark.parametrize("url", ["http://127.0.0.1/hook", "http://169.254.169.254/latest/meta-data", "http://[::1]/x", "http://localhost:8080/x", "http://user:pw@example.com/x", "ftp://example.com/x"])
def test_webhook_destinations_are_checked(client, url):
    admin = Api(client, "admin")
    r = admin.post("/webhooks", json={"name": "w", "url": url, "events": ["plan.published"]})
    assert r.status_code == 422, (url, r.text)


def test_private_networks_follow_the_policy(monkeypatch):
    from monxuplan.core.errors import ValidationFailed
    from monxuplan.core.netpolicy import check_host

    monkeypatch.setenv("MONXU_EGRESS_PRIVATE", "deny")
    with pytest.raises(ValidationFailed):
        check_host("10.1.2.3", 5432, "t")
    monkeypatch.setenv("MONXU_EGRESS_ALLOW", "10.1.0.0/16")
    assert check_host("10.1.2.3", 5432, "t").address == "10.1.2.3"
    monkeypatch.setenv("MONXU_EGRESS_PRIVATE", "allow")
    monkeypatch.delenv("MONXU_EGRESS_ALLOW")
    assert check_host("192.168.1.10", 5432, "t").address == "192.168.1.10"
    with pytest.raises(ValidationFailed):  # loopback and metadata stay refused
        check_host("127.0.0.1", 5432, "t")
    with pytest.raises(ValidationFailed):
        check_host("169.254.169.254", 80, "t")


# ------------------------------------------------------------------ connector SQL guard
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * INTO copy_of_items FROM articulos",
        "SELECT codigo INTO OUTFILE '/tmp/x' FROM articulos",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT load_file('/etc/passwd')",
        "SELECT pg_sleep(100)",
        "SELECT * FROM openrowset('SQLNCLI', 'x', 'select 1')",
        "WITH d AS (DELETE FROM articulos RETURNING *) SELECT * FROM d",
        "SELECT utl_http.request('http://169.254.169.254') FROM dual",
    ],
)
def test_connector_rejects_writes_and_dangerous_functions(sql):
    from monxuplan.core.errors import ValidationFailed
    from monxuplan.services.dbconnect import _clean_sql

    with pytest.raises(ValidationFailed):
        _clean_sql(sql)


def test_connector_accepts_plain_reads():
    from monxuplan.services.dbconnect import _clean_sql

    for sql in ("SELECT codigo, 'insert into x' AS note FROM articulos", "WITH a AS (SELECT 1 AS n) SELECT n FROM a", "SELECT offset_days FROM t ORDER BY 1 OFFSET 5"):
        assert _clean_sql(sql)
