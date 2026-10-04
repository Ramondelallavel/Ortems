"use client";
import { useState } from "react";
import { Badge, Button, DataTable, ErrorState, Loading, PageHeader, Select, StatusPill } from "@/components/ui";
import { dt, duration } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import { useSession } from "@/lib/session";
import { SectionData } from "@/components/data/SectionData";

/** Dispatch list of the published plan (falls back to the current plan until one is published). */
export default function DispatchPage() {
  const { t, plant } = useSession();
  const [hours, setHours] = useState("24");
  const [res, setRes] = useState("");
  const resources = useApi<any>(plant ? "/resources" : null, plant ? { plant_id: plant.id } : undefined);
  const d = useApi<any>(plant ? "/dispatch" : null, plant ? { plant_id: plant.id, hours, resource_id: res || undefined } : undefined);
  if (!plant) return <Loading />;
  return (
    <div className="flex flex-col h-full min-h-0">
      <PageHeader
        title={t("nav.dispatch")}
        subtitle={d.data ? `${d.data.plan.number} (${d.data.plan.status.toLowerCase()}) · ${dt(d.data.from)} → ${dt(d.data.to)}` : undefined}
        actions={
          <>
            <SectionData tables={["actual-production", "order-operations", "maintenance", "downtimes"]} />
            <Select ariaLabel="Resource" value={res} onChange={setRes} options={[{ value: "", label: "All resources" }, ...(resources.data?.items || []).filter((r: any) => ["MACHINE", "WORK_CENTER", "LINE"].includes(r.kind)).map((r: any) => ({ value: r.id, label: r.code }))]} />
            <Select ariaLabel="Window" value={hours} onChange={setHours} options={["8", "12", "24", "48", "72", "168"].map((h) => ({ value: h, label: `next ${h} h` }))} />
            <Button icon="print" onClick={() => window.print()} disabled={!d.data}>
              Print
            </Button>
          </>
        }
      />
      <div className="flex-1 min-h-0 bg-white">
        {d.error ? (
          <ErrorState error={d.error} onRetry={d.reload} />
        ) : !d.data ? (
          <Loading />
        ) : (
          <>
          {/* the screen table is virtualised and scrolls; the printed list holds every row */}
          <PrintList data={d.data} />
          <div className="h-full print:hidden">
          <DataTable
            rows={d.data.rows}
            rowKey={(r) => r.op_id + r.resource_id}
            exportName="dispatch-list"
            initialSort={{ key: "setup_start", dir: 1 }}
            columns={[
              { key: "resource", label: "Resource", mono: true, width: 90 },
              { key: "setup_start", label: "Setup", width: 120, render: (r) => dt(r.setup_start) },
              { key: "start", label: "Start", width: 120, render: (r) => dt(r.start) },
              { key: "end", label: "End", width: 120, render: (r) => dt(r.end) },
              { key: "order", label: "Order", mono: true, width: 100 },
              { key: "op_id", label: "Operation", mono: true, width: 120 },
              { key: "operation", label: "Step" },
              { key: "product", label: "Product", mono: true },
              { key: "quantity", label: "Qty", align: "right", width: 60 },
              { key: "setup_minutes", label: "Setup", align: "right", width: 70, render: (r) => duration(r.setup_minutes) },
              { key: "material", label: "Material", width: 100, render: (r) => <StatusPill status={r.material} /> },
              { key: "status", label: "Status", width: 100, render: (r) => <StatusPill status={r.status} /> },
              { key: "flags", label: "", sortable: false, width: 90, render: (r) => <>{r.late && <Badge tone="bad">late</Badge>} {r.fixed && <Badge tone="info">🔒︎</Badge>}</> },
            ]}
          />
          </div>
          </>
        )}
      </div>
    </div>
  );
}

function PrintList({ data }: { data: any }) {
  const rows = [...data.rows].sort((a: any, b: any) => (a.resource || "").localeCompare(b.resource || "") || String(a.setup_start).localeCompare(String(b.setup_start)));
  return (
    <div className="hidden print:block">
      <h1 className="text-[15px] font-semibold">
        Dispatch list · {data.plan.number} ({data.plan.status.toLowerCase()})
      </h1>
      <div className="mb-2">
        {dt(data.from)} → {dt(data.to)} · {rows.length} operations · printed {dt(new Date())}
      </div>
      <table className="mx-table">
        <thead>
          <tr>
            <th>Resource</th>
            <th>Setup</th>
            <th>Start</th>
            <th>End</th>
            <th>Order</th>
            <th>Operation</th>
            <th>Step</th>
            <th>Product</th>
            <th className="!text-right">Qty</th>
            <th>Material</th>
            <th>Status</th>
            <th>Done</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r: any) => (
            <tr key={r.op_id + r.resource_id}>
              <td className="code">{r.resource}</td>
              <td>{dt(r.setup_start)}</td>
              <td>{dt(r.start)}</td>
              <td>{dt(r.end)}</td>
              <td className="code">{r.order}</td>
              <td className="code">{r.op_id}</td>
              <td>{r.operation}</td>
              <td className="code">{r.product}</td>
              <td className="num">{r.quantity}</td>
              <td>{r.material}</td>
              <td>
                {r.status}
                {r.late ? " · late" : ""}
                {r.fixed ? " · locked" : ""}
              </td>
              <td>☐</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
