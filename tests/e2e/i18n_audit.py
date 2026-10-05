"""Language audit (Playwright): opens every screen and every tab in a language other than English and
lists visible texts (text nodes, title, aria-label, placeholder) that are still in English.

    MONXU_WEB_URL=http://127.0.0.1:3000 python tests/e2e/i18n_audit.py [--locale es] [--route /admin]

A text is reported when it is an English interface text that has a translation (a value of en.ts or a
key of the phrase dictionary) or when it contains common English words. Data (codes, names, numbers)
is not reported. Exit code 1 when anything was found.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent))
from crawl import BASE, ROUTES, close_overlays, login, settle  # noqa: E402

I18N = Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "i18n"
ENGLISH_WORDS = re.compile(
    r"\b(the|and|with|without|of|for|from|by|is|are|not|no|yet|all|none|this|these|orders?|operations?|resources?|"
    r"materials?|plans?|late|due|available|waiting|blocked|shown|created|updated|rows?|entries|search|select|"
    r"show|hide|open|close|save|cancel|delete|edit|add|new|loading|error|errors|warnings?|total|more|less|"
    r"next|previous|today|week|month|hours?|minutes?|days?|setup|start|end|status|type|name|user|when|why)\b",
    re.I,
)


SPANISH_WORDS = re.compile(r"\b(de|del|la|las|el|los|en|con|por|para|que|sin|hay|una|un|es|está|son|al|se|su|sus|y|o|más|aún|todas?|todos?|ninguna?)\b", re.I)
CODE = re.compile(r"^[a-z_]+:[a-z_]+$|^[a-z]+(-[a-z]+)+$")


def known_english(locale: str) -> set[str]:
    """English interface texts that have a translation in `locale` (so seeing them means one was missed)."""
    def entries(p: Path) -> dict[str, str]:
        out = {}
        for m in re.finditer(r'^\s*"((?:[^"\\]|\\.)+)":\s*"((?:[^"\\]|\\.)*)",?\s*$', p.read_text(), re.M):
            out[json.loads(f'"{m.group(1)}"')] = json.loads(f'"{m.group(2)}"')
        return out

    en = entries(I18N / "en.ts")
    loc = entries(I18N / f"{locale}.ts")
    phrases = entries(I18N / f"{locale}-phrases.ts")
    texts = {v for k, v in en.items() if k in loc and loc[k] != v}
    texts |= {k for k, v in phrases.items() if k != v}
    # a text that is also a translation ("Plan" → "Plan") is correct in both languages
    texts -= set(loc.values()) | set(phrases.values())
    # texts with placeholders cannot match verbatim; one-letter or symbol-only texts are noise
    return {x for x in texts if "{" not in x and len(x) > 2}


def visible_texts(page) -> list[str]:
    return page.evaluate(
        """() => {
      const out = new Set();
      const vis = (el) => { if (!el) return false; const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
      const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      let n; while ((n = w.nextNode())) { const t = n.textContent.trim(); if (t && vis(n.parentElement) && !n.parentElement.closest('code,pre,.code,[data-i18n-skip]')) out.add(t); }
      for (const el of document.querySelectorAll('[title],[aria-label],[placeholder]')) {
        if (!vis(el)) continue;
        for (const a of ['title', 'aria-label', 'placeholder']) { const v = el.getAttribute(a); if (v && v.trim()) out.add(v.trim()); }
      }
      return [...out];
    }"""
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--locale", default="es")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--route", action="append")
    a = ap.parse_args()
    known = known_english(a.locale)
    found: dict[str, set[str]] = {}

    def check(where: str, page):
        for t in visible_texts(page):
            if CODE.match(t):
                continue
            if t in known or (len(t) > 3 and ENGLISH_WORDS.search(t) and not re.search(r"[áéíóúñ¿¡]", t, re.I) and len(ENGLISH_WORDS.findall(t)) >= 2 and not SPANISH_WORDS.search(t) and known_like(t)):
                found.setdefault(t, set()).add(where)

    def known_like(t: str) -> bool:
        # sentences/labels made of words: ignore codes such as WO-10042 or CNC-01 · PLAN-12
        words = re.findall(r"[A-Za-z]{2,}", t)
        return len(words) >= 2 and sum(w.islower() for w in words) >= 1

    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH") or None)
        ctx = b.new_context(viewport={"width": 1600, "height": 950}, locale="es-ES")
        page = ctx.new_page()
        page.set_default_timeout(6000)
        login(page, a.user)
        page.evaluate(f"localStorage.setItem('mx.locale', '{a.locale}')")
        for r in a.route or ROUTES:
            page.goto(BASE + r)
            settle(page, 900)
            check(r, page)
            tabs = page.locator("[role=tab]")
            for i in range(tabs.count()):
                try:
                    tabs.nth(i).click()
                    settle(page, 700)
                    check(f"{r} » tab {i + 1}", page)
                except Exception as e:  # a tab that vanished after a re-render is not a language problem
                    print(f"  (tab {i + 1} of {r}: {str(e)[:80]})")
            close_overlays(page)
            print(f"{r}: checked", flush=True)
        b.close()
    for t, where in sorted(found.items()):
        print(f"EN  {t!r}  ← {', '.join(sorted(where))[:200]}")
    print(f"ENGLISH TEXTS {len(found)}")
    sys.exit(1 if found else 0)


if __name__ == "__main__":
    main()
