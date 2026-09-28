"use client";
import { useMemo, useState } from "react";
import { Chart, INK, SEQ_BLUE, STATUS, axisCat, axisVal } from "@/components/charts/Chart";
import { Badge, ErrorState, Loading, PageHeader, Panel, Select, statusTone } from "@/components/ui";
import { num } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import { useScenarioSelection } from "@/lib/plan";
import { useSession } from "@/lib/session";

const STATE_GLYPH: Record<string, string> = { UNDERLOADED: "○", BALANCED: "●", HIGH_LOAD: "◆", OVERLOADED: "▲", UNAVAILABLE: "✕" };

export default function CapacityPage() {
  const { t, plant } = useSession();
  const { selected } = useScenarioSelection();
  const planId = selected?.head_plan_id;
  const [bucket, setBucket] = useState("day");
  const [groupBy, setGroupBy] = useState("resource");
  const [focus, setFocus] = useState<string | null>(null);
  const load = useApi<any>(planId ? "/capacity/load" : null, planId ? { plan_id: planId, bucket, group_by: groupBy } : undefined);
  const bn = useApi<any>(planId ? "/bottlenecks" : null, planId ? { plan_id: planId } : undefined);

  const rows = useMemo(() => (load.data?.rows || []).filter((r: any) => !["LABOR_POOL", "TOOL"].includes(r.kind) || r.scheduled > 0 || r.requirement > 0), [load.data]);
  const sel = rows.find((r: any) => r.id === focus) || rows.find((r: any) => r.requirement > r.capacity) || rows[0];
  const labels = (load.data?.buckets || []).map((b: any) => b.label);

  const loadOption = useMemo(() => {
    if (!sel) return {};
    const h = (m: number) => Math.round((m / 60) * 10) / 10;
    return {
      legend: { top: 0, left: 0, data: ["Scheduled", "Overload", "Requirement", "Capacity"] },
      grid: { left: 40, right: 16, top: 36, bottom: 24, containLabel: true },
      xAxis: { ...axisCat, data: labels },
      yAxis: { ...axisVal, axisLabel: { color: INK.secondary, formatter: "{value} h" } },
      series: [
        { name: "Scheduled", type: "bar", stack: "load", barMaxWidth: 22, itemStyle: { color: SEQ_BLUE[5], borderRadius: [0, 0, 0, 0] }, data: sel.buckets.map((b: any) => h(Math.min(b.scheduled, b.capacity || b.scheduled))) },
        { name: "Overload", type: "bar", stack: "load", barMaxWidth: 22, itemStyle: { color: STATUS.bad, borderRadius: [3, 3, 0, 0] }, data: sel.buckets.map((b: any) => h(b.overload || 0)) },
        { name: "Requirement", type: "line", step: "middle", symbol: "none", lineStyle: { color: INK.primary, width: 2, type: "dashed" }, itemStyle: { color: INK.primary }, data: sel.buckets.map((b: any) => h(b.requirement)) },
        { name: "Capacity", type: "line", step: "middle", symbol: "none", lineStyle: { color: INK.muted, width: 2 }, itemStyle: { color: INK.muted }, data: sel.buckets.map((b: any) => h(b.capacity)) },
      ],
      tooltip: { trigger: "axis", valueFormatter: (v: number) => `${num(v, 1)} h` },
    };
  }, [sel, labels]);

  if (!plant) return <Loading />;
  return (
    <div className="flex flex-col h-full min-h-0">
      <PageHeader
        title={t("nav.capacity")}
        subtitle={selected ? `${selected.name} · ${selected.head_plan?.number || "no plan"} · load = scheduled work; requirement = work needed to meet due dates` : undefined}
        actions={
          <>
            <Select ariaLabel={t("cap.bucket")} value={bucket} onChange={setBucket} options={["hour", "shift", "day", "week", "month"].map((b) => ({ value: b, label: b }))} />
            <Select ariaLabel={t("cap.groupBy")} value={groupBy} onChange={(v) => { setGroupBy(v); setFocus(null); }} options={["resource", "group", "area", "plant"].map((b) => ({ value: b, label: `by ${b}` }))} />
          </>
        }
      />
      <div className="flex-1 overflow-auto mx-scroll p-3 space-y-3">
        {!planId && <div className="text-slate-600 p-4">No plan yet.</div>}
        <ErrorState error={load.error} onRetry={load.reload} />
        {planId && !load.data && <Loading />}
        {load.data && (
          <>
            <Panel title={sel ? `${t("cap.load")} — ${sel.code}${sel.name && sel.name !== sel.code ? ` · ${sel.name}` : ""}` : t("cap.load")} id="load">
              <div className="p-2">{sel && <Chart option={loadOption} height={260} ariaLabel={`Load chart of ${sel.code} by ${bucket}`} />}</div>
            </Panel>
            <Panel
              title={t("cap.heatmap")}
              id="heat"
              actions={
                <div className="flex gap-2 text-[11px] font-normal">
                  {load.data.states.map((s: string) => (
                    <span key={s} className="flex items-center gap-1">
                      <span aria-hidden="true">{STATE_GLYPH[s]}</span>
                      {t(`heat.${s}`)}
                    </span>
                  ))}
                </div>
              }
            >
              <div className="overflow-auto mx-scroll max-h-[520px]">
                <table className="border-separate border-spacing-[2px] text-[11px]" role="grid" aria-label="Capacity heatmap: utilisation per resource and period">
                  <thead>
                    <tr>
                      <th className="sticky left-0 top-0 z-10 bg-white text-left px-1 min-w-[120px]">{groupBy}</th>
                      {labels.map((l: string, i: number) => (
                        <th key={i} className="sticky top-0 bg-white font-normal text-slate-600 px-0.5 whitespace-nowrap">
                          {l}
                        </th>
                      ))}
                      <th className="sticky top-0 bg-white px-1 text-right">Total</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((r: any) => (
                      <tr key={r.id} aria-selected={sel?.id === r.id}>
                        <th scope="row" className={`sticky left-0 bg-white text-left px-1 font-normal code cursor-pointer ${sel?.id === r.id ? "underline" : ""}`} onClick={() => setFocus(r.id)}>
                          {r.code}
                        </th>
                        {r.buckets.map((b: any, i: number) => {
                          const u = b.requirement_utilization ?? b.utilization;
                          const bg = b.state === "UNAVAILABLE" ? "#dde2e8" : u === null || u === undefined ? "#f6f7f9" : SEQ_BLUE[Math.min(SEQ_BLUE.length - 1, Math.floor(Math.min(u, 1) * (SEQ_BLUE.length - 1)))];
                          const dark = u !== null && u !== undefined && u > 0.55;
                          const over = b.state === "OVERLOADED";
                          return (
                            <td
                              key={i}
                              title={`${r.code} · ${labels[i]}: ${t(`heat.${b.state}`)} — scheduled ${num(b.scheduled / 60, 1)} h, requirement ${num(b.requirement / 60, 1)} h, capacity ${num(b.capacity / 60, 1)} h`}
                              onClick={() => setFocus(r.id)}
                              className="min-w-[34px] h-[22px] text-center tabular cursor-pointer rounded-[2px]"
                              style={{ background: bg, color: dark ? "#fff" : INK.primary, outline: over ? `2px solid ${STATUS.bad}` : undefined, outlineOffset: -2 }}
                            >
                              {b.state === "UNAVAILABLE" ? "✕" : u === null || u === undefined ? "" : `${over ? "▲" : ""}${Math.round(u * 100)}`}
                            </td>
                          );
                        })}
                        <td className="text-right px-1 tabular">{r.capacity ? `${Math.round((100 * r.scheduled) / r.capacity)} %` : "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="px-2 py-1 text-[11px] text-slate-600 border-t border-gray-200">Cell value = requirement ÷ capacity in %, blue scale by magnitude; ▲ with red outline = overloaded; ✕ = no capacity (calendar or maintenance).</div>
            </Panel>
          </>
        )}
        {bn.data && (
          <Panel title={`Bottlenecks — ${bn.data.ranking_basis}`} id="bn">
            <table className="mx-table">
              <thead>
                <tr>
                  <th>#</th>
                  <th>Constraint</th>
                  <th>Kind</th>
                  <th className="!text-right">Utilisation</th>
                  <th className="!text-right">Overload</th>
                  <th className="!text-right">Induced waiting</th>
                  <th className="!text-right">Late orders</th>
                  <th>Main causes</th>
                </tr>
              </thead>
              <tbody>
                {bn.data.bottlenecks.map((b: any) => {
                  const code = rows.find((r: any) => r.id === b.resource_id)?.code || b.ref;
                  return (
                    <tr key={b.rank} onClick={() => b.resource_id && setFocus(b.resource_id)} className={b.resource_id ? "cursor-pointer" : ""}>
                      <td className="num">{b.rank}</td>
                      <td className="code">{code}</td>
                      <td>
                        <Badge tone={statusTone(b.kind)}>{b.kind.replaceAll("_", " ").toLowerCase()}</Badge>
                      </td>
                      <td className="num">{b.resource_id ? `${Math.round(b.utilization * 100)} %` : "—"}</td>
                      <td className="num">{b.overload_minutes ? `${num(b.overload_minutes / 60, 1)} h` : "—"}</td>
                      <td className="num">{b.induced_wait_minutes ? `${num(b.induced_wait_minutes / 60, 1)} h` : "—"}</td>
                      <td className="num">{b.orders_affected || "—"}</td>
                      <td className="text-slate-600">{(b.causes || []).map((c: any) => `${c.label} ${Math.round(c.share * 100)} %`).join(" · ")}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </Panel>
        )}
      </div>
    </div>
  );
}
