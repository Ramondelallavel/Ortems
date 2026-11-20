import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from playwright.sync_api import sync_playwright
from crawl import BASE, login, settle
AXE = os.environ.get("AXE_JS", "axe.min.js")  # path to node_modules/axe-core/axe.min.js
out = []
def axe(page, name):
    page.add_script_tag(path=AXE)
    res = page.evaluate("async () => await axe.run(document, {runOnly: {type: 'tag', values: ['wcag2a','wcag2aa','wcag21a','wcag21aa','wcag22aa','best-practice']}})")
    for v in res["violations"]:
        out.append((name, v["impact"], v["id"], v["help"], [n["target"][0] for n in v["nodes"][:2]]))
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=os.environ["CHROMIUM_PATH"])
    page = b.new_context(viewport={"width": 1400, "height": 900}).new_page(); login(page, "admin")
    def step(name, route, action):
        page.goto(BASE + route); settle(page, 1200)
        try:
            action(); settle(page, 600); axe(page, name)
        except Exception as e:
            out.append((name, "ERR", str(e)[:120], "", []))
        page.keyboard.press("Escape")
    step("optimize", "/planning", lambda: page.get_by_role("button", name="Optimize").click())
    step("account", "/dashboard", lambda: page.get_by_label("Account", exact=False).first.click() if False else page.locator("header button[aria-label*='account' i], header button[aria-label*='cuenta' i]").first.click())
    step("shortcuts", "/dashboard", lambda: page.keyboard.press("Shift+?"))
    step("assistant", "/dashboard", lambda: page.keyboard.press("Control+j"))
    step("new-user", "/admin", lambda: page.get_by_role("button", name="New user").click())
    step("new-key", "/admin", lambda: (page.get_by_role("tab", name="API keys").click(), page.get_by_role("button", name="New key").click()))
    step("new-scenario", "/planning/scenarios", lambda: page.get_by_role("button", name="New scenario").click())
    step("what-if", "/planning/scenarios", lambda: page.get_by_label("What-if", exact=True).select_option("OVERTIME"))
    step("webhook", "/integrations?tab=webhooks", lambda: page.get_by_role("button", name="Add webhook").click())
    step("data", "/planning/orders", lambda: page.get_by_role("button", name="Data").click())
    step("new-order", "/planning/orders", lambda: page.get_by_role("button", name="New order").click())
    step("import-wizard", "/integrations", lambda: None)
    step("master-new", "/master-data/machines", lambda: page.get_by_role("button", name=__import__("re").compile("New|Add", 2)).first.click())
    b.close()
for o in out: print(o)
print(len(out), "findings")
