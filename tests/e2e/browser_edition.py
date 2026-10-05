"""Browser edition (Playwright): the built page with the Python API running in Pyodide. Boots it from a
clean profile (demo factory + first plan computed by the real engine), opens every screen through the
navigation, runs the optimiser from the planning board and switches the interface to Spanish.
Fails on console errors, page errors and error banners.

    cd browser && node build.mjs            # → browser/dist
    python tests/e2e/browser_edition.py     # serves dist on a local port

The artifact host wraps the page in a document; the test does the same with a minimal wrapper.
"""
from __future__ import annotations

import functools
import http.server
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).parent))
from crawl import ui_errors  # noqa: E402

DIST = Path(__file__).resolve().parents[2] / "browser" / "dist"


def serve() -> tuple[str, http.server.ThreadingHTTPServer]:
    root = Path(tempfile.mkdtemp(prefix="monxu-browser-"))
    (root / "index.html").write_text("<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><link rel=icon href='data:,'></head><body>" + (DIST / "index.html").read_text() + "</body></html>")
    for name in ("backend-worker.js", "py", "pyodide"):
        os.symlink(DIST / name, root / name)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a, **k: None  # type: ignore[attr-defined]
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}/", srv


def link(path: str) -> str:
    """The in-memory router's link for a route (next/link shim: "#-planning-orders")."""
    return "a[href='#" + path.replace("/", "-") + "']"


def main():
    url, srv = serve()
    problems: list[str] = []
    t0 = time.time()
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH") or None)
        page = b.new_context(viewport={"width": 1500, "height": 900}, locale="en-GB").new_page()
        page.on("console", lambda m: m.type == "error" and problems.append(f"console: {m.text[:300]} @ {m.location.get('url', '')}"))
        page.on("pageerror", lambda e: problems.append(f"pageerror: {str(e)[:300]}"))
        page.on("response", lambda r: r.status >= 400 and problems.append(f"http {r.status}: {r.url[:200]}"))
        page.goto(url)
        nav = page.get_by_role("navigation", name=re.compile(r"^(Main navigation|Navegación principal)$"))
        expect(nav).to_be_visible(timeout=600000)  # first visit: runtime, demo factory, first plan
        print(f"booted in {time.time() - t0:.0f}s", flush=True)
        hrefs = [a.get_attribute("href") for a in nav.locator("a").all()]
        assert len(hrefs) >= 15, hrefs
        for h in hrefs:
            nav.locator(f"a[href='{h}']").click()
            page.wait_for_timeout(2500)
            for txt in ui_errors(page):
                problems.append(f"ui-error @ {h}: {txt}")
            print(f"{h}: ok", flush=True)
        # optimise from the planning board with the engine running in the page
        nav.locator(link("/planning")).click()
        page.get_by_role("button", name="Optimize").click()
        dlg = page.get_by_role("dialog")
        dlg.get_by_label("Custom time limit in seconds").fill("5")
        dlg.get_by_role("button", name="Start").click()
        expect(page.locator("[role=status]").filter(has_text=re.compile(r"Plan PLAN-.* ready"))).to_be_visible(timeout=600000)
        print("optimised", flush=True)
        # Spanish
        page.get_by_label(re.compile(r"^(Language|Idioma)$")).first.select_option("es")
        nav_es = page.get_by_role("navigation", name="Navegación principal")
        expect(nav_es).to_be_visible(timeout=10000)
        for h in ("/dashboard", "/planning/alerts", "/admin/data-quality"):
            nav_es.locator(link(h)).click()
            page.wait_for_timeout(2500)
            for txt in ui_errors(page):
                problems.append(f"ui-error (es) @ {h}: {txt}")
        b.close()
    srv.shutdown()
    for x in problems:
        print("  !", x)
    print(f"PROBLEMS {len(problems)} · {time.time() - t0:.0f}s")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
