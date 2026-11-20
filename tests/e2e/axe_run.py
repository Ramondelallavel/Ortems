import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from playwright.sync_api import sync_playwright
from crawl import ROUTES, BASE, login, settle
AXE = os.environ.get("AXE_JS", "axe.min.js")  # path to node_modules/axe-core/axe.min.js
width = int(sys.argv[1]) if len(sys.argv) > 1 else 1400
theme = sys.argv[2] if len(sys.argv) > 2 else "light"
agg = {}
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=os.environ["CHROMIUM_PATH"])
    ctx = b.new_context(viewport={"width": width, "height": 900}, color_scheme=theme)
    page = ctx.new_page(); login(page, "admin")
    for r in ROUTES + ["/master-data/items", "/integrations?tab=webhooks", "/integrations?tab=database"]:
        page.goto(BASE + r); settle(page, 1500)
        page.add_script_tag(path=AXE)
        res = page.evaluate("async () => await axe.run(document, {runOnly: {type: 'tag', values: ['wcag2a','wcag2aa','wcag21a','wcag21aa','wcag22aa','best-practice']}})")
        for v in res["violations"]:
            key = (v["id"], v["impact"])
            ent = agg.setdefault(key, {"help": v["help"], "routes": set(), "nodes": []})
            ent["routes"].add(r)
            for n in v["nodes"][:2]:
                ent["nodes"].append((r, n["target"][0] if n["target"] else "", n.get("failureSummary", "")[:160].replace("\n", " ")))
    b.close()
for (vid, imp), e in sorted(agg.items(), key=lambda kv: {"critical": 0, "serious": 1, "moderate": 2, "minor": 3}.get(kv[0][1], 4)):
    print(f"{imp:9} {vid:32} {len(e['routes']):2} routes  {e['help']}")
    for n in e["nodes"][:3]:
        print("            ", n)
