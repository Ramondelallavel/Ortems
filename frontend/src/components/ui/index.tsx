"use client";
// MonxuPlan component library: dense, keyboard friendly, colour never the only status signal.
import { cloneElement, createContext, isValidElement, useCallback, useContext, useEffect, useId, useMemo, useRef, useState, type ButtonHTMLAttributes, type ReactElement, type ReactNode } from "react";
import { ApiError } from "@/lib/api";
import { useSession } from "@/lib/session";
import { Icon } from "./Icon";
import { saveBlob } from "@/lib/save";

export { Icon };

// ------------------------------------------------------------------ Button
type BtnProps = ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "secondary" | "ghost" | "danger"; size?: "sm" | "md"; icon?: string; busy?: boolean };
export function Button({ variant = "secondary", size = "md", icon, busy, children, className = "", disabled, ...rest }: BtnProps) {
  const v = variant === "primary" ? "mx-btn-primary" : variant === "danger" ? "mx-btn-danger" : variant === "ghost" ? "mx-btn-ghost" : "";
  return (
    <button type="button" className={`mx-btn ${v} ${size === "sm" ? "mx-btn-sm" : ""} ${className}`} disabled={disabled || busy} aria-busy={busy || undefined} {...rest}>
      {busy ? <Spinner size={12} /> : icon ? <Icon name={icon} size={size === "sm" ? 13 : 15} /> : null}
      {children}
    </button>
  );
}

export function Spinner({ size = 14 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" className="animate-spin" aria-hidden="true">
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeOpacity="0.25" strokeWidth="3" fill="none" />
      <path d="M21 12a9 9 0 00-9-9" stroke="currentColor" strokeWidth="3" fill="none" strokeLinecap="round" />
    </svg>
  );
}

// ------------------------------------------------------------------ Panel
export function Panel({ title, actions, children, className = "", bodyClass = "", id }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string; bodyClass?: string; id?: string }) {
  return (
    <section className={`mx-panel flex flex-col min-h-0 ${className}`} aria-labelledby={title && id ? `${id}-h` : undefined}>
      {title !== undefined && (
        <header className="mx-panel-head">
          <h2 id={id ? `${id}-h` : undefined} className="flex-1 truncate">
            {title}
          </h2>
          {actions}
        </header>
      )}
      <div className={`min-h-0 ${bodyClass}`}>{children}</div>
    </section>
  );
}

// ------------------------------------------------------------------ Status
const TONES = {
  ok: "bg-green-100 text-green-600 border-green-600/30",
  warn: "bg-amber-100 text-amber-600 border-amber-500/40",
  bad: "bg-red-100 text-red-600 border-red-600/30",
  info: "bg-blue-100 text-blue-500 border-blue-500/30",
  neutral: "bg-gray-100 text-slate-600 border-gray-300",
  dark: "bg-navy-900 text-white border-navy-900",
} as const;
export type Tone = keyof typeof TONES;
const GLYPH: Record<Tone, string> = { ok: "●", warn: "◆", bad: "▲", info: "■", neutral: "○", dark: "●" };

export function Badge({ tone = "neutral", children, glyph = true, title }: { tone?: Tone; children: ReactNode; glyph?: boolean; title?: string }) {
  return (
    <span title={title} className={`inline-flex items-center gap-1 h-[18px] px-1.5 rounded-[3px] border text-[11px] font-semibold whitespace-nowrap ${TONES[tone]}`}>
      {glyph && <span aria-hidden="true" className="text-[9px] leading-none">{GLYPH[tone]}</span>}
      {children}
    </span>
  );
}

export function statusTone(s: string | null | undefined): Tone {
  switch (s) {
    case "ON_TIME":
    case "OK":
    case "DONE":
    case "SUCCEEDED":
    case "PUBLISHED":
    case "COMPLETED":
    case "AVAILABLE":
    case "IMPORTED":
    case "VALIDATED":
    case "UNDERLOADED":
    case "BALANCED":
      return "ok";
    case "RISK":
    case "WARNING":
    case "HIGH_LOAD":
    case "HIGH_UTILIZATION":
    case "QUEUE":
    case "PARTIAL":
    case "LATE_SUPPLY":
    case "MAINTENANCE":
    case "DRAFT":
    case "RUNNING":
    case "QUEUED":
      return "warn";
    case "LATE":
    case "UNSCHEDULED":
    case "SHORTAGE":
    case "CRITICAL":
    case "FAILED":
    case "DOWN":
    case "OVERLOADED":
    case "INVALID":
    case "HARD":
      return "bad";
    case "INFO":
    case "IN_PROGRESS":
    case "RELEASED":
      return "info";
    default:
      return "neutral";
  }
}

export function StatusPill({ status, label }: { status: string | null | undefined; label?: string }) {
  const { t } = useSession();
  if (!status) return <span className="text-slate-400">—</span>;
  const tr = t(`status.${status}`);
  return <Badge tone={statusTone(status)}>{label || (tr.startsWith("status.") ? status.replaceAll("_", " ").toLowerCase() : tr)}</Badge>;
}

// ------------------------------------------------------------------ KPI
export function Kpi({ label, value, unit, delta, trend, onClick, hint }: { label: string; value: ReactNode; unit?: string; delta?: string | null; trend?: "better" | "worse" | null; onClick?: () => void; hint?: string }) {
  const Tag = onClick ? "button" : "div";
  return (
    <Tag onClick={onClick} title={hint} className={`mx-panel text-left px-3 py-2 min-w-[120px] ${onClick ? "hover:border-navy-600 cursor-pointer" : ""}`}>
      <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-600 truncate">{label}</div>
      <div className="flex items-baseline gap-1 mt-0.5">
        <span className="text-[20px] font-semibold tabular text-graphite-800">{value}</span>
        {unit && <span className="text-slate-600 text-[12px]">{unit}</span>}
      </div>
      {delta && (
        <div className={`text-[11px] tabular ${trend === "better" ? "text-green-600" : trend === "worse" ? "text-red-600" : "text-slate-600"}`}>
          <span aria-hidden="true">{trend === "better" ? "✓" : trend === "worse" ? "!" : ""}</span> {delta}
          {trend && <span className="sr-only"> ({trend})</span>}
        </div>
      )}
    </Tag>
  );
}

// ------------------------------------------------------------------ states
export function Loading({ label }: { label?: string }) {
  const { t } = useSession();
  return (
    <div role="status" className="flex items-center gap-2 p-4 text-slate-600">
      <Spinner /> {label || t("common.loading")}
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: ApiError | Error | undefined; onRetry?: () => void }) {
  const { t } = useSession();
  if (!error) return null;
  const e = error as ApiError;
  if (e.status === 403) return <NoPermission message={e.message} />;
  return (
    <div role="alert" className="m-3 p-3 border border-red-600/30 bg-red-100 rounded-[3px] text-[13px]">
      <div className="font-semibold text-red-600 flex items-center gap-1.5">
        <Icon name="alert" /> {e.code === "OFFLINE" ? t("common.offline") : e.code || "Error"}
      </div>
      <div className="mt-1 text-graphite-800">{e.message}</div>
      {onRetry && (
        <Button size="sm" className="mt-2" icon="refresh" onClick={onRetry}>
          {t("common.retry")}
        </Button>
      )}
    </div>
  );
}

export function NoPermission({ message }: { message?: string }) {
  const { t } = useSession();
  return (
    <div className="m-3 p-3 border border-gray-300 bg-gray-100 rounded-[3px] flex items-center gap-2 text-slate-600">
      <Icon name="lock" /> {message || t("common.noPermission")}
    </div>
  );
}

export function Empty({ title, children, icon = "info" }: { title: string; children?: ReactNode; icon?: string }) {
  return (
    <div className="flex flex-col items-center justify-center text-center p-8 text-slate-600 gap-2">
      <Icon name={icon} size={22} />
      <div className="font-semibold text-graphite-800">{title}</div>
      {children && <div className="max-w-md text-[12.5px]">{children}</div>}
    </div>
  );
}

export function ComingSoon({ feature }: { feature: string }) {
  const { t } = useSession();
  return (
    <span className="inline-flex items-center gap-1 text-[11px] text-slate-600 border border-dashed border-gray-300 rounded-[3px] px-1.5 h-[18px]" title={`${feature}: ${t("common.comingSoon")}`}>
      <Icon name="clock" size={11} /> {t("common.comingSoon")}
    </span>
  );
}

export function Load<T>({ state, children, empty }: { state: { data: T | undefined; error?: ApiError; loading: boolean; reload: () => void }; children: (d: T) => ReactNode; empty?: (d: T) => boolean }) {
  if (state.error && !state.data) return <ErrorState error={state.error} onRetry={state.reload} />;
  if (state.data === undefined) return <Loading />;
  if (empty && empty(state.data)) return <Empty title="No data" />;
  return <>{children(state.data)}</>;
}

// ------------------------------------------------------------------ Tabs
export function Tabs({ tabs, value, onChange }: { tabs: { id: string; label: ReactNode; badge?: ReactNode }[]; value: string; onChange: (id: string) => void }) {
  return (
    <div role="tablist" className="flex border-b border-gray-200 bg-white px-2 gap-1 overflow-x-auto">
      {tabs.map((tb) => (
        <button
          key={tb.id}
          role="tab"
          aria-selected={value === tb.id}
          onClick={() => onChange(tb.id)}
          className={`h-8 px-3 text-[12.5px] font-medium border-b-2 -mb-px whitespace-nowrap flex items-center gap-1.5 ${value === tb.id ? "border-navy-700 text-navy-700" : "border-transparent text-slate-600 hover:text-graphite-800"}`}
        >
          {tb.label}
          {tb.badge}
        </button>
      ))}
    </div>
  );
}

// ------------------------------------------------------------------ fields
export function Field({ label, children, hint, error }: { label: string; children: ReactNode; hint?: string; error?: string }) {
  const id = useId();
  const hintId = `${id}-hint`;
  // the label names the control: a single input/select/textarea/<Select> child receives the id;
  // several controls (e.g. a select plus a number box) are named as a group
  const single =
    isValidElement(children) &&
    (typeof children.type === "string" ? ["input", "select", "textarea"].includes(children.type) : children.type === Select || !!(children.type as { acceptsId?: boolean }).acceptsId);
  const extra = hint || error ? { "aria-describedby": hintId, ...(error ? { "aria-invalid": true } : {}) } : {};
  const el = single ? cloneElement(children as ReactElement<Record<string, unknown>>, { id: ((children as ReactElement<{ id?: string }>).props.id as string) || id, ...extra }) : null;
  const ctlId = el ? (el.props as { id: string }).id : undefined;
  return (
    <div className="min-w-0">
      {el ? (
        <label className="mx-label" htmlFor={ctlId}>
          {label}
        </label>
      ) : (
        <div className="mx-label" id={`${id}-label`}>
          {label}
        </div>
      )}
      {el || (
        <div role="group" aria-labelledby={`${id}-label`}>
          {children}
        </div>
      )}
      {hint && !error && (
        <div id={hintId} className="text-[11px] text-slate-600 mt-0.5">
          {hint}
        </div>
      )}
      {error && (
        <div id={hintId} role="alert" className="text-[11px] text-red-600 mt-0.5">
          ▲ {error}
        </div>
      )}
    </div>
  );
}

export function Select({ value, onChange, options, className = "", ariaLabel, disabled, id, ...aria }: { value: string; onChange: (v: string) => void; options: { value: string; label: string }[]; className?: string; ariaLabel?: string; disabled?: boolean; id?: string; "aria-describedby"?: string; "aria-invalid"?: boolean }) {
  return (
    <select id={id} aria-label={ariaLabel} {...aria} className={`mx-select ${className}`} value={value} onChange={(e) => onChange(e.target.value)} disabled={disabled}>
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );
}

// ------------------------------------------------------------------ Dialog / Drawer
// open dialogs, innermost last: keyboard handling (Escape, focus trap) belongs to the top one only
const dialogStack: object[] = [];

export function Dialog({ open, title, onClose, children, footer, width = 520 }: { open: boolean; title: ReactNode; onClose: () => void; children: ReactNode; footer?: ReactNode; width?: number }) {
  const ref = useRef<HTMLDivElement>(null);
  // callers often pass an inline onClose: keep the latest one without re-running the open effect
  // (re-running it would move the focus back to the first field on every keystroke)
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    if (!open) return;
    const me = {};
    dialogStack.push(me);
    const prev = document.activeElement as HTMLElement | null;
    const el = ref.current;
    const first = el?.querySelector<HTMLElement>("input,select,textarea,button:not([data-close])");
    (first || el)?.focus();
    const h = (e: KeyboardEvent) => {
      if (dialogStack[dialogStack.length - 1] !== me) return;
      if (e.key === "Escape") {
        e.stopPropagation();
        closeRef.current();
      }
      if (e.key === "Tab" && el) {
        const f = Array.from(el.querySelectorAll<HTMLElement>("a,button,input,select,textarea,[tabindex]:not([tabindex='-1'])")).filter((x) => !x.hasAttribute("disabled"));
        if (!f.length) return;
        if (e.shiftKey && document.activeElement === f[0]) {
          e.preventDefault();
          f[f.length - 1].focus();
        } else if (!e.shiftKey && document.activeElement === f[f.length - 1]) {
          e.preventDefault();
          f[0].focus();
        }
      }
    };
    document.addEventListener("keydown", h);
    return () => {
      document.removeEventListener("keydown", h);
      const i = dialogStack.indexOf(me);
      if (i >= 0) dialogStack.splice(i, 1);
      prev?.focus();
    };
  }, [open]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center pt-[8vh] bg-navy-950/40" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div ref={ref} role="dialog" aria-modal="true" aria-label={typeof title === "string" ? title : undefined} tabIndex={-1} className="bg-white rounded-[4px] shadow-2xl max-h-[84vh] flex flex-col max-w-[96vw]" style={{ width }}>
        <div className="flex items-center h-10 px-3 border-b border-gray-200 font-semibold">
          <div className="flex-1 truncate">{title}</div>
          <button data-close className="mx-btn mx-btn-ghost mx-btn-sm" aria-label="Close" onClick={onClose}>
            <Icon name="x" />
          </button>
        </div>
        <div className="p-3 overflow-auto mx-scroll">{children}</div>
        {footer && <div className="flex justify-end gap-2 px-3 py-2 border-t border-gray-200 bg-gray-50">{footer}</div>}
      </div>
    </div>
  );
}

export function Drawer({ open, title, onClose, children, width = 440 }: { open: boolean; title: ReactNode; onClose: () => void; children: ReactNode; width?: number }) {
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    if (!open) return;
    // a dialog opened from the drawer handles Escape first
    const h = (e: KeyboardEvent) => e.key === "Escape" && !dialogStack.length && closeRef.current();
    document.addEventListener("keydown", h);
    return () => document.removeEventListener("keydown", h);
  }, [open]);
  if (!open) return null;
  return (
    <aside role="complementary" aria-label={typeof title === "string" ? title : undefined} className="fixed top-11 right-0 bottom-0 z-40 bg-white border-l border-gray-200 shadow-xl flex flex-col" style={{ width, maxWidth: "100vw" }}>
      <div className="flex items-center h-9 px-3 border-b border-gray-200 bg-gray-100 font-semibold text-[12.5px]">
        <div className="flex-1 truncate">{title}</div>
        <button className="mx-btn mx-btn-ghost mx-btn-sm" aria-label="Close" onClick={onClose}>
          <Icon name="x" />
        </button>
      </div>
      <div className="flex-1 overflow-auto mx-scroll">{children}</div>
    </aside>
  );
}

// ------------------------------------------------------------------ Toasts
type Toast = { id: number; tone: Tone; text: string };
const ToastCtx = createContext<(tone: Tone, text: string) => void>(() => {});
export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);
  const push = useCallback((tone: Tone, text: string) => {
    const id = Date.now() + Math.random();
    setItems((x) => [...x.slice(-3), { id, tone, text }]);
    setTimeout(() => setItems((x) => x.filter((t) => t.id !== id)), tone === "bad" ? 9000 : 5000);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div aria-live="polite" className="fixed bottom-3 right-3 z-[60] flex flex-col gap-2 w-[380px] max-w-[92vw]">
        {items.map((t) => (
          <div key={t.id} role={t.tone === "bad" ? "alert" : "status"} className={`border rounded-[3px] px-3 py-2 shadow-lg bg-white text-[12.5px] flex gap-2 ${t.tone === "bad" ? "border-red-600" : t.tone === "ok" ? "border-green-600" : t.tone === "warn" ? "border-amber-500" : "border-blue-500"}`}>
            <span aria-hidden="true" className={t.tone === "bad" ? "text-red-600" : t.tone === "ok" ? "text-green-600" : t.tone === "warn" ? "text-amber-600" : "text-blue-500"}>
              {GLYPH[t.tone]}
            </span>
            <span className="flex-1">{t.text}</span>
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}
export function useToast() {
  const push = useContext(ToastCtx);
  return useMemo(
    () => ({
      ok: (s: string) => push("ok", s),
      warn: (s: string) => push("warn", s),
      info: (s: string) => push("info", s),
      error: (e: unknown) => push("bad", e instanceof Error ? e.message : String(e)),
    }),
    [push],
  );
}

// ------------------------------------------------------------------ DataTable (sort, filter, virtual rows)
export type Column<T> = { key: string; label: string; width?: number; align?: "left" | "right" | "center"; render?: (row: T) => ReactNode; value?: (row: T) => string | number | null | undefined; sortable?: boolean; mono?: boolean };

export function DataTable<T extends Record<string, any>>({ rows, columns, rowKey, onRowClick, selectedKey, height, filterable = true, initialSort, emptyText = "No rows", toolbar, exportName }: {
  rows: T[];
  columns: Column<T>[];
  rowKey: (r: T) => string;
  onRowClick?: (r: T) => void;
  selectedKey?: string | null;
  height?: number | string;
  filterable?: boolean;
  initialSort?: { key: string; dir: 1 | -1 };
  emptyText?: string;
  toolbar?: ReactNode;
  exportName?: string;
}) {
  const [q, setQ] = useState("");
  const [sort, setSort] = useState<{ key: string; dir: 1 | -1 } | undefined>(initialSort);
  const scroller = useRef<HTMLDivElement>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewH, setViewH] = useState(600);
  const ROW = 26;

  const val = useCallback((r: T, c: Column<T>) => (c.value ? c.value(r) : r[c.key]), []);
  const filtered = useMemo(() => {
    let out = rows;
    if (q.trim()) {
      const terms = q.toLowerCase().split(/\s+/).filter(Boolean);
      out = out.filter((r) => {
        const hay = columns.map((c) => String(val(r, c) ?? "")).join(" ").toLowerCase();
        return terms.every((t) => hay.includes(t));
      });
    }
    if (sort) {
      const c = columns.find((x) => x.key === sort.key);
      if (c) {
        out = [...out].sort((a, b) => {
          const va = val(a, c);
          const vb = val(b, c);
          if (va === vb) return 0;
          if (va === null || va === undefined || va === "") return 1;
          if (vb === null || vb === undefined || vb === "") return -1;
          return (va > vb ? 1 : -1) * sort.dir;
        });
      }
    }
    return out;
  }, [rows, q, sort, columns, val]);

  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setViewH(el.clientHeight));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const virtual = filtered.length > 200;
  const first = virtual ? Math.max(0, Math.floor(scrollTop / ROW) - 10) : 0;
  const last = virtual ? Math.min(filtered.length, Math.ceil((scrollTop + viewH) / ROW) + 10) : filtered.length;
  const slice = filtered.slice(first, last);

  const exportCsv = () => {
    const esc = (v: unknown) => `"${String(v ?? "").replaceAll('"', '""')}"`;
    const lines = [columns.map((c) => esc(c.label)).join(","), ...filtered.map((r) => columns.map((c) => esc(val(r, c))).join(","))];
    const blob = new Blob(["﻿" + lines.join("\n")], { type: "text/csv" });
    saveBlob(blob, `${exportName || "export"}.csv`);
  };

  return (
    <div className="flex flex-col min-h-0 h-full">
      {(filterable || toolbar) && (
        <div className="flex items-center gap-2 px-2 py-1.5 border-b border-gray-200 bg-white">
          {filterable && (
            <div className="relative">
              <Icon name="search" size={13} className="absolute left-2 top-[7px] text-slate-400" />
              <input className="mx-input pl-7 w-[240px]" placeholder="Filter…" aria-label="Filter rows" value={q} onChange={(e) => setQ(e.target.value)} />
            </div>
          )}
          <span className="text-[11.5px] text-slate-600 tabular">
            {filtered.length === rows.length ? rows.length : `${filtered.length} / ${rows.length}`} rows
          </span>
          <div className="flex-1" />
          {toolbar}
          {exportName && (
            <Button size="sm" variant="ghost" icon="download" onClick={exportCsv} title="Export visible rows (CSV)">
              CSV
            </Button>
          )}
        </div>
      )}
      <div ref={scroller} className="overflow-auto mx-scroll flex-1 min-h-0" style={{ height }} onScroll={(e) => setScrollTop((e.target as HTMLDivElement).scrollTop)}>
        <table className="mx-table" role="grid" aria-rowcount={filtered.length}>
          <thead>
            <tr>
              {columns.map((c) => (
                <th key={c.key} style={{ width: c.width, textAlign: c.align || "left" }} aria-sort={sort?.key === c.key ? (sort.dir === 1 ? "ascending" : "descending") : "none"}>
                  {c.sortable === false ? (
                    c.label
                  ) : (
                    <button className="inline-flex items-center gap-1 hover:text-graphite-800" onClick={() => setSort((s) => (s?.key === c.key ? (s.dir === 1 ? { key: c.key, dir: -1 } : undefined) : { key: c.key, dir: 1 }))}>
                      {c.label}
                      {sort?.key === c.key && <span aria-hidden="true">{sort.dir === 1 ? "▲" : "▼"}</span>}
                    </button>
                  )}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {virtual && first > 0 && (
              <tr aria-hidden="true">
                <td colSpan={columns.length} style={{ height: first * ROW, padding: 0, border: 0 }} />
              </tr>
            )}
            {slice.map((r) => {
              const k = rowKey(r);
              return (
                <tr
                  key={k}
                  aria-selected={selectedKey === k}
                  onClick={onRowClick ? () => onRowClick(r) : undefined}
                  onKeyDown={onRowClick ? (e) => e.key === "Enter" && onRowClick(r) : undefined}
                  tabIndex={onRowClick ? 0 : undefined}
                  className={onRowClick ? "cursor-pointer" : ""}
                >
                  {columns.map((c) => (
                    <td key={c.key} style={{ textAlign: c.align || "left" }} className={`${c.align === "right" ? "tabular" : ""} ${c.mono ? "code" : ""}`}>
                      {c.render ? c.render(r) : (val(r, c) ?? "—")}
                    </td>
                  ))}
                </tr>
              );
            })}
            {virtual && last < filtered.length && (
              <tr aria-hidden="true">
                <td colSpan={columns.length} style={{ height: (filtered.length - last) * ROW, padding: 0, border: 0 }} />
              </tr>
            )}
          </tbody>
        </table>
        {!filtered.length && <div className="p-6 text-center text-slate-600">{emptyText}</div>}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ Confirm with reason
export function useConfirm() {
  const [state, setState] = useState<{ title: string; body?: ReactNode; danger?: boolean; reason?: boolean; resolve: (r: { ok: boolean; reason?: string }) => void } | null>(null);
  const [reason, setReason] = useState("");
  const confirm = (title: string, opts: { body?: ReactNode; danger?: boolean; reason?: boolean } = {}) =>
    new Promise<{ ok: boolean; reason?: string }>((resolve) => {
      setReason("");
      setState({ title, ...opts, resolve });
    });
  const close = (ok: boolean) => {
    state?.resolve({ ok, reason: reason || undefined });
    setState(null);
  };
  const node = (
    <Dialog
      open={!!state}
      title={state?.title || ""}
      onClose={() => close(false)}
      footer={
        <>
          <Button onClick={() => close(false)}>Cancel</Button>
          <Button variant={state?.danger ? "danger" : "primary"} onClick={() => close(true)} disabled={!!state?.reason && !reason.trim()}>
            Confirm
          </Button>
        </>
      }
    >
      {state?.body}
      {state?.reason && (
        <div className="mt-2">
          <label className="mx-label" htmlFor="confirm-reason">
            Reason (recorded in the audit log)
          </label>
          <textarea id="confirm-reason" className="mx-input w-full" rows={3} value={reason} onChange={(e) => setReason(e.target.value)} />
        </div>
      )}
    </Dialog>
  );
  return { confirm, node };
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="mx-kbd">{children}</kbd>;
}

export function ProgressBar({ value, indeterminate }: { value?: number; indeterminate?: boolean }) {
  return (
    <div className="h-1.5 bg-gray-200 rounded overflow-hidden relative" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={value !== undefined ? Math.round(value * 100) : undefined}>
      {indeterminate ? <div className="absolute inset-y-0 w-1/3 bg-navy-700 mx-indeterminate" /> : <div className="h-full bg-navy-700 transition-all" style={{ width: `${Math.round((value || 0) * 100)}%` }} />}
    </div>
  );
}

export function PageHeader({ title, subtitle, actions }: { title: ReactNode; subtitle?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="flex items-center gap-3 px-4 h-12 border-b border-gray-200 bg-white">
      <div className="min-w-0 flex-1">
        <h1 className="text-[15px] font-semibold truncate">{title}</h1>
        {subtitle && <div className="text-[11.5px] text-slate-600 truncate">{subtitle}</div>}
      </div>
      <div className="flex items-center gap-2">{actions}</div>
    </div>
  );
}
