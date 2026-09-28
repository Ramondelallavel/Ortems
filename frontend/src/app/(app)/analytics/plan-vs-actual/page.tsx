"use client";
import { useState } from "react";
import { DataTable, ErrorState, Kpi, Loading, PageHeader, Select } from "@/components/ui";
import { dt, num } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import { useSession } from "@/lib/session";

export default function PlanVsActual() {
  const { t, plant } = useSession();
  const [days, setDays] = useState("14");
  const d = useApi<any>(plant ? "/analytics/plan-vs-actual" : null, plant ? { plant_id: plant.id, days } : undefined);
  if (!plant) return <Loading />;
  return (
    <div className="flex flex-col h-full min-h-0">
      <PageHeader title={t("nav.planVsActual")} subtitle="Executed operations (MES feedback) against the last published plan that contained them." actions={<Select ariaLabel="Period" value={days} onChange={setDays} options={["7", "14", "30", "60"].map((x) => ({ value: x, label: `last ${x} days` }))} />} />
      {d.error && <ErrorState error={d.error} onRetry={d.reload} />}
      {!d.data ? (
        <Loading />
      ) : (
        <>
          <div className="grid grid-cols-2 md:grid-cols-5 gap-2 p-3">
            <Kpi label="Executed operations" value={num(d.data.operations_executed)} />
            <Kpi label="Compared with a plan" value={num(d.data.operations_compared)} />
            <Kpi label="Schedule adherence (±60 min)" value={d.data.schedule_adherence_pct === null ? "—" : num(d.data.schedule_adherence_pct, 1)} unit="%" />
            <Kpi label="Mean start deviation" value={d.data.mean_abs_start_deviation_min === null ? "—" : num(d.data.mean_abs_start_deviation_min, 0)} unit="min" />
            <Kpi label="Mean duration deviation" value={d.data.mean_duration_deviation_pct === null ? "—" : num(d.data.mean_duration_deviation_pct, 1)} unit="%" />
          </div>
          {d.data.note && <div className="px-3 pb-2 text-slate-600">{d.data.note}</div>}
          <div className="flex-1 min-h-0 bg-white border-t border-gray-200">
            <DataTable
              rows={d.data.rows}
              rowKey={(r: any) => r.op_id}
              exportName="plan-vs-actual"
              columns={[
                { key: "op_id", label: "Operation", mono: true },
                { key: "resource", label: "Resource", mono: true },
                { key: "planned_start", label: "Planned start", render: (r: any) => dt(r.planned_start) },
                { key: "actual_start", label: "Actual start", render: (r: any) => dt(r.actual_start) },
                { key: "start_deviation_min", label: "Start Δ (min)", align: "right", render: (r: any) => (r.start_deviation_min === null ? "—" : <span className={Math.abs(r.start_deviation_min) > 60 ? "text-red-600" : ""}>{Math.abs(r.start_deviation_min) > 60 ? "▲ " : ""}{r.start_deviation_min}</span>) },
                { key: "duration_deviation_pct", label: "Duration Δ %", align: "right" },
                { key: "good", label: "Good", align: "right" },
                { key: "scrap", label: "Scrap", align: "right" },
              ]}
            />
          </div>
        </>
      )}
    </div>
  );
}
