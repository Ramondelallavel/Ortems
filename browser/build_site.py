"""Pack the Python files the browser edition needs into JSON bundles.

Artifact hosting serves scripts, WebAssembly and data files but not archives, so instead of zip files
the build writes the files themselves, as JSON maps ``{path: text}`` (binary files base64 under
``"b"``), split into chunks below the hosting size limit:

* ``py/stdlib-N.json`` — Pyodide's Python standard library (from ``python_stdlib.zip``);
* ``py/site-N.json``   — the MonxuPlan backend (``monxuplan``, ``monxuplan_engine``) plus the pure-Python
  and WebAssembly wheels listed in ``requirements-browser.txt``;
* ``py/manifest.json`` — the chunk list.

Comments are stripped from Python sources (tokenize-based, docstrings kept) to cut the download.
"""

from __future__ import annotations

import base64
import io
import json
import sys
import tokenize
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {"__pycache__", "migrations", "tests", "test"}
CHUNK = 6_000_000
# SQLAlchemy parts the browser edition never uses (SQLite only; the PostgreSQL dialect stays for model types)
SKIP_PREFIXES = ("sqlalchemy/dialects/mysql/", "sqlalchemy/dialects/oracle/", "sqlalchemy/dialects/mssql/", "sqlalchemy/testing/", "sqlalchemy/ext/mypy/")
STDLIB_SKIP = ("_pyrepl/", "pydoc_data/", "idlelib/", "turtledemo/", "ensurepip/", "venv/", "lib2to3/", "tkinter/")


def strip_comments(src: str) -> str:
    try:
        out = []
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT and not tok.string.startswith(("#!", "# -*-", "# type:", "# noqa")):
                continue
            out.append(tok)
        text = tokenize.untokenize(out)
        return "\n".join(line.rstrip() for line in text.splitlines() if line.strip()) + "\n"
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return src


def add(files: dict, path: str, data: bytes) -> None:
    if path.endswith(".py"):
        files["t"][path] = strip_comments(data.decode("utf-8"))
        return
    try:
        text = data.decode("utf-8")
        if "\x00" not in text:
            files["t"][path] = text
            return
    except UnicodeDecodeError:
        pass
    files["b"][path] = base64.b64encode(data).decode()


def write_chunks(files: dict, out_dir: Path, name: str) -> list[str]:
    chunks, cur, size = [], {"t": {}, "b": {}}, 0
    for kind in ("t", "b"):
        for path, content in sorted(files[kind].items()):
            if size + len(content) > CHUNK and size:
                chunks.append(cur)
                cur, size = {"t": {}, "b": {}}, 0
            cur[kind][path] = content
            size += len(content) + len(path)
    chunks.append(cur)
    names = []
    for i, c in enumerate(chunks):
        fn = f"{name}-{i}.json"
        (out_dir / fn).write_text(json.dumps(c, separators=(",", ":")))
        names.append(fn)
    return names


def main(wheel_dir: str, stdlib_zip: str, out: str) -> None:
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)

    site: dict = {"t": {}, "b": {}}
    for pkg in ("monxuplan", "monxuplan_engine"):
        base = ROOT / "backend" / pkg
        for f in sorted(base.rglob("*")):
            rel = f.relative_to(base.parent)
            if f.is_dir() or SKIP_DIRS & set(rel.parts) or f.suffix in {".pyc", ".ini"}:
                continue
            add(site, rel.as_posix(), f.read_bytes())
    for whl in sorted(Path(wheel_dir).glob("*.whl")):
        with zipfile.ZipFile(whl) as w:
            for info in w.infolist():
                name = info.filename
                if info.is_dir() or "/tests/" in name or name.endswith((".pyi", "/RECORD")) or name.startswith(SKIP_PREFIXES):
                    continue
                add(site, name, w.read(name))

    std: dict = {"t": {}, "b": {}}
    with zipfile.ZipFile(stdlib_zip) as z:
        for info in z.infolist():
            if info.is_dir() or info.filename.startswith(STDLIB_SKIP) or "/test" in info.filename:
                continue
            add(std, info.filename, z.read(info.filename))

    manifest = {"stdlib": write_chunks(std, out_dir, "stdlib"), "site": write_chunks(site, out_dir, "site")}
    (out_dir / "manifest.json").write_text(json.dumps(manifest))
    total = sum((out_dir / f).stat().st_size for f in manifest["stdlib"] + manifest["site"])
    print(f"{out_dir}: {len(manifest['stdlib']) + len(manifest['site'])} bundles, {total / 1e6:.1f} MB")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3])
