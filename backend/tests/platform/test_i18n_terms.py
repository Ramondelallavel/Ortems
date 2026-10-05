"""Every English text template the server sends for translation (alerts, attention items) has a
Spanish phrase in the interface, so a new alert never shows up half in English."""

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PHRASES = ROOT / "frontend" / "src" / "lib" / "i18n" / "es-phrases.ts"


def _templates() -> set[str]:
    found: set[str] = set()
    for name in ("alerts.py", "overview.py", "events_in.py"):
        tree = ast.parse((ROOT / "backend" / "monxuplan" / "services" / name).read_text())
        for node in ast.walk(tree):
            # ("template {x}", {...}) tuples are alert texts
            if isinstance(node, ast.Tuple) and len(node.elts) == 2 and isinstance(node.elts[1], ast.Dict):
                first = node.elts[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    found.add(first.value)
                elif isinstance(first, ast.IfExp):
                    # "{res} is down" if … else "…": every branch is a template (the test is not)
                    found |= {c.value for c in (first.body, first.orelse) if isinstance(c, ast.Constant)}
                    if isinstance(first.orelse, ast.IfExp):
                        found |= {c.value for c in (first.orelse.body, first.orelse.orelse) if isinstance(c, ast.Constant)}
            # the dictionary of unscheduled-reason titles
            if isinstance(node, ast.Dict) and node.keys and all(isinstance(k, ast.Constant) and isinstance(k.value, str) and k.value.isupper() for k in node.keys):
                vals = [v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str)]
                found |= {v for v in vals if "{n}" in v}
    # alert summaries assigned before the call: summary = ("template", {...})
    for name in ("alerts.py",):
        tree = ast.parse((ROOT / "backend" / "monxuplan" / "services" / name).read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and getattr(getattr(node, "target", None) or node.targets[0], "id", "") == "summary":
                v = node.value
                if isinstance(v, ast.Tuple) and isinstance(v.elts[0], ast.Constant):
                    found.add(v.elts[0].value)
    # delay causes of the engine: _cause(category, code, template, …), templates assigned before the
    # call, and the fixed terms passed as "…_term" values
    tree = ast.parse((ROOT / "backend" / "monxuplan_engine" / "kpis.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_cause" and len(node.args) >= 3 and isinstance(node.args[2], ast.Constant):
            found.add(node.args[2].value)
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Tuple) and isinstance(node.targets[0], ast.Tuple):
            first = node.value.elts[0]
            if getattr(node.targets[0].elts[0], "id", "") in ("tpl", "summary") and isinstance(first, ast.Constant):
                found.add(first.value)
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") in ("_LIMIT_TERM", "_WAIT_TERM"):
            found |= {v.value for v in node.value.values}
    return found


def test_alert_templates_have_spanish_phrases():
    keys = {json.loads(m.group(1)) for m in re.finditer(r'^\s*("(?:[^"\\]|\\.)+"):', PHRASES.read_text(), re.M)}
    templates = _templates()
    assert len(templates) >= 40, sorted(templates)
    missing = sorted(t for t in templates if t not in keys)
    assert not missing, missing
