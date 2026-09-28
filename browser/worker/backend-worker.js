// MonxuPlan browser edition — backend worker.
// Runs the MonxuPlan API (the same Python code as the server) in Pyodide, inside a Web Worker so the
// page stays responsive while the planner works. The database is SQLite in the worker's file
// system, persisted to this browser's IndexedDB when available.

let py = null;
let host = null;
let queue = Promise.resolve();
let persist = false;
let syncTimer = null;

const post = (m) => self.postMessage(m);
const describe = (e) => {
  if (e && e.stack) return String(e.stack);
  if (e && typeof e === "object") {
    try {
      return JSON.stringify({ name: e.name, message: e.message, errno: e.errno, code: e.code, ...e });
    } catch {}
  }
  return String(e);
};
const status = (step, detail) => post({ kind: "boot", step, detail });

function syncSoon() {
  if (!persist) return;
  clearTimeout(syncTimer);
  syncTimer = setTimeout(() => {
    py.FS.syncfs(false, (err) => err && post({ kind: "log", level: "warn", text: "Could not save to browser storage: " + err }));
  }, 1500);
}

function writeBundle(FS, root, bundle) {
  const dirs = new Set();
  const write = (rel, data) => {
    const full = root + "/" + rel;
    const dir = full.slice(0, full.lastIndexOf("/"));
    if (!dirs.has(dir)) {
      FS.mkdirTree(dir);
      dirs.add(dir);
    }
    FS.writeFile(full, data);
  };
  for (const [rel, text] of Object.entries(bundle.t)) write(rel, text);
  for (const [rel, b64] of Object.entries(bundle.b)) write(rel, Uint8Array.from(atob(b64), (c) => c.charCodeAt(0)));
}

async function boot({ base }) {
  status("runtime", "Loading the Python runtime (WebAssembly)");
  const get = async (rel) => {
    const r = await fetch(new URL(rel, base));
    if (!r.ok) throw new Error(`${rel}: HTTP ${r.status}`);
    return r.json();
  };
  const manifest = await get("py/manifest.json");
  const stdlib = Promise.all(manifest.stdlib.map((f) => get("py/" + f)));
  const site = Promise.all(manifest.site.map((f) => get("py/" + f)));
  const { loadPyodide } = await import(new URL("pyodide/pyodide.mjs", base).href);
  // the standard library is written as plain files (see build_site.py) onto PYTHONPATH; no stdlib archive
  const emptyZip = URL.createObjectURL(new Blob([new Uint8Array([0x50, 0x4b, 5, 6, ...new Array(18).fill(0)])]));
  py = await loadPyodide({
    indexURL: new URL("pyodide/", base).href,
    stdLibURL: emptyZip,
    env: { HOME: "/home/pyodide", PYTHONPATH: "/lib/python3.14" },
    stdout: () => {},
    stderr: (t) => post({ kind: "log", level: "debug", text: t }),
    fsInit: async (FS, { sitePackages }) => {
      const lib = sitePackages.slice(0, sitePackages.lastIndexOf("/"));
      for (const b of await stdlib) writeBundle(FS, lib, b);
    },
  });

  status("code", "Loading MonxuPlan (API, services and planning engine)");
  const sitePackages = py.runPython("import site; site.getsitepackages()[0]");
  for (const b of await site) writeBundle(py.FS, sitePackages, b);
  py.runPython("import importlib; importlib.invalidate_caches()");

  py.FS.mkdirTree("/data");
  try {
    py.FS.mount(py.FS.filesystems.IDBFS, {}, "/data");
    await new Promise((res, rej) => py.FS.syncfs(true, (e) => (e ? rej(e) : res())));
    persist = true;
  } catch (e) {
    post({ kind: "log", level: "warn", text: "Browser storage unavailable; data lasts until the page is closed." });
  }
  let existing = false;
  try {
    existing = py.FS.stat("/data/monxuplan.db").size > 0;
  } catch {}

  host = py.pyimport("monxuplan.browser");
  host.configure("/data/monxuplan.db");
  host.set_event_sink((text) => post({ kind: "event", text }));
  if (!existing) status("seed", "Creating the demo factory (first visit only)");
  else status("open", "Opening your saved data");
  const info = host.init_database(true).toJs({ dict_converter: Object.fromEntries });
  syncSoon();
  status("ready", "");
  post({ kind: "ready", fresh: !existing, info: JSON.parse(JSON.stringify(info, (k, v) => (typeof v === "bigint" ? Number(v) : v))), persist });
}

async function request({ id, method, path, query, headers, body }) {
  try {
    const r = await host.handle(method, path, query || "", JSON.stringify(headers), body ? new Uint8Array(body) : null);
    const res = r.toJs({ dict_converter: Object.fromEntries });
    r.destroy();
    const bytes = res.body;
    post({ kind: "response", id, status: res.status, headers: res.headers, body: bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) });
  } catch (e) {
    post({ kind: "response", id, status: 500, headers: [["content-type", "application/json"]], body: new TextEncoder().encode(JSON.stringify({ error: { code: "BROWSER_RUNTIME", message: String(e && e.message ? e.message : e).slice(-600) } })).buffer });
  }
  if (method !== "GET") {
    syncSoon();
    // planning runs are queued by the API and executed here, between requests
    if (host.queued_runs() > 0) pump();
  }
}

function pump() {
  queue = queue.then(() => {
    try {
      while (host.queued_runs() > 0) host.pump_jobs(1);
    } catch (e) {
      post({ kind: "log", level: "error", text: "Planning run failed: " + e });
    }
    syncSoon();
  });
}

self.onmessage = (ev) => {
  const m = ev.data;
  if (m.kind === "boot") {
    queue = queue.then(() => boot(m)).catch((e) => post({ kind: "fatal", text: describe(e) }));
  } else if (m.kind === "request") {
    queue = queue.then(() => request(m));
  }
};
