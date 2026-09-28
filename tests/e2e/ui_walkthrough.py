"""Browser walkthrough of MonxuPlan (Playwright): logs in, runs the planner from the UI, opens every
screen, takes screenshots and fails if any console error, page error or failed API call occurs.

    MONXU_WEB_URL=http://localhost:3000 python tests/e2e/ui_walkthrough.py
Requires a running stack with the demo tenant (python -m monxuplan.seed.demo).
"""
import json
import sys
import time
from playwright.sync_api import sync_playwright

import os
BASE = os.environ.get("MONXU_WEB_URL", "http://localhost:3000")
OUT = os.environ.get("MONXU_SCREENSHOTS", "docs/screenshots")
problems = []

def watch(page, label):
    page.on("console", lambda m: m.type == "error" and problems.append((label[0], "console", m.text[:300])))
    page.on("pageerror", lambda e: problems.append((label[0], "pageerror", str(e)[:300])))
    page.on("response", lambda r: r.status >= 400 and "/api/" in r.url and problems.append((label[0], r.status, r.url[:160])))

with sync_playwright() as p:
    b = p.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH") or None)
    ctx = b.new_context(viewport={"width": 1600, "height": 950}, locale="en-GB")
    page = ctx.new_page()
    label = ["login"]
    watch(page, label)
    page.goto(BASE + "/login")
    page.fill("#u", "planner"); page.fill("#p", "Monxu-Demo-2026")
    page.screenshot(path=f"{OUT}/01-login.png")
    page.click("button[type=submit]")
    page.wait_for_url("**/dashboard", timeout=20000)
    label[0] = "planning-run"
    page.goto(BASE + "/planning")
    page.wait_for_timeout(2500)
    page.get_by_role("button", name="Optimize").click()
    dlg = page.get_by_role("dialog")
    dlg.get_by_label("Custom time limit in seconds").fill("12")
    dlg.get_by_role("button", name="Start").click()
    page.wait_for_selector("text=Planning run", timeout=10000)
    page.wait_for_timeout(1500)
    page.screenshot(path=f"{OUT}/02-planning-running.png")
    t0 = time.time()
    while time.time() - t0 < 120:
        if page.locator("canvas[aria-roledescription='Gantt chart']").count():
            break
        page.wait_for_timeout(1000)
    page.wait_for_timeout(4000)
    page.screenshot(path=f"{OUT}/03-planning-board.png")
    # select an operation via keyboard and open panel
    canvas = page.locator("canvas[aria-roledescription='Gantt chart']")
    if canvas.count():
        box = canvas.bounding_box()
        # click near first rows to try hitting a bar
        for dy in range(70, 400, 28):
            for dx in range(260, 1100, 60):
                canvas.click(position={"x": dx, "y": dy})
                page.wait_for_timeout(60)
                if page.locator("text=Why here?").count():
                    break
            if page.locator("text=Why here?").count():
                break
        page.wait_for_timeout(1500)
        page.screenshot(path=f"{OUT}/04-why-here.png")
    for i, (path, name) in enumerate([
        ("/dashboard", "05-command-center"),
        ("/planning/orders", "06-orders"),
        ("/planning/capacity", "07-capacity"),
        ("/planning/materials", "08-materials"),
        ("/planning/mps", "09-mps"),
        ("/planning/scenarios", "10-scenarios"),
        ("/planning/alerts", "11-alerts"),
        ("/shopfloor/dispatch", "12-dispatch"),
        ("/shopfloor/supervisor", "13-supervisor"),
        ("/shopfloor/operator", "14-operator"),
        ("/analytics", "15-analytics"),
        ("/analytics/plan-vs-actual", "16-plan-vs-actual"),
        ("/master-data", "17-master-data"),
        ("/master-data/machines", "18-machines"),
        ("/integrations", "19-integrations"),
        ("/admin/data-quality", "20-data-quality"),
    ]):
        label[0] = path
        page.goto(BASE + path)
        page.wait_for_timeout(3500 if path in ("/planning/capacity", "/dashboard", "/analytics") else 2500)
        if path == "/planning/mps":
            page.get_by_role("button", name="Calculate MPS / MRP").click(); page.wait_for_timeout(4000)
        page.screenshot(path=f"{OUT}/{name}.png")
    # assistant
    label[0] = "assistant"
    page.goto(BASE + "/dashboard"); page.wait_for_timeout(2000)
    page.keyboard.press("Control+j"); page.wait_for_timeout(500)
    page.get_by_text("Which orders are late?").click(); page.wait_for_timeout(3000)
    page.screenshot(path=f"{OUT}/21-assistant.png")
    # admin as admin user
    ctx2 = b.new_context(viewport={"width": 1600, "height": 950})
    pg2 = ctx2.new_page(); label2 = ["admin"]; watch(pg2, label2)
    pg2.goto(BASE + "/login"); pg2.fill("#u", "admin"); pg2.fill("#p", "Monxu-Demo-2026"); pg2.click("button[type=submit]"); pg2.wait_for_url("**/dashboard")
    pg2.goto(BASE + "/admin"); pg2.wait_for_timeout(2500); pg2.screenshot(path=f"{OUT}/22-admin.png")
    # Spanish
    page.goto(BASE + "/dashboard"); page.get_by_label("Language").select_option("es"); page.wait_for_timeout(2500)
    page.screenshot(path=f"{OUT}/23-command-center-es.png")
    # mobile
    ctx3 = b.new_context(viewport={"width": 390, "height": 844}, storage_state=ctx.storage_state())
    pg3 = ctx3.new_page(); watch(pg3, ["mobile"])
    pg3.goto(BASE + "/shopfloor/operator"); pg3.wait_for_timeout(2500); pg3.screenshot(path=f"{OUT}/24-operator-mobile.png")
    b.close()
print(json.dumps(problems, indent=1))
print("PROBLEMS", len(problems))
sys.exit(1 if problems else 0)
