"use client";
import { useMemo, useState } from "react";
import { Badge, Button, DataTable, ErrorState, Loading, PageHeader, Panel, Select, Tabs, useToast } from "@/components/ui";
import { api } from "@/lib/api";
import { date, num } from "@/lib/format";
import { useSession } from "@/lib/session";
import { SectionData } from "@/components/data/SectionData";

/** MPS / MRP: netting, lot sizing and rough-cut capacity per week, plus the aggregate (MIP) plan. */
export default function MpsPage() {
  const { t, plant, can } = useSession();
  const toast = useToast();
  const [weeks, setWeeks] = useState("12");
  const [tab, setTab] = useState("mps");
  const [data, setData] = useState<any>(null);
  const [agg, setAgg] = useState<any>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<any>(null);
  const [itemFilter, setItemFilter] = useState("FINISHED");
  const [picked, setPicked] = useState<Set<string>>(new Set());

  const run = async () => {
    if (!plant) return;
    setBusy("mps");
    setError(null);
    try {
      setData(await api("/planning/mrp", { body: { plant_id: plant.id, weeks: Number(weeks) } }));
      setPicked(new Set());
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  };
  const runAgg = async () => {
    if (!plant) return;
    setBusy("agg");
    try {
      setAgg(await api("/planning/aggregate", { body: { plant_id: plant.id, weeks: Number(weeks) } }));
    } catch (e) {
      toast.error(e);
    } finally {
      setBusy(null);
    }
  };
  const firm = async () => {
    if (!plant || !data) return;
    const sel = data.planned_orders.filter((p: any) => picked.has(p.id));
    setBusy("firm");
    try {
      const r = await api("/planning/mrp/firm", { body: { plant_id: plant.id, planned_orders: sel } });
      toast.ok(`${r.created.length} firm production orders created. They are planned at the next run.`);
      run();
    } catch (e) {
      toast.error(e);
    } finally {
      setBusy(null);
    }
  };

  const items = useMemo(() => (data?.items || []).filter((i: any) => !itemFilter || i.item_type === itemFilter).filter((i: any) => i.gross_requirements.some((x: number) => x) || i.planned_receipts?.some?.((x: number) => x)), [data, itemFilter]);
  const periods: string[] = data?.periods || [];

  return (
    <div className="flex flex-col h-full min-h-0">
      <PageHeader
        title={t("nav.mps")}
        subtitle="Weekly netting of demand (forecast, customer orders, dependent demand) against stock and receipts; lot sizing; rough-cut capacity."
        actions={
          <>
            <SectionData tables={["demands", "sales-orders", "sales-orders.lines", "items", "item-plants", "product-families"]} />
            <Select ariaLabel="Weeks" value={weeks} onChange={setWeeks} options={["4", "8", "12", "16", "26"].map((w) => ({ value: w, label: `${w} weeks` }))} />
            <Button variant="primary" icon="play" busy={busy === "mps"} onClick={run}>
              Calculate MPS / MRP
            </Button>
          </>
        }
      />
      <Tabs
        value={tab}
        onChange={setTab}
        tabs={[
          { id: "mps", label: "Master schedule" },
          { id: "po", label: "Planned orders", badge: data ? <Badge tone="neutral" glyph={false}>{data.planned_orders.length}</Badge> : undefined },
          { id: "exc", label: "Exceptions", badge: data?.exceptions?.length ? <Badge tone="warn">{data.exceptions.length}</Badge> : undefined },
          { id: "rccp", label: "Rough-cut capacity" },
          { id: "agg", label: "Aggregate plan (MIP)" },
        ]}
      />
      <div className="flex-1 min-h-0 overflow-auto mx-scroll p-3">
        <ErrorState error={error} onRetry={run} />
        {!data && !busy && tab !== "agg" && <div className="text-slate-600 p-4">Calculate the MPS to see requirements, planned orders and rough-cut capacity. Nothing is created until you firm planned orders.</div>}
        {busy === "mps" && <Loading label="Netting requirements level by level…" />}
        {data && tab === "mps" && (
          <Panel
            title={`Items with requirements (${items.length})`}
            actions={<Select ariaLabel="Item type" value={itemFilter} onChange={setItemFilter} options={[{ value: "FINISHED", label: "Finished goods" }, { value: "SEMI_FINISHED", label: "Semi-finished" }, { value: "RAW", label: "Raw materials" }, { value: "", label: "All" }]} />}
            bodyClass="overflow-auto mx-scroll max-h-[calc(100vh-230px)]"
          >
            <table className="mx-table">
              <thead>
                <tr>
                  <th>Item</th>
                  <th>Row</th>
                  {periods.map((p) => (
                    <th key={p} className="!text-right">
                      {date(p).slice(0, 6)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {items.slice(0, 150).map((it: any) =>
                  [
                    ["Gross requirements", it.gross_requirements],
                    ["Scheduled receipts", it.scheduled_receipts],
                    ["Projected on hand", it.projected_on_hand],
                    ["Planned receipts", it.planned_receipts || []],
                  ].map(([label, arr]: any, j) => (
                    <tr key={`${it.item_id}-${j}`} className={j === 3 ? "border-b-2" : ""}>
                      {j === 0 ? (
                        <td rowSpan={4} className="code align-top pt-1">
                          {it.item_code}
                          <div className="text-[10.5px] text-slate-600 font-sans">
                            on hand {num(it.on_hand)} · SS {num(it.safety_stock)} · LLC {it.llc}
                          </div>
                        </td>
                      ) : null}
                      <td className="text-slate-600">{label}</td>
                      {periods.map((_, k) => {
                        const v = arr?.[k];
                        const neg = label === "Projected on hand" && v < 0;
                        return (
                          <td key={k} className={`num ${neg ? "text-red-600 font-semibold" : ""}`}>
                            {v ? `${neg ? "▲" : ""}${num(v, 0)}` : ""}
                          </td>
                        );
                      })}
                    </tr>
                  )),
                )}
              </tbody>
            </table>
            {Object.keys(data.beyond_horizon_demand || {}).length > 0 && <div className="p-2 text-[11.5px] text-slate-600">Demand after the last period is not included ({Object.keys(data.beyond_horizon_demand).length} items). Extend the number of weeks to include it.</div>}
          </Panel>
        )}
        {data && tab === "po" && (
          <div className="h-[calc(100vh-200px)] bg-white border border-gray-200">
            <DataTable
              rows={data.planned_orders}
              rowKey={(r: any) => r.id}
              exportName="planned-orders"
              toolbar={
                can("orders:write") && (
                  <Button size="sm" variant="primary" disabled={!picked.size} busy={busy === "firm"} onClick={firm} title="Only MAKE proposals become production orders; BUY proposals are for purchasing">
                    Firm {picked.size} selected
                  </Button>
                )
              }
              columns={[
                {
                  key: "sel",
                  label: "",
                  sortable: false,
                  width: 30,
                  render: (r: any) =>
                    r.type === "MAKE" ? (
                      <input
                        type="checkbox"
                        aria-label={`Select ${r.id}`}
                        checked={picked.has(r.id)}
                        onChange={(e) => {
                          const n = new Set(picked);
                          if (e.target.checked) n.add(r.id);
                          else n.delete(r.id);
                          setPicked(n);
                        }}
                      />
                    ) : null,
                },
                { key: "id", label: "Proposal", mono: true },
                { key: "item_code", label: "Item", mono: true },
                { key: "type", label: "Type", render: (r: any) => <Badge tone={r.type === "MAKE" ? "info" : "neutral"}>{r.type}</Badge> },
                { key: "quantity", label: "Quantity", align: "right" },
                { key: "release_date", label: "Release", render: (r: any) => <span className={r.past_due_release ? "text-red-600" : ""}>{r.past_due_release ? "▲ " : ""}{date(r.release_date)}</span> },
                { key: "due_date", label: "Due", render: (r: any) => date(r.due_date) },
                { key: "pegging", label: "Pegged demand", value: (r: any) => r.pegging.length, render: (r: any) => `${r.pegging.length} sources` },
              ]}
            />
          </div>
        )}
        {data && tab === "exc" && (
          <Panel title="MRP exceptions">
            <table className="mx-table">
              <tbody>
                {data.exceptions.map((e: any, i: number) => (
                  <tr key={i}>
                    <td className="w-[180px]">
                      <Badge tone="warn">{e.type}</Badge>
                    </td>
                    <td className="code w-[160px]">{e.item}</td>
                    <td className="whitespace-normal">{e.message}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Panel>
        )}
        {data && tab === "rccp" && (
          <Panel title="Rough-cut capacity — load of planned + firm requirements vs capacity (hours per week)">
            <table className="mx-table">
              <thead>
                <tr>
                  <th>Resource / group</th>
                  {periods.map((p) => (
                    <th key={p} className="!text-right">
                      {date(p).slice(0, 6)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {data.capacity.map((c: any) => (
                  <tr key={c.key}>
                    <td className="code">{c.key}</td>
                    {c.utilization.map((u: number | null, k: number) => (
                      <td key={k} className={`num ${u !== null && u > 1 ? "text-red-600 font-semibold" : u !== null && u > 0.85 ? "text-amber-600" : ""}`} title={`${num(c.load_h[k], 1)} h of ${num(c.capacity_h[k], 1)} h`}>
                        {u === null ? "—" : `${u > 1 ? "▲" : u > 0.85 ? "◆" : ""}${Math.round(u * 100)} %`}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </Panel>
        )}
        {tab === "agg" && (
          <div className="space-y-3">
            <div className="flex items-center gap-2">
              <Button variant="primary" icon="play" busy={busy === "agg"} onClick={runAgg}>
                Solve aggregate plan
              </Button>
              <span className="text-[12px] text-slate-600">Family × week production, inventory and backlog under group capacity with limited overtime (linear program, SCIP/GLOP). Shadow prices show the value of one extra hour.</span>
            </div>
            {agg && (
              <>
                <div className="text-[12px]">
                  Status <Badge tone={agg.status === "OPTIMAL" ? "ok" : "warn"}>{agg.status}</Badge> · objective {num(agg.objective, 1)}
                </div>
                <Panel title="Families">
                  <table className="mx-table">
                    <thead>
                      <tr>
                        <th>Family</th>
                        <th>Row</th>
                        {agg.periods.map((p: string) => (
                          <th key={p} className="!text-right">
                            {date(p).slice(0, 6)}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {agg.families.map((f: any) =>
                        ["demand", "production", "inventory", "backlog"].map((k, j) => (
                          <tr key={`${f.id}-${k}`}>
                            {j === 0 && (
                              <td rowSpan={4} className="code align-top">
                                {f.id}
                              </td>
                            )}
                            <td className="text-slate-600">{k}</td>
                            {f[k].map((v: number, i: number) => (
                              <td key={i} className={`num ${k === "backlog" && v > 0 ? "text-red-600" : ""}`}>
                                {v ? num(v, 0) : ""}
                              </td>
                            ))}
                          </tr>
                        )),
                      )}
                    </tbody>
                  </table>
                </Panel>
                <Panel title="Capacity groups">
                  <table className="mx-table">
                    <thead>
                      <tr>
                        <th>Group</th>
                        <th>Row</th>
                        {agg.periods.map((p: string) => (
                          <th key={p} className="!text-right">
                            {date(p).slice(0, 6)}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {agg.groups.map((g: any) =>
                        [
                          ["utilisation", g.utilization.map((u: number | null) => (u === null ? "—" : `${Math.round(u * 100)} %`))],
                          ["overtime h", g.overtime_hours.map((v: number) => (v ? num(v, 1) : ""))],
                          ["shadow price", g.shadow_price.map((v: number | null) => (v ? num(v, 2) : ""))],
                        ].map(([label, vals]: any, j) => (
                          <tr key={`${g.id}-${j}`}>
                            {j === 0 && (
                              <td rowSpan={3} className="code align-top">
                                {g.id}
                              </td>
                            )}
                            <td className="text-slate-600">{label}</td>
                            {vals.map((v: string, i: number) => (
                              <td key={i} className="num">
                                {v}
                              </td>
                            ))}
                          </tr>
                        )),
                      )}
                    </tbody>
                  </table>
                </Panel>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
