"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "./api";
import { loc } from "./loc";

export type Loadable<T> = { data: T | undefined; error: ApiError | undefined; loading: boolean; reload: () => void; setData: (d: T) => void };

/** Fetch-on-change with cancellation. `path === null` disables the request. */
export function useApi<T = any>(path: string | null, query?: Record<string, any>, deps: unknown[] = []): Loadable<T> {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<ApiError>();
  const [loading, setLoading] = useState<boolean>(!!path);
  const [tick, setTick] = useState(0);
  const key = path === null ? null : path + JSON.stringify(query || {});
  useEffect(() => {
    if (key === null || path === null) {
      setLoading(false);
      return;
    }
    const ctl = new AbortController();
    setLoading(true);
    setError(undefined);
    api<T>(path, { query, signal: ctl.signal })
      .then((d) => {
        setData(d);
        setLoading(false);
      })
      .catch((e) => {
        if (e?.name === "AbortError") return;
        setError(e instanceof ApiError ? e : new ApiError(0, "ERROR", String(e)));
        setLoading(false);
      });
    return () => ctl.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, tick, ...deps]);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { data, error, loading, reload, setData };
}

/** Server-sent events from /api/v1/events/stream (plan changes, run progress, alerts). */
export function useEvents(onEvent: (type: string, data: any) => void, plantId?: string | null) {
  const cb = useRef(onEvent);
  cb.current = onEvent;
  useEffect(() => {
    if (typeof EventSource === "undefined") return;
    const url = `/api/v1/events/stream${plantId ? `?plant_id=${plantId}` : ""}`;
    let es: EventSource | null = new EventSource(url);
    const handler = (ev: MessageEvent) => {
      try {
        const d = JSON.parse(ev.data);
        cb.current(d.type || ev.type, d.data ?? d);
      } catch {
        /* ignore malformed */
      }
    };
    es.onmessage = handler;
    for (const t of ["planning.run.progress", "planning.run.finished", "planning.run.started", "plan.created", "plan.published", "plan.head_changed", "alert.created"]) es.addEventListener(t, handler as EventListener);
    es.onerror = () => {
      /* browser reconnects automatically */
    };
    return () => {
      es?.close();
      es = null;
    };
  }, [plantId]);
}

export function useLocalState<T>(key: string, initial: T): [T, (v: T) => void] {
  const [v, setV] = useState<T>(initial);
  useEffect(() => {
    try {
      const s = localStorage.getItem(key);
      if (s !== null) setV(JSON.parse(s));
    } catch {
      /* storage unavailable */
    }
  }, [key]);
  const set = useCallback(
    (nv: T) => {
      setV(nv);
      try {
        localStorage.setItem(key, JSON.stringify(nv));
      } catch {
        /* ignore */
      }
    },
    [key],
  );
  return [v, set];
}

export function useQueryParam(name: string): string | null {
  const [v, setV] = useState<string | null>(null);
  useEffect(() => {
    const read = () => setV(new URLSearchParams(loc.search()).get(name));
    read();
    window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, [name]);
  return v;
}

export function useHotkeys(map: Record<string, (e: KeyboardEvent) => void>, enabled = true) {
  const ref = useRef(map);
  ref.current = map;
  useEffect(() => {
    if (!enabled) return;
    const h = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      const typing = t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable);
      const k = [(e.ctrlKey || e.metaKey) && "mod", e.shiftKey && "shift", e.altKey && "alt", e.key.toLowerCase()].filter(Boolean).join("+");
      const fn = ref.current[k];
      if (fn && (!typing || k.startsWith("mod+") || k === "escape")) {
        e.preventDefault();
        fn(e);
      }
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [enabled]);
}
