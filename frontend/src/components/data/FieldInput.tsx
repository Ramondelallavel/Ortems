"use client";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { isoToLocalInput, localInputToIso } from "@/lib/format";

export type FieldDef = { name: string; type: string; required: boolean; ref: string | null; readonly: boolean; max_length: number | null };

type RefOption = { id: string; label: string };
const refCache = new Map<string, Promise<RefOption[]>>();

/** Options of a referenced table, fetched once per page load and shared by every cell. */
export function loadRefOptions(entity: string): Promise<RefOption[]> {
  let p = refCache.get(entity);
  if (!p) {
    p = api<any>(`/master-data/${entity}`, { query: { limit: 5000 } }).then((d) =>
      (d.items || []).map((o: any) => ({ id: o.id, label: [o.code || o.number || o.lot_number || o.name || o.id.slice(0, 8), o.code && o.name ? o.name : null].filter(Boolean).join(" · ") })),
    );
    p.catch(() => refCache.delete(entity));
    refCache.set(entity, p);
  }
  return p;
}

export function invalidateRefOptions(entity?: string) {
  if (entity) refCache.delete(entity);
  else refCache.clear();
}

export function RefSelect({ entity, value, label, onChange, compact }: { entity: string; value: string | null; label?: string; onChange: (v: string | null) => void; compact?: boolean }) {
  const [opts, setOpts] = useState<RefOption[] | null>(null);
  useEffect(() => {
    let live = true;
    loadRefOptions(entity)
      .then((o) => live && setOpts(o))
      .catch(() => live && setOpts([]));
    return () => {
      live = false;
    };
  }, [entity]);
  return (
    <select className={`mx-select w-full ${compact ? "!h-7 !text-[12px]" : ""}`} value={value || ""} onChange={(e) => onChange(e.target.value || null)} aria-label={entity}>
      <option value="">—</option>
      {!opts && value && <option value={value}>{label || value}</option>}
      {(opts || []).map((o) => (
        <option key={o.id} value={o.id}>
          {o.label}
        </option>
      ))}
    </select>
  );
}

/** One editable value, rendered by the field's type (text, number, date, reference…). */
export function FieldInput({ f, value, label, onChange, compact }: { f: FieldDef; value: any; label?: string; onChange: (v: any) => void; compact?: boolean }) {
  const cls = `mx-input w-full ${compact ? "!h-7 !text-[12px] !px-1.5" : ""}`;
  if (f.readonly) return <div className="text-slate-600 tabular">{String(value ?? "—")}</div>;
  if (f.type === "boolean") return <input type="checkbox" checked={!!value} onChange={(e) => onChange(e.target.checked)} aria-label={f.name} />;
  if (f.type === "integer" || f.type === "number")
    return <input className={`${cls} tabular`} type="number" step={f.type === "integer" ? 1 : "any"} value={value ?? ""} onChange={(e) => onChange(e.target.value === "" ? null : Number(e.target.value))} aria-label={f.name} />;
  if (f.type === "datetime") return <input className={cls} type="datetime-local" value={value ? isoToLocalInput(String(value)) : ""} onChange={(e) => onChange(e.target.value ? localInputToIso(e.target.value) : null)} aria-label={f.name} />;
  if (f.type === "date") return <input className={cls} type="date" value={value ? String(value).slice(0, 10) : ""} onChange={(e) => onChange(e.target.value || null)} aria-label={f.name} />;
  if (f.type === "time") return <input className={cls} type="time" value={value ? String(value).slice(0, 5) : ""} onChange={(e) => onChange(e.target.value || null)} aria-label={f.name} />;
  if (f.type === "json")
    return compact ? (
      <input className={`${cls} code`} value={typeof value === "string" ? value : JSON.stringify(value ?? null)} onChange={(e) => onChange(e.target.value)} aria-label={f.name} />
    ) : (
      <textarea className="mx-input w-full code" rows={3} value={typeof value === "string" ? value : JSON.stringify(value ?? {}, null, 1)} onChange={(e) => onChange(e.target.value)} aria-label={f.name} />
    );
  if (f.type === "uuid" && f.ref) return <RefSelect entity={f.ref} value={value} label={label} onChange={onChange} compact={compact} />;
  return <input className={cls} maxLength={f.max_length || undefined} value={value ?? ""} onChange={(e) => onChange(e.target.value)} aria-label={f.name} />;
}

/** JSON fields are edited as text; parse them back before saving. */
export function parseJsonFields(fields: FieldDef[], row: Record<string, any>): { ok: true; row: Record<string, any> } | { ok: false; field: string } {
  const out = { ...row };
  for (const f of fields) {
    if (f.type === "json" && typeof out[f.name] === "string") {
      const t = out[f.name].trim();
      if (!t) {
        out[f.name] = null;
        continue;
      }
      try {
        out[f.name] = JSON.parse(t);
      } catch {
        return { ok: false, field: f.name };
      }
    }
  }
  return { ok: true, row: out };
}

export function fieldLabel(name: string): string {
  return name.replace(/_id$/, "").replaceAll("_", " ");
}
