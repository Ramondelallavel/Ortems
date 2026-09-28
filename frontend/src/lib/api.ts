import { saveBlob } from "./save";

// Thin API client. Browser sessions use the httpOnly session cookie + double-submit CSRF header.

export class ApiError extends Error {
  status: number;
  code: string;
  context: Record<string, unknown>;
  constructor(status: number, code: string, message: string, context: Record<string, unknown> = {}) {
    super(message);
    this.status = status;
    this.code = code;
    this.context = context;
  }
}

function csrf(): string | undefined {
  if (typeof document === "undefined") return undefined;
  const m = document.cookie.match(/(?:^|;\s*)mx_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : undefined;
}

type Query = Record<string, string | number | boolean | null | undefined | string[]>;

export function qs(q?: Query): string {
  if (!q) return "";
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(q)) {
    if (v === undefined || v === null || v === "") continue;
    if (Array.isArray(v)) v.forEach((x) => p.append(k, x));
    else p.set(k, String(v));
  }
  const s = p.toString();
  return s ? `?${s}` : "";
}

let onUnauthorized: (() => void) | null = null;
export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn;
}

export async function api<T = any>(path: string, opts: { method?: string; body?: unknown; query?: Query; form?: FormData; signal?: AbortSignal; raw?: boolean } = {}): Promise<T> {
  const method = opts.method || (opts.body !== undefined || opts.form ? "POST" : "GET");
  const headers: Record<string, string> = { Accept: "application/json" };
  if (method !== "GET") {
    const t = csrf();
    if (t) headers["x-csrf-token"] = t;
  }
  let body: BodyInit | undefined;
  if (opts.form) body = opts.form;
  else if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.body);
  }
  let res: Response;
  try {
    res = await fetch(`/api/v1${path}${qs(opts.query)}`, { method, headers, body, credentials: "same-origin", signal: opts.signal });
  } catch (e) {
    if ((e as Error).name === "AbortError") throw e;
    throw new ApiError(0, "OFFLINE", "MonxuPlan server is not reachable. Check your connection; your changes were not sent.");
  }
  if (opts.raw && res.ok) return res as unknown as T;
  const ct = res.headers.get("content-type") || "";
  const data = ct.includes("application/json") ? await res.json().catch(() => null) : null;
  if (!res.ok) {
    const err = (data && (data.error || data.detail)) || {};
    const code = typeof err === "object" && err.code ? err.code : `HTTP_${res.status}`;
    const message = typeof err === "string" ? err : err.message || `Request failed (${res.status})`;
    if (res.status === 401 && onUnauthorized) onUnauthorized();
    throw new ApiError(res.status, code, message, (err && err.context) || {});
  }
  return data as T;
}

export async function download(path: string, query?: Query): Promise<void> {
  const res = await api<Response>(path, { query, raw: true });
  const blob = await res.blob();
  const cd = res.headers.get("content-disposition") || "";
  const m = cd.match(/filename="?([^";]+)"?/);
  await saveBlob(blob, m ? m[1] : "export");
}
