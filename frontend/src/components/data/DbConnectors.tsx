"use client";
import { useMemo, useState } from "react";
import { Badge, Button, Dialog, Field, Panel, Select, StatusPill, useConfirm, useToast } from "@/components/ui";
import { api } from "@/lib/api";
import { dt } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import { useSession } from "@/lib/session";
import { invalidateRefOptions } from "./FieldInput";

type Source = { id?: string; entity: string; table?: string; query?: string; mapping?: Record<string, string | null>; mode?: string; date_format?: string; enabled?: boolean; auto_commit?: boolean; skip_invalid_rows?: boolean };
type Conn = { id?: string; code: string; name: string; is_active: boolean; has_password?: boolean; settings: { dialect: string; host?: string; port?: number | null; database?: string; username?: string; options?: Record<string, string>; timeout_s?: number; sync_every_minutes?: number | null; sources: Source[] } };

const EMPTY: Conn = { code: "", name: "", is_active: true, settings: { dialect: "postgresql", host: "", port: null, database: "", username: "", sources: [], sync_every_minutes: null } };

/** Database connectors: connection, sources (table or SELECT → MonxuPlan table, with column mapping), sync. */
export function DbConnectors() {
  const { t, plant, can } = useSession();
  const toast = useToast();
  const { confirm, node } = useConfirm();
  const drivers = useApi<any>("/connectors/database/drivers");
  const list = useApi<any>(can("integration:manage") ? "/connectors" : null);
  const [edit, setEdit] = useState<Conn | null>(null);
  const [syncing, setSyncing] = useState<string | null>(null);
  const [result, setResult] = useState<any>(null);
  const conns: any[] = (list.data?.configured || []).filter((c: any) => c.system === "DATABASE");
  const supported = drivers.data?.supported !== false;

  const sync = async (c: any) => {
    setSyncing(c.id);
    try {
      const r = await api(`/connectors/database/${c.id}/sync`, { body: { plant_id: plant?.id } });
      setResult(r);
      invalidateRefOptions();
      list.reload();
      toast.ok(t("db.synced", { s: r.status }));
    } catch (e) {
      toast.error(e);
    } finally {
      setSyncing(null);
    }
  };
  const del = async (c: any) => {
    const r = await confirm(t("db.deleteQ", { c: c.code }), { danger: true });
    if (!r.ok) return;
    try {
      await api(`/connectors/database/${c.id}`, { method: "DELETE" });
      list.reload();
    } catch (e) {
      toast.error(e);
    }
  };

  return (
    <div className="space-y-3 max-w-5xl">
      <Panel title={t("db.title")} actions={supported && can("integration:manage") ? <Button size="sm" variant="primary" icon="plus" onClick={() => setEdit(structuredClone(EMPTY))}>{t("db.new")}</Button> : null}>
        <div className="p-3 space-y-2 text-[12.5px]">
          <p className="text-slate-600">{t("db.help")}</p>
          {!supported && <p className="text-amber-600">◆ {drivers.data?.reason}</p>}
          {supported && (
            <div className="flex flex-wrap gap-2">
              {(drivers.data?.drivers || []).map((d: any) => (
                <Badge key={d.dialect} tone={d.installed ? "ok" : "neutral"} title={d.reason || undefined}>
                  {d.label}
                  {d.installed ? "" : ` — ${t("db.noDriver")}`}
                </Badge>
              ))}
            </div>
          )}
        </div>
        <div className="overflow-x-auto">
        <table className="mx-table">
          <tbody>
            {conns.map((c) => (
              <tr key={c.id}>
                <td>
                  <div className="font-medium">{c.name}</div>
                  <div className="code text-[11px] text-slate-500">
                    {c.code} · {c.settings?.dialect} {c.settings?.host ? `· ${c.settings.host}` : ""} {c.settings?.database ? `/ ${c.settings.database}` : ""}
                  </div>
                </td>
                <td className="text-[12px]">
                  {(c.settings?.sources || []).length} {t("db.sources")}
                  {c.settings?.sync_every_minutes ? ` · ${t("db.every", { m: c.settings.sync_every_minutes })}` : ` · ${t("db.manual")}`}
                </td>
                <td className="text-[12px]">
                  {c.last_run_at ? dt(c.last_run_at) : "—"} {c.last_status && <StatusPill status={c.last_status} />}
                </td>
                <td className="text-right whitespace-nowrap">
                  <Button size="sm" variant="primary" icon="refresh" busy={syncing === c.id} disabled={!supported} onClick={() => sync(c)}>
                    {t("db.syncNow")}
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => setEdit({ id: c.id, code: c.code, name: c.name, is_active: c.is_active, has_password: c.has_password, settings: { sources: [], ...c.settings } })}>
                    {t("common.edit")}
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => del(c)}>
                    {t("common.delete")}
                  </Button>
                </td>
              </tr>
            ))}
            {list.data && !conns.length && (
              <tr>
                <td className="text-slate-600">{t("db.none")}</td>
              </tr>
            )}
          </tbody>
        </table>
        </div>
      </Panel>
      {result && (
        <Panel title={`${t("db.lastSync")}: ${result.connector} · ${result.status}`} actions={<Button size="sm" variant="ghost" onClick={() => setResult(null)}>✕</Button>}>
          <table className="mx-table">
            <tbody>
              {result.results.map((r: any, i: number) => (
                <tr key={i}>
                  <td className="code">{r.entity}</td>
                  <td>
                    <StatusPill status={r.status} />
                  </td>
                  <td className="text-[12px]">
                    {r.message ||
                      (r.stats?.created !== undefined
                        ? `${r.stats.created} ${t("imp.created")} · ${r.stats.updated} ${t("imp.updated")}`
                        : `${r.errors} ${t("imp.withErrors")} — ${t("db.review")}`)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      )}
      {edit && <ConnectorDialog conn={edit} drivers={drivers.data?.drivers || []} onClose={() => setEdit(null)} onSaved={() => { setEdit(null); list.reload(); }} />}
      {node}
    </div>
  );
}

function ConnectorDialog({ conn, drivers, onClose, onSaved }: { conn: Conn; drivers: any[]; onClose: () => void; onSaved: () => void }) {
  const { t } = useSession();
  const toast = useToast();
  const [c, setC] = useState<Conn>(conn);
  const [password, setPassword] = useState("");
  const [test, setTest] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [srcOpen, setSrcOpen] = useState<number | null>(null);
  const set = (k: string, v: any) => setC({ ...c, settings: { ...c.settings, [k]: v } });
  const isSqlite = c.settings.dialect === "sqlite";
  const port = drivers.find((d) => d.dialect === c.settings.dialect)?.default_port;

  const doTest = async () => {
    setBusy(true);
    try {
      setTest(await api("/connectors/database/test", { body: { settings: c.settings, password: password || undefined, connector_id: c.id } }));
    } catch (e) {
      setTest(null);
      toast.error(e);
    } finally {
      setBusy(false);
    }
  };
  const save = async () => {
    setBusy(true);
    try {
      const body = { code: c.code, name: c.name || c.code, is_active: c.is_active, settings: c.settings, password: password || undefined };
      if (c.id) await api(`/connectors/database/${c.id}`, { method: "PUT", body });
      else await api("/connectors/database", { body });
      toast.ok(t("common.saved"));
      onSaved();
    } catch (e) {
      toast.error(e);
    } finally {
      setBusy(false);
    }
  };
  const sources = c.settings.sources || [];
  return (
    <Dialog
      open
      onClose={onClose}
      title={c.id ? `${t("db.connector")}: ${c.code}` : t("db.new")}
      width={880}
      footer={
        <>
          <Button onClick={onClose}>{t("common.cancel")}</Button>
          <Button variant="primary" busy={busy} onClick={save}>
            {t("common.save")}
          </Button>
        </>
      }
    >
      <div className="space-y-3 text-[12.5px]">
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
          <Field label={t("db.code")}>
            <input className="mx-input w-full" value={c.code} onChange={(e) => setC({ ...c, code: e.target.value })} />
          </Field>
          <Field label={t("db.name")}>
            <input className="mx-input w-full" value={c.name} onChange={(e) => setC({ ...c, name: e.target.value })} />
          </Field>
          <Field label={t("db.type")}>
            <Select value={c.settings.dialect} onChange={(v) => set("dialect", v)} className="w-full" options={drivers.map((d) => ({ value: d.dialect, label: d.label + (d.installed ? "" : ` (${t("db.noDriver")})`) }))} />
          </Field>
          {!isSqlite && (
            <>
              <Field label={t("db.host")}>
                <input className="mx-input w-full" value={c.settings.host || ""} onChange={(e) => set("host", e.target.value)} placeholder="erp-db.company.local" />
              </Field>
              <Field label={t("db.port")}>
                <input className="mx-input w-full tabular" type="number" value={c.settings.port ?? ""} placeholder={port ? String(port) : ""} onChange={(e) => set("port", e.target.value ? Number(e.target.value) : null)} />
              </Field>
            </>
          )}
          <Field label={isSqlite ? t("db.file") : c.settings.dialect === "oracle" ? t("db.service") : t("db.database")}>
            <input className="mx-input w-full" value={c.settings.database || ""} onChange={(e) => set("database", e.target.value)} />
          </Field>
          {!isSqlite && (
            <>
              <Field label={t("db.user")} hint={t("db.userHint")}>
                <input className="mx-input w-full" value={c.settings.username || ""} onChange={(e) => set("username", e.target.value)} autoComplete="off" />
              </Field>
              <Field label={t("db.password")} hint={c.has_password ? t("db.passwordKept") : t("db.passwordHint")}>
                <input className="mx-input w-full" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" />
              </Field>
            </>
          )}
          <Field label={t("db.schedule")} hint={t("db.scheduleHint")}>
            <input className="mx-input w-full tabular" type="number" min={5} value={c.settings.sync_every_minutes ?? ""} onChange={(e) => set("sync_every_minutes", e.target.value ? Number(e.target.value) : null)} />
          </Field>
        </div>
        <div className="flex items-center gap-2">
          <Button icon="plug" busy={busy} onClick={doTest}>
            {t("db.test")}
          </Button>
          {test && (
            <span className="text-green-600">
              ✓ {t("db.connected")} {test.server_version ? `(v${test.server_version})` : ""} · {test.table_count} {t("db.tables")}
            </span>
          )}
        </div>
        <div className="border-t border-gray-200 pt-3 space-y-2">
          <div className="flex items-center gap-2">
            <h3 className="font-semibold flex-1">{t("db.sourcesTitle")}</h3>
            <Button size="sm" icon="plus" onClick={() => { set("sources", [...sources, { entity: "items", table: "", mode: "UPSERT", enabled: true, auto_commit: true }]); setSrcOpen(sources.length); }}>
              {t("db.addSource")}
            </Button>
          </div>
          <p className="text-slate-600">{t("db.sourcesHelp")}</p>
          {sources.map((s, i) => (
            <SourceEditor
              key={i}
              conn={c}
              src={s}
              tables={test?.tables || []}
              open={srcOpen === i}
              onToggle={() => setSrcOpen(srcOpen === i ? null : i)}
              onChange={(ns) => set("sources", sources.map((x, j) => (j === i ? ns : x)))}
              onRemove={() => set("sources", sources.filter((_, j) => j !== i))}
            />
          ))}
        </div>
      </div>
    </Dialog>
  );
}

function SourceEditor({ conn, src, tables, open, onToggle, onChange, onRemove }: { conn: Conn; src: Source; tables: string[]; open: boolean; onToggle: () => void; onChange: (s: Source) => void; onRemove: () => void }) {
  const { t } = useSession();
  const toast = useToast();
  const templates = useApi<any[]>(open ? "/imports/templates" : null);
  const tbls = useApi<any[]>(open ? "/imports/tables" : null);
  const [preview, setPreview] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const entityOptions = useMemo(
    () => [...(templates.data || []).map((x) => ({ value: x.entity, label: `${t("imp.guided")}: ${x.label}` })), ...(tbls.data || []).filter((x) => x.writable).map((x) => ({ value: x.entity, label: `${t("imp.table")}: ${x.label}` }))],
    [templates.data, tbls.data, t],
  );
  const useQuery = typeof src.query === "string";
  const loadPreview = async () => {
    if (!conn.id) {
      toast.error(new Error(t("db.saveFirst")));
      return;
    }
    setBusy(true);
    try {
      const p = await api(`/connectors/database/${conn.id}/preview`, { body: { source: { table: src.table || undefined, query: src.query || undefined }, entity: src.entity } });
      setPreview(p);
      onChange({ ...src, mapping: { ...p.mapping, ...Object.fromEntries(Object.entries(src.mapping || {}).filter(([, v]) => v && p.columns.includes(v))) } });
    } catch (e) {
      toast.error(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="border border-gray-200 rounded-[3px]">
      <div className="flex items-center gap-2 px-2 py-1.5 bg-gray-50">
        <button className="flex-1 text-left" onClick={onToggle} aria-expanded={open}>
          {open ? "▾" : "▸"} <span className="code">{src.table || (src.query ? "SELECT …" : "—")}</span> → <b>{src.entity}</b> {src.enabled === false && <Badge tone="neutral">{t("db.disabled")}</Badge>}
        </button>
        <Button size="sm" variant="ghost" onClick={onRemove}>
          {t("common.delete")}
        </Button>
      </div>
      {open && (
        <div className="p-2 space-y-2">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
            <Field label={t("db.feeds")}>
              <Select value={src.entity} onChange={(v) => onChange({ ...src, entity: v, mapping: {} })} className="w-full" options={entityOptions.length ? entityOptions : [{ value: src.entity, label: src.entity }]} />
            </Field>
            <Field label={t("db.from")}>
              <Select value={useQuery ? "query" : "table"} onChange={(v) => onChange(v === "query" ? { ...src, table: undefined, query: src.query || "SELECT * FROM " } : { ...src, query: undefined, table: src.table || "" })} className="w-full" options={[{ value: "table", label: t("db.fromTable") }, { value: "query", label: t("db.fromQuery") }]} />
            </Field>
          </div>
          {useQuery ? (
            <Field label="SELECT" hint={t("db.queryHint")}>
              <textarea className="mx-input w-full code" rows={4} value={src.query || ""} onChange={(e) => onChange({ ...src, query: e.target.value })} />
            </Field>
          ) : (
            <Field label={t("db.tableOrView")} hint={tables.length ? undefined : t("db.testForList")}>
              <input className="mx-input w-full code" aria-label={t("db.tableOrView")} list={`tables-${conn.code}`} value={src.table || ""} onChange={(e) => onChange({ ...src, table: e.target.value })} />
              <datalist id={`tables-${conn.code}`}>
                {tables.map((x) => (
                  <option key={x} value={x} />
                ))}
              </datalist>
            </Field>
          )}
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
            <Field label={t("imp.mode")}>
              <Select value={src.mode || "UPSERT"} onChange={(v) => onChange({ ...src, mode: v })} className="w-full" options={[{ value: "UPSERT", label: t("imp.mode.upsert") }, { value: "CREATE_ONLY", label: t("imp.mode.create") }, { value: "UPDATE_ONLY", label: t("imp.mode.update") }]} />
            </Field>
            <label className="flex gap-2 items-center pt-4">
              <input type="checkbox" checked={src.enabled !== false} onChange={(e) => onChange({ ...src, enabled: e.target.checked })} /> {t("db.enabled")}
            </label>
            <label className="flex gap-2 items-center pt-4">
              <input type="checkbox" checked={src.auto_commit !== false} onChange={(e) => onChange({ ...src, auto_commit: e.target.checked })} /> {t("db.autoCommit")}
            </label>
            <label className="flex gap-2 items-center pt-4">
              <input type="checkbox" checked={!!src.skip_invalid_rows} onChange={(e) => onChange({ ...src, skip_invalid_rows: e.target.checked })} /> {t("imp.onlyValid")}
            </label>
          </div>
          <Button size="sm" icon="eye" busy={busy} onClick={loadPreview}>
            {t("db.preview")}
          </Button>
          {preview && (
            <div className="space-y-2">
              <div className="overflow-x-auto max-h-[180px] overflow-y-auto mx-scroll border border-gray-200">
                <table className="mx-table">
                  <thead>
                    <tr>
                      {preview.columns.map((col: string) => (
                        <th key={col}>{col}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {preview.rows.slice(0, 8).map((r: any[], i: number) => (
                      <tr key={i}>
                        {r.map((v, j) => (
                          <td key={j} className="tabular">
                            {String(v ?? "")}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="overflow-x-auto max-h-[260px] overflow-y-auto mx-scroll border border-gray-200">
                <table className="mx-table">
                  <thead>
                    <tr>
                      <th>{t("imp.field")}</th>
                      <th>{t("imp.column")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(preview.fields || []).map((f: any) => (
                      <tr key={f.name}>
                        <td>
                          <span className="code">{f.name}</span> {f.required && <Badge tone="bad">{t("imp.required")}</Badge>}
                        </td>
                        <td>
                          <select className="mx-select" aria-label={f.name} value={src.mapping?.[f.name] || ""} onChange={(e) => onChange({ ...src, mapping: { ...(src.mapping || {}), [f.name]: e.target.value || null } })}>
                            <option value="">{t("imp.notMapped")}</option>
                            {preview.columns.map((col: string) => (
                              <option key={col} value={col}>
                                {col}
                              </option>
                            ))}
                          </select>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
