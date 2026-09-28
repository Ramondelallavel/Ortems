"use client";
import { useState } from "react";
import { Badge, DataTable, ErrorState, Loading, PageHeader, Select, StatusPill } from "@/components/ui";
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
            <button className="mx-btn" onClick={() => window.print()}>
              Print
            </button>
          </>
        }
      />
      <div className="flex-1 min-h-0 bg-white">
        {d.error ? (
          <ErrorState error={d.error} onRetry={d.reload} />
        ) : !d.data ? (
          <Loading />
        ) : (
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
        )}
      </div>
    </div>
  );
}
