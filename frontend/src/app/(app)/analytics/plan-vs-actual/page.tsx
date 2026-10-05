"use client";
import { useState } from "react";
import { DataTable, ErrorState, Kpi, Loading, PageHeader, Select } from "@/components/ui";
import { dt, num } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import { useSession } from "@/lib/session";
import { SectionData } from "@/components/data/SectionData";

export default function PlanVsActual() {
  const { t, plant } = useSession();
  const [days, setDays] = useState("14");
  const d = useApi<any>(plant ? "/analytics/plan-vs-actual" : null, plant ? { plant_id: plant.id, days } : undefined);
  if (!plant) return <Loading />;
  return (
    <div className="flex flex-col h-full min-h-0">
      <PageHeader title={t("nav.planVsActual")} subtitle={t("Executed operations (MES feedback) against the last published plan that contained them.")} actions={<><SectionData tables={["actual-production"]} /><Select ariaLabel={t("Period")} value={days} onChange={setDays} options={["7", "14", "30", "60"].map((x) => ({ value: x, label: t("last {n} days", { n: x }) }))} /></>} />
      {d.error && <ErrorState error={d.error} onRetry={d.reload} />}
      {!d.data ? (
        <Loading />
      ) : (
        <>
          <div className="grid grid-cols-2 md:grid-cols-5 gap-2 p-3">
            <Kpi label={t("Executed operations")} value={num(d.data.operations_executed)} />
            <Kpi label={t("Compared with a plan")} value={num(d.data.operations_compared)} />
            <Kpi label={t("Schedule adherence (±60 min)")} value={d.data.schedule_adherence_pct === null ? "—" : num(d.data.schedule_adherence_pct, 1)} unit="%" />
            <Kpi label={t("Mean start deviation")} value={d.data.mean_abs_start_deviation_min === null ? "—" : num(d.data.mean_abs_start_deviation_min, 0)} unit="min" />
            <Kpi label={t("Mean duration deviation")} value={d.data.mean_duration_deviation_pct === null ? "—" : num(d.data.mean_duration_deviation_pct, 1)} unit="%" />
          </div>
          {d.data.note && <div className="px-3 pb-2 text-slate-600">{t(d.data.note)}</div>}
          <div className="flex-1 min-h-0 bg-white border-t border-gray-200">
            <DataTable
              rows={d.data.rows}
              rowKey={(r: any) => r.op_id}
              exportName="plan-vs-actual"
              columns={[
                { key: "op_id", label: t("Operation"), mono: true },
                { key: "resource", label: t("Resource"), mono: true },
                { key: "planned_start", label: t("Planned start"), render: (r: any) => dt(r.planned_start) },
                { key: "actual_start", label: t("Actual start"), render: (r: any) => dt(r.actual_start) },
                { key: "start_deviation_min", label: t("Start Δ (min)"), align: "right", render: (r: any) => (r.start_deviation_min === null ? "—" : <span className={Math.abs(r.start_deviation_min) > 60 ? "text-red-600" : ""}>{Math.abs(r.start_deviation_min) > 60 ? "▲ " : ""}{r.start_deviation_min}</span>) },
                { key: "duration_deviation_pct", label: t("Duration Δ %"), align: "right" },
                { key: "good", label: t("Good"), align: "right" },
                { key: "scrap", label: t("Scrap"), align: "right" },
              ]}
            />
          </div>
        </>
      )}
    </div>
  );
}
