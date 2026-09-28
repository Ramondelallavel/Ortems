"use client";
import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";
import { Chart, INK, SEQ_BLUE, STATUS, axisVal } from "@/components/charts/Chart";
import { Badge, Button, DataTable, Dialog, ErrorState, Field, Loading, PageHeader, Panel, StatusPill, Tabs, useToast } from "@/components/ui";
import { api } from "@/lib/api";
import { dt, duration, num } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import { useScenarioSelection } from "@/lib/plan";
import { useSession } from "@/lib/session";
import { SectionData } from "@/components/data/SectionData";

export default function MaterialsPage() {
  const { t, plant, can } = useSession();
  const router = useRouter();
  const toast = useToast();
  const { selected } = useScenarioSelection();
  const planId = selected?.head_plan_id;
  const [tab, setTab] = useState("shortages");
  const [mat, setMat] = useState<{ id: string; code: string } | null>(null);
  const [delay, setDelay] = useState<{ id: string; code: string } | null>(null);
  const [days, setDays] = useState("3");
  const av = useApi<any>(planId ? "/materials/availability" : null, planId ? { plan_id: planId } : undefined);
  const receipts = useApi<any[]>(plant && tab === "receipts" ? "/receipts" : null, plant ? { plant_id: plant.id } : undefined);
  const proj = useApi<any>(mat && planId ? `/materials/${mat.id}/projection` : null, planId ? { plan_id: planId } : undefined);
  const impact = useApi<any>(mat && planId ? `/materials/${mat.id}/impact` : null, planId ? { plan_id: planId } : undefined);

  const projOption = useMemo(() => {
    if (!proj.data) return {};
    const pts = proj.data.points.map((p: any) => [p.time, p.level]);
    return {
      grid: { left: 48, right: 16, top: 24, bottom: 28, containLabel: true },
      xAxis: { type: "time", axisLine: { lineStyle: { color: INK.axis } }, axisLabel: { color: INK.secondary } },
      yAxis: { ...axisVal, name: proj.data.uom },
      tooltip: { trigger: "axis", valueFormatter: (v: number) => `${num(v, 2)} ${proj.data.uom}` },
      series: [
        { name: "Projected stock", type: "line", step: "end", symbol: "none", lineStyle: { color: SEQ_BLUE[6], width: 2 }, areaStyle: { color: "rgba(42,120,214,0.08)" }, data: pts, markLine: { symbol: "none", silent: true, data: [{ yAxis: 0, lineStyle: { color: STATUS.bad, type: "solid" }, label: { formatter: "zero", color: STATUS.bad } }, ...(proj.data.safety_stock ? [{ yAxis: proj.data.safety_stock, lineStyle: { color: STATUS.warn, type: "dashed" }, label: { formatter: "safety stock", color: STATUS.warn } }] : [])] } },
      ],
    };
  }, [proj.data]);

  const whatIfDelay = async () => {
    if (!delay || !plant) return;
    try {
      const sc = await api("/scenarios/what-if", { body: { plant_id: plant.id, kind: "MATERIAL_DELAY", params: { material_id: delay.id, delay_minutes: Math.round(Number(days) * 1440) }, name: `What-if: ${delay.code} +${days} d`, run: true } });
      toast.ok(`Scenario "${sc.name}" created and planning started.`);
      router.push(`/planning/scenarios?id=${sc.id}`);
    } catch (e) {
      toast.error(e);
    }
  };

  if (!plant) return <Loading />;
  const d = av.data;
  return (
    <div className="flex flex-col h-full min-h-0">
      <PageHeader
        title={t("nav.materials")}
        subtitle={d ? Object.entries(d.status_counts).map(([k, v]) => `${t(`status.${k}`)} ${v}`).join(" · ") : undefined}
        actions={<SectionData tables={["inventory", "material-lots", "purchase-orders", "purchase-orders.lines", "materials", "boms", "boms.lines", "suppliers", "inventory-transactions"]} />}
      />
      <Tabs value={tab} onChange={setTab} tabs={[{ id: "shortages", label: "Shortages & late supply" }, { id: "orders", label: "Order material status" }, { id: "receipts", label: "Open receipts" }]} />
      <div className="flex flex-1 min-h-0">
        <div className="flex-1 min-w-0 overflow-auto mx-scroll p-3 space-y-3">
          <ErrorState error={av.error} onRetry={av.reload} />
          {planId && !d && <Loading />}
          {d && tab === "shortages" && (
            <>
              <Panel title={`Shortages — demand no stock or receipt covers (${d.shortages.length})`} id="sh">
                <table className="mx-table">
                  <thead>
                    <tr>
                      <th>Material</th>
                      <th className="!text-right">Required</th>
                      <th className="!text-right">Shortfall</th>
                      <th className="!text-right">Orders</th>
                      <th className="!text-right">Replenishment lead time</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {d.shortages.map((s: any) => (
                      <tr key={s.material_id} className="cursor-pointer" onClick={() => setMat({ id: s.material_id, code: s.material })}>
                        <td className="code">▲ {s.material}</td>
                        <td className="num">{num(s.required, 2)} {s.uom}</td>
                        <td className="num text-red-600">{num(s.shortfall, 2)}</td>
                        <td className="num">{s.orders.length}</td>
                        <td className="num">{s.replenishment_lead_time_minutes ? duration(s.replenishment_lead_time_minutes) : "—"}</td>
                        <td>
                          {can("scenario:write") && (
                            <Button size="sm" variant="ghost" onClick={(e) => { e.stopPropagation(); setDelay({ id: s.material_id, code: s.material }); }}>
                              What-if delay…
                            </Button>
                          )}
                        </td>
                      </tr>
                    ))}
                    {!d.shortages.length && (
                      <tr>
                        <td className="text-slate-600">✓ No shortage.</td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </Panel>
              <Panel title={`Late supply — operations waiting for material (${d.late_supply.length})`} id="ls">
                <table className="mx-table">
                  <thead>
                    <tr>
                      <th>Material</th>
                      <th className="!text-right">Operations</th>
                      <th className="!text-right">Waiting (working time)</th>
                      <th className="!text-right">Orders</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {d.late_supply.map((s: any) => (
                      <tr key={s.material_id} className="cursor-pointer" onClick={() => setMat({ id: s.material_id, code: s.material })}>
                        <td className="code">◆ {s.material}</td>
                        <td className="num">{s.operations}</td>
                        <td className="num">{duration(s.wait_minutes)}</td>
                        <td className="num">{s.orders.length}</td>
                        <td>
                          {can("scenario:write") && (
                            <Button size="sm" variant="ghost" onClick={(e) => { e.stopPropagation(); setDelay({ id: s.material_id, code: s.material }); }}>
                              What-if delay…
                            </Button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </Panel>
            </>
          )}
          {d && tab === "orders" && (
            <div className="h-[calc(100vh-190px)] bg-white border border-gray-200">
              <DataTable
                rows={d.orders}
                rowKey={(r: any) => r.order_id}
                exportName="order-material-status"
                columns={[
                  { key: "number", label: "Order", mono: true },
                  { key: "material_status", label: "Material", render: (r: any) => <StatusPill status={r.material_status} /> },
                  { key: "status", label: "Plan", render: (r: any) => <StatusPill status={r.status} /> },
                  { key: "due", label: "Due", render: (r: any) => dt(r.due) },
                  { key: "end", label: "Planned end", render: (r: any) => dt(r.end) },
                ]}
                onRowClick={(r: any) => router.push(`/planning/orders?q=${r.number}`)}
                toolbar={
                  d.orders_total > d.orders.length ? (
                    <span className="text-[11.5px] text-slate-600">
                      The {d.orders.length.toLocaleString()} most critical of {d.orders_total.toLocaleString()} orders with material status — the order book filters all of them
                    </span>
                  ) : undefined
                }
              />
            </div>
          )}
          {tab === "receipts" && (
            <div className="h-[calc(100vh-190px)] bg-white border border-gray-200">
              {!receipts.data ? (
                <Loading />
              ) : (
                <DataTable
                  rows={receipts.data}
                  rowKey={(r: any) => r.line_id}
                  exportName="open-receipts"
                  onRowClick={(r: any) => setMat({ id: r.item_id, code: r.item })}
                  columns={[
                    { key: "purchase_order", label: "PO", mono: true },
                    { key: "line", label: "Line", align: "right" },
                    { key: "item", label: "Item", mono: true },
                    { key: "supplier", label: "Supplier" },
                    { key: "quantity", label: "Qty", align: "right" },
                    { key: "received", label: "Received", align: "right" },
                    { key: "expected", label: "Expected", render: (r: any) => dt(r.expected), value: (r: any) => r.expected },
                    { key: "original", label: "Original date", render: (r: any) => (r.original && r.original !== r.expected ? <span className="text-amber-600">◆ {dt(r.original)}</span> : "") },
                    { key: "confirmed", label: "Confirmed", render: (r: any) => (r.confirmed ? <Badge tone="ok">yes</Badge> : <Badge tone="warn">no</Badge>) },
                  ]}
                />
              )}
            </div>
          )}
        </div>
        {mat && (
          <aside className="w-[460px] max-w-full border-l border-gray-200 bg-white overflow-auto mx-scroll">
            <div className="mx-panel-head">
              <span className="code flex-1">{mat.code}</span>
              <button className="mx-btn mx-btn-ghost mx-btn-sm" onClick={() => setMat(null)} aria-label="Close">
                ✕
              </button>
            </div>
            <div className="p-3 space-y-3">
              <ErrorState error={proj.error} />
              {proj.data && (
                <>
                  <div className="text-[12px] text-slate-600">
                    {proj.data.name} · supply {num(proj.data.supply_total, 2)} · demand {num(proj.data.demand_total, 2)} {proj.data.uom}
                  </div>
                  {proj.data.alerts.map((a: any, i: number) => (
                    <div key={i} className={a.type === "STOCKOUT" ? "text-red-600" : "text-amber-600"}>
                      {a.type === "STOCKOUT" ? "▲" : "◆"} {a.type.replaceAll("_", " ").toLowerCase()} {a.at ? `at ${dt(a.at)}` : ""} (level {num(a.level, 2)})
                    </div>
                  ))}
                  <Chart option={projOption} height={220} ariaLabel={`Projected stock of ${mat.code}`} />
                </>
              )}
              {impact.data && (
                <div>
                  <h3 className="mx-label">
                    Depending orders: {impact.data.orders_affected} · customers {impact.data.customers_affected}
                    {impact.data.revenue_exposure ? ` · revenue ${num(impact.data.revenue_exposure)} €` : ""}
                  </h3>
                  <table className="mx-table">
                    <tbody>
                      {impact.data.orders.map((o: any) => (
                        <tr key={o.order_id}>
                          <td className="code">{o.number}</td>
                          <td className="truncate max-w-[120px]">{o.customer || "—"}</td>
                          <td className="tabular">{dt(o.due)}</td>
                          <td>
                            <StatusPill status={o.status} />
                          </td>
                          <td className="text-[11px] text-slate-600">{o.direct ? "direct" : "via component"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              {proj.loading && <Loading />}
            </div>
          </aside>
        )}
      </div>
      <Dialog
        open={!!delay}
        onClose={() => setDelay(null)}
        title={`What-if: delay supply of ${delay?.code}`}
        footer={
          <>
            <Button onClick={() => setDelay(null)}>Cancel</Button>
            <Button variant="primary" icon="play" onClick={whatIfDelay}>
              Create scenario and plan
            </Button>
          </>
        }
      >
        <Field label="Delay (days)" hint="All open receipts of this material arrive later by this amount in a copy of the live plan.">
          <input className="mx-input w-[120px]" type="number" min={0.25} step={0.25} value={days} onChange={(e) => setDays(e.target.value)} />
        </Field>
      </Dialog>
    </div>
  );
}
