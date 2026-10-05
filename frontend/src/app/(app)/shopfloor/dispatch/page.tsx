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
        subtitle={d.data ? `${d.data.plan.number} (${t(`status.${d.data.plan.status}`).toLowerCase()}) · ${dt(d.data.from)} → ${dt(d.data.to)}` : undefined}
        actions={
          <>
            <SectionData tables={["actual-production", "order-operations", "maintenance", "downtimes"]} />
            <Select ariaLabel={t("Resource")} value={res} onChange={setRes} options={[{ value: "", label: t("All resources") }, ...(resources.data?.items || []).filter((r: any) => ["MACHINE", "WORK_CENTER", "LINE"].includes(r.kind)).map((r: any) => ({ value: r.id, label: r.code }))]} />
            <Select ariaLabel={t("Window")} value={hours} onChange={setHours} options={["8", "12", "24", "48", "72", "168"].map((h) => ({ value: h, label: t("next {n} h", { n: h }) }))} />
            <Button icon="print" onClick={() => window.print()} disabled={!d.data}>
              {t("Print")}
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
          <PrintList data={d.data} t={t} />
          <div className="h-full print:hidden">
          <DataTable
            rows={d.data.rows}
            rowKey={(r) => r.op_id + r.resource_id}
            exportName="dispatch-list"
            initialSort={{ key: "setup_start", dir: 1 }}
            columns={[
              { key: "resource", label: t("Resource"), mono: true, width: 90 },
              { key: "setup_start", label: t("Setup"), width: 120, render: (r) => dt(r.setup_start) },
              { key: "start", label: t("Start"), width: 120, render: (r) => dt(r.start) },
              { key: "end", label: t("End"), width: 120, render: (r) => dt(r.end) },
              { key: "order", label: t("Order"), mono: true, width: 100 },
              { key: "op_id", label: t("Operation"), mono: true, width: 120 },
              { key: "operation", label: t("Step") },
              { key: "product", label: t("Product"), mono: true },
              { key: "quantity", label: t("Qty"), align: "right", width: 60 },
              { key: "setup_minutes", label: t("Setup"), align: "right", width: 70, render: (r) => duration(r.setup_minutes) },
              { key: "material", label: t("Material"), width: 100, render: (r) => <StatusPill status={r.material} /> },
              { key: "status", label: t("Status"), width: 100, render: (r) => <StatusPill status={r.status} /> },
              { key: "flags", label: "", sortable: false, width: 90, render: (r) => <>{r.late && <Badge tone="bad">{t("late")}</Badge>} {r.fixed && <Badge tone="info">🔒︎</Badge>}</> },
            ]}
          />
          </div>
          </>
        )}
      </div>
    </div>
  );
}

function PrintList({ data, t }: { data: any; t: (k: string, v?: Record<string, string | number>) => string }) {
  const rows = [...data.rows].sort((a: any, b: any) => (a.resource || "").localeCompare(b.resource || "") || String(a.setup_start).localeCompare(String(b.setup_start)));
  return (
    <div className="hidden print:block">
      <h1 className="text-[15px] font-semibold">
        {t("Dispatch list")} · {data.plan.number} ({t(`status.${data.plan.status}`).toLowerCase()})
      </h1>
      <div className="mb-2">
        {dt(data.from)} → {dt(data.to)} · {t("{n} operations", { n: rows.length })} · {t("printed {when}", { when: dt(new Date()) })}
      </div>
      <table className="mx-table">
        <thead>
          <tr>
            <th>{t("Resource")}</th>
            <th>{t("Setup")}</th>
            <th>{t("Start")}</th>
            <th>{t("End")}</th>
            <th>{t("Order")}</th>
            <th>{t("Operation")}</th>
            <th>{t("Step")}</th>
            <th>{t("Product")}</th>
            <th className="!text-right">{t("Qty")}</th>
            <th>{t("Material")}</th>
            <th>{t("Status")}</th>
            <th>{t("Done")}</th>
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
              <td>{t(`status.${r.material}`)}</td>
              <td>
                {t(`status.${r.status}`)}
                {r.late ? ` · ${t("late")}` : ""}
                {r.fixed ? ` · ${t("locked")}` : ""}
              </td>
              <td>☐</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
