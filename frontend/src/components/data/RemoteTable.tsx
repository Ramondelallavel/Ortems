"use client";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ErrorState, Icon, type Column } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { useSession } from "@/lib/session";

export type RemotePage<T> = { items: T[]; total: number; [k: string]: any };

const ROW = 26;

/**
 * A table over a server-side list of any size (100 000+ rows): only the rows on screen are fetched, in
 * pages, while the scrollbar spans the whole list. Sorting and searching run on the server. Columns
 * are sortable when ``sortKeys`` maps them to a server sort key.
 */
export function RemoteTable<T extends Record<string, any>>({
  path,
  query,
  columns,
  rowKey,
  onRowClick,
  selectedKey,
  sortKeys = {},
  initialSort,
  toolbar,
  emptyText,
  pageSize = 200,
  onMeta,
  reloadKey,
  searchPlaceholder,
}: {
  path: string | null;
  query?: Record<string, any>;
  columns: Column<T>[];
  rowKey: (r: T) => string;
  onRowClick?: (r: T) => void;
  selectedKey?: string | null;
  sortKeys?: Record<string, string>;
  initialSort?: { key: string; dir: 1 | -1 };
  toolbar?: ReactNode;
  emptyText?: string;
  pageSize?: number;
  onMeta?: (page: RemotePage<T>) => void;
  reloadKey?: unknown;
  searchPlaceholder?: string;
}) {
  const { t } = useSession();
  const [sort, setSort] = useState(initialSort);
  const [typed, setTyped] = useState("");
  const [q, setQ] = useState("");
  const [pages, setPages] = useState<Record<number, T[]>>({});
  const [total, setTotal] = useState<number | null>(null);
  const [error, setError] = useState<ApiError>();
  const [retry, setRetry] = useState(0);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewH, setViewH] = useState(600);
  const scroller = useRef<HTMLDivElement>(null);
  const inflight = useRef(new Set<number>());
  const gen = useRef(0);
  const meta = useRef(onMeta);
  meta.current = onMeta;

  // search as you type, without a request per keystroke
  useEffect(() => {
    const h = setTimeout(() => setQ(typed.trim()), 300);
    return () => clearTimeout(h);
  }, [typed]);

  const params = useMemo(() => {
    const p: Record<string, any> = { ...(query || {}) };
    if (q) p.q = q;
    if (sort && sortKeys[sort.key]) {
      p.sort = sortKeys[sort.key];
      p.dir = sort.dir === 1 ? "asc" : "desc";
    }
    return p;
  }, [query, q, sort, sortKeys]);
  const resetKey = path === null ? null : `${path}${JSON.stringify(params)}|${String(reloadKey ?? "")}|${retry}`;

  const load = useCallback(
    (page: number) => {
      if (path === null || inflight.current.has(page)) return;
      inflight.current.add(page);
      const g = gen.current;
      api<RemotePage<T>>(path, { query: { ...params, offset: page * pageSize, limit: pageSize } })
        .then((d) => {
          if (g !== gen.current) return;
          setPages((prev) => ({ ...prev, [page]: d.items }));
          setTotal(d.total);
          if (page === 0) meta.current?.(d);
        })
        .catch((e) => {
          if (g === gen.current) setError(e instanceof ApiError ? e : new ApiError(0, "ERROR", String(e)));
        })
        .finally(() => {
          if (g === gen.current) inflight.current.delete(page);
        });
    },
    [path, params, pageSize],
  );

  // a new query, sort or search starts from an empty list at the top
  useEffect(() => {
    gen.current += 1;
    inflight.current = new Set();
    setPages({});
    setTotal(null);
    setError(undefined);
    if (scroller.current) scroller.current.scrollTop = 0;
    setScrollTop(0);
    if (resetKey !== null) load(0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resetKey]);

  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setViewH(el.clientHeight));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const n = total ?? 0;
  const first = Math.max(0, Math.floor(scrollTop / ROW) - 10);
  const last = Math.min(n, Math.ceil((scrollTop + viewH) / ROW) + 10);

  // fetch the pages under the viewport (and the next one, so scrolling never waits)
  useEffect(() => {
    if (total === null) return;
    const a = Math.floor(first / pageSize);
    const b = Math.floor(Math.max(last - 1, 0) / pageSize) + 1;
    for (let p = a; p <= b && p * pageSize < n; p++) if (!pages[p]) load(p);
  }, [first, last, total, n, pageSize, pages, load]);

  const cell = (r: T, c: Column<T>) => (c.render ? c.render(r) : ((c.value ? c.value(r) : r[c.key]) ?? "—"));
  const rows: { i: number; r: T | undefined }[] = [];
  for (let i = first; i < last; i++) rows.push({ i, r: pages[Math.floor(i / pageSize)]?.[i % pageSize] });

  return (
    <div className="flex flex-col min-h-0 h-full">
      <div className="flex items-center gap-2 px-2 py-1.5 border-b border-gray-200 bg-white">
        <div className="relative">
          <Icon name="search" size={13} className="absolute left-2 top-[7px] text-slate-400" />
          <input className="mx-input pl-7 w-[240px]" placeholder={searchPlaceholder ?? t("Search…")} aria-label={t("Search rows")} value={typed} onChange={(e) => setTyped(e.target.value)} />
        </div>
        <span className="text-[11.5px] text-slate-600 tabular" aria-live="polite">
          {total === null ? "…" : t("{n} rows", { n: total.toLocaleString() })}
        </span>
        <div className="flex-1" />
        {toolbar}
      </div>
      {error && <ErrorState error={error} onRetry={() => setRetry((n) => n + 1)} />}
      <div ref={scroller} className="overflow-auto mx-scroll flex-1 min-h-0" onScroll={(e) => setScrollTop((e.target as HTMLDivElement).scrollTop)}>
        <table className="mx-table" role="grid" aria-rowcount={n}>
          <thead>
            <tr>
              {columns.map((c) => {
                const key = sortKeys[c.key];
                return (
                  <th key={c.key} style={{ width: c.width, textAlign: c.align || "left" }} aria-sort={sort?.key === c.key ? (sort.dir === 1 ? "ascending" : "descending") : "none"}>
                    {key ? (
                      <button className="inline-flex items-center gap-1 hover:text-graphite-800" onClick={() => setSort((s) => (s?.key === c.key ? (s.dir === 1 ? { key: c.key, dir: -1 } : initialSort) : { key: c.key, dir: 1 }))}>
                        {c.label}
                        {sort?.key === c.key && <span aria-hidden="true">{sort.dir === 1 ? "▲" : "▼"}</span>}
                      </button>
                    ) : (
                      c.label
                    )}
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {first > 0 && (
              <tr aria-hidden="true">
                <td colSpan={columns.length} style={{ height: first * ROW, padding: 0, border: 0 }} />
              </tr>
            )}
            {rows.map(({ i, r }) =>
              r ? (
                <tr
                  key={rowKey(r)}
                  aria-rowindex={i + 1}
                  aria-selected={selectedKey === rowKey(r)}
                  onClick={onRowClick ? () => onRowClick(r) : undefined}
                  onKeyDown={onRowClick ? (e) => e.key === "Enter" && onRowClick(r) : undefined}
                  tabIndex={onRowClick ? 0 : undefined}
                  className={onRowClick ? "cursor-pointer" : ""}
                >
                  {columns.map((c) => (
                    <td key={c.key} style={{ textAlign: c.align || "left" }} className={`${c.align === "right" ? "tabular" : ""} ${c.mono ? "code" : ""}`}>
                      {cell(r, c)}
                    </td>
                  ))}
                </tr>
              ) : (
                <tr key={`loading-${i}`} aria-rowindex={i + 1} aria-busy="true">
                  <td colSpan={columns.length} className="text-slate-400">
                    …
                  </td>
                </tr>
              ),
            )}
            {last < n && (
              <tr aria-hidden="true">
                <td colSpan={columns.length} style={{ height: (n - last) * ROW, padding: 0, border: 0 }} />
              </tr>
            )}
          </tbody>
        </table>
        {total === 0 && <div className="p-6 text-center text-slate-600">{emptyText ?? t("No rows")}</div>}
      </div>
    </div>
  );
}
