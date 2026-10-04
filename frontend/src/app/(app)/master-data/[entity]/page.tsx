"use client";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useLayoutEffect, useMemo, useState } from "react";
import { Badge, Button, DataTable, Dialog, Drawer, ErrorState, Field, Loading, PageHeader, Tabs, useConfirm, useToast, type Column } from "@/components/ui";
import { ChildGrid, EditableGrid } from "@/components/data/EditableGrid";
import { FieldInput, fieldLabel, invalidateRefOptions, parseJsonFields, type FieldDef } from "@/components/data/FieldInput";
import { ImportWizard } from "@/components/data/ImportWizard";
import { api, ApiError, download } from "@/lib/api";
import { dt } from "@/lib/format";
import { useApi, useUnsavedWarning } from "@/lib/hooks";
import { useSession } from "@/lib/session";

const HIDDEN = new Set(["tenant_id"]);
const LIMIT = 5000;
const ALIASES: Record<string, string> = { bom: "boms" };

export default function EntityPage() {
  const params = useParams<{ entity: string }>();
  const entity = ALIASES[params.entity] || params.entity;
  const { plant, can, t } = useSession();
  const toast = useToast();
  const schema = useApi<any>(`/master-data/${entity}/schema`);
  const [typed, setTyped] = useState("");
  const [q, setQ] = useState("");
  useEffect(() => {
    const h = setTimeout(() => setQ(typed.trim()), 300);
    return () => clearTimeout(h);
  }, [typed]);
  const list = useApi<any>(`/master-data/${entity}`, { limit: LIMIT, plant_id: plant?.id, q: q || undefined });
  const [edit, setEdit] = useState<any | null>(null);
  const [mode, setMode] = useState<"list" | "grid">("list");
  const [importing, setImporting] = useState(false);
  const { confirm, node: confirmNode } = useConfirm();
  const [editorDirty, setEditorDirty] = useState(false);
  const [gridDirty, setGridDirty] = useState(0);
  const discardOk = async (dirty: boolean) => !dirty || (await confirm(t("md.discardQ"), { danger: true, body: t("md.discardBody") })).ok;
  const closeEditor = async () => {
    if (await discardOk(editorDirty)) {
      setEdit(null);
      setEditorDirty(false);
    }
  };
  const switchMode = async (m: "list" | "grid") => {
    if (m === mode) return;
    if (await discardOk(gridDirty > 0)) {
      setMode(m);
      setGridDirty(0);
    }
  };

  const fields: FieldDef[] = schema.data?.fields || [];
  const cols: Column<any>[] = useMemo(() => {
    const show = fields.filter((f) => !HIDDEN.has(f.name) && f.type !== "json" && !["description", "notes", "instructions"].includes(f.name)).slice(0, 12);
    return show.map((f) => ({
      key: f.name,
      label: fieldLabel(f.name),
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

  const writable = !!schema.data && (can("masterdata:write") || can("orders:write") || can("admin:config"));
  const defaults = plant && fields.some((f) => f.name === "plant_id") ? { plant_id: plant.id } : undefined;
  const tables = [entity, ...(schema.data?.children || []).map((c: any) => `${entity}.${c.name}`)];
  const [importTable, setImportTable] = useState(entity);
  const title = schema.data?.label || entity;
  return (
    <div className="flex flex-col h-full min-h-0">
      <PageHeader
        title={title}
        subtitle={
          <>
            <Link href="/master-data" className="text-blue-500 hover:underline">
              {t("nav.masterData")}
            </Link>{" "}
            · {list.data ? t("md.records", { n: list.data.total }) : ""}
          </>
        }
        actions={
          <>
            <input className="mx-input w-[180px] hidden md:block" placeholder={t("md.search")} aria-label={t("md.search")} value={typed} onChange={(e) => setTyped(e.target.value)} />
            {writable && (
              <div className="flex rounded-[3px] border border-gray-300 overflow-hidden" role="group" aria-label={t("md.view")}>
                <button className={`px-2.5 h-7 text-[12px] ${mode === "list" ? "bg-navy-700 text-white" : "bg-white"}`} aria-pressed={mode === "list"} onClick={() => switchMode("list")}>
                  {t("md.viewList")}
                </button>
                <button className={`px-2.5 h-7 text-[12px] ${mode === "grid" ? "bg-navy-700 text-white" : "bg-white"}`} aria-pressed={mode === "grid"} onClick={() => switchMode("grid")}>
                  {t("md.viewGrid")}
                </button>
              </div>
            )}
            {can("integration:export") && (
              <Button icon="download" onClick={() => download(`/exports/table:${entity}`, { format: "xlsx", plant_id: plant?.id }).catch(toast.error)}>
                Excel
              </Button>
            )}
            {writable && can("integration:import") && (
              <Button icon="upload" onClick={() => setImporting(true)}>
                {t("data.import")}
              </Button>
            )}
            {writable && (
              <Button variant="primary" icon="plus" onClick={() => setEdit({})}>
                {t("md.new")}
              </Button>
            )}
          </>
        }
      />
      <div className="flex-1 min-h-0 bg-white">
        {(schema.error || list.error) && <ErrorState error={schema.error || list.error} onRetry={list.reload} />}
        {!list.data || !schema.data ? (
          <Loading />
        ) : mode === "grid" && writable ? (
          <EditableGrid entity={entity} fields={fields} rows={list.data.items} defaults={defaults} onSaved={list.reload} onDirty={setGridDirty} />
        ) : (
          <DataTable rows={list.data.items} columns={cols} rowKey={(r) => r.id} onRowClick={async (r) => (await discardOk(editorDirty)) && (setEditorDirty(false), setEdit(r))} selectedKey={edit?.id} exportName={entity} filterable />
        )}
        {list.data && list.data.total > list.data.items.length && <div className="px-3 py-1 text-[11.5px] text-slate-600 border-t border-gray-200">{t("md.truncated", { n: list.data.items.length, total: list.data.total })}</div>}
      </div>
      <Drawer open={!!edit} onClose={closeEditor} title={edit?.id ? `${title}: ${edit.code || edit.number || edit.name || edit.id.slice(0, 8)}` : `${t("md.new")}: ${title}`} width={640}>
        {edit && schema.data && (
          <Editor
            entity={entity}
            schema={schema.data}
            id={edit.id}
            onSaved={() => {
              list.reload();
              setEditorDirty(false);
              setEdit(null);
            }}
            onError={toast.error}
            onDirty={setEditorDirty}
          />
        )}
      </Drawer>
      <Dialog open={importing} onClose={() => setImporting(false)} title={`${t("data.import")}: ${title}`} width={900}>
        {tables.length > 1 && (
          <div className="flex flex-wrap gap-2 mb-2 text-[12.5px]">
            {tables.map((tb) => (
              <button key={tb} className={`px-2 h-7 rounded-[3px] border ${importTable === tb ? "bg-navy-700 text-white border-navy-700" : "border-gray-300"}`} onClick={() => setImportTable(tb)}>
                {tb === entity ? title : `${title} — ${tb.split(".")[1]}`}
              </button>
            ))}
          </div>
        )}
        {importing && (
          <ImportWizard
            key={importTable}
            compact
            choices={[{ entity: `table:${importTable}`, label: importTable === entity ? title : `${title} — ${importTable.split(".")[1]}` }]}
            initialEntity={`table:${importTable}`}
            onImported={list.reload}
          />
        )}
      </Dialog>
      {confirmNode}
    </div>
  );
}

function Editor({ entity, schema, id, onSaved, onError, onDirty }: { entity: string; schema: any; id?: string; onSaved: () => void; onError: (e: unknown) => void; onDirty: (dirty: boolean) => void }) {
  const { plant, can, t } = useSession();
  const toast = useToast();
  const { confirm, node } = useConfirm();
  const row = useApi<any>(id ? `/master-data/${entity}/${id}` : null);
  const [form, setForm] = useState<any>({});
  const [kids, setKids] = useState<Record<string, any[]>>({});
  const [dirtyKids, setDirtyKids] = useState<Set<string>>(new Set());
  const [errs, setErrs] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [tab, setTab] = useState("fields");
  const [base, setBase] = useState<string>("{}");
  const children: any[] = schema.children || [];
  const dirty = dirtyKids.size > 0 || JSON.stringify(form) !== base;
  useUnsavedWarning(dirty);
  useLayoutEffect(() => onDirty(dirty), [dirty, onDirty]); // before the next key press (Escape closes)
  useEffect(() => {
    if (row.data) {
      setForm(row.data);
      setBase(JSON.stringify(row.data));
      setKids(Object.fromEntries(children.map((c) => [c.name, row.data[c.name] || []])));
      setDirtyKids(new Set());
    } else if (!id) {
      const init = plant && schema.fields.some((f: FieldDef) => f.name === "plant_id") ? { plant_id: plant.id } : {};
      setForm(init);
      setBase(JSON.stringify(init));
      setKids(Object.fromEntries(children.map((c) => [c.name, []])));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [row.data, id, plant, schema]);

  const save = async () => {
    setBusy(true);
    setErrs({});
    try {
      const fields = schema.fields as FieldDef[];
      const body: any = {};
      for (const f of fields) if (f.name in form) body[f.name] = form[f.name] === "" ? null : form[f.name];
      const p = parseJsonFields(fields, body);
      if (!p.ok) {
        setErrs({ [p.field]: t("grid.badJson") });
        return;
      }
      const out: any = p.row;
      for (const c of children) {
        if (!dirtyKids.has(c.name)) continue;
        const rowsOut = [];
        for (const r of kids[c.name] || []) {
          const pr = parseJsonFields(c.field_defs || [], r);
          if (!pr.ok) {
            setErrs({ [c.name]: `${pr.field}: ${t("grid.badJson")}` });
            setTab(c.name);
            return;
          }
          rowsOut.push(Object.fromEntries(Object.entries(pr.row).filter(([k]) => !k.endsWith("_label"))));
        }
        out[c.name] = rowsOut;
      }
      if (id) out.version = row.data?.version;
      if (id) await api(`/master-data/${entity}/${id}`, { method: "PUT", body: out });
      else await api(`/master-data/${entity}`, { body: out });
      invalidateRefOptions(entity);
      toast.ok(t("common.saved"));
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
    const r = await confirm(t("md.deleteQ"), { danger: true, reason: true, body: t("grid.deleteBody") });
    if (!r.ok) return;
    try {
      const out = await api(`/master-data/${entity}/${id}`, { method: "DELETE", query: { reason: r.reason } });
      invalidateRefOptions(entity);
      toast.ok(out.deactivated ? out.reason : t("md.deleted"));
      onSaved();
    } catch (e) {
      onError(e);
    }
  };
  if (id && !row.data) return row.error ? <ErrorState error={row.error} /> : <Loading />;
  return (
    <div className="flex flex-col h-full">
      {(children.length > 0 || entity === "items" || entity === "products") && (
        <Tabs
          value={tab}
          onChange={setTab}
          tabs={[
            { id: "fields", label: t("md.fields") },
            ...children.map((c: any) => ({ id: c.name, label: `${fieldLabel(c.name)} (${(kids[c.name] || []).length})${dirtyKids.has(c.name) ? " •" : ""}` })),
            ...(id && (entity === "items" || entity === "products") ? [{ id: "bom", label: t("md.bomTree") }] : []),
          ]}
        />
      )}
      <div className="p-3 space-y-2.5 flex-1 overflow-auto mx-scroll">
        {tab === "fields" &&
          (schema.fields as FieldDef[])
            .filter((f) => !HIDDEN.has(f.name))
            .map((f) => (
              <Field key={f.name} label={`${fieldLabel(f.name)}${f.required ? " *" : ""}`} error={errs[f.name]}>
                <FieldInput f={f} value={form[f.name]} label={form[f.name.replace(/_id$/, "") + "_label"]} onChange={(v) => setForm({ ...form, [f.name]: v })} />
              </Field>
            ))}
        {tab !== "fields" && tab !== "bom" && (
          <>
            {errs[tab] && <div className="text-red-600 text-[12px]">▲ {errs[tab]}</div>}
            <ChildGrid
              fields={children.find((c: any) => c.name === tab)?.field_defs || []}
              rows={kids[tab] || []}
              onChange={(rows) => {
                setKids({ ...kids, [tab]: rows });
                setDirtyKids(new Set([...dirtyKids, tab]));
              }}
            />
          </>
        )}
        {tab === "bom" && id && <BomTree itemId={id} />}
      </div>
      <div className="flex gap-2 justify-end p-2 border-t border-gray-200 bg-gray-50">
        {id && (can("masterdata:write") || can("orders:write")) && (
          <Button variant="danger" onClick={del}>
            {t("common.delete")}
          </Button>
        )}
        <div className="flex-1 text-[11px] text-slate-600 self-center">{row.data?.version ? t("md.version", { v: row.data.version, at: dt(row.data.updated_at), by: row.data.updated_by || "—" }) : ""}</div>
        <Button variant="primary" busy={busy} onClick={save}>
          {t("common.save")}
        </Button>
      </div>
      {node}
    </div>
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
