// In-memory router for the browser edition (the artifact frame has no server-side routes).
import { useSyncExternalStore } from "react";

type State = { path: string; search: string; reloads: number };
let state: State = { path: "/dashboard", search: "", reloads: 0 };
const history: string[] = [];
const listeners = new Set<() => void>();

function emit() {
  for (const l of listeners) l();
  window.dispatchEvent(new PopStateEvent("popstate"));
  window.scrollTo?.(0, 0);
}

function parse(url: string): { path: string; search: string } {
  const u = new URL(url, "https://monxuplan.local" + state.path);
  let path = u.pathname.replace(/\/+$/, "") || "/";
  if (path === "/") path = "/dashboard";
  return { path, search: u.search };
}

export const router = {
  get: () => state,
  subscribe(fn: () => void) {
    listeners.add(fn);
    return () => listeners.delete(fn);
  },
  push(url: string) {
    history.push(state.path + state.search);
    state = { ...state, ...parse(url) };
    emit();
  },
  replace(url: string) {
    state = { ...state, ...parse(url) };
    emit();
  },
  back() {
    const prev = history.pop();
    if (prev) {
      state = { ...state, ...parse(prev) };
      emit();
    }
  },
  /** Equivalent of a full page load: providers remount and the session is re-read. */
  reload(url?: string) {
    state = { ...(url ? parse(url) : state), reloads: state.reloads + 1 };
    emit();
  },
};

export function useRouterState(): State {
  return useSyncExternalStore(router.subscribe, router.get, router.get);
}
