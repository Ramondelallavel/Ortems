"""Database connectors: read the customer's database through the import pipeline.

SQLite always runs; set MONXU_TEST_SOURCE_PG (host:port:db:user:password) to also exercise PostgreSQL."""

import os
import sqlite3
import tempfile

import pytest
from conftest import Api


@pytest.fixture(scope="module")
def erp_file():
    path = os.path.join(tempfile.mkdtemp(prefix="erp-"), "erp.db")
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE articulos (codigo TEXT PRIMARY KEY, descripcion TEXT, tipo TEXT, stock_seguridad REAL);
        INSERT INTO articulos VALUES ('ERP-A1', 'Artículo ERP uno', 'RAW', 12.5), ('ERP-A2', 'Artículo ERP dos', 'RAW', 3);
        CREATE TABLE clientes (codigo TEXT, nombre TEXT, prioridad INTEGER, activo INTEGER);
        INSERT INTO clientes VALUES ('ERP-C1', 'Cliente ERP', 2, 1), ('ERP-C2', 'Cliente inactivo', 1, 0);
        CREATE VIEW clientes_activos AS SELECT codigo, nombre, prioridad FROM clientes WHERE activo = 1;
        """
    )
    con.commit()
    con.close()
    return path


def test_drivers_listed(planner):
    d = planner.ok(planner.get("/connectors/database/drivers"))
    assert d["supported"] is True
    by = {x["dialect"]: x for x in d["drivers"]}
    assert set(by) == {"postgresql", "mysql", "mssql", "oracle", "sqlite"}
    assert by["sqlite"]["installed"] and by["postgresql"]["installed"]


def test_sqlite_connector_end_to_end(client, sevilla, erp_file):
    admin = Api(client, "admin")
    cfg = {"dialect": "sqlite", "database": erp_file}
    t = admin.ok(admin.post("/connectors/database/test", json={"settings": cfg}))
    assert "articulos" in t["tables"] and "clientes_activos" in t["tables"]
    conn = admin.ok(
        admin.post(
            "/connectors/database",
            json={
                "code": "ERP-SQLITE",
                "name": "ERP (SQLite test)",
                "settings": {
                    **cfg,
                    "sources": [
                        {"entity": "items", "table": "articulos"},
                        {"entity": "table:customers", "query": "SELECT codigo AS code, nombre AS name, prioridad AS priority FROM clientes_activos"},
                    ],
                },
            },
        ),
        201,
    )
    assert conn["system"] == "DATABASE" and "secret_encrypted" not in conn
    src = conn["settings"]["sources"]
    p = admin.ok(admin.post(f"/connectors/database/{conn['id']}/preview", json={"source": src[0], "entity": "items"}))
    assert p["columns"] == ["codigo", "descripcion", "tipo", "stock_seguridad"]
    assert p["mapping"]["code"] == "codigo" and p["mapping"]["name"] == "descripcion" and p["mapping"]["safety_stock"] == "stock_seguridad"
    r = admin.ok(admin.post(f"/connectors/database/{conn['id']}/sync", json={"plant_id": sevilla["id"]}))
    assert r["status"] == "OK", r
    assert [x["status"] for x in r["results"]] == ["IMPORTED", "IMPORTED"]
    items = admin.ok(admin.get("/master-data/items", params={"q": "ERP-A"}))["items"]
    assert {i["code"] for i in items} == {"ERP-A1", "ERP-A2"}
    assert next(i for i in items if i["code"] == "ERP-A1")["safety_stock"] == 12.5
    custs = admin.ok(admin.get("/master-data/customers", params={"q": "ERP-C"}))["items"]
    assert [c["code"] for c in custs] == ["ERP-C1"]
    # second sync: same rows → updates only
    r2 = admin.ok(admin.post(f"/connectors/database/{conn['id']}/sync", json={"plant_id": sevilla["id"]}))
    assert all(x["stats"]["created"] == 0 for x in r2["results"])
    hist = admin.ok(admin.get("/imports"))
    assert any(j["source"] == "DATABASE" and j["filename"].startswith("ERP-SQLITE") for j in hist)


def test_only_select_queries(client, erp_file):
    admin = Api(client, "admin")
    for bad in ("DELETE FROM articulos", "SELECT 1; DROP TABLE articulos", "UPDATE articulos SET tipo='X'", "select * from articulos where 1=1 -- ok\n; delete from articulos"):
        r = admin.post("/connectors/database", json={"code": "BAD", "settings": {"dialect": "sqlite", "database": erp_file, "sources": [{"entity": "items", "query": bad}]}})
        assert r.status_code == 422, bad
    # codes are unique
    dup = admin.post("/connectors/database", json={"code": "erp-sqlite", "settings": {"dialect": "sqlite", "database": erp_file}})
    assert dup.status_code == 422 and "already" in dup.text
    # a string literal that contains a keyword is fine
    ok = admin.post("/connectors/database", json={"code": "OK-LIT", "settings": {"dialect": "sqlite", "database": erp_file, "sources": [{"entity": "items", "query": "SELECT codigo, 'delete me' AS note FROM articulos"}]}})
    assert ok.status_code == 201, ok.text


def test_planner_cannot_manage_connectors(planner, erp_file):
    r = planner.post("/connectors/database", json={"code": "X", "settings": {"dialect": "sqlite", "database": erp_file}})
    assert r.status_code == 403


def test_errors_left_for_review(client, sevilla, erp_file):
    admin = Api(client, "admin")
    conn = admin.ok(admin.post("/connectors/database", json={"code": "ERP-BAD", "settings": {"dialect": "sqlite", "database": erp_file, "sources": [{"entity": "production-orders", "query": "SELECT 'PO-ERP-1' AS number, 'NO-SUCH' AS item_code, 5 AS quantity, '2030-01-01' AS due_date"}]}}), 201)
    r = admin.ok(admin.post(f"/connectors/database/{conn['id']}/sync", json={"plant_id": sevilla["id"]}))
    assert r["status"] == "REVIEW" and r["results"][0]["status"] == "INVALID"
    job = admin.ok(admin.get(f"/imports/{r['results'][0]['job_id']}"))
    assert any("unknown item" in e["message"] for e in job["errors"])


@pytest.mark.skipif(not os.environ.get("MONXU_TEST_SOURCE_PG"), reason="set MONXU_TEST_SOURCE_PG=host:port:db:user:password")
def test_postgresql_source(client, sevilla):
    host, port, db, user, pwd = os.environ["MONXU_TEST_SOURCE_PG"].split(":")
    admin = Api(client, "admin")
    cfg = {"dialect": "postgresql", "host": host, "port": int(port), "database": db, "username": user}
    t = admin.ok(admin.post("/connectors/database/test", json={"settings": cfg, "password": pwd}))
    assert t["ok"] and t["server_version"]
    conn = admin.ok(admin.post("/connectors/database", json={"code": "ERP-PG", "settings": {**cfg, "sources": [{"entity": "table:suppliers", "query": "SELECT 'PG-S1' AS code, 'Supplier from PostgreSQL' AS name, 7 AS lead_time_days"}]}, "password": pwd}), 201)
    r = admin.ok(admin.post(f"/connectors/database/{conn['id']}/sync", json={"plant_id": sevilla["id"]}))
    assert r["status"] == "OK", r
    assert admin.ok(admin.get("/master-data/suppliers", params={"q": "PG-S1"}))["items"][0]["lead_time_days"] == 7
