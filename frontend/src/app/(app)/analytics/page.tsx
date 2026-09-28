"use client";
import { useEffect, useMemo, useState } from "react";
import { Chart, INK, SERIES, axisCat, axisVal } from "@/components/charts/Chart";
import { Badge, Button, ErrorState, Kpi, Loading, PageHeader, Panel, StatusPill, Tabs, useToast } from "@/components/ui";
import { api } from "@/lib/api";
import { dt, duration, num } from "@/lib/format";
import { useApi, useQueryParam } from "@/lib/hooks";
import { useScenarioSelection } from "@/lib/plan";
import { useSession } from "@/lib/session";

export default function AnalyticsPage() {
  const { t, plant } = useSession();
  const toast = useToast();
  const { selected } = useScenarioSelection();
  const planId = selected?.head_plan_id;
  const kpiParam = useQueryParam("kpi");
  const [code, setCode] = useState<string>("otif");
  const [tab, setTab] = useState("kpis");
  useEffect(() => {
    if (kpiParam) setCode(kpiParam);
  }, [kpiParam]);
  const k = useApi<any>(planId ? "/kpis" : null, planId ? { plan_id: planId } : undefined);
  const dd = useApi<any>(planId ? `/kpis/${code}/drilldown` : null, planId ? { plan_id: planId } : undefined);
  const trends = useApi<any>(plant && tab === "trends" ? "/analytics/trends" : null, plant ? { plant_id: plant.id } : undefined);
  const [sim, setSim] = useState<any>(null);
  const [sens, setSens] = useState<any>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const groups = useMemo(() => {
    const g: Record<string, any[]> = {};
    for (const c of k.data?.catalogue || []) (g[c.group] ||= []).push(c);
    return g;
  }, [k.data]);

  const trendOption = useMemo(() => {
    if (!trends.data) return {};
    const pts: any[] = trends.data.points || [];
    const codes = ["otif", "utilization", "late_orders", "setup_h"];
    return {
      legend: { top: 0, left: 0 },
      grid: { left: 40, right: 16, top: 32, bottom: 24, containLabel: true },
      xAxis: { ...axisCat, data: pts.map((p: any) => p.number.replace("PLAN-", "")) },
      yAxis: { ...axisVal },
      tooltip: { trigger: "axis" },
      series: codes.map((name, i) => ({ name, type: "line", symbolSize: 8, lineStyle: { width: 2, color: SERIES[i] }, itemStyle: { color: SERIES[i] }, data: pts.map((p: any) => p[name] ?? null) })),
    };
  }, [trends.data]);

  const causeOption = useMemo(() => {
    const c = dd.data?.causes;
    if (!c?.length) return null;
    return {
      grid: { left: 110, right: 30, top: 8, bottom: 8, containLabel: false },
      xAxis: { ...axisVal, show: false },
      yAxis: { ...axisCat, type: "category", data: c.map((x: any) => x.category).reverse(), axisLabel: { color: INK.primary } },
      tooltip: { trigger: "item", formatter: (p: any) => `${p.name}: ${p.value} orders` },
      series: [{ type: "bar", barMaxWidth: 16, itemStyle: { color: SERIES[0], borderRadius: [0, 3, 3, 0] }, label: { show: true, position: "right", color: INK.primary }, data: c.map((x: any) => x.orders).reverse() }],
    };
  }, [dd.data]);

  const runSim = async () => {
    if (!planId) return;
    setBusy("sim");
    try {
      setSim(await api(`/plans/${planId}/simulate`, { body: { runs: 30 } }));
    } catch (e) {
      toast.error(e);
    } finally {
      setBusy(null);
    }
  };
  const runSens = async () => {
    if (!planId) return;
    setBusy("sens");
    try {
      setSens(await api(`/plans/${planId}/sensitivity`, { method: "POST" }));
    } catch (e) {
      toast.error(e);
    } finally {
      setBusy(null);
    }
  };

  if (!plant) return <Loading />;
  const v = k.data?.values || {};
  return (
    <div className="flex flex-col h-full min-h-0">
      <PageHeader title={t("nav.kpis")} subtitle={k.data ? `${k.data.plan.number} · every figure opens its drill-down` : undefined} />
      <Tabs value={tab} onChange={setTab} tabs={[{ id: "kpis", label: "KPIs & root cause" }, { id: "trends", label: "Trends" }, { id: "robust", label: "Robustness" }]} />
      <div className="flex-1 overflow-auto mx-scroll p-3 space-y-3">
        {!planId && <div className="text-slate-600">No plan yet.</div>}
        <ErrorState error={k.error} onRetry={k.reload} />
        {tab === "kpis" && k.data && (
          <>
            {Object.entries(groups).map(([g, cs]) => (
              <div key={g}>
                <h2 className="mx-label">{g}</h2>
                <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2">
                  {cs.map((c: any) => (
                    <div key={c.code} className={code === c.code ? "ring-2 ring-navy-700 rounded-[3px]" : ""}>
                      <Kpi label={c.label} value={v[c.code] === null || v[c.code] === undefined ? "—" : num(v[c.code], c.unit === "%" || c.unit === "h" ? 1 : 0)} unit={c.unit} onClick={() => setCode(c.code)} />
                    </div>
                  ))}
                </div>
              </div>
            ))}
            <Panel title={`Drill-down: ${code}`} id="dd">
              {dd.loading && <Loading />}
              {dd.data && (
                <div className="p-3 grid grid-cols-1 lg:grid-cols-2 gap-4">
                  <div className="space-y-2">
                    {dd.data.levels && (
                      <div className="flex gap-4 text-[12.5px]">
                        {dd.data.levels.map((l: any) => (
                          <span key={l.label}>
                            {l.label}: <b className="tabular">{l.value}</b>
                            {l.share !== undefined && l.share !== null ? ` (${Math.round(l.share * 100)} %)` : ""}
                          </span>
                        ))}
                      </div>
                    )}
                    {causeOption && <Chart option={causeOption} height={Math.max(120, 28 * dd.data.causes.length)} ariaLabel="Late orders by primary cause" />}
                    {dd.data.resources?.length > 0 && (
                      <table className="mx-table">
                        <thead>
                          <tr>
                            <th>Resource</th>
                            <th className="!text-right">{dd.data.resources[0].orders !== undefined ? "Late orders" : dd.data.resources[0].setup_h !== undefined ? "Setup h" : "Utilisation"}</th>
                          </tr>
                        </thead>
                        <tbody>
                          {dd.data.resources.slice(0, 15).map((r: any, i: number) => (
                            <tr key={i}>
                              <td className="code">{r.resource || r.resource_id?.slice(0, 8)}</td>
                              <td className="num">{r.orders ?? r.setup_h ?? (r.utilization !== undefined ? `${num(r.utilization, 1)} %` : "")}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    )}
                    {dd.data.materials?.length > 0 && <div className="text-[12.5px]">Short materials: {dd.data.materials.length}</div>}
                    {!dd.data.levels && !dd.data.resources && !dd.data.materials && <div className="text-slate-600">Value {dd.data.value ?? "—"}. No further breakdown for this indicator.</div>}
                  </div>
                  {dd.data.orders && (
                    <div className="max-h-[360px] overflow-auto mx-scroll">
                      <table className="mx-table">
                        <thead>
                          <tr>
                            <th>Order</th>
                            <th>Status</th>
                            <th className="!text-right">Delay</th>
                            <th>Root cause</th>
                          </tr>
                        </thead>
                        <tbody>
                          {dd.data.orders.map((o: any) => (
                            <tr key={o.order_id}>
                              <td className="code">{o.number}</td>
                              <td>
                                <StatusPill status={o.status} />
                              </td>
                              <td className="num">{o.lateness_minutes ? duration(o.lateness_minutes) : "—"}</td>
                              <td className="truncate max-w-[280px]" title={o.cause?.text}>
                                <Badge tone="neutral" glyph={false}>
                                  {o.cause?.category}
                                </Badge>{" "}
                                {o.cause?.text}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>
              )}
            </Panel>
          </>
        )}
        {tab === "trends" && (
          <Panel title="KPI trend across plan versions">
            {trends.error && <ErrorState error={trends.error} />}
            {!trends.data ? <Loading /> : <div className="p-2"><Chart option={trendOption} height={300} ariaLabel="KPI trends over plan versions" /></div>}
          </Panel>
        )}
        {tab === "robust" && (
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
            <Panel title="Monte Carlo simulation" actions={<Button size="sm" variant="primary" busy={busy === "sim"} onClick={runSim}>Run 30 replications</Button>}>
              <div className="p-3 text-[12.5px] space-y-2">
                <p className="text-slate-600">Replays the plan&apos;s sequence with random run-time variation (±10 %), supplier delays and breakdowns. Shows how fragile the delivery promises are.</p>
                {sim && (
                  <>
                    <div className="grid grid-cols-2 gap-2">
                      {["late_orders", "throughput_units", "lead_time_h", "wip_orders"].map((kk) => (
                        <div key={kk} className="mx-panel px-2 py-1">
                          <div className="text-[11px] text-slate-600">{kk.replaceAll("_", " ")}</div>
                          <div className="tabular">
                            p10 {num(sim[kk]?.p10, 1)} · p50 {num(sim[kk]?.p50, 1)} · p90 {num(sim[kk]?.p90, 1)}
                          </div>
                        </div>
                      ))}
                    </div>
                    <table className="mx-table">
                      <thead>
                        <tr>
                          <th>Order</th>
                          <th>Planned end</th>
                          <th className="!text-right">P(on time)</th>
                        </tr>
                      </thead>
                      <tbody>
                        {[...(sim.orders || [])]
                          .sort((a: any, b: any) => a.on_time_probability - b.on_time_probability)
                          .slice(0, 12)
                          .map((o: any) => (
                            <tr key={o.order_id}>
                              <td className="code">{o.number}</td>
                              <td className="tabular">{dt(o.planned_end)}</td>
                              <td className={`num ${o.on_time_probability < 0.8 ? "text-red-600" : ""}`}>{Math.round(o.on_time_probability * 100)} %</td>
                            </tr>
                          ))}
                      </tbody>
                    </table>
                  </>
                )}
              </div>
            </Panel>
            <Panel title="Sensitivity: where does extra capacity pay off?" actions={<Button size="sm" variant="primary" busy={busy === "sens"} onClick={runSens}>Analyse</Button>}>
              <div className="p-3 text-[12.5px] space-y-2">
                <p className="text-slate-600">Re-plans with +1 shift or +10 % speed on the top bottlenecks and reports the measured effect on late orders.</p>
                {sens && (
                  <table className="mx-table">
                    <thead>
                      <tr>
                        <th>Experiment</th>
                        <th className="!text-right">Added h</th>
                        <th className="!text-right">Δ late orders</th>
                        <th className="!text-right">Δ tardiness h</th>
                        <th className="!text-right">Δ units</th>
                      </tr>
                    </thead>
                    <tbody>
                      {sens.experiments.map((e: any, i: number) => (
                        <tr key={i}>
                          <td>{e.label}</td>
                          <td className="num">{e.added_hours ?? "—"}</td>
                          <td className={`num ${e.delta_late_orders < 0 ? "text-green-600" : ""}`}>{num(e.delta_late_orders, 0)}</td>
                          <td className="num">{num(e.delta_tardiness_h, 1)}</td>
                          <td className="num">{num(e.delta_throughput_units, 0)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            </Panel>
          </div>
        )}
      </div>
    </div>
  );
}
