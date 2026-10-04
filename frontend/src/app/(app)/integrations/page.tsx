"use client";
import { useEffect, useState } from "react";
import { Badge, Button, ComingSoon, DataTable, Dialog, ErrorState, Field, Loading, PageHeader, Panel, Select, StatusPill, Tabs, useConfirm, useToast } from "@/components/ui";
import { api, download } from "@/lib/api";
import { dt } from "@/lib/format";
import { useApi, useQueryParam } from "@/lib/hooks";
import { useSession } from "@/lib/session";
import { DbConnectors } from "@/components/data/DbConnectors";
import { ImportWizard } from "@/components/data/ImportWizard";
import { WorkbookImport } from "@/components/data/WorkbookImport";

export default function IntegrationsPage() {
  const { can, t } = useSession();
  const q = useQueryParam("tab");
  const [tab, setTab] = useState("import");
  useEffect(() => {
    if (q && ["import", "workbook", "database", "history", "export", "webhooks", "connectors", "events"].includes(q)) setTab(q);
  }, [q]);
  return (
    <div className="flex flex-col h-full min-h-0">
      <PageHeader title={t("nav.integrations")} subtitle={t("int.subtitle")} />
      <Tabs
        value={tab}
        onChange={setTab}
        tabs={[
          { id: "import", label: t("int.import") },
          { id: "workbook", label: t("int.workbook") },
          ...(can("integration:manage") ? [{ id: "database", label: t("int.database") }] : []),
          { id: "history", label: t("int.history") },
          { id: "export", label: t("int.export") },
          ...(can("integration:manage") ? [{ id: "webhooks", label: "Webhooks" }, { id: "connectors", label: t("int.connectors") }] : []),
          { id: "events", label: t("int.events") },
        ]}
      />
      <div className="flex-1 min-h-0 overflow-auto mx-scroll p-3">
        {tab === "import" && <ImportWizard />}
        {tab === "workbook" && <WorkbookImport />}
        {tab === "database" && <DbConnectors />}
        {tab === "history" && <History />}
        {tab === "export" && <Exports />}
        {tab === "webhooks" && <Webhooks />}
        {tab === "connectors" && <Connectors />}
        {tab === "events" && <Events />}
      </div>
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
  const { confirm, node: confirmNode } = useConfirm();
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
  const del = async (w: any) => {
    const r = await confirm(`Delete webhook "${w.name}"?`, { danger: true, body: "Its pending deliveries are dropped and the destination receives no more events. Pausing keeps it instead." });
    if (!r.ok) return;
    try {
      await api(`/webhooks/${w.id}`, { method: "DELETE" });
      if (deliv === w.id) setDeliv(null);
      hooks.reload();
    } catch (e) {
      toast.error(e);
    }
  };
  const setActive = async (w: any, is_active: boolean) => {
    try {
      await api(`/webhooks/${w.id}`, { method: "PATCH", body: { is_active } });
      hooks.reload();
    } catch (e) {
      toast.error(e);
    }
  };
  const sendTest = async (w: any) => {
    try {
      await api(`/webhooks/${w.id}/test`, { method: "POST" });
      toast.ok(`Test delivery queued for "${w.name}": its result appears under Deliveries within a few seconds.`);
      setDeliv(w.id);
      setTimeout(() => deliveries.reload(), 12000);
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
                <td>{w.is_active ? <Badge tone="ok">active</Badge> : <Badge tone="neutral">paused</Badge>}</td>
                <td className="whitespace-nowrap text-right">
                  <Button size="sm" variant="ghost" onClick={() => setDeliv(w.id)}>
                    Deliveries
                  </Button>
                  {w.is_active && (
                    <Button size="sm" variant="ghost" onClick={() => sendTest(w)}>
                      Send test
                    </Button>
                  )}
                  <Button size="sm" variant="ghost" onClick={() => setActive(w, !w.is_active)}>
                    {w.is_active ? "Pause" : "Resume"}
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => del(w)}>
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
        <Panel
          title={`Recent deliveries — ${(hooks.data || []).find((w) => w.id === deliv)?.name || ""}`}
          actions={
            <div className="flex gap-1">
              <Button size="sm" variant="ghost" icon="refresh" onClick={deliveries.reload}>
                Refresh
              </Button>
              <Button size="sm" variant="ghost" aria-label="Close deliveries" onClick={() => setDeliv(null)}>
                ✕
              </Button>
            </div>
          }
        >
          <table className="mx-table">
            <tbody>
              {!deliveries.data.length && (
                <tr>
                  <td className="text-slate-600">No deliveries yet.</td>
                </tr>
              )}
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
      {confirmNode}
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
