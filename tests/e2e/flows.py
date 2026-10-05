"""Functional end-to-end flows (Playwright): every state-changing button of the application, driven
through the real UI against a running stack, each step checking its visible outcome.

    MONXU_WEB_URL=http://127.0.0.1:3000 python tests/e2e/flows.py [--only planning,orders] [--shots DIR]

Requires the demo tenant (python -m monxuplan.seed.demo) and a running worker. The flows change data
(they create orders, scenarios, users, webhooks… with unique names) — run them on a test database.
Every step reports PASS/FAIL; console errors, page errors and unexpected API failures are reported too.
Exit code 1 when anything failed.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import traceback
import uuid
from datetime import datetime, timedelta

from playwright.sync_api import expect, sync_playwright

BASE = os.environ.get("MONXU_WEB_URL", "http://127.0.0.1:3000")
PASSWORD = os.environ.get("MONXU_DEMO_PASSWORD", "Monxu-Demo-2026")
RUN = uuid.uuid4().hex[:6]


class Report:
    def __init__(self, shots: str | None):
        self.results: list[tuple[str, str, str]] = []
        self.noise: list[str] = []
        self.shots = shots
        self.allowed_http: list[re.Pattern] = []  # API failures a step provokes on purpose

    def watch(self, page, who: str):
        page.on("console", lambda m: m.type == "error" and not _benign(m.text) and self.noise.append(f"[{who}] console: {m.text[:300]}"))
        page.on("pageerror", lambda e: self.noise.append(f"[{who}] pageerror: {str(e)[:300]}"))

        def resp(r):
            if "/api/" in r.url and r.status >= 400 and not any(p.search(f"{r.status} {r.url}") for p in self.allowed_http):
                try:
                    body = r.text()[:200]
                except Exception:  # noqa: BLE001 - the body of an aborted request is gone
                    body = ""
                self.noise.append(f"[{who}] HTTP {r.status} {r.request.method} {r.url[len(BASE):][:160]} {body}")

        page.on("response", resp)

    def step(self, name: str, fn, page=None):
        t0 = time.time()
        try:
            fn()
            self.results.append(("PASS", name, f"{time.time() - t0:.1f}s"))
            print(f"PASS  {name} ({time.time() - t0:.1f}s)", flush=True)
            return True
        except Exception as e:  # noqa: BLE001 - every failure is reported, the next flows still run
            msg = f"{type(e).__name__}: {str(e).splitlines()[0][:400] if str(e) else ''}"
            self.results.append(("FAIL", name, msg))
            print(f"FAIL  {name}: {msg}", flush=True)
            traceback.print_exc(limit=3)
            if page is not None and self.shots:
                try:
                    page.screenshot(path=f"{self.shots}/FAIL-{re.sub(r'[^a-z0-9]+', '-', name.lower())}.png")
                except Exception:  # noqa: BLE001
                    pass
            return False

    def allow(self, pattern: str):
        self.allowed_http.append(re.compile(pattern))


def _benign(text: str) -> bool:
    # a refused request is already reported (or allowed) through its HTTP status
    return "Failed to load resource" in text


def toast(page, pattern: str, timeout=30000):
    """Waits for a toast (status or alert) whose text matches."""
    loc = page.locator("[aria-live=polite] > div").filter(has_text=re.compile(pattern, re.I))
    expect(loc.first).to_be_visible(timeout=timeout)
    return loc.first.inner_text()


def no_error_toast(page):
    bad = page.locator("[aria-live=polite] > [role=alert]")
    if bad.count():
        raise AssertionError("error toast: " + bad.first.inner_text())


def login(ctx, user, password=PASSWORD, rep: Report | None = None):
    page = ctx.new_page()
    page.set_default_timeout(15000)
    if rep:
        rep.watch(page, user)
    page.goto(BASE + "/login")
    page.fill("#u", user)
    page.fill("#p", password)
    page.click("button[type=submit]")
    page.wait_for_url("**/dashboard", timeout=30000)
    return page


def api(page, method: str, path: str, body=None):
    """The page's own session (cookie + CSRF) for set-up and checks."""
    csrf = next((c["value"] for c in page.context.cookies() if c["name"] == "mx_csrf"), "")
    r = page.request.fetch(BASE + "/api/v1" + path, method=method, data=json.dumps(body) if body is not None else None, headers={"content-type": "application/json", "x-csrf-token": csrf})
    if not r.ok:
        raise AssertionError(f"{method} {path} → {r.status}: {r.text()[:300]}")
    return r.json()


def drawer(page):
    """The open side drawer (the navigation sidebar is an <aside> too)."""
    return page.locator("aside[role=complementary]")


def wait_canvas(page):
    expect(page.locator("canvas[aria-roledescription='Gantt chart']")).to_be_visible(timeout=60000)


def plant(page):
    me = api(page, "GET", "/auth/me")
    return next(p for p in me["plants"] if p["code"] == "SEV")


def head_plan(page):
    p = plant(page)
    scs = api(page, "GET", f"/scenarios?plant_id={p['id']}")
    live = next(s for s in scs if s["is_live"])
    return live, live["head_plan_id"]


# ============================================================================================ flows
def flow_auth(b, rep: Report):
    ctx = b.new_context(viewport={"width": 1400, "height": 900})
    page = ctx.new_page()
    page.set_default_timeout(15000)
    rep.watch(page, "anon")
    rep.allow(r"^401 .*/api/v1/(auth/me|auth/login)")

    def redirect_and_bad_password():
        page.goto(BASE + "/planning/orders")
        page.wait_for_url(re.compile(r"/login\?next=%2Fplanning%2Forders"), timeout=20000)
        page.fill("#u", "planner")
        page.fill("#p", "wrong-password")
        page.click("button[type=submit]")
        expect(page.locator("#login-error")).to_contain_text("Invalid username or password")

    def login_goes_to_next():
        page.fill("#p", PASSWORD)
        page.click("button[type=submit]")
        page.wait_for_url("**/planning/orders", timeout=20000)
        expect(page.get_by_role("heading", name="Orders")).to_be_visible()

    def logout():
        page.get_by_label("Sign out").click()
        page.wait_for_url("**/login", timeout=15000)
        page.goto(BASE + "/dashboard")
        page.wait_for_url(re.compile(r"/login"), timeout=15000)

    rep.step("auth: protected page → login with next, wrong password shown", redirect_and_bad_password, page)
    rep.step("auth: login returns to the requested page", login_goes_to_next, page)
    rep.step("auth: sign out ends the session", logout, page)
    ctx.close()


def flow_planning(b, rep: Report):
    ctx = b.new_context(viewport={"width": 1600, "height": 950}, accept_downloads=True)
    page = login(ctx, "planner", rep=rep)
    state: dict = {}

    def optimize():
        page.goto(BASE + "/planning")
        wait_canvas(page)
        page.get_by_role("button", name="Optimize").click()
        dlg = page.get_by_role("dialog")
        dlg.get_by_label("Custom time limit in seconds").fill("5")
        dlg.get_by_role("button", name="Start").click()
        expect(page.get_by_text("Planning run")).to_be_visible()
        toast(page, r"Plan PLAN-.* ready", timeout=180000)
        wait_canvas(page)
        expect(page.get_by_text(re.compile("Solver"))).to_be_visible()

    def validate():
        page.get_by_role("button", name="Validate").click()
        toast(page, r"Validation: (feasible|not feasible)")

    def find_and_panel():
        live, pid = head_plan(page)
        g = api(page, "GET", f"/plans/{pid}/gantt")
        ops = [o for o in g["operations"] if not o["fixed"] and o["zone"] != "FROZEN"]
        op = ops[len(ops) // 3]
        state["op"] = op
        box = page.get_by_label("Find order or operation")
        box.fill(op["id"])
        box.press("Enter")
        panel = page.locator("span.code.font-semibold", has_text=op["id"])
        expect(panel).to_be_visible()
        expect(page.get_by_role("tab", name="Why here?")).to_be_visible()
        for tab in ("Constraint explorer", "Order chain", "Pegging", "Why here?"):
            page.get_by_role("tab", name=tab).click()
            page.wait_for_timeout(400)
            no_error_toast(page)

    def lock_unlock():
        op = state["op"]
        page.get_by_role("button", name="Lock", exact=True).click()
        toast(page, rf"{re.escape(op['id'])} locked")
        expect(page.get_by_role("button", name="Unlock", exact=True)).to_be_visible()
        page.get_by_role("button", name="Unlock", exact=True).click()
        toast(page, rf"{re.escape(op['id'])} unlocked")
        expect(page.get_by_role("button", name="Lock", exact=True)).to_be_visible()

    def drag_move_apply():
        page.keyboard.press("Escape")
        canvas = page.locator("canvas[aria-roledescription='Gantt chart']")
        box = canvas.bounding_box()
        # find a movable bar: click along the rows until the panel shows a non-fixed operation
        live, pid = head_plan(page)
        movable = {o["id"] for o in api(page, "GET", f"/plans/{pid}/gantt")["operations"] if not o["fixed"] and o["zone"] != "FROZEN"}
        hit = None
        for dy in range(70, int(box["height"]) - 20, 28):
            for dx in range(420, int(box["width"]) - 60, 45):
                canvas.click(position={"x": dx, "y": dy})
                head = page.locator("span.code.font-semibold")
                if head.count() and head.first.inner_text() in movable:
                    hit = (dx, dy, head.first.inner_text())
                    break
            if hit:
                break
        assert hit, "no movable bar found on the board"
        dx, dy, opid = hit
        page.keyboard.press("Escape")
        page.mouse.move(box["x"] + dx, box["y"] + dy)
        page.mouse.down()
        for k in range(1, 9):
            page.mouse.move(box["x"] + dx + 12 * k, box["y"] + dy, steps=2)
        page.mouse.up()
        dlg = page.get_by_role("dialog")
        expect(dlg).to_contain_text("Move preview", timeout=15000)
        expect(dlg.get_by_text(re.compile(r"✓ Feasible|▲ Would violate"))).to_be_visible(timeout=60000)
        dlg.get_by_label(re.compile(r"Reason")).fill("e2e: customer asked")
        dlg.get_by_role("button", name=re.compile(r"Apply")).click()
        toast(page, r"created \(manual edit\)", timeout=60000)
        state["moved"] = opid

    def undo_redo():
        page.get_by_role("button", name="Undo").click()
        toast(page, r"Back to PLAN-")
        page.get_by_role("button", name="Redo").click()
        toast(page, r"Forward to PLAN-")

    def reschedule():
        page.get_by_role("button", name="Reschedule").click()
        toast(page, r"operations affected", timeout=120000)

    def excel():
        with page.expect_download(timeout=60000) as d:
            page.get_by_role("button", name="Excel", exact=True).click()
        dl = d.value
        assert dl.suggested_filename.endswith(".xlsx"), dl.suggested_filename
        path = dl.path()
        assert path and os.path.getsize(path) > 2000, "empty workbook"

    def zoom_colour_now():
        for z in ("15 min", "Hour", "Shift", "Week", "Month", "Day"):
            page.get_by_role("group", name="Zoom").get_by_role("button", name=z).click()
            page.wait_for_timeout(200)
        for c in ("customer", "status", "family"):
            page.get_by_label("Colour by").select_option(c)
        page.get_by_role("button", name="Now", exact=True).click()
        page.get_by_label("Hide exceptions").click()
        page.get_by_role("button", name=re.compile(r"▲ Exceptions")).click()
        no_error_toast(page)

    def publish():
        page.get_by_role("button", name="Publish").click()
        dlg = page.get_by_role("dialog")
        expect(dlg).to_contain_text("Publish PLAN-")
        dlg.get_by_role("button", name="Confirm").click()
        page.wait_for_timeout(1500)
        over = page.get_by_role("dialog").filter(has_text="Override the publication checks")
        if over.count():
            # the gate listed its blockers: override explicitly, with a reason
            expect(over.get_by_role("button", name="Confirm")).to_be_disabled()
            over.get_by_label("Reason (recorded in the audit log)").fill("e2e: approved by the plant manager")
            over.get_by_role("button", name="Confirm").click()
            toast(page, r"published with \d+ override", timeout=60000)
        else:
            toast(page, r"PLAN-.* published", timeout=60000)
        expect(page.get_by_text(re.compile(r"^\W*Published$")).first).to_be_visible(timeout=15000)

    def compare_link():
        page.get_by_role("button", name="Compare").click()
        page.wait_for_url(re.compile(r"/planning/scenarios\?compare="), timeout=15000)

    rep.allow(r"^422 .*/api/v1/plans/[^/]+/publish$")  # PUBLISH_BLOCKED is answered with the override dialog
    rep.step("planning: optimise from the board", optimize, page)
    rep.step("planning: validate", validate, page)
    rep.step("planning: find an operation, panel tabs", find_and_panel, page)
    rep.step("planning: lock / unlock from the panel", lock_unlock, page)
    rep.step("planning: drag a bar → preview → apply", drag_move_apply, page)
    rep.step("planning: undo / redo", undo_redo, page)
    rep.step("planning: reschedule", reschedule, page)
    rep.step("planning: Excel export downloads a workbook", excel, page)
    rep.step("planning: zoom, colour, now, exceptions panel", zoom_colour_now, page)
    rep.step("planning: publish (gate, explicit override with reason)", publish, page)
    rep.step("planning: compare opens the scenario comparison", compare_link, page)
    ctx.close()


def flow_orders(b, rep: Report):
    ctx = b.new_context(viewport={"width": 1500, "height": 900})
    page = login(ctx, "planner", rep=rep)
    number = f"E2E-{RUN}"

    def search_and_open():
        page.goto(BASE + "/planning/orders")
        rows = page.locator("[aria-live=polite]", has_text=re.compile(r"[\d,.]+ rows"))
        expect(rows.first).to_be_visible(timeout=20000)
        page.get_by_label("Search rows").fill("WO-1001")
        expect(page.get_by_text(re.compile(r"^\d{1,2} rows$"))).to_be_visible(timeout=15000)
        page.get_by_role("row").filter(has_text="WO-10010").first.click()
        expect(drawer(page)).to_contain_text("WO-10010")
        expect(drawer(page)).to_contain_text("Operations", timeout=20000)
        drawer(page).get_by_label("Close").click()

    def new_order():
        page.get_by_role("button", name="New order").click()
        dlg = page.get_by_role("dialog")
        dlg.get_by_label("Number").fill(number)
        prod = dlg.get_by_label("Product")
        expect(prod.locator("option")).not_to_have_count(1, timeout=15000)
        prod.select_option(index=1)
        due = (datetime.now() + timedelta(days=12)).strftime("%Y-%m-%dT%H:%M")
        dlg.get_by_label("Due (plant time)").fill(due)
        dlg.get_by_role("button", name="Create").click()
        toast(page, rf"{number} created with \d+ operations")
        page.get_by_label("Search rows").fill(number)
        expect(page.get_by_role("row").filter(has_text=number)).to_have_count(1, timeout=15000)

    def filters():
        page.get_by_label("Search rows").fill("")
        page.get_by_label("Plan status").select_option("LATE")
        page.wait_for_timeout(800)
        page.get_by_label("Scope").select_option("all")
        page.wait_for_timeout(800)
        page.get_by_label("Plan status").select_option("")
        no_error_toast(page)

    def deep_link():
        page.goto(BASE + "/planning/orders?q=WO-10042")
        expect(drawer(page)).to_contain_text("WO-10042", timeout=20000)

    rep.step("orders: search and open an order", search_and_open, page)
    rep.step("orders: create a production order", new_order, page)
    rep.step("orders: plan status and scope filters", filters, page)
    rep.step("orders: deep link opens the order", deep_link, page)
    ctx.close()


def flow_scenarios(b, rep: Report):
    ctx = b.new_context(viewport={"width": 1500, "height": 900})
    page = login(ctx, "planner", rep=rep)
    name = f"E2E scenario {RUN}"

    def create():
        page.goto(BASE + "/planning/scenarios")
        page.get_by_role("button", name="New scenario").click()
        dlg = page.get_by_role("dialog")
        dlg.get_by_label("Name").fill(name)
        dlg.get_by_label("Description").fill("created by the e2e flows")
        dlg.get_by_role("button", name="Create").click()
        toast(page, rf"Scenario “{re.escape(name)}” created")
        expect(page.get_by_role("button", name=re.compile(re.escape(name)))).to_be_visible()

    def plan_it():
        page.get_by_role("button", name=re.compile(re.escape(name))).click()
        page.get_by_role("button", name="Plan scenario").click()
        expect(page.get_by_text("Planning run")).to_be_visible()
        expect(page.locator("[role=status]").get_by_text("SUCCEEDED")).to_be_visible(timeout=240000)

    def what_if():
        page.get_by_label("What-if", exact=True).select_option("OVERTIME")
        dlg = page.get_by_role("dialog")
        expect(dlg).to_contain_text("What-if: Allow overtime")
        dlg.get_by_label("Scenario name (optional)").fill(f"E2E overtime {RUN}")
        dlg.get_by_role("button", name="Create and plan").click()
        toast(page, r"created; planning started")
        # the list shows the scenario's own plan once its run has finished
        expect(page.get_by_role("button", name=re.compile(rf"E2E overtime {RUN}.*PLAN-"))).to_be_visible(timeout=240000)

    def compare():
        page.reload()
        page.get_by_label(f"Compare {name}").check()
        page.get_by_label(f"Compare E2E overtime {RUN}").check()
        page.get_by_role("tab", name=re.compile("Compare")).click()
        expect(page.get_by_text("KPI comparison (first column is the reference)")).to_be_visible(timeout=60000)

    def archive():
        page.get_by_role("tab", name="Scenarios").click()
        for n in (f"E2E overtime {RUN}", name):
            page.get_by_role("button", name=re.compile(re.escape(n))).click()
            page.get_by_role("button", name="Archive").click()
            dlg = page.get_by_role("dialog")
            expect(dlg).to_contain_text(f"Archive “{n}”")
            dlg.get_by_role("button", name="Confirm").click()
            expect(page.get_by_role("button", name=re.compile(re.escape(n)))).to_have_count(0, timeout=15000)

    rep.step("scenarios: create a copy of the live plan", create, page)
    rep.step("scenarios: plan the scenario", plan_it, page)
    rep.step("scenarios: what-if (overtime) created and planned", what_if, page)
    rep.step("scenarios: compare two scenarios", compare, page)
    rep.step("scenarios: archive both", archive, page)
    ctx.close()


def flow_alerts(b, rep: Report):
    ctx = b.new_context(viewport={"width": 1500, "height": 900})
    page = login(ctx, "planner", rep=rep)

    def acknowledge():
        page.goto(BASE + "/planning/alerts")
        row = page.get_by_role("row").nth(1)
        expect(row).to_be_visible(timeout=20000)
        title = row.locator("td").nth(2).inner_text()
        row.click()
        dlg = page.get_by_role("dialog")
        dlg.get_by_label("Note").fill("e2e: seen")
        dlg.get_by_role("button", name="Acknowledge").click()
        expect(dlg).to_have_count(0)
        page.get_by_label("Status").select_option("ACKNOWLEDGED")
        expect(page.get_by_role("row").filter(has_text=title).first).to_be_visible(timeout=15000)

    def dashboard_alert_link():
        page.goto(BASE + "/dashboard")
        first = page.locator("#att-h").locator("xpath=../..").locator("li button").first
        expect(first).to_be_visible(timeout=20000)
        first.click()
        page.wait_for_url(re.compile(r"/planning"), timeout=15000)

    rep.step("alerts: acknowledge with a note", acknowledge, page)
    rep.step("dashboard: an alert opens its context", dashboard_alert_link, page)
    ctx.close()


def flow_shopfloor(b, rep: Report):
    ctx = b.new_context(viewport={"width": 1400, "height": 900}, accept_downloads=True)
    page = login(ctx, "admin", rep=rep)

    def operator():
        page.goto(BASE + "/shopfloor/operator")
        sel = page.get_by_label("Operator — Resource")
        expect(sel.locator("option")).not_to_have_count(1, timeout=15000)
        values = sel.locator("option").evaluate_all("os => os.map(o => o.value).filter(Boolean)")
        found = False
        for v in values:
            sel.select_option(v)
            page.wait_for_timeout(900)
            if page.get_by_role("region", name="Current job").count():
                found = True
                break
        assert found, "no machine with a current job"
        if page.get_by_role("button", name="▶ Start").count():
            page.get_by_role("button", name="▶ Start").click()
            toast(page, r"start reported")
        page.get_by_label("Good quantity").fill("1,5")
        page.get_by_role("button", name="Report quantity").click()
        toast(page, r"quantity reported")
        page.get_by_label("Scrap").fill("-2")
        page.get_by_role("button", name="Report quantity").click()
        toast(page, r"positive numbers")
        page.get_by_label("Scrap").fill("")
        page.get_by_role("button", name="❚❚ Pause").click()
        toast(page, r"pause reported")

    def dispatch():
        page.goto(BASE + "/shopfloor/dispatch")
        expect(page.get_by_role("grid")).to_be_visible(timeout=20000)
        page.get_by_label("Window").select_option("48")
        page.wait_for_timeout(800)
        with page.expect_download() as d:
            page.get_by_role("button", name="CSV").click()
        assert d.value.suggested_filename == "dispatch-list.csv"
        page.emulate_media(media="print")
        assert page.locator("header.h-11").is_hidden(), "the app header is printed"
        expect(page.get_by_text(re.compile(r"Dispatch list · PLAN-"))).to_be_visible()
        printed = page.locator("div.print\\:block tbody tr").count()
        assert printed > 0, "printed list is empty"
        page.emulate_media(media="screen")

    def supervisor():
        page.goto(BASE + "/shopfloor/supervisor")
        expect(page.get_by_text(re.compile(r"PLAN-.* resources"))).to_be_visible(timeout=20000)
        page.get_by_label("Area").select_option(index=1)
        page.wait_for_timeout(800)
        no_error_toast(page)

    rep.allow(r"^422 .*/api/v1/execution/report$")
    rep.step("operator: start, report quantity (decimal comma), reject negative, pause", operator, page)
    rep.step("dispatch: window, CSV, print layout with every row", dispatch, page)
    rep.step("supervisor: area filter", supervisor, page)
    ctx.close()


def flow_masterdata(b, rep: Report):
    ctx = b.new_context(viewport={"width": 1500, "height": 900})
    page = login(ctx, "admin", rep=rep)
    code = f"E2E-C-{RUN}"

    def create_edit_delete():
        page.goto(BASE + "/master-data/customers")
        expect(page.get_by_role("grid")).to_be_visible(timeout=20000)
        page.get_by_role("button", name="New", exact=True).click()
        dr = drawer(page)
        dr.get_by_label("Code *").fill(code)
        dr.get_by_label("Name *").fill("E2E customer")
        dr.get_by_role("button", name="Save").click()
        toast(page, r"^.?\s*Saved")
        page.get_by_label("Search", exact=False).first.fill(code)
        row = page.get_by_role("row").filter(has_text=code)
        expect(row).to_have_count(1, timeout=15000)
        row.click()
        dr = drawer(page)
        dr.get_by_label("Name *").fill("E2E customer renamed")
        # closing with unsaved changes asks first
        page.keyboard.press("Escape")
        conf = page.get_by_role("dialog").filter(has_text="Discard unsaved changes?")
        expect(conf).to_be_visible()
        conf.get_by_role("button", name="Cancel").click()
        dr.get_by_role("button", name="Save").click()
        toast(page, r"Saved")
        expect(page.get_by_role("row").filter(has_text="E2E customer renamed")).to_have_count(1, timeout=15000)
        page.get_by_role("row").filter(has_text=code).click()
        dr = drawer(page)
        dr.get_by_role("button", name="Delete").click()
        conf = page.get_by_role("dialog")
        conf.get_by_label("Reason (recorded in the audit log)").fill("e2e cleanup")
        conf.get_by_role("button", name="Confirm").click()
        toast(page, r"Deleted|deactivated")
        expect(page.get_by_role("row").filter(has_text=code)).to_have_count(0, timeout=15000)

    def grid_edit():
        page.goto(BASE + "/master-data/customers")
        expect(page.get_by_role("grid")).to_be_visible(timeout=20000)
        page.get_by_role("button", name="Edit as table").click()
        page.get_by_role("button", name="Add row").click()
        new_row = page.locator("tr.bg-green-50")
        inputs = new_row.locator("input[type=text], input:not([type])")
        inputs.nth(0).fill(f"{code}-G")
        inputs.nth(1).fill("E2E grid customer")
        # switching view with pending changes asks first
        page.get_by_role("button", name="List").click()
        conf = page.get_by_role("dialog").filter(has_text="Discard unsaved changes?")
        expect(conf).to_be_visible()
        conf.get_by_role("button", name="Cancel").click()
        page.get_by_role("button", name="Save", exact=True).click()
        toast(page, r"saved")
        page.get_by_role("button", name="List").click()
        page.get_by_label("Search").first.fill(f"{code}-G")
        expect(page.get_by_role("row").filter(has_text=f"{code}-G")).to_have_count(1, timeout=15000)

    def excel_and_bom():
        page.goto(BASE + "/master-data/products")
        expect(page.get_by_role("grid")).to_be_visible(timeout=20000)
        with page.expect_download() as d:
            page.get_by_role("button", name="Excel").click()
        assert d.value.suggested_filename.endswith(".xlsx")
        page.get_by_role("row").nth(1).click()
        dr = drawer(page)
        dr.get_by_role("tab", name=re.compile("BOM")).last.click()
        expect(dr.get_by_text(re.compile(r"MAKE|BUY")).first).to_be_visible(timeout=15000)
        page.keyboard.press("Escape")

    rep.step("master data: create, edit (discard guard), delete with reason", create_edit_delete, page)
    rep.step("master data: grid add row (view-switch guard) and save", grid_edit, page)
    rep.step("master data: Excel export and BOM tree", excel_and_bom, page)
    ctx.close()


def flow_import(b, rep: Report):
    ctx = b.new_context(viewport={"width": 1500, "height": 950}, accept_downloads=True)
    page = login(ctx, "admin", rep=rep)

    def round_trip():
        page.goto(BASE + "/integrations")
        tmp = tempfile.mkdtemp(prefix="mx-e2e-")
        path = os.path.join(tmp, "customers.csv")
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["code", "name", "country"])
            w.writerow([f"E2E-IMP-{RUN}", "Imported customer", "ES"])
            w.writerow([f"E2E-IMP2-{RUN}", "Imported customer 2", "PT"])
        sel = page.get_by_role("combobox").filter(has=page.locator("option", has_text="Customers")).first
        value = sel.locator("option", has_text="Customers").first.get_attribute("value")
        sel.select_option(value)
        page.locator("input[type=file]").set_input_files(path)
        page.get_by_role("button", name=re.compile(r"^Upload")).click()
        expect(page.get_by_text(re.compile(r"customers\.csv \(2 "))).to_be_visible(timeout=20000)
        page.get_by_role("button", name=re.compile(r"Validate")).click()
        expect(page.get_by_text(re.compile(r"2 valid"))).to_be_visible(timeout=30000)
        page.get_by_role("button", name="Preview", exact=True).click()
        page.get_by_role("button", name=re.compile(r"Import 2")).click()
        toast(page, r"created", timeout=30000)
        expect(page.get_by_text(re.compile(r"created\s*2", re.I))).to_be_visible()
        page.get_by_role("tab", name=re.compile("history", re.I)).click()
        expect(page.get_by_role("row").filter(has_text="customers.csv").first).to_be_visible(timeout=15000)

    def exports():
        page.get_by_role("tab", name=re.compile("Export")).click()
        with page.expect_download() as d:
            page.get_by_role("row").nth(0).get_by_role("button", name="CSV").click()
        assert d.value.suggested_filename.endswith(".csv")

    rep.step("integrations: CSV import upload → validate → preview → import → history", round_trip, page)
    rep.step("integrations: data export", exports, page)
    ctx.close()


def flow_admin(b, rep: Report):
    ctx = b.new_context(viewport={"width": 1500, "height": 950})
    page = login(ctx, "admin", rep=rep)
    user = f"e2e-{RUN}"
    state: dict = {}

    def users():
        page.goto(BASE + "/admin")
        page.get_by_role("button", name="New user").click()
        dlg = page.get_by_role("dialog")
        dlg.get_by_label("Username").fill(user)
        dlg.get_by_label("Full name").fill("E2E User")
        dlg.get_by_label("Email").fill(f"{user}@example.com")
        dlg.get_by_label("Initial password").fill("E2e-Initial-Pass-1")
        dlg.get_by_role("button", name="Save").click()
        toast(page, r"User saved")
        page.get_by_role("row").filter(has_text=user).click()
        dlg = page.get_by_role("dialog")
        dlg.get_by_label("Set a new password (optional)").fill("E2e-Second-Pass-2")
        dlg.get_by_role("button", name="Save").click()
        toast(page, r"User saved")
        state["password"] = "E2e-Second-Pass-2"

    def api_keys():
        page.get_by_role("tab", name="API keys").click()
        page.get_by_role("button", name="New key").click()
        dlg = page.get_by_role("dialog")
        dlg.get_by_label("Name").fill(f"e2e key {RUN}")
        dlg.get_by_role("button", name="Create").click()
        created = page.get_by_role("dialog").filter(has_text="API key created")
        expect(created.locator("pre")).to_contain_text("mxk_")
        created.get_by_role("button", name="Done").click()
        row = page.get_by_role("row").filter(has_text=f"e2e key {RUN}")
        row.get_by_role("button", name="Revoke").click()
        page.get_by_role("dialog").get_by_role("button", name="Confirm").click()
        expect(row).to_contain_text("revoked", timeout=15000)

    def audit():
        page.get_by_role("tab", name="Audit log").click()
        sel = page.get_by_label("Action")
        expect(sel.locator("option", has_text="PASSWORD_CHANGE")).to_have_count(1, timeout=15000)
        sel.select_option("PASSWORD_CHANGE")
        expect(page.get_by_role("row").filter(has_text=user).first).to_be_visible(timeout=15000)
        page.get_by_label("Search audit").fill(user)
        page.wait_for_timeout(800)
        page.get_by_role("row").filter(has_text=user).first.click()
        expect(page.get_by_role("dialog")).to_contain_text("Before")
        page.keyboard.press("Escape")

    def settings():
        page.get_by_role("tab", name="Plant settings").click()
        box = page.get_by_label(re.compile(r"Require validation before publishing"))
        before = box.is_checked()
        box.set_checked(not before)
        page.get_by_role("button", name="Save", exact=True).click()
        toast(page, r"Settings saved")
        page.reload()
        page.get_by_role("tab", name="Plant settings").click()
        expect(page.get_by_label(re.compile(r"Require validation before publishing"))).to_be_checked(checked=not before)
        page.get_by_label(re.compile(r"Require validation before publishing")).set_checked(before)
        page.get_by_role("button", name="Save", exact=True).click()
        toast(page, r"Settings saved")

    def webhooks():
        page.goto(BASE + "/integrations?tab=webhooks")
        page.get_by_role("button", name="Add webhook").click()
        dlg = page.get_by_role("dialog")
        dlg.get_by_label("Name").fill(f"e2e hook {RUN}")
        dlg.get_by_label(re.compile(r"URL")).fill("http://10.255.255.1/monxu")
        dlg.get_by_label("plan.published").check()
        dlg.get_by_role("button", name="Create").click()
        created = page.get_by_role("dialog").filter(has_text="Webhook created")
        expect(created.locator("pre")).not_to_be_empty()
        created.get_by_role("button", name="Done").click()
        row = page.get_by_role("row").filter(has_text=f"e2e hook {RUN}")
        row.get_by_role("button", name="Pause").click()
        expect(row).to_contain_text("paused", timeout=15000)
        row.get_by_role("button", name="Resume").click()
        expect(row).to_contain_text("active", timeout=15000)
        row.get_by_role("button", name="Send test").click()
        toast(page, r"Test delivery queued")
        expect(page.get_by_text(re.compile(r"Recent deliveries"))).to_be_visible()
        expect(page.locator("td.code", has_text="webhook.test").first).to_be_visible(timeout=15000)
        row.get_by_role("button", name="Delete").click()
        page.get_by_role("dialog").get_by_role("button", name="Confirm").click()
        expect(page.get_by_role("row").filter(has_text=f"e2e hook {RUN}")).to_have_count(0, timeout=15000)

    def db_connector():
        db = os.path.join(tempfile.mkdtemp(prefix="mx-e2e-db-"), "erp.sqlite")
        con = sqlite3.connect(db)
        con.execute("create table erp_customers (code text, name text, country text)")
        con.executemany("insert into erp_customers values (?,?,?)", [(f"E2E-DB-{RUN}-{i}", f"DB customer {i}", "ES") for i in range(3)])
        con.commit()
        con.close()
        page.goto(BASE + "/integrations?tab=database")
        page.get_by_role("button", name="New database connector").click()
        dlg = page.get_by_role("dialog")
        dlg.get_by_label("Code").fill(f"E2EDB{RUN}")
        dlg.get_by_label("Name").fill("E2E SQLite")
        dlg.get_by_label("Database type").select_option("sqlite")
        dlg.get_by_label("SQLite file path on the server").fill(db)
        dlg.get_by_role("button", name="Test connection").click()
        expect(dlg.get_by_text(re.compile(r"Connected"))).to_be_visible(timeout=20000)
        dlg.get_by_role("button", name="Add source").click()
        feeds = dlg.get_by_label("Feeds the MonxuPlan table")
        feeds.select_option(feeds.locator("option", has_text="Customers").first.get_attribute("value"))
        dlg.locator("input[list^=tables-]").fill("erp_customers")
        dlg.get_by_role("button", name="Save").click()
        toast(page, r"Saved")
        row = page.get_by_role("row").filter(has_text=f"E2EDB{RUN}")
        row.get_by_role("button", name="Sync now").click()
        toast(page, r"Sync finished", timeout=60000)
        expect(page.get_by_text(re.compile(r"3 created"))).to_be_visible(timeout=15000)
        row.get_by_role("button", name="Delete").click()
        page.get_by_role("dialog").get_by_role("button", name="Confirm").click()
        expect(page.get_by_role("row").filter(has_text=f"E2EDB{RUN}")).to_have_count(0, timeout=15000)

    rep.allow(r"^4\d\d .*/api/v1/webhooks/[^/]+/deliveries")
    rep.step("admin: create user, reset password", users, page)
    rep.step("admin: API key create (shown once) and revoke", api_keys, page)
    rep.step("admin: audit log filter by real action + detail", audit, page)
    rep.step("admin: plant setting saved and restored", settings, page)
    rep.step("integrations: webhook add, pause/resume, test delivery, delete", webhooks, page)
    rep.step("integrations: SQLite database connector test, source, sync, delete", db_connector, page)
    ctx.close()

    def account():
        c2 = b.new_context(viewport={"width": 1300, "height": 900})
        p2 = login(c2, user, state["password"], rep)
        other = login(b.new_context(), user, state["password"], rep)
        p2.get_by_label("My account").click()
        dlg = p2.get_by_role("dialog")
        dlg.get_by_label("Current password").fill(state["password"])
        dlg.get_by_label("New password", exact=True).fill("E2e-Third-Pass-33")
        dlg.get_by_label("Repeat new password").fill("E2e-Third-Pass-3x")
        expect(dlg.get_by_role("button", name="Change password")).to_be_disabled()
        dlg.get_by_label("Repeat new password").fill("E2e-Third-Pass-33")
        dlg.get_by_role("button", name="Change password").click()
        toast(p2, r"Password changed")
        # this browser stays signed in, the other session ended
        p2.goto(BASE + "/planning/orders")
        expect(p2.get_by_role("heading", name="Orders")).to_be_visible(timeout=20000)
        other.goto(BASE + "/dashboard")
        other.wait_for_url(re.compile(r"/login"), timeout=20000)
        p2.get_by_label("My account").click()
        dlg = p2.get_by_role("dialog")
        dlg.get_by_label("Language").select_option("es")
        dlg.get_by_role("button", name="Save").click()
        toast(p2, r"Preferencias guardadas")
        p2.keyboard.press("Escape")
        expect(p2.get_by_role("link", name="Tablero de planificación")).to_be_visible()
        p2.get_by_label("Mi cuenta").click()
        p2.get_by_role("button", name="Cerrar sesión en todos los dispositivos").click()
        p2.get_by_role("dialog").filter(has_text="¿Cerrar sesión en todos los dispositivos?").get_by_role("button", name="Confirm").click()
        p2.wait_for_url(re.compile(r"/login"), timeout=20000)
        c2.close()

    rep.allow(r"^401 .*/api/v1/")
    if "password" in state:
        rep.step("account: change password (other sessions end), language, sign out everywhere", account)


def flow_mps(b, rep: Report):
    ctx = b.new_context(viewport={"width": 1500, "height": 900})
    page = login(ctx, "planner", rep=rep)

    def mps():
        page.goto(BASE + "/planning/mps")
        page.get_by_role("button", name="Calculate MPS / MRP").click()
        expect(page.get_by_text(re.compile(r"Items with requirements \(\d+\)"))).to_be_visible(timeout=120000)
        for tab in ("Exceptions", "Rough-cut capacity", "Planned orders"):
            page.get_by_role("tab", name=re.compile(tab)).click()
            page.wait_for_timeout(400)
        box = page.locator("input[aria-label^='Select ']").first
        if box.count():
            box.check()
            page.get_by_role("button", name=re.compile(r"Firm 1 selected")).click()
            toast(page, r"firm production orders created", timeout=60000)
        page.get_by_role("tab", name="Aggregate plan (MIP)").click()
        page.get_by_role("button", name="Solve aggregate plan").click()
        expect(page.get_by_text(re.compile(r"objective [\d.,]+"))).to_be_visible(timeout=120000)
        expect(page.get_by_text(re.compile(r"^\W*(OPTIMAL|FEASIBLE)$")).first).to_be_visible()

    rep.step("MPS/MRP: calculate, tabs, firm a planned order, aggregate plan", mps, page)
    ctx.close()


def flow_analytics_assistant(b, rep: Report):
    ctx = b.new_context(viewport={"width": 1500, "height": 900})
    page = login(ctx, "planner", rep=rep)

    def analytics():
        page.goto(BASE + "/analytics")
        expect(page.get_by_text(re.compile(r"Drill-down: OTIF"))).to_be_visible(timeout=30000)
        page.get_by_role("tab", name="Trends").click()
        page.wait_for_timeout(800)
        page.get_by_role("tab", name="Robustness").click()
        page.get_by_role("button", name="Run 30 replications").click()
        expect(page.get_by_text(re.compile(r"replications|OTIF", re.I)).first).to_be_visible(timeout=180000)
        page.get_by_role("button", name="Analyse").click()
        page.wait_for_timeout(500)
        expect(page.get_by_role("button", name="Analyse")).to_be_enabled(timeout=180000)
        no_error_toast(page)

    def assistant():
        page.goto(BASE + "/dashboard")
        page.keyboard.press("Control+j")
        dr = drawer(page)
        dr.get_by_text("Which orders are late?").click()
        expect(dr.get_by_text(re.compile(r"late", re.I)).nth(1)).to_be_visible(timeout=60000)
        dr.get_by_label(re.compile(r"Ask")).fill("What is the bottleneck?")
        dr.get_by_role("button", name="Send").click()
        page.wait_for_timeout(1500)
        no_error_toast(page)

    rep.step("analytics: KPIs, trends, Monte Carlo, sensitivity", analytics, page)
    rep.step("assistant: suggested and typed questions", assistant, page)
    ctx.close()


def flow_mobile(b, rep: Report):
    ctx = b.new_context(viewport={"width": 390, "height": 844})
    page = login(ctx, "operator", rep=rep)

    def header_fits():
        page.goto(BASE + "/shopfloor/operator")
        out = page.get_by_label("Sign out")
        expect(out).to_be_visible()
        bx = out.bounding_box()
        assert bx and bx["x"] + bx["width"] <= 390, f"sign-out button outside the screen: {bx}"
        page.get_by_label("Menu").click()
        expect(page.get_by_role("link", name="Operator")).to_be_visible()
        page.get_by_role("link", name="Dispatch list").click()
        page.wait_for_url("**/shopfloor/dispatch")

    rep.step("mobile: header fits 390 px, menu navigates", header_fits, page)
    ctx.close()


FLOWS = {
    "auth": flow_auth,
    "planning": flow_planning,
    "orders": flow_orders,
    "scenarios": flow_scenarios,
    "alerts": flow_alerts,
    "shopfloor": flow_shopfloor,
    "masterdata": flow_masterdata,
    "import": flow_import,
    "admin": flow_admin,
    "mps": flow_mps,
    "analytics": flow_analytics_assistant,
    "mobile": flow_mobile,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    ap.add_argument("--shots")
    a = ap.parse_args()
    rep = Report(a.shots)
    names = a.only.split(",") if a.only else list(FLOWS)
    t0 = time.time()
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH") or None)
        for n in names:
            print(f"--- {n}", flush=True)
            try:
                FLOWS[n](b, rep)
            except Exception as e:  # noqa: BLE001 - a broken flow must not hide the others
                rep.results.append(("FAIL", f"{n}: flow aborted", str(e)[:300]))
                traceback.print_exc(limit=3)
        b.close()
    failed = [r for r in rep.results if r[0] == "FAIL"]
    print("\n=== summary")
    for r in rep.results:
        print(f"{r[0]}  {r[1]}  {r[2] if r[0] == 'FAIL' else ''}")
    if rep.noise:
        print("\n=== console / page / API problems")
        for n in rep.noise:
            print(" ", n)
    print(f"\n{len(rep.results) - len(failed)} passed, {len(failed)} failed, {len(rep.noise)} console/API problems · {time.time() - t0:.0f}s")
    sys.exit(1 if failed or rep.noise else 0)


if __name__ == "__main__":
    main()
