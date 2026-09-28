"use client";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { Badge, Button, DataTable, Drawer, ErrorState, Field, Loading, PageHeader, Tabs, useConfirm, useToast, type Column } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { dt, isoToLocalInput, localInputToIso } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import { useSession } from "@/lib/session";

type FieldDef = { name: string; type: string; required: boolean; ref: string | null; readonly: boolean; max_length: number | null };
const HIDDEN = new Set(["tenant_id"]);
const ALIASES: Record<string, string> = { bom: "boms", resources: "resources", "setup-matrices": "setup-matrices" };

export default function EntityPage() {
  const params = useParams<{ entity: string }>();
  const entity = ALIASES[params.entity] || params.entity;
  const { plant, can } = useSession();
  const toast = useToast();
  const schema = useApi<any>(`/master-data/${entity}/schema`);
  const [q, setQ] = useState("");
  const list = useApi<any>(`/master-data/${entity}`, { limit: 5000, plant_id: plant?.id, q: q || undefined });
  const [edit, setEdit] = useState<any | null>(null);

  const fields: FieldDef[] = schema.data?.fields || [];
  const cols: Column<any>[] = useMemo(() => {
    const show = fields.filter((f) => !HIDDEN.has(f.name) && f.type !== "json" && !["description", "notes", "instructions"].includes(f.name)).slice(0, 12);
    return show.map((f) => ({
      key: f.name,
      label: f.name.replace(/_id$/, "").replaceAll("_", " "),
      mono: f.name === "code" || f.name === "number",
      align: f.type === "integer" || f.type === "number" ? "right" : undefined,
      value: (r: any) => (f.type === "uuid" ? r[f.name.replace(/_id$/, "") + "_label"] ?? r[f.name] : r[f.name]),
      render: (r: any) => {
        const v = r[f.name];
        if (f.type === "boolean") return v ? "✓" : "—";
        if (f.type === "datetime") return dt(v);
        if (f.type === "uuid") return <span className="code">{r[f.name.replace(/_id$/, "") + "_label"] ?? (v ? String(v).slice(0, 8) : "—")}</span>;
        return v === null || v === undefined ? "—" : String(v);
      },
    }));
  }, [fields]);

  const writable = schema.data && (can("masterdata:write") || can("orders:write") || can("admin:config"));
  return (
    <div className="flex flex-col h-full min-h-0">
      <PageHeader
        title={schema.data?.label ? `${schema.data.label}s` : entity}
        subtitle={
          <>
            <Link href="/master-data" className="text-blue-500 hover:underline">
              Master data
            </Link>{" "}
            · {list.data ? `${list.data.total} records` : ""}
          </>
        }
        actions={
          <>
            <input className="mx-input w-[220px]" placeholder="Server search (code, name…)" aria-label="Search" value={q} onChange={(e) => setQ(e.target.value)} />
            {writable && (
              <Button variant="primary" icon="plus" onClick={() => setEdit({})}>
                New
              </Button>
            )}
          </>
        }
      />
      <div className="flex-1 min-h-0 bg-white">
        {(schema.error || list.error) && <ErrorState error={schema.error || list.error} onRetry={list.reload} />}
        {!list.data || !schema.data ? <Loading /> : <DataTable rows={list.data.items} columns={cols} rowKey={(r) => r.id} onRowClick={(r) => setEdit(r)} selectedKey={edit?.id} exportName={entity} filterable />}
      </div>
      <Drawer open={!!edit} onClose={() => setEdit(null)} title={edit?.id ? `${schema.data?.label}: ${edit.code || edit.number || edit.name || edit.id.slice(0, 8)}` : `New ${schema.data?.label || ""}`} width={520}>
        {edit && schema.data && (
          <Editor
            entity={entity}
            schema={schema.data}
            id={edit.id}
            onSaved={() => {
              list.reload();
              setEdit(null);
            }}
            onError={toast.error}
          />
        )}
      </Drawer>
    </div>
  );
}

function Editor({ entity, schema, id, onSaved, onError }: { entity: string; schema: any; id?: string; onSaved: () => void; onError: (e: unknown) => void }) {
  const { plant, can } = useSession();
  const toast = useToast();
  const { confirm, node } = useConfirm();
  const row = useApi<any>(id ? `/master-data/${entity}/${id}` : null);
  const [form, setForm] = useState<any>({});
  const [errs, setErrs] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [tab, setTab] = useState("fields");
  useEffect(() => {
    if (row.data) setForm(row.data);
    else if (!id) setForm(plant && schema.fields.some((f: FieldDef) => f.name === "plant_id") ? { plant_id: plant.id } : {});
  }, [row.data, id, plant, schema]);

  const save = async () => {
    setBusy(true);
    setErrs({});
    try {
      const body: any = {};
      for (const f of schema.fields as FieldDef[]) {
        if (f.name in form && f.type !== "json") body[f.name] = form[f.name] === "" ? null : form[f.name];
        if (f.type === "json" && typeof form[f.name] === "string") {
          try {
            body[f.name] = JSON.parse(form[f.name]);
          } catch {
            setErrs({ [f.name]: "invalid JSON" });
            setBusy(false);
            return;
          }
        } else if (f.type === "json" && f.name in form) body[f.name] = form[f.name];
      }
      if (id) body.version = row.data?.version;
      if (id) await api(`/master-data/${entity}/${id}`, { method: "PUT", body });
      else await api(`/master-data/${entity}`, { body });
      toast.ok("Saved");
      onSaved();
    } catch (e) {
      if (e instanceof ApiError && e.context?.field) setErrs({ [String(e.context.field)]: e.message });
      if (e instanceof ApiError && Array.isArray((e.context as any)?.fields)) setErrs(Object.fromEntries((e.context as any).fields.map((x: any) => [x.field, x.message])));
      onError(e);
    } finally {
      setBusy(false);
    }
  };
  const del = async () => {
    const r = await confirm("Delete this record?", { danger: true, reason: true, body: "If other data references it, it is deactivated instead." });
    if (!r.ok) return;
    try {
      const out = await api(`/master-data/${entity}/${id}`, { method: "DELETE", query: { reason: r.reason } });
      toast.ok(out.deactivated ? out.reason : "Deleted");
      onSaved();
    } catch (e) {
      onError(e);
    }
  };
  if (id && !row.data) return row.error ? <ErrorState error={row.error} /> : <Loading />;
  const children = schema.children || [];
  return (
    <div className="flex flex-col h-full">
      {(children.length > 0 || entity === "items" || entity === "products") && (
        <Tabs value={tab} onChange={setTab} tabs={[{ id: "fields", label: "Fields" }, ...children.map((c: any) => ({ id: c.name, label: `${c.name} (${(row.data?.[c.name] || []).length})` })), ...(id && (entity === "items" || entity === "products") ? [{ id: "bom", label: "BOM tree" }] : [])]} />
      )}
      <div className="p-3 space-y-2.5 flex-1 overflow-auto mx-scroll">
        {tab === "fields" &&
          (schema.fields as FieldDef[])
            .filter((f) => !HIDDEN.has(f.name))
            .map((f) => (
              <Field key={f.name} label={`${f.name.replaceAll("_", " ")}${f.required ? " *" : ""}`} error={errs[f.name]}>
                <Input f={f} value={form[f.name]} label={form[f.name.replace(/_id$/, "") + "_label"]} onChange={(v) => setForm({ ...form, [f.name]: v })} />
              </Field>
            ))}
        {tab !== "fields" && tab !== "bom" && (
          <table className="mx-table">
            <thead>
              <tr>
                {(children.find((c: any) => c.name === tab)?.fields || []).slice(0, 8).map((c: string) => (
                  <th key={c}>{c.replaceAll("_", " ")}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {(row.data?.[tab] || []).map((r: any) => (
                <tr key={r.id}>
                  {(children.find((c: any) => c.name === tab)?.fields || []).slice(0, 8).map((c: string) => (
                    <td key={c} className="tabular">
                      {typeof r[c] === "object" && r[c] !== null ? JSON.stringify(r[c]) : String(r[c] ?? "—").slice(0, 36)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {tab === "bom" && id && <BomTree itemId={id} />}
      </div>
      <div className="flex gap-2 justify-end p-2 border-t border-gray-200 bg-gray-50">
        {id && (can("masterdata:write") || can("orders:write")) && (
          <Button variant="danger" onClick={del}>
            Delete
          </Button>
        )}
        <div className="flex-1 text-[11px] text-slate-600 self-center">{row.data?.version ? `version ${row.data.version} · updated ${dt(row.data.updated_at)} by ${row.data.updated_by || "—"}` : ""}</div>
        <Button variant="primary" busy={busy} onClick={save}>
          Save
        </Button>
      </div>
      {node}
    </div>
  );
}

function Input({ f, value, label, onChange }: { f: FieldDef; value: any; label?: string; onChange: (v: any) => void }) {
  if (f.readonly) return <div className="text-slate-600 tabular">{String(value ?? "—")}</div>;
  if (f.type === "boolean") return <input type="checkbox" checked={!!value} onChange={(e) => onChange(e.target.checked)} />;
  if (f.type === "integer" || f.type === "number") return <input className="mx-input w-full tabular" type="number" step={f.type === "integer" ? 1 : "any"} value={value ?? ""} onChange={(e) => onChange(e.target.value === "" ? null : Number(e.target.value))} />;
  if (f.type === "datetime") return <input className="mx-input w-full" type="datetime-local" value={value ? isoToLocalInput(String(value)) : ""} onChange={(e) => onChange(e.target.value ? localInputToIso(e.target.value) : null)} />;
  if (f.type === "date") return <input className="mx-input w-full" type="date" value={value ? String(value).slice(0, 10) : ""} onChange={(e) => onChange(e.target.value || null)} />;
  if (f.type === "time") return <input className="mx-input w-full" type="time" value={value ? String(value).slice(0, 5) : ""} onChange={(e) => onChange(e.target.value || null)} />;
  if (f.type === "json") return <textarea className="mx-input w-full code" rows={3} value={typeof value === "string" ? value : JSON.stringify(value ?? {}, null, 1)} onChange={(e) => onChange(e.target.value)} />;
  if (f.type === "uuid" && f.ref) return <RefSelect entity={f.ref} value={value} label={label} onChange={onChange} />;
  return <input className="mx-input w-full" maxLength={f.max_length || undefined} value={value ?? ""} onChange={(e) => onChange(e.target.value)} />;
}

function RefSelect({ entity, value, label, onChange }: { entity: string; value: string | null; label?: string; onChange: (v: string | null) => void }) {
  const opts = useApi<any>(`/master-data/${entity}`, { limit: 2000 });
  return (
    <select className="mx-select w-full" value={value || ""} onChange={(e) => onChange(e.target.value || null)}>
      <option value="">—</option>
      {!opts.data && value && <option value={value}>{label || value}</option>}
      {(opts.data?.items || []).map((o: any) => (
        <option key={o.id} value={o.id}>
          {o.code || o.number || o.name} {o.name && o.code ? `· ${o.name}` : ""}
        </option>
      ))}
    </select>
  );
}

function BomTree({ itemId }: { itemId: string }) {
  const tree = useApi<any>(`/items/${itemId}/bom-tree`);
  if (!tree.data) return tree.error ? <ErrorState error={tree.error} /> : <Loading />;
  const Node = ({ n, d }: { n: any; d: number }) => (
    <div style={{ paddingLeft: d * 14 }}>
      <div className="py-0.5 text-[12.5px]">
        {d ? "└ " : ""}
        <span className="code">{n.code}</span> {n.name} · <span className="tabular">{n.quantity}</span> {n.uom} <Badge tone="neutral" glyph={false}>{n.make_or_buy}</Badge>
        {n.operation_seq ? <span className="text-slate-600"> @op {n.operation_seq}</span> : null}
        {n.cycle && <span className="text-red-600"> ▲ cycle</span>}
      </div>
      {n.children.map((c: any, i: number) => (
        <Node key={i} n={c} d={d + 1} />
      ))}
    </div>
  );
  return <Node n={tree.data} d={0} />;
}

