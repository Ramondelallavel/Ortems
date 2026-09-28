"use client";
import { Fragment, useMemo, useState } from "react";
import { Button, useConfirm, useToast } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { useSession } from "@/lib/session";
import { FieldInput, fieldLabel, invalidateRefOptions, parseJsonFields, type FieldDef } from "./FieldInput";

const PAGE = 150;
const HIDDEN = new Set(["tenant_id"]);

/**
 * Spreadsheet-style editing of a whole table: change any cell, add rows, delete selected rows,
 * then save once. Every change goes through the same API as the record editor (validation,
 * optimistic locking by version, audit); rows that fail keep their changes and show the reason.
 */
export function EditableGrid({ entity, fields, rows, onSaved, defaults }: { entity: string; fields: FieldDef[]; rows: any[]; onSaved: () => void; defaults?: Record<string, any> }) {
  const { t } = useSession();
  const toast = useToast();
  const { confirm, node } = useConfirm();
  const cols = useMemo(() => fields.filter((f) => !HIDDEN.has(f.name)), [fields]);
  const [edits, setEdits] = useState<Record<string, Record<string, any>>>({});
  const [added, setAdded] = useState<{ tmp: string; values: Record<string, any> }[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [filter, setFilter] = useState("");
  const [page, setPage] = useState(0);
  const [busy, setBusy] = useState(false);

  const visible = useMemo(() => {
    const q = filter.trim().toLowerCase();
    if (!q) return rows;
    return rows.filter((r) => cols.some((f) => String(r[f.name.replace(/_id$/, "") + "_label"] ?? r[f.name] ?? "").toLowerCase().includes(q)));
  }, [rows, cols, filter]);
  const pages = Math.max(1, Math.ceil(visible.length / PAGE));
  const slice = visible.slice(page * PAGE, page * PAGE + PAGE);
  const dirty = Object.keys(edits).length + added.length;

  const setCell = (id: string, name: string, v: any) => setEdits((e) => ({ ...e, [id]: { ...(e[id] || {}), [name]: v } }));
  const setNew = (tmp: string, name: string, v: any) => setAdded((a) => a.map((r) => (r.tmp === tmp ? { ...r, values: { ...r.values, [name]: v } } : r)));
  const toggle = (id: string) => setSelected((s) => {
    const n = new Set(s);
    if (n.has(id)) n.delete(id);
    else n.add(id);
    return n;
  });

  const save = async () => {
    setBusy(true);
    const errs: Record<string, string> = {};
    let ok = 0;
    for (const [id, ch] of Object.entries(edits)) {
      const row = rows.find((r) => r.id === id);
      const p = parseJsonFields(cols, ch);
      if (!p.ok) {
        errs[id] = `${p.field}: ${t("grid.badJson")}`;
        continue;
      }
      try {
        await api(`/master-data/${entity}/${id}`, { method: "PUT", body: { ...p.row, version: row?.version } });
        ok++;
      } catch (e) {
        errs[id] = e instanceof ApiError ? e.message : String(e);
      }
    }
    const keepNew: typeof added = [];
    for (const r of added) {
      const p = parseJsonFields(cols, r.values);
      if (!p.ok) {
        errs[r.tmp] = `${p.field}: ${t("grid.badJson")}`;
        keepNew.push(r);
        continue;
      }
      try {
        await api(`/master-data/${entity}`, { body: { ...(defaults || {}), ...p.row } });
        ok++;
      } catch (e) {
        errs[r.tmp] = e instanceof ApiError ? e.message : String(e);
        keepNew.push(r);
      }
    }
    setEdits(Object.fromEntries(Object.entries(edits).filter(([id]) => errs[id])));
    setAdded(keepNew);
    setErrors(errs);
    setBusy(false);
    invalidateRefOptions(entity);
    if (ok) onSaved();
    if (Object.keys(errs).length) toast.error(new Error(t("grid.someFailed", { n: Object.keys(errs).length, ok })));
    else toast.ok(t("grid.saved", { n: ok }));
  };

  const delSelected = async () => {
    const r = await confirm(t("grid.deleteQ", { n: selected.size }), { danger: true, reason: true, body: t("grid.deleteBody") });
    if (!r.ok) return;
    setBusy(true);
    let deleted = 0,
      deactivated = 0;
    const errs: Record<string, string> = {};
    for (const id of selected) {
      try {
        const out = await api(`/master-data/${entity}/${id}`, { method: "DELETE", query: { reason: r.reason } });
        if (out.deactivated) deactivated++;
        else deleted++;
      } catch (e) {
        errs[id] = e instanceof ApiError ? e.message : String(e);
      }
    }
    setBusy(false);
    setSelected(new Set());
    setErrors(errs);
    invalidateRefOptions(entity);
    onSaved();
    toast.ok(t("grid.deleted", { d: deleted, x: deactivated }));
  };

  const cell = (f: FieldDef, value: any, label: string | undefined, onChange: (v: any) => void) => (
    <td key={f.name} className="!p-0.5 min-w-[120px]">
      <FieldInput compact f={f} value={value} label={label} onChange={onChange} />
    </td>
  );

  return (
    <div className="flex flex-col h-full min-h-0">
      <div className="flex flex-wrap items-center gap-2 px-2 py-1.5 border-b border-gray-200 bg-white">
        <input className="mx-input w-[200px]" placeholder={t("grid.filter")} aria-label={t("grid.filter")} value={filter} onChange={(e) => { setFilter(e.target.value); setPage(0); }} />
        <Button size="sm" icon="plus" onClick={() => setAdded((a) => [...a, { tmp: `new-${Date.now()}-${a.length}`, values: {} }])}>
          {t("grid.addRow")}
        </Button>
        <Button size="sm" variant="danger" disabled={!selected.size || busy} onClick={delSelected}>
          {t("grid.deleteSel", { n: selected.size })}
        </Button>
        <div className="flex-1" />
        {pages > 1 && (
          <span className="text-[12px] text-slate-600 tabular">
            <button className="px-1" disabled={page === 0} onClick={() => setPage(page - 1)} aria-label="previous page">
              ‹
            </button>
            {page + 1} / {pages}
            <button className="px-1" disabled={page >= pages - 1} onClick={() => setPage(page + 1)} aria-label="next page">
              ›
            </button>
          </span>
        )}
        <span className="text-[12px] text-slate-600">{dirty ? t("grid.pending", { n: dirty }) : t("grid.noChanges")}</span>
        <Button size="sm" disabled={!dirty || busy} onClick={() => { setEdits({}); setAdded([]); setErrors({}); }}>
          {t("grid.discard")}
        </Button>
        <Button size="sm" variant="primary" busy={busy} disabled={!dirty} onClick={save}>
          {t("common.save")}
        </Button>
      </div>
      <div className="flex-1 min-h-0 overflow-auto mx-scroll bg-white">
        <table className="mx-table">
          <thead className="sticky top-0 z-10 bg-gray-50">
            <tr>
              <th className="w-8">
                <input type="checkbox" aria-label={t("grid.selectAll")} checked={slice.length > 0 && slice.every((r) => selected.has(r.id))} onChange={(e) => setSelected(e.target.checked ? new Set([...selected, ...slice.map((r) => r.id)]) : new Set([...selected].filter((id) => !slice.some((r) => r.id === id))))} />
              </th>
              {cols.map((f) => (
                <th key={f.name} title={f.name}>
                  {fieldLabel(f.name)}
                  {f.required ? " *" : ""}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {added.map((r) => (
              <Fragment key={r.tmp}>
                <tr className="bg-green-50">
                  <td className="text-green-600 text-center">+</td>
                  {cols.map((f) => cell(f, r.values[f.name] ?? defaults?.[f.name], undefined, (v) => setNew(r.tmp, f.name, v)))}
                </tr>
                {errors[r.tmp] && (
                  <tr>
                    <td colSpan={cols.length + 1} className="text-red-600 text-[12px]">
                      ▲ {errors[r.tmp]}
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
            {slice.map((r) => {
              const ch = edits[r.id] || {};
              return (
                <Fragment key={r.id}>
                  <tr className={edits[r.id] ? "bg-amber-50" : selected.has(r.id) ? "bg-blue-50" : ""}>
                    <td>
                      <input type="checkbox" aria-label={`select ${r.code || r.number || r.id}`} checked={selected.has(r.id)} onChange={() => toggle(r.id)} />
                    </td>
                    {cols.map((f) => cell(f, f.name in ch ? ch[f.name] : r[f.name], r[f.name.replace(/_id$/, "") + "_label"], (v) => setCell(r.id, f.name, v)))}
                  </tr>
                  {errors[r.id] && (
                    <tr>
                      <td colSpan={cols.length + 1} className="text-red-600 text-[12px]">
                        ▲ {errors[r.id]}
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
      {node}
    </div>
  );
}

/** Editable child rows inside a record (BOM lines, shifts, routing operations…). */
export function ChildGrid({ fields, rows, onChange }: { fields: FieldDef[]; rows: any[]; onChange: (rows: any[]) => void }) {
  const { t } = useSession();
  const cols = fields.filter((f) => !HIDDEN.has(f.name));
  return (
    <div className="space-y-2">
      <div className="overflow-x-auto border border-gray-200">
        <table className="mx-table">
          <thead>
            <tr>
              {cols.map((f) => (
                <th key={f.name}>
                  {fieldLabel(f.name)}
                  {f.required ? " *" : ""}
                </th>
              ))}
              <th />
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={r.id || `n${i}`}>
                {cols.map((f) => (
                  <td key={f.name} className="!p-0.5 min-w-[110px]">
                    <FieldInput compact f={f} value={r[f.name]} label={r[f.name.replace(/_id$/, "") + "_label"]} onChange={(v) => onChange(rows.map((x, j) => (j === i ? { ...x, [f.name]: v } : x)))} />
                  </td>
                ))}
                <td>
                  <button className="text-red-600 px-1" aria-label={t("grid.removeRow")} title={t("grid.removeRow")} onClick={() => onChange(rows.filter((_, j) => j !== i))}>
                    ✕
                  </button>
                </td>
              </tr>
            ))}
            {!rows.length && (
              <tr>
                <td colSpan={cols.length + 1} className="text-slate-600">
                  {t("grid.noRows")}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <Button size="sm" icon="plus" onClick={() => onChange([...rows, {}])}>
        {t("grid.addRow")}
      </Button>
    </div>
  );
}
