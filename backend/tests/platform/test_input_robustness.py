"""Inputs the schemas must stop (or the last-resort handlers must classify): none of them may produce a
500. Found by fuzzing the OpenAPI schema; each case below failed before."""

import uuid

import pytest


@pytest.mark.parametrize(
    "path",
    [
        "/orders?limit=-1",
        "/orders?offset=-5",
        "/orders?offset=9223372036854775808",
        "/events?limit=-3",
        "/imports?limit=-1",
        "/audit?offset=9223372036854775808",
        "/audit?limit=0",
    ],
)
def test_pagination_is_bounded(planner, sevilla, path):
    sep = "&" if "?" in path else "?"
    r = planner.get(path + (f"{sep}plant_id={sevilla['id']}" if path.startswith(("/orders", "/events")) else ""))
    assert r.status_code == 422, (path, r.status_code, r.text[:200])
    assert r.json()["error"]["code"] == "VALIDATION_FAILED"


def test_alert_and_trend_limits_are_bounded(planner, sevilla):
    for path in ("/alerts", "/analytics/trends"):
        for bad in ("-131", "9223372036854775808", "0"):
            r = planner.get(path, params={"plant_id": sevilla["id"], "limit": bad})
            assert r.status_code == 422, (path, bad, r.status_code)


def test_nul_and_lone_surrogates_are_a_client_error(planner, sevilla):
    """PostgreSQL refuses NUL in text (422 VALUE_NOT_ALLOWED); SQLite stores it. Either way: no 500."""
    r = planner.get("/views", params={"page": "a\x00b"})
    assert r.status_code in (200, 422), r.text
    r = planner.post("/views", json={"page": "orders", "name": "x\x00y", "filters": {}, "columns": [], "is_shared": False})
    assert r.status_code in (201, 422), r.text
    if r.status_code == 422:
        assert r.json()["error"]["code"] == "VALUE_NOT_ALLOWED"
    else:
        planner.ok(planner.delete(f"/views/{r.json()['id']}"))
    # a lone surrogate cannot be encoded as UTF-8
    body = b'{"page": "orders", "name": "\\ud800", "filters": {}, "columns": [], "is_shared": false}'
    r = planner.post("/views", content=body, headers={"content-type": "application/json"})
    assert r.status_code in (400, 422), (r.status_code, r.text[:200])


def test_duplicate_scenario_name_is_a_conflict_not_a_server_error(planner, sevilla):
    name = f"dup-{uuid.uuid4().hex[:6]}"
    planner.ok(planner.post("/scenarios", json={"plant_id": sevilla["id"], "name": name, "visibility": "SHARED"}), 201)
    r = planner.post("/scenarios", json={"plant_id": sevilla["id"], "name": name, "visibility": "SHARED"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "DUPLICATE_SCENARIO", r.text


def test_simultaneous_scenarios_with_one_name_never_give_a_server_error(client, sevilla):
    """Both requests pass the name check; the database's unique constraint decides: 201 and 409."""
    import threading

    from conftest import Api

    name = f"race-{uuid.uuid4().hex[:6]}"
    apis = [Api(client, "planner") for _ in range(4)]
    codes: list[int] = []

    def go(a):
        codes.append(a.post("/scenarios", json={"plant_id": sevilla["id"], "name": name, "visibility": "SHARED"}).status_code)

    threads = [threading.Thread(target=go, args=(a,)) for a in apis]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(set(codes)) in ([201, 409], [201]), codes
    assert codes.count(201) == 1, codes


def test_firming_planned_orders_validates_its_input(planner, sevilla):
    for bad in ([{}], [{"item_id": "not-a-uuid", "quantity": 5, "due_date": "2026-10-10"}], [{"item_id": str(uuid.uuid4()), "quantity": -1, "due_date": "2026-10-10"}], [{"item_id": str(uuid.uuid4()), "quantity": 1, "due_date": "yesterday"}]):
        r = planner.post("/planning/mrp/firm", json={"plant_id": sevilla["id"], "planned_orders": bad})
        assert r.status_code == 422, (bad, r.status_code, r.text[:200])
    r = planner.post("/planning/mrp/firm", json={"plant_id": str(uuid.uuid4()), "planned_orders": []})
    assert r.status_code in (403, 404), r.text


def test_webhook_url_with_impossible_host_name_is_rejected(planner):
    for url in ("https://\U000fbade÷.example/", "https://\x87¦!.example/"):
        r = planner.post("/webhooks", json={"name": "bad", "url": url, "events": ["plan.published"]})
        assert 400 <= r.status_code < 500, (url, r.status_code, r.text[:200])


def test_operator_view_rejects_a_malformed_resource_id(planner, sevilla):
    r = planner.get("/operator", params={"plant_id": sevilla["id"], "resource_id": "not-a-uuid"})
    assert r.status_code == 422, r.text


def test_scenario_change_that_refers_to_nothing_is_reported_not_a_server_error(planner, sevilla):
    sc = planner.ok(planner.post("/scenarios", json={"plant_id": sevilla["id"], "name": f"badchg-{uuid.uuid4().hex[:6]}", "visibility": "SHARED"}), 201)
    planner.ok(planner.post(f"/scenarios/{sc['id']}/changes", json={"type": "CHANGE_CAPACITY", "payload": {"resource_id": "NO-SUCH-MACHINE", "capacity": 2}}), 201)
    r = planner.post("/planning/run", json={"scenario_id": sc["id"], "solver": {"provider": "heuristic", "time_limit_s": 5}})
    # either refused when queued, or failed with the reason: never a 500
    assert r.status_code in (202, 409, 422), r.text
    if r.status_code == 202:
        run = planner.wait_run(r.json()["run_id"])
        assert run["status"] == "FAILED" and "cannot be applied" in (run.get("error_message") or ""), run
    else:
        assert r.json()["error"]["code"] in ("INVALID_SCENARIO_CHANGE", "DATA_QUALITY_BLOCK"), r.text


def test_a_failing_commit_is_an_error_for_the_client(planner, monkeypatch):
    """The session is committed before the response is sent: when the commit fails the caller must not
    be told that it worked (the change is rolled back)."""
    from sqlalchemy.exc import IntegrityError

    from monxuplan.api import deps

    real = deps.new_session

    def failing(*a, **k):
        s = real(*a, **k)
        if len(a) > 1 and a[1] != "auth":

            def boom():
                raise IntegrityError("COMMIT", {}, Exception("simulated commit failure"))

            s.commit = boom  # type: ignore[method-assign]
        return s

    name = f"commit-fail-{uuid.uuid4().hex[:6]}"
    monkeypatch.setattr(deps, "new_session", failing)
    r = planner.post("/views", json={"page": "orders", "name": name, "filters": {}, "columns": [], "is_shared": False})
    monkeypatch.setattr(deps, "new_session", real)
    assert r.status_code == 409 and r.json()["error"]["code"] == "INTEGRITY_ERROR", (r.status_code, r.text)
    assert name not in [v["name"] for v in planner.ok(planner.get("/views", params={"page": "orders"}))]
