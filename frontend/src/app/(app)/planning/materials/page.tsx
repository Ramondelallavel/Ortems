"use client";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { Chart, INK, SEQ_BLUE, STATUS, axisVal } from "@/components/charts/Chart";
import { Badge, Button, DataTable, Dialog, ErrorState, Field, Loading, PageHeader, Panel, StatusPill, Tabs, useToast } from "@/components/ui";
import { api } from "@/lib/api";
import { dt, duration, num } from "@/lib/format";
import { useApi, useQueryParam } from "@/lib/hooks";
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
  // deep link from an alert: /planning/materials?material=<code> opens that material's projection
  const matParam = useQueryParam("material");
  const matLookup = useApi<any>(matParam ? "/master-data/items" : null, { q: matParam, limit: 20 });
  useEffect(() => {
    const hit = (matLookup.data?.items || []).find((i: any) => i.code === matParam);
    if (hit) setMat({ id: hit.id, code: hit.code });
  }, [matLookup.data, matParam]);
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
        { name: t("Projected stock"), type: "line", step: "end", symbol: "none", lineStyle: { color: SEQ_BLUE[6], width: 2 }, areaStyle: { color: "rgba(42,120,214,0.08)" }, data: pts, markLine: { symbol: "none", silent: true, data: [{ yAxis: 0, lineStyle: { color: STATUS.bad, type: "solid" }, label: { formatter: t("zero"), color: STATUS.bad } }, ...(proj.data.safety_stock ? [{ yAxis: proj.data.safety_stock, lineStyle: { color: STATUS.warn, type: "dashed" }, label: { formatter: t("safety stock"), color: STATUS.warn } }] : [])] } },
      ],
    };
  }, [proj.data, t]);

  const whatIfDelay = async () => {
    if (!delay || !plant) return;
    try {
      const sc = await api("/scenarios/what-if", { body: { plant_id: plant.id, kind: "MATERIAL_DELAY", params: { material_id: delay.id, delay_minutes: Math.round(Number(days) * 1440) }, name: `${t("What-if")}: ${delay.code} +${days} d`, run: true } });
      if (sc.run_blocked) toast.warn(t("run.whatIfBlocked"));
      else toast.ok(t("Scenario “{name}” created; planning started.", { name: sc.name }));
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
      <Tabs value={tab} onChange={setTab} tabs={[{ id: "shortages", label: t("Shortages & late supply") }, { id: "orders", label: t("Order material status") }, { id: "receipts", label: t("Open receipts") }]} />
      <div className="flex flex-1 min-h-0">
        <div className="flex-1 min-w-0 overflow-auto mx-scroll p-3 space-y-3">
          <ErrorState error={av.error} onRetry={av.reload} />
          {planId && !d && <Loading />}
          {d && tab === "shortages" && (
            <>
              <Panel title={t("Shortages — demand no stock or receipt covers ({n})", { n: d.shortages.length })} id="sh">
                <table className="mx-table">
                  <thead>
                    <tr>
                      <th>{t("Material")}</th>
                      <th className="!text-right">{t("Required")}</th>
                      <th className="!text-right">{t("Shortfall")}</th>
                      <th className="!text-right">{t("Orders")}</th>
                      <th className="!text-right">{t("Replenishment lead time")}</th>
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
                              {t("What-if delay…")}
                            </Button>
                          )}
                        </td>
                      </tr>
                    ))}
                    {!d.shortages.length && (
                      <tr>
                        <td className="text-slate-600">✓ {t("No shortage.")}</td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </Panel>
              <Panel title={t("Late supply — operations waiting for material ({n})", { n: d.late_supply.length })} id="ls">
                <table className="mx-table">
                  <thead>
                    <tr>
                      <th>{t("Material")}</th>
                      <th className="!text-right">{t("Operations")}</th>
                      <th className="!text-right">{t("Waiting (working time)")}</th>
                      <th className="!text-right">{t("Orders")}</th>
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
                              {t("What-if delay…")}
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
                  { key: "number", label: t("Order"), mono: true },
                  { key: "material_status", label: t("Material"), render: (r: any) => <StatusPill status={r.material_status} /> },
                  { key: "status", label: t("Plan"), render: (r: any) => <StatusPill status={r.status} /> },
                  { key: "due", label: t("Due"), render: (r: any) => dt(r.due) },
                  { key: "end", label: t("Planned end"), render: (r: any) => dt(r.end) },
                ]}
                onRowClick={(r: any) => router.push(`/planning/orders?q=${r.number}`)}
                toolbar={
                  d.orders_total > d.orders.length ? (
                    <span className="text-[11.5px] text-slate-600">
                      {t("The {n} most critical of {total} orders with material status — the order book filters all of them", { n: d.orders.length.toLocaleString(), total: d.orders_total.toLocaleString() })}
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
                    { key: "purchase_order", label: t("PO"), mono: true },
                    { key: "line", label: t("Line"), align: "right" },
                    { key: "item", label: t("Item"), mono: true },
                    { key: "supplier", label: t("Supplier") },
                    { key: "quantity", label: t("Qty"), align: "right" },
                    { key: "received", label: t("Received"), align: "right" },
                    { key: "expected", label: t("Expected"), render: (r: any) => dt(r.expected), value: (r: any) => r.expected },
                    { key: "original", label: t("Original date"), render: (r: any) => (r.original && r.original !== r.expected ? <span className="text-amber-600">◆ {dt(r.original)}</span> : "") },
                    { key: "confirmed", label: t("Confirmed"), render: (r: any) => (r.confirmed ? <Badge tone="ok">{t("common.yes")}</Badge> : <Badge tone="warn">{t("common.no")}</Badge>) },
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
              <button className="mx-btn mx-btn-ghost mx-btn-sm" onClick={() => setMat(null)} aria-label={t("Close")}>
                ✕
              </button>
            </div>
            <div className="p-3 space-y-3">
              <ErrorState error={proj.error} />
              {proj.data && (
                <>
                  <div className="text-[12px] text-slate-600">
                    {t("{name} · supply {s} · demand {d} {uom}", { name: proj.data.name, s: num(proj.data.supply_total, 2), d: num(proj.data.demand_total, 2), uom: proj.data.uom || "" })}
                  </div>
                  {proj.data.alerts.map((a: any, i: number) => (
                    <div key={i} className={a.type === "STOCKOUT" ? "text-red-600" : "text-amber-600"}>
                      {a.type === "STOCKOUT" ? "▲" : "◆"} {t(`proj.${a.type}`).startsWith("proj.") ? a.type.replaceAll("_", " ").toLowerCase() : t(`proj.${a.type}`)} {a.at ? t("at {at}", { at: dt(a.at) }) : ""} ({t("level {n}", { n: num(a.level, 2) })})
                    </div>
                  ))}
                  <Chart option={projOption} height={220} ariaLabel={t("Projected stock of {code}", { code: mat.code })} />
                </>
              )}
              {impact.data && (
                <div>
                  <h3 className="mx-label">
                    {t("Depending orders: {n} · customers {c}", { n: impact.data.orders_affected, c: impact.data.customers_affected })}
                    {impact.data.revenue_exposure ? ` · ${t("revenue {v} €", { v: num(impact.data.revenue_exposure) })}` : ""}
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
                          <td className="text-[11px] text-slate-600">{o.direct ? t("direct") : t("via component")}</td>
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
        title={t("What-if: delay supply of {code}", { code: delay?.code || "" })}
        footer={
          <>
            <Button onClick={() => setDelay(null)}>{t("common.cancel")}</Button>
            <Button variant="primary" icon="play" onClick={whatIfDelay} disabled={!(Number(days) > 0)}>
              {t("Create scenario and plan")}
            </Button>
          </>
        }
      >
        <Field label={t("Delay (days)")} hint={t("All open receipts of this material arrive later by this amount in a copy of the live plan.")}>
          <input className="mx-input w-[120px]" type="number" min={0.25} step={0.25} value={days} onChange={(e) => setDays(e.target.value)} />
        </Field>
      </Dialog>
    </div>
  );
}
