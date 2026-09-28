// Main-thread side of the browser edition: forwards `/api/...` fetches and the live event stream to the
// backend worker, keeping the session cookies the API sets (the worker is the "server").

type Pending = { resolve: (r: Response) => void };
type BootListener = (m: { kind: string; step?: string; detail?: string; text?: string; fresh?: boolean; persist?: boolean }) => void;

const pending = new Map<number, Pending>();
const cookies = new Map<string, string>();
const streams = new Set<FakeEventSource>();
let nextId = 1;
let worker: Worker;
const bootListeners = new Set<BootListener>();

export function onBoot(fn: BootListener) {
  bootListeners.add(fn);
  return () => bootListeners.delete(fn);
}

export function startBackend(base: string) {
  worker = new Worker(new URL("backend-worker.js", base), { type: "module" });
  worker.onmessage = (ev) => {
    const m = ev.data;
    if (m.kind === "response") {
      const p = pending.get(m.id);
      pending.delete(m.id);
      const h = new Headers();
      for (const [k, v] of m.headers as [string, string][]) {
        if (k.toLowerCase() === "set-cookie") {
          const [kv, ...attrs] = v.split(";");
          const i = kv.indexOf("=");
          const name = kv.slice(0, i).trim();
          const value = kv.slice(i + 1).trim();
          const expired = attrs.some((a) => /max-age=0/i.test(a) || /expires=thu, 01 jan 1970/i.test(a));
          if (expired || value === "" || value === '""') cookies.delete(name);
          else cookies.set(name, value);
        } else h.append(k, v);
      }
      if (m.status >= 500) console.error("[MonxuPlan] API error", m.status, new TextDecoder().decode(m.body).slice(0, 400));
      const nullBody = m.status === 204 || m.status === 304;
      p?.resolve(new Response(nullBody ? null : m.body, { status: m.status, headers: h }));
    } else if (m.kind === "event") {
      for (const s of streams) s._deliver(m.text);
    } else if (m.kind === "log") {
      if (m.level !== "debug") console[m.level === "error" ? "error" : "warn"]("[MonxuPlan]", m.text);
    } else {
      for (const fn of bootListeners) fn(m);
    }
  };
  worker.onerror = (e) => {
    for (const fn of bootListeners) fn({ kind: "fatal", text: e.message || "The backend worker could not start." });
  };
  worker.postMessage({ kind: "boot", base });
}

async function bodyBytes(input: Request): Promise<ArrayBuffer | null> {
  if (input.method === "GET" || input.method === "HEAD") return null;
  const b = await input.arrayBuffer();
  return b.byteLength ? b : null;
}

export async function apiFetch(input: Request): Promise<Response> {
  const url = new URL(input.url);
  const headers: [string, string][] = [];
  input.headers.forEach((v, k) => headers.push([k, v]));
  if (cookies.size) headers.push(["cookie", [...cookies].map(([k, v]) => `${k}=${v}`).join("; ")]);
  const csrf = cookies.get("mx_csrf");
  if (csrf && input.method !== "GET" && !input.headers.has("x-csrf-token")) headers.push(["x-csrf-token", decodeURIComponent(csrf)]);
  const body = await bodyBytes(input);
  const id = nextId++;
  return new Promise((resolve) => {
    pending.set(id, { resolve });
    worker.postMessage({ kind: "request", id, method: input.method, path: url.pathname, query: url.search.slice(1), headers, body }, body ? [body] : []);
  });
}

/** Replaces `fetch` for `/api/...` URLs; everything else goes to the real network stack. */
export function installFetch() {
  const realFetch = window.fetch.bind(window);
  window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
    const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    if (raw.startsWith("/api/")) return apiFetch(new Request(new URL(raw, "https://monxuplan.local"), init));
    return realFetch(input, init);
  };
}

/** Live updates: the API's event bus, delivered by the worker (same messages as the SSE stream). */
class FakeEventSource extends EventTarget {
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  readyState = 1;
  private plantId: string | null;
  constructor(url: string) {
    super();
    this.plantId = new URL(url, "https://monxuplan.local").searchParams.get("plant_id");
    streams.add(this);
  }
  _deliver(text: string) {
    const msg = JSON.parse(text);
    if (this.plantId && msg.plant_id && msg.plant_id !== this.plantId) return;
    const ev = new MessageEvent(msg.type, { data: text });
    this.dispatchEvent(ev);
  }
  close() {
    this.readyState = 2;
    streams.delete(this);
  }
}

export function installEventSource() {
  (window as any).EventSource = FakeEventSource;
}
