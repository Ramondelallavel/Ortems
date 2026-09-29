"""Platform tests: a throw-away SQLite database (or MONXU_TEST_DATABASE_URL), the demo tenant and the
real FastAPI app with its in-process worker."""

import os
import tempfile
import time

import pytest

_tmp = tempfile.mkdtemp(prefix="monxu-test-")
os.environ["MONXU_DATABASE_URL"] = os.environ.get("MONXU_TEST_DATABASE_URL") or f"sqlite:///{_tmp}/test.db"
os.environ.setdefault("MONXU_LOG_JSON", "0")
os.environ.setdefault("MONXU_LOG_LEVEL", "WARNING")
os.environ["MONXU_RATE_LIMIT_PER_MINUTE"] = "100000"
os.environ["MONXU_LOGIN_RATE_LIMIT_PER_MINUTE"] = "1000"

PASSWORD = "Monxu-Demo-2026"


@pytest.fixture(scope="session")
def app():
    from monxuplan.core.db import create_all
    from monxuplan.seed.demo import seed_demo

    create_all()
    seed_demo(reset=True)
    from monxuplan.api.app import app as _app

    return _app


@pytest.fixture(scope="session")
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c


class Api:
    """Logged-in browser-like session (cookie + CSRF header)."""

    def __init__(self, client, username, password=PASSWORD, tenant=None):
        from fastapi.testclient import TestClient

        self.c = TestClient(client.app)
        self.c.__enter__()
        r = self.c.post("/api/v1/auth/login", json={"username": username, "password": password, "tenant": tenant})
        assert r.status_code == 200, r.text
        self.me = r.json()["user"]
        self.h = {"x-csrf-token": r.json()["csrf_token"]}

    def get(self, path, **kw):
        return self.c.get("/api/v1" + path, **kw)

    def post(self, path, **kw):
        return self.c.post("/api/v1" + path, headers={**self.h, **kw.pop("headers", {})}, **kw)

    def put(self, path, **kw):
        return self.c.put("/api/v1" + path, headers={**self.h, **kw.pop("headers", {})}, **kw)

    def patch(self, path, **kw):
        return self.c.patch("/api/v1" + path, headers={**self.h, **kw.pop("headers", {})}, **kw)

    def delete(self, path, **kw):
        return self.c.delete("/api/v1" + path, headers={**self.h, **kw.pop("headers", {})}, **kw)

    def ok(self, r, code=200):
        assert r.status_code == code, f"{r.request.method} {r.request.url} → {r.status_code}: {r.text[:2000]}"
        return r.json()

    def wait_run(self, run_id, timeout=240):
        t0 = time.time()
        while time.time() - t0 < timeout:
            r = self.ok(self.get(f"/planning/runs/{run_id}"))
            if r["status"] in ("SUCCEEDED", "FAILED", "CANCELLED", "STALE"):
                return r
            time.sleep(0.5)
        raise AssertionError("planning run did not finish")


@pytest.fixture(scope="session")
def planner(client):
    return Api(client, "planner")


@pytest.fixture(scope="session")
def sevilla(planner):
    plants = planner.ok(planner.get("/plants"))
    return next(p for p in plants if p["code"] == "SEV")


@pytest.fixture(scope="session")
def base_plan(planner, sevilla):
    """One optimised plan of the Sevilla live scenario (heuristic, short time limit)."""
    r = planner.ok(planner.post("/planning/run", json={"scenario_id": sevilla["live_scenario_id"], "solver": {"provider": "heuristic", "time_limit_s": 5}}), 202)
    run = planner.wait_run(r["run_id"])
    assert run["status"] == "SUCCEEDED", run
    return planner.ok(planner.get(f"/plans/{run['plan_id']}"))
