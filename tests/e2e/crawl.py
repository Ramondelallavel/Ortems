"""Exhaustive UI crawler (Playwright): opens every screen, clicks every visible button, tab, link and
toggle, opens and closes every dialog, changes every select, and reports console errors, uncaught page
errors, failed API calls (HTTP >= 400) and error banners rendered by the UI.

    MONXU_WEB_URL=http://127.0.0.1:3000 python tests/e2e/crawl.py [--user admin] [--route /planning] [--locale es]

Requires a running stack with the demo tenant (python -m monxuplan.seed.demo) and a finished plan.
Destructive controls (delete, publish, sign out, …) are not clicked here: tests/e2e/flows.py drives
them with real assertions. Exit code 1 when anything was reported.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

from playwright.sync_api import Error as PwError
from playwright.sync_api import TimeoutError as PwTimeout
from playwright.sync_api import sync_playwright

BASE = os.environ.get("MONXU_WEB_URL", "http://127.0.0.1:3000")
PASSWORD = os.environ.get("MONXU_DEMO_PASSWORD", "Monxu-Demo-2026")

ROUTES = [
    "/dashboard",
    "/getting-started",
    "/planning",
    "/planning/orders",
    "/planning/capacity",
    "/planning/materials",
    "/planning/mps",
    "/planning/scenarios",
    "/planning/alerts",
    "/shopfloor/dispatch",
    "/shopfloor/supervisor",
    "/shopfloor/operator",
    "/analytics",
    "/analytics/plan-vs-actual",
    "/master-data",
    "/integrations",
    "/admin/data-quality",
    "/admin",
]

# never clicked by the crawler (state-changing or leaving the session); covered by flows.py
DANGER = re.compile(
    r"\b(delete|remove|borrar|eliminar|quitar|archive|archivar|publish|publicar|sign out|cerrar sesión|logout|revoke|revocar|"
    r"deactivate|desactivar|reset|restablecer|discard|descartar|purge|apply|aplicar|commit|confirm|confirmar|"
    r"save|guardar|import|importar|sync|sincronizar|optimi[sz]e|optimizar|reschedule|replanificar|start|iniciar|"
    r"finish|finalizar|terminar|pause|pausar|report|reportar|acknowledge|reconocer|resolve|resolver|lock|bloquear|"
    r"unlock|desbloquear|undo|deshacer|redo|rehacer|calculate|calcular|run|ejecutar|create|crear|new|nuevo|nueva|"
    r"add|añadir|clone|clonar|duplicate|duplicar|send|enviar|test|probar|rotate|rotar|promote|promover|release|"
    r"liberar|split|dividir|freeze|congelar|validate|validar|upload|subir|password|contraseña|load demo|retry|reintentar)\b",
    re.I,
)
# safe to click even though a word above matches (pure navigation / view toggles)
SAFE = re.compile(r"^(close|cerrar|cancel|cancelar)$", re.I)


class Recorder:
    def __init__(self):
        self.where = "start"
        self.items: list[dict] = []
        self.inflight: set = set()

    def add(self, kind, detail):
        self.items.append({"where": self.where, "kind": kind, "detail": detail})
        print(f"  ! {kind} @ {self.where}: {detail[:300]}", flush=True)

    def attach(self, page):
        page.on("console", lambda m: m.type == "error" and self.add("console", m.text[:400]))
        page.on("pageerror", lambda e: self.add("pageerror", str(e)[:400]))
        page.on("response", self._resp)
        # in-flight API calls (the event stream stays open forever, so "networkidle" never happens)
        page.on("request", lambda r: _tracked(r) and self.inflight.add(r))
        page.on("requestfinished", lambda r: self.inflight.discard(r))
        page.on("requestfailed", lambda r: self.inflight.discard(r))
        page.on("requestfailed", lambda r: "/api/" in r.url and not _aborted(r) and self.add("requestfailed", f"{r.method} {r.url[:200]} {r.failure}"))

    def _resp(self, r):
        if "/api/" in r.url and r.status >= 400:
            body = ""
            try:
                body = r.text()[:300]
            except Exception:  # noqa: BLE001 - body may be unavailable for aborted requests
                pass
            self.add("http", f"{r.status} {r.request.method} {r.url[len(BASE):][:200]} {body}")


def _tracked(r) -> bool:
    return "/api/" in r.url and "/stream" not in r.url


def _aborted(r) -> bool:
    f = (r.failure or "").lower()
    return "abort" in f or "cancel" in f


def login(page, user):
    page.goto(BASE + "/login")
    page.fill("#u", user)
    page.fill("#p", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url("**/dashboard", timeout=30000)


REC: "Recorder | None" = None


def settle(page, ms=700, limit_s=20.0):
    """Wait until no API call has been in flight for `ms` (or `limit_s` passed)."""
    t0 = time.time()
    quiet_since = None
    while time.time() - t0 < limit_s:
        busy = bool(REC and REC.inflight)
        if busy:
            quiet_since = None
        elif quiet_since is None:
            quiet_since = time.time()
        elif (time.time() - quiet_since) * 1000 >= ms:
            return
        page.wait_for_timeout(50)
    if REC:
        REC.add("slow", f"API calls still running after {limit_s:.0f}s: {[r.url[len(BASE):][:120] for r in list(REC.inflight)[:3]]}")


def ui_errors(page) -> list[str]:
    """Error banners and error toasts currently shown (role=alert)."""
    out = []
    for el in page.locator("[role=alert]").all():
        try:
            if el.is_visible():
                txt = el.inner_text(timeout=500).strip()
                if txt:
                    out.append(txt[:300])
        except PwError:
            pass
    return out


def describe(el) -> str:
    try:
        d = el.evaluate(
            "e => (e.getAttribute('aria-label') || e.innerText || e.getAttribute('title') || e.getAttribute('href') || e.value || '').trim().replace(/\\s+/g,' ').slice(0,80)"
        )
    except PwError:
        d = ""
    return d or "?"


CANDIDATES = "button:visible, [role=tab]:visible, a[href]:visible, summary:visible, input[type=checkbox]:visible"


def close_overlays(page):
    for _ in range(3):
        dlg = page.locator("[role=dialog]:visible")
        if not dlg.count():
            break
        page.keyboard.press("Escape")
        page.wait_for_timeout(250)
        if dlg.count():
            btn = dlg.first.locator("button[aria-label=Close]")
            if btn.count():
                btn.first.click(timeout=2000)
                page.wait_for_timeout(250)


def crawl_route(page, rec: Recorder, route: str, shots: str | None, max_clicks=160):
    rec.where = route
    page.goto(BASE + route)
    settle(page, 1500)
    if route == "/planning":
        page.wait_for_selector("canvas[aria-roledescription='Gantt chart']", timeout=30000)
    for txt in ui_errors(page):
        rec.add("ui-error", txt)
    if shots:
        page.screenshot(path=f"{shots}/crawl{route.replace('/', '_') or '_root'}.png")
    seen: set[str] = set()
    clicks = 0
    stale_rounds = 0
    while clicks < max_clicks and stale_rounds < 3:
        progressed = False
        els = page.locator(CANDIDATES).all()
        for el in els:
            if clicks >= max_clicks:
                break
            try:
                if el.is_disabled():
                    continue
            except PwError:
                continue
            d = describe(el)
            try:
                tag = el.evaluate("e => e.tagName.toLowerCase() + (e.getAttribute('role') ? '[' + e.getAttribute('role') + ']' : '')")
                href = el.get_attribute("href") or ""
                inside_nav = el.evaluate("e => !!e.closest('nav[aria-label=Main], header')")
            except PwError:
                continue
            key = f"{tag}|{d}|{href}"
            if key in seen:
                continue
            seen.add(key)
            if inside_nav:
                continue  # navigation and header are exercised separately
            if href and (href.startswith("http") and not href.startswith(BASE)):
                continue
            if DANGER.search(d) and not SAFE.match(d):
                continue
            progressed = True
            clicks += 1
            rec.where = f"{route} » {tag} '{d}'"
            url_before = page.url
            try:
                el.click(timeout=3000)
            except (PwTimeout, PwError) as e:
                msg = str(e).splitlines()[0][:200]
                if "intercepts pointer events" in str(e) or "not visible" in msg or "detached" in msg or "outside of the viewport" in msg:
                    continue
                rec.add("click-failed", msg)
                continue
            settle(page, 400)
            for txt in ui_errors(page):
                rec.add("ui-error", txt)
            close_overlays(page)
            if page.url.split("#")[0] != url_before.split("#")[0]:
                page.goto(BASE + route)
                settle(page, 900)
                break  # DOM rebuilt: re-enumerate
        if not progressed:
            stale_rounds += 1
        rec.where = route
    # every select: pick each other option once, then restore
    sels = page.locator("main select:visible").all()
    for i, sel in enumerate(sels):
        try:
            label = describe(sel)
            opts = sel.evaluate("s => Array.from(s.options).map(o => o.value)")
            cur = sel.input_value()
        except PwError:
            continue
        rec.where = f"{route} » select '{label}'"
        for v in [o for o in opts if o != cur][:4]:
            try:
                sel.select_option(v, timeout=2000)
            except (PwTimeout, PwError):
                break
            settle(page, 400)
            for txt in ui_errors(page):
                rec.add("ui-error", f"[{v}] {txt}")
        try:
            sel.select_option(cur, timeout=2000)
            settle(page, 300)
        except (PwTimeout, PwError):
            pass
    rec.where = route
    return clicks


def crawl_shell(page, rec: Recorder):
    rec.where = "shell"
    page.goto(BASE + "/dashboard")
    settle(page, 1200)
    # every navigation link
    links = page.locator("nav[aria-label=Main] a").all()
    hrefs = [a.get_attribute("href") for a in links]
    for h in hrefs:
        rec.where = f"nav {h}"
        page.locator(f"nav[aria-label=Main] a[href='{h}']").click()
        page.wait_for_url(f"**{h}", timeout=15000)
        settle(page, 600)
        for txt in ui_errors(page):
            rec.add("ui-error", txt)
    rec.where = "shell » collapse"
    page.get_by_label("Collapse navigation").click()
    page.get_by_label("Expand navigation").click()
    rec.where = "shell » shortcuts"
    page.keyboard.press("Shift+?")
    page.wait_for_selector("[role=dialog]")
    close_overlays(page)
    rec.where = "shell » assistant"
    page.keyboard.press("Control+j")
    page.wait_for_timeout(500)
    page.keyboard.press("Control+j")
    for k in "123456":
        page.keyboard.press(f"Alt+{k}")
        settle(page, 500)
    return hrefs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default="admin")
    ap.add_argument("--route", action="append")
    ap.add_argument("--locale", default="en")
    ap.add_argument("--shots")
    ap.add_argument("--max-clicks", type=int, default=160)
    a = ap.parse_args()
    global REC
    rec = REC = Recorder()
    t0 = time.time()
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH") or None)
        ctx = b.new_context(viewport={"width": 1600, "height": 950}, locale="en-GB", accept_downloads=True)
        page = ctx.new_page()
        # element handles go stale whenever the page re-renders: fail fast instead of waiting 30 s
        page.set_default_timeout(4000)
        page.on("dialog", lambda d: d.dismiss())
        rec.attach(page)
        login(page, a.user)
        if a.locale != "en":
            page.evaluate(f"localStorage.setItem('mx.locale', '{a.locale}')")
        if not a.route:
            crawl_shell(page, rec)
        total = 0
        for r in a.route or ROUTES:
            n = crawl_route(page, rec, r, a.shots, a.max_clicks)
            total += n
            print(f"{r}: {n} controls", flush=True)
        b.close()
    print(json.dumps(rec.items, indent=1, ensure_ascii=False))
    print(f"PROBLEMS {len(rec.items)} · {total} controls · {time.time() - t0:.0f}s")
    sys.exit(1 if rec.items else 0)


if __name__ == "__main__":
    main()
