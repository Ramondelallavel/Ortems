"""Every screen against hostile API answers. A screen must never go blank or throw an uncaught error:
it shows an error state (or an empty one) and the shell keeps working."""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from playwright.sync_api import sync_playwright
from crawl import ROUTES, BASE, login, settle

MODES = {
    "http500": lambda route: route.fulfill(status=500, content_type="application/json", body='{"error":{"code":"INTERNAL_ERROR","message":"boom","context":{}}}'),
    "http502html": lambda route: route.fulfill(status=502, content_type="text/html", body="<html>Bad gateway</html>"),
    "malformed": lambda route: route.fulfill(status=200, content_type="application/json", body='{"items": [1,2,'),
    "html200": lambda route: route.fulfill(status=200, content_type="text/html", body="<html>captive portal</html>"),
    "null": lambda route: route.fulfill(status=200, content_type="application/json", body="null"),
    "empty_obj": lambda route: route.fulfill(status=200, content_type="application/json", body="{}"),
    "empty_list": lambda route: route.fulfill(status=200, content_type="application/json", body="[]"),
    "abort": lambda route: route.abort("connectionfailed"),
}
KEEP = ("/auth/", "/plants", "/events/stream")
only = sys.argv[1].split(",") if len(sys.argv) > 1 else list(MODES)
bad = []
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH"))
    for mode in only:
        ctx = b.new_context(viewport={"width": 1400, "height": 900})
        page = ctx.new_page()
        login(page, "admin")
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)[:300]))

        def make(mode):
            def handler(route):
                u = route.request.url
                if any(k in u for k in KEEP) or "/api/" not in u:
                    return route.continue_()
                return MODES[mode](route)
            return handler

        page.route("**/api/v1/**", make(mode))
        for r in ROUTES + ["/master-data/items", "/master-data/machines", "/integrations?tab=history", "/integrations?tab=webhooks", "/admin"]:
            errs.clear()
            page.goto(BASE + r)
            settle(page, 800)
            text = page.inner_text("body").strip()
            nav_ok = page.get_by_role("navigation").count() > 0
            crashed = ("Application error" in text) or ("client-side exception" in text) or not nav_ok
            if errs or crashed:
                bad.append((mode, r, "CRASH" if crashed else "", errs[:1]))
                print("!!", mode, r, "CRASH" if crashed else "", errs[:1], flush=True)
        ctx.close()
    b.close()
print(f"{len(bad)} problems")
