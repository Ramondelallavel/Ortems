"use client";
import { useState } from "react";
import { Badge, Button, ComingSoon, DataTable, Dialog, ErrorState, Field, Loading, PageHeader, Panel, Select, StatusPill, Tabs, useToast } from "@/components/ui";
import { api, download } from "@/lib/api";
import { dt } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import { useSession } from "@/lib/session";

const STEPS = ["Upload", "Mapping", "Validation", "Preview", "Import", "Report"];

export default function IntegrationsPage() {
  const { can } = useSession();
  const [tab, setTab] = useState("import");
  return (
    <div className="flex flex-col h-full min-h-0">
      <PageHeader title="Integrations" subtitle="Excel/CSV/JSON import with validation, exports, inbound events (MES/ERP), outbound webhooks." />
      <Tabs
        value={tab}
        onChange={setTab}
        tabs={[
          { id: "import", label: "Import wizard" },
          { id: "history", label: "Import history" },
          { id: "export", label: "Exports" },
          ...(can("integration:manage") ? [{ id: "webhooks", label: "Webhooks" }, { id: "connectors", label: "Connectors" }] : []),
          { id: "events", label: "Event log" },
        ]}
      />
      <div className="flex-1 min-h-0 overflow-auto mx-scroll p-3">
        {tab === "import" && <ImportWizard />}
        {tab === "history" && <History />}
        {tab === "export" && <Exports />}
        {tab === "webhooks" && <Webhooks />}
        {tab === "connectors" && <Connectors />}
        {tab === "events" && <Events />}
      </div>
    </div>
  );
}

function ImportWizard() {
  const { plant } = useSession();
  const toast = useToast();
  const templates = useApi<any[]>("/imports/templates");
  const [step, setStep] = useState(0);
  const [entity, setEntity] = useState("items");
  const [file, setFile] = useState<File | null>(null);
  const [job, setJob] = useState<any>(null);
  const [mapping, setMapping] = useState<Record<string, string | null>>({});
  const [opts, setOpts] = useState<any>({ mode: "UPSERT", date_format: "DMY", skip_invalid_rows: false });
  const [busy, setBusy] = useState(false);
  const tpl = templates.data?.find((t) => t.entity === entity);

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    try {
      await fn();
    } catch (e) {
      toast.error(e);
    } finally {
      setBusy(false);
    }
  };
  const upload = () =>
    run(async () => {
      const fd = new FormData();
      fd.append("entity", entity);
      fd.append("file", file!);
      if (plant) fd.append("plant_id", plant.id);
      const j = await api("/imports", { form: fd });
      setJob(j);
      setMapping(j.mapping);
      setStep(1);
    });
  const validate = () =>
    run(async () => {
      await api(`/imports/${job.id}/mapping`, { method: "PUT", body: { mapping, options: opts } });
      const v = await api(`/imports/${job.id}/validate`, { method: "POST" });
      setJob(v);
      setStep(2);
    });
  const commit = () =>
    run(async () => {
      const c = await api(`/imports/${job.id}/commit`, { method: "POST" });
      setJob(c);
      setStep(5);
      toast.ok(`Imported: ${c.stats.created} created, ${c.stats.updated} updated`);
    });
  const reset = () => {
    setStep(0);
    setJob(null);
    setFile(null);
  };

  return (
    <div className="max-w-5xl space-y-3">
      <ol className="flex gap-1" aria-label="Import steps">
        {STEPS.map((s, i) => (
          <li key={s} aria-current={i === step ? "step" : undefined} className={`flex-1 h-8 flex items-center justify-center text-[12px] rounded-[3px] border ${i === step ? "bg-navy-700 text-white border-navy-700" : i < step ? "bg-green-100 border-green-600/30 text-green-600" : "bg-white border-gray-200 text-slate-600"}`}>
            {i < step ? "✓ " : `${i + 1}. `}
            {s}
          </li>
        ))}
      </ol>
      {step === 0 && (
        <Panel title="1. Upload">
          <div className="p-3 grid grid-cols-2 gap-3">
            <Field label="What are you importing?">
              <Select value={entity} onChange={setEntity} className="w-full" options={(templates.data || []).map((t) => ({ value: t.entity, label: t.label }))} />
            </Field>
            <Field label="File (.xlsx, .csv, .json)" hint="Max 25 MB / 100 000 rows. Headers in the first row.">
              <input type="file" accept=".xlsx,.xlsm,.csv,.txt,.tsv,.json" className="mx-input w-full pt-1" onChange={(e) => setFile(e.target.files?.[0] || null)} />
            </Field>
            {tpl && <p className="col-span-2 text-slate-600 text-[12.5px]">{tpl.description} Natural key: <span className="code">{tpl.key.join(" + ")}</span>. Records are created or updated; nothing is deleted by an import.</p>}
            <div className="col-span-2 flex gap-2">
              <Button variant="primary" icon="upload" busy={busy} disabled={!file} onClick={upload}>
                Upload and detect columns
              </Button>
              <Button icon="download" onClick={() => download(`/imports/templates/${entity}`, { format: "xlsx" }).catch(toast.error)}>
                Excel template
              </Button>
              <Button icon="download" onClick={() => download(`/imports/templates/${entity}`, { format: "csv" }).catch(toast.error)}>
                CSV template
              </Button>
            </div>
          </div>
        </Panel>
      )}
      {step === 1 && job && (
        <Panel title={`2. Mapping — ${job.filename} (${job.rows} rows, ${job.columns.length} columns)`}>
          <div className="p-3 space-y-3">
            <p className="text-[12.5px] text-slate-600">Columns were matched automatically by name (English and Spanish headers). Check every required field.</p>
            <table className="mx-table">
              <thead>
                <tr>
                  <th>MonxuPlan field</th>
                  <th>Type</th>
                  <th>Column in your file</th>
                  <th>Sample</th>
                </tr>
              </thead>
              <tbody>
                {job.fields.map((f: any) => (
                  <tr key={f.name}>
                    <td>
                      <span className="code">{f.name}</span> {f.required && <Badge tone="bad">required</Badge>}
                      <div className="text-[11px] text-slate-600 whitespace-normal">{f.description}</div>
                    </td>
                    <td className="text-slate-600">{f.enum?.length ? f.enum.join(" / ") : f.ref ? `code of ${f.ref}` : f.type}</td>
                    <td>
                      <select className="mx-select" aria-label={`Column for ${f.name}`} value={mapping[f.name] || ""} onChange={(e) => setMapping({ ...mapping, [f.name]: e.target.value || null })} aria-invalid={f.required && !mapping[f.name]}>
                        <option value="">— not mapped —</option>
                        {job.columns.map((c: string) => (
                          <option key={c} value={c}>
                            {c}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td className="text-slate-600 truncate max-w-[180px]">{mapping[f.name] ? String(job.sample?.[0]?.[mapping[f.name]!] ?? "") : ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="grid grid-cols-3 gap-3">
              <Field label="Mode">
                <Select value={opts.mode} onChange={(v) => setOpts({ ...opts, mode: v })} className="w-full" options={[{ value: "UPSERT", label: "Create and update" }, { value: "CREATE_ONLY", label: "Only create new" }, { value: "UPDATE_ONLY", label: "Only update existing" }]} />
              </Field>
              <Field label="Date format">
                <Select value={opts.date_format} onChange={(v) => setOpts({ ...opts, date_format: v })} className="w-full" options={[{ value: "DMY", label: "DD/MM/YYYY" }, { value: "MDY", label: "MM/DD/YYYY" }]} />
              </Field>
              <label className="flex gap-2 items-center pt-5">
                <input type="checkbox" checked={opts.skip_invalid_rows} onChange={(e) => setOpts({ ...opts, skip_invalid_rows: e.target.checked })} /> Import only valid rows (explicit choice)
              </label>
            </div>
            <div className="flex gap-2">
              <Button onClick={reset}>Back</Button>
              <Button variant="primary" busy={busy} onClick={validate} disabled={job.fields.some((f: any) => f.required && !mapping[f.name])}>
                Validate
              </Button>
            </div>
          </div>
        </Panel>
      )}
      {(step === 2 || step === 3) && job && (
        <Panel title={step === 2 ? "3. Validation" : "4. Preview"}>
          <div className="p-3 space-y-3">
            <div className="flex flex-wrap gap-2 text-[12.5px]">
              <Badge tone="neutral" glyph={false}>
                {job.stats.rows} rows
              </Badge>
              <Badge tone="ok">{job.stats.valid_rows} valid</Badge>
              <Badge tone={job.stats.error_rows ? "bad" : "ok"}>{job.stats.error_rows} with errors</Badge>
              <Badge tone="info">{job.stats.to_create} to create</Badge>
              <Badge tone="info">{job.stats.to_update} to update</Badge>
              {job.stats.to_skip > 0 && <Badge tone="warn">{job.stats.to_skip} skipped</Badge>}
              <StatusPill status={job.status} />
            </div>
            {job.stats.blocking_reason && <div className="text-red-600">▲ {job.stats.blocking_reason}</div>}
            {job.warnings?.map((w: any, i: number) => (
              <div key={i} className="text-amber-600 text-[12.5px]">
                ◆ {w.message}
              </div>
            ))}
            {step === 2 && job.errors?.length > 0 && (
              <div className="max-h-[320px] overflow-auto mx-scroll border border-gray-200">
                <table className="mx-table">
                  <thead>
                    <tr>
                      <th>Row</th>
                      <th>Field</th>
                      <th>Value</th>
                      <th>Problem</th>
                    </tr>
                  </thead>
                  <tbody>
                    {job.errors.map((e: any, i: number) => (
                      <tr key={i}>
                        <td className="num">{e.row ?? "—"}</td>
                        <td className="code">{e.field}</td>
                        <td className="truncate max-w-[160px]">{String(e.value ?? "")}</td>
                        <td className="text-red-600 whitespace-normal">▲ {e.message}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {step === 3 && (
              <div className="max-h-[380px] overflow-auto mx-scroll border border-gray-200">
                <table className="mx-table">
                  <thead>
                    <tr>
                      <th>Row</th>
                      <th>Action</th>
                      {Object.keys(job.preview?.[0]?.values || {})
                        .slice(0, 8)
                        .map((k) => (
                          <th key={k}>{k}</th>
                        ))}
                    </tr>
                  </thead>
                  <tbody>
                    {(job.preview || []).map((p: any) => (
                      <tr key={p.row}>
                        <td className="num">{p.row}</td>
                        <td>
                          <Badge tone={p.action === "CREATE" ? "ok" : p.action === "SKIP" ? "warn" : "info"}>{p.action}</Badge>
                        </td>
                        {Object.keys(job.preview[0].values)
                          .slice(0, 8)
                          .map((k) => (
                            <td key={k} className="tabular">
                              {typeof p.values[k] === "object" ? JSON.stringify(p.values[k]) : String(p.values[k] ?? "")}
                            </td>
                          ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <div className="flex gap-2">
              <Button onClick={() => setStep(1)}>Back to mapping</Button>
              {step === 2 && (
                <Button variant="primary" disabled={!job.stats.can_import} onClick={() => setStep(3)}>
                  Preview
                </Button>
              )}
              {step === 3 && (
                <Button variant="primary" busy={busy} disabled={!job.stats.can_import} onClick={commit}>
                  Import {job.stats.to_create + job.stats.to_update} rows
                </Button>
              )}
            </div>
          </div>
        </Panel>
      )}
      {step === 5 && job && (
        <Panel title="6. Report">
          <div className="p-3 space-y-2 text-[12.5px]">
            <div className="text-green-600 font-semibold">✓ Import committed in one transaction ({job.entity_label}).</div>
            <div>
              Created <b>{job.stats.created}</b> · updated <b>{job.stats.updated}</b> · skipped <b>{job.stats.skipped}</b>
              {job.stats.operations_generated ? (
                <>
                  {" "}
                  · operations generated <b>{job.stats.operations_generated}</b>
                </>
              ) : null}
            </div>
            {job.stats.related_created && Object.keys(job.stats.related_created).length > 0 && <div>Also created: {Object.entries(job.stats.related_created).map(([k, v]) => `${v} ${k}`).join(", ")}</div>}
            {job.stats.error_rows > 0 && <div className="text-amber-600">◆ {job.stats.error_rows} invalid rows were not imported (see history for the list).</div>}
            <p className="text-slate-600">New data is used by the next planning run. Check Data quality before planning.</p>
            <Button onClick={reset}>New import</Button>
          </div>
        </Panel>
      )}
    </div>
  );
}

function History() {
  const jobs = useApi<any[]>("/imports");
  if (jobs.error) return <ErrorState error={jobs.error} onRetry={jobs.reload} />;
  if (!jobs.data) return <Loading />;
  return (
    <div className="h-[calc(100vh-200px)] bg-white border border-gray-200">
      <DataTable
        rows={jobs.data}
        rowKey={(r) => r.id}
        columns={[
          { key: "created_at", label: "When", render: (r) => dt(r.created_at) },
          { key: "created_by", label: "User" },
          { key: "entity_label", label: "Type" },
          { key: "filename", label: "File" },
          { key: "rows", label: "Rows", align: "right" },
          { key: "status", label: "Status", render: (r) => <StatusPill status={r.status} /> },
          { key: "stats", label: "Result", value: (r) => `${r.stats.created ?? 0}/${r.stats.updated ?? 0}`, render: (r) => (r.status === "IMPORTED" ? `${r.stats.created} created · ${r.stats.updated} updated` : r.stats.errors ? `${r.stats.errors} errors` : "") },
        ]}
      />
    </div>
  );
}

function Exports() {
  const { plant } = useSession();
  const toast = useToast();
  const templates = useApi<any[]>("/imports/templates");
  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-3 max-w-5xl">
      <Panel title="Plan export">
        <div className="p-3 text-[12.5px] space-y-2">
          <p>Excel workbook with sheets Schedule, Orders, Operations, Capacity, Materials, Alerts and KPIs, in plant local time. Available from the Planning Board toolbar and via the API:</p>
          <pre className="code bg-gray-50 border border-gray-200 p-2 whitespace-pre-wrap">GET /api/v1/plans/{"{plan_id}"}/export?format=xlsx|csv|json&amp;sheet=Schedule</pre>
        </div>
      </Panel>
      <Panel title="Data export (same columns as the import templates)">
        <table className="mx-table">
          <tbody>
            {(templates.data || []).map((t) => (
              <tr key={t.entity}>
                <td>{t.label}</td>
                <td className="w-[210px]">
                  {["xlsx", "csv", "json"].map((f) => (
                    <Button key={f} size="sm" variant="ghost" onClick={() => download(`/exports/${t.entity}`, { format: f, plant_id: plant?.id }).catch(toast.error)}>
                      {f.toUpperCase()}
                    </Button>
                  ))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>
    </div>
  );
}

function Webhooks() {
  const toast = useToast();
  const hooks = useApi<any[]>("/webhooks");
  const types = useApi<any>("/events/types");
  const [open, setOpen] = useState(false);
  const [f, setF] = useState<any>({ name: "", url: "", events: [] as string[] });
  const [created, setCreated] = useState<any>(null);
  const [deliv, setDeliv] = useState<string | null>(null);
  const deliveries = useApi<any[]>(deliv ? `/webhooks/${deliv}/deliveries` : null);
  const save = async () => {
    try {
      const w = await api("/webhooks", { body: f });
      setCreated(w);
      setOpen(false);
      hooks.reload();
    } catch (e) {
      toast.error(e);
    }
  };
  const del = async (id: string) => {
    try {
      await api(`/webhooks/${id}`, { method: "DELETE" });
      hooks.reload();
    } catch (e) {
      toast.error(e);
    }
  };
  return (
    <div className="space-y-3 max-w-5xl">
      <Panel title="Outbound webhooks — signed with HMAC-SHA256 (X-Monxu-Signature), retried with back-off" actions={<Button size="sm" variant="primary" onClick={() => setOpen(true)}>Add webhook</Button>}>
        <table className="mx-table">
          <tbody>
            {(hooks.data || []).map((w) => (
              <tr key={w.id}>
                <td>{w.name}</td>
                <td className="code truncate max-w-[280px]">{w.url}</td>
                <td>{(w.events || []).join(", ")}</td>
                <td>{w.is_active ? <Badge tone="ok">active</Badge> : <Badge tone="neutral">inactive</Badge>}</td>
                <td className="w-[160px]">
                  <Button size="sm" variant="ghost" onClick={() => setDeliv(w.id)}>
                    Deliveries
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => del(w.id)}>
                    Delete
                  </Button>
                </td>
              </tr>
            ))}
            {!hooks.data?.length && (
              <tr>
                <td className="text-slate-600">No webhooks.</td>
              </tr>
            )}
          </tbody>
        </table>
      </Panel>
      {deliv && deliveries.data && (
        <Panel title="Recent deliveries">
          <table className="mx-table">
            <tbody>
              {deliveries.data.map((d) => (
                <tr key={d.id}>
                  <td>{dt(d.created_at)}</td>
                  <td className="code">{d.event_type}</td>
                  <td>
                    <StatusPill status={d.status === "DELIVERED" ? "OK" : d.status === "FAILED" ? "FAILED" : "QUEUED"} label={d.status.toLowerCase()} />
                  </td>
                  <td className="num">{d.attempts}</td>
                  <td className="num">{d.response_code ?? ""}</td>
                  <td className="text-red-600 truncate max-w-[260px]">{d.error}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      )}
      <Dialog open={open} onClose={() => setOpen(false)} title="New webhook" footer={<><Button onClick={() => setOpen(false)}>Cancel</Button><Button variant="primary" onClick={save} disabled={!f.name || !f.url || !f.events.length}>Create</Button></>}>
        <div className="space-y-3">
          <Field label="Name">
            <input className="mx-input w-full" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} />
          </Field>
          <Field label="URL (HTTPS in production)">
            <input className="mx-input w-full" value={f.url} onChange={(e) => setF({ ...f, url: e.target.value })} placeholder="https://erp.example.com/hooks/monxu" />
          </Field>
          <Field label="Events">
            <div className="grid grid-cols-2 gap-1">
              {(types.data?.outbound || []).map((ev: string) => (
                <label key={ev} className="flex gap-2 items-center code text-[12px]">
                  <input type="checkbox" checked={f.events.includes(ev)} onChange={(e) => setF({ ...f, events: e.target.checked ? [...f.events, ev] : f.events.filter((x: string) => x !== ev) })} /> {ev}
                </label>
              ))}
            </div>
          </Field>
        </div>
      </Dialog>
      <Dialog open={!!created} onClose={() => setCreated(null)} title="Webhook created" footer={<Button onClick={() => setCreated(null)}>Done</Button>}>
        <p className="mb-2">{created?.note}</p>
        <pre className="code bg-gray-50 border border-gray-200 p-2 select-all break-all whitespace-pre-wrap">{created?.secret}</pre>
      </Dialog>
    </div>
  );
}

function Connectors() {
  const c = useApi<any>("/connectors");
  if (!c.data) return c.error ? <ErrorState error={c.error} /> : <Loading />;
  return (
    <div className="grid grid-cols-1 md:grid-cols-3 gap-3 max-w-5xl">
      {c.data.systems.map((s: any) => (
        <div key={s.code} className="mx-panel p-3">
          <div className="font-semibold">{s.label}</div>
          <div className="mt-1">{s.status === "AVAILABLE" ? <Badge tone="ok">available</Badge> : <ComingSoon feature={s.label} />}</div>
          <div className="text-[12px] text-slate-600 mt-1">{s.code === "GENERIC_REST" ? "POST events to /api/v1/events with an API key; read plans via the REST API (OpenAPI at /api/docs)." : s.code === "FILE" ? "Excel/CSV/JSON through the import wizard or POST /api/v1/imports." : "Use the generic REST API or file import until the native connector is available."}</div>
        </div>
      ))}
    </div>
  );
}

function Events() {
  const { plant } = useSession();
  const ev = useApi<any[]>(plant ? "/events" : null, plant ? { plant_id: plant.id, limit: 300 } : undefined);
  if (ev.error) return <ErrorState error={ev.error} onRetry={ev.reload} />;
  if (!ev.data) return <Loading />;
  return (
    <div className="h-[calc(100vh-200px)] bg-white border border-gray-200">
      <DataTable
        rows={ev.data}
        rowKey={(r) => r.id}
        exportName="events"
        columns={[
          { key: "received_at", label: "Received", render: (r) => dt(r.received_at) },
          { key: "occurred_at", label: "Occurred", render: (r) => dt(r.occurred_at) },
          { key: "type", label: "Type", mono: true },
          { key: "source", label: "Source" },
          { key: "correlation_id", label: "Correlation", mono: true },
          { key: "payload", label: "Payload", value: (r) => JSON.stringify(r.payload) },
          { key: "processing_result", label: "Result", value: (r) => JSON.stringify(r.processing_result) },
        ]}
      />
    </div>
  );
}
