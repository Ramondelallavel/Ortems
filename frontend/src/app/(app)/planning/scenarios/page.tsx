"use client";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { Chart, SERIES, axisCat, axisVal } from "@/components/charts/Chart";
import { RunProgress } from "@/components/planning/RunProgress";
import { Badge, Button, Dialog, Empty, ErrorState, Field, Loading, PageHeader, Panel, Select, StatusPill, Tabs, useConfirm, useToast } from "@/components/ui";
import { api } from "@/lib/api";
import { dt, duration, localInputToIso, num } from "@/lib/format";
import { useApi, useQueryParam } from "@/lib/hooks";
import type { ScenarioRow } from "@/lib/plan";
import { startRun } from "@/lib/runs";
import { useSession } from "@/lib/session";
import { SectionData } from "@/components/data/SectionData";

const WHATIFS = [
  { kind: "NIGHT_SHIFT", label: "Add a night shift", desc: "22:00–06:00 on selected machines and weekdays" },
  { kind: "ADD_MACHINE", label: "Add a machine", desc: "A copy of an existing machine, available from a date" },
  { kind: "RUSH_ORDER", label: "Rush order", desc: "Insert an urgent order and see who pays for it" },
  { kind: "MATERIAL_DELAY", label: "Supplier delay", desc: "Receipts of a material arrive later" },
  { kind: "BREAKDOWN", label: "Machine breakdown", desc: "A resource is unavailable for a period" },
  { kind: "ADD_OPERATOR", label: "More operators / tools", desc: "Increase a labour pool or tool capacity" },
  { kind: "OVERTIME", label: "Allow overtime", desc: "Use the overtime windows of the calendars" },
];

export default function ScenariosPage() {
  const { t, plant, can } = useSession();
  const router = useRouter();
  const toast = useToast();
  const { confirm, node } = useConfirm();
  const idParam = useQueryParam("id");
  const compareParam = useQueryParam("compare");
  const scen = useApi<ScenarioRow[]>(plant ? "/scenarios" : null, plant ? { plant_id: plant.id } : undefined);
  const [sel, setSel] = useState<string | null>(null);
  const [tab, setTab] = useState("detail");
  const [cmpIds, setCmpIds] = useState<string[]>([]);
  const [wizard, setWizard] = useState<string | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [cloneOpen, setCloneOpen] = useState(false);
  useEffect(() => {
    if (idParam) setSel(idParam);
  }, [idParam]);
  useEffect(() => {
    if (compareParam && scen.data) {
      const live = scen.data.find((s) => s.is_live);
      const others = scen.data.filter((s) => s.head_plan_id && s.head_plan_id !== compareParam).map((s) => s.head_plan_id!);
      setCmpIds([compareParam, ...(live?.head_plan_id && live.head_plan_id !== compareParam ? [live.head_plan_id] : []), ...others].filter((v, i, a) => a.indexOf(v) === i).slice(0, 3));
      setTab("compare");
    }
  }, [compareParam, scen.data]);
  const list = scen.data || [];
  const cur = list.find((s) => s.id === sel) || list[0];

  const archive = async (s: ScenarioRow) => {
    const r = await confirm(`Archive "${s.name}"?`, { danger: true, body: "The scenario and its plans stay in the history but disappear from the list." });
    if (!r.ok) return;
    try {
      await api(`/scenarios/${s.id}/archive`, { method: "POST" });
      scen.reload();
    } catch (e) {
      toast.error(e);
    }
  };
  const runScenario = async (s: ScenarioRow) => {
    try {
      const id = await startRun({ scenario_id: s.id }, confirm, t);
      if (id) setRunId(id);
    } catch (e) {
      toast.error(e);
    }
  };
  const removeChange = async (s: ScenarioRow, cid: string) => {
    try {
      await api(`/scenarios/${s.id}/changes/${cid}`, { method: "DELETE" });
      scen.reload();
    } catch (e) {
      toast.error(e);
    }
  };

  if (!plant) return <Loading />;
  return (
    <div className="flex flex-col h-full min-h-0">
      <PageHeader
        title={t("nav.scenarios")}
        subtitle="Copy-on-write copies of the live plan. Changes are recorded as a list and re-applied at every run; the live plan is never touched."
        actions={
          can("scenario:write") && (
            <>
              <SectionData tables={["planning-rules", "sequence-rules", "setup-rules", "optimization-profiles"]} />
              <Button icon="plus" onClick={() => setCloneOpen(true)}>
                New scenario
              </Button>
              <Select ariaLabel="What-if" value="" onChange={(v) => v && setWizard(v)} options={[{ value: "", label: "What-if…" }, ...WHATIFS.map((w) => ({ value: w.kind, label: w.label }))]} />
            </>
          )
        }
      />
      <Tabs value={tab} onChange={setTab} tabs={[{ id: "detail", label: "Scenarios" }, { id: "compare", label: "Compare", badge: cmpIds.length ? <Badge tone="neutral" glyph={false}>{cmpIds.length}</Badge> : undefined }]} />
      {scen.error && <ErrorState error={scen.error} onRetry={scen.reload} />}
      {tab === "detail" && (
        <div className="flex flex-1 min-h-0">
          <div className="w-[340px] border-r border-gray-200 bg-white overflow-auto mx-scroll">
            {list.map((s) => (
              <button key={s.id} onClick={() => setSel(s.id)} className={`w-full text-left px-3 py-2 border-b border-gray-100 ${cur?.id === s.id ? "bg-blue-100" : "hover:bg-gray-50"}`} aria-current={cur?.id === s.id}>
                <div className="flex items-center gap-2">
                  {s.is_live ? <Badge tone="dark">LIVE</Badge> : <Badge tone="neutral" glyph={false}>{s.kind.replace("_", "-")}</Badge>}
                  <span className="font-semibold truncate flex-1">{s.name}</span>
                  <input
                    type="checkbox"
                    aria-label={`Compare ${s.name}`}
                    disabled={!s.head_plan_id}
                    checked={!!s.head_plan_id && cmpIds.includes(s.head_plan_id)}
                    onClick={(e) => e.stopPropagation()}
                    onChange={(e) => s.head_plan_id && setCmpIds(e.target.checked ? [...cmpIds, s.head_plan_id].slice(-6) : cmpIds.filter((x) => x !== s.head_plan_id))}
                  />
                </div>
                <div className="text-[11.5px] text-slate-600 mt-0.5 tabular">
                  {s.head_plan ? `${s.head_plan.number} · OTIF ${num(s.head_plan.kpis?.otif, 1)} % · late ${num(s.head_plan.kpis?.late_orders)}` : "not planned yet"}
                  {s.last_run && s.last_run.status !== "SUCCEEDED" && <span className="ml-1">· run {s.last_run.status.toLowerCase()}</span>}
                </div>
              </button>
            ))}
          </div>
          <div className="flex-1 overflow-auto mx-scroll p-3">
            {cur ? (
              <div className="space-y-3 relative">
                {runId && <RunProgress runId={runId} onClose={() => setRunId(null)} onDone={() => scen.reload()} />}
                <Panel
                  title={cur.name}
                  actions={
                    <div className="flex gap-1.5">
                      {can("plan:run") && (
                        <Button size="sm" variant="primary" icon="play" onClick={() => runScenario(cur)}>
                          Plan scenario
                        </Button>
                      )}
                      <Button size="sm" icon="board" onClick={() => router.push(`/planning?scenario=${cur.id}`)} disabled={!cur.head_plan_id}>
                        Open in board
                      </Button>
                      {can("scenario:write") && !cur.is_live && (
                        <Button size="sm" variant="danger" onClick={() => archive(cur)}>
                          Archive
                        </Button>
                      )}
                    </div>
                  }
                >
                  <div className="p-3 grid grid-cols-2 gap-3 text-[12.5px]">
                    <div>
                      <div className="mx-label">Description</div>
                      {cur.description || "—"}
                    </div>
                    <div>
                      <div className="mx-label">Lineage</div>
                      {cur.parent ? `copied from ${cur.parent.name}` : cur.is_live ? "operational plan" : "—"} · owner {cur.owner || "—"} · {cur.plans} plan versions
                    </div>
                    <div>
                      <div className="mx-label">Current plan</div>
                      {cur.head_plan ? (
                        <>
                          {cur.head_plan.number} <StatusPill status={cur.head_plan.status} /> {cur.head_plan.feasible ? <Badge tone="ok">feasible</Badge> : <Badge tone="warn">see exceptions</Badge>}
                        </>
                      ) : (
                        "—"
                      )}
                    </div>
                    <div>
                      <div className="mx-label">Last run</div>
                      {cur.last_run ? (
                        <>
                          <StatusPill status={cur.last_run.status} /> {dt(cur.last_run.created_at)} {cur.last_run.error ? <span className="text-red-600">▲ {cur.last_run.error}</span> : null}
                        </>
                      ) : (
                        "—"
                      )}
                    </div>
                  </div>
                </Panel>
                <Panel title={`Changes (${cur.changes?.length || 0}) — applied in order on top of the base data`}>
                  <table className="mx-table">
                    <tbody>
                      {(cur.changes || []).map((c) => (
                        <tr key={c.id}>
                          <td className="num w-8">{c.seq}</td>
                          <td className="w-[150px]">
                            <Badge tone="info" glyph={false}>
                              {c.type}
                            </Badge>
                          </td>
                          <td className="whitespace-normal">{c.description}</td>
                          <td className="w-10">
                            {can("scenario:write") && !cur.is_live && (
                              <Button size="sm" variant="ghost" aria-label="Remove change" onClick={() => removeChange(cur, c.id)}>
                                ✕
                              </Button>
                            )}
                          </td>
                        </tr>
                      ))}
                      {!cur.changes?.length && (
                        <tr>
                          <td className="text-slate-600">No changes: this scenario plans the current data.</td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </Panel>
              </div>
            ) : (
              <Empty title="No scenarios" />
            )}
          </div>
        </div>
      )}
      {tab === "compare" && <Compare planIds={cmpIds} />}
      <WhatIfWizard
        kind={wizard}
        onClose={() => setWizard(null)}
        onCreated={(s) => {
          scen.reload();
          setSel(s.id);
          if (s.run_id) setRunId(s.run_id);
          if (s.run_blocked) toast.warn(t("run.whatIfBlocked"));
          setTab("detail");
        }}
      />
      <CloneDialog open={cloneOpen} onClose={() => setCloneOpen(false)} scenarios={list} onCreated={(s) => { scen.reload(); setSel(s.id); }} />
      {node}
    </div>
  );
}

function Compare({ planIds }: { planIds: string[] }) {
  const cmp = useApi<any>(planIds.length >= 2 ? "/analytics/compare" : null, { plan_ids: planIds });
  const option = useMemo(() => {
    if (!cmp.data) return {};
    const codes = ["otif", "late_orders", "orders_unscheduled", "setup_h", "utilization", "overtime_h"];
    const rows = cmp.data.kpis.filter((k: any) => codes.includes(k.code));
    return {
      legend: { top: 0, left: 0 },
      grid: { left: 40, right: 16, top: 32, bottom: 24, containLabel: true },
      xAxis: { ...axisCat, data: rows.map((r: any) => `${r.label}${r.unit ? ` (${r.unit})` : ""}`) },
      yAxis: { ...axisVal },
      series: cmp.data.plans.map((p: any, i: number) => ({ name: `${p.scenario_name} · ${p.number}`, type: "bar", barGap: "10%", barMaxWidth: 26, itemStyle: { color: SERIES[i % SERIES.length], borderRadius: [3, 3, 0, 0] }, data: rows.map((r: any) => r.values[i]) })),
      tooltip: { trigger: "axis" },
    };
  }, [cmp.data]);
  if (planIds.length < 2) return <Empty title="Select at least two scenarios (checkboxes in the list) to compare their current plans." icon="compare" />;
  if (cmp.error) return <ErrorState error={cmp.error} onRetry={cmp.reload} />;
  if (!cmp.data) return <Loading label="Comparing plans operation by operation…" />;
  const d = cmp.data;
  return (
    <div className="flex-1 overflow-auto mx-scroll p-3 space-y-3">
      <Panel title="KPI comparison (first column is the reference)">
        <div className="p-2">
          <Chart option={option} height={240} ariaLabel="KPI comparison bar chart" />
        </div>
        <table className="mx-table">
          <thead>
            <tr>
              <th>KPI</th>
              {d.plans.map((p: any) => (
                <th key={p.id} className="!text-right">
                  {p.scenario_name}
                  <div className="font-normal code">{p.number}</div>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {d.kpis
              .filter((k: any) => k.values.some((v: any) => v !== null && v !== undefined))
              .map((k: any) => (
                <tr key={k.code}>
                  <td>
                    {k.label} {k.unit && <span className="text-slate-600">({k.unit})</span>}
                  </td>
                  {k.values.map((v: any, i: number) => {
                    const base = k.values[0];
                    const better = i > 0 && k.better && typeof v === "number" && typeof base === "number" && v !== base ? (v > base) === (k.better === "up") : null;
                    return (
                      <td key={i} className={`num ${better === true ? "text-green-600 font-semibold" : better === false ? "text-red-600" : ""}`}>
                        {v === null || v === undefined ? "—" : num(v, 2)} {better === true ? "✓" : better === false ? "!" : ""}
                      </td>
                    );
                  })}
                </tr>
              ))}
          </tbody>
        </table>
      </Panel>
      {d.diffs_vs_first.map((df: any) => (
        <Panel key={df.plan_id} title={`${df.number} vs ${d.plans[0].number}: ${df.operations_moved} operations moved, ${df.orders_delayed} orders later, ${df.orders_advanced} earlier`}>
          <div className="p-3 grid grid-cols-4 gap-3 text-[12.5px]">
            <div>
              Setup change <b className="tabular">{duration(df.setup_delta_minutes)}</b>
            </div>
            <div>
              Overtime change <b className="tabular">{duration(df.overtime_delta_minutes)}</b>
            </div>
            <div>
              Sequence changes <b className="tabular">{df.sequence_changes}</b>
            </div>
            <div>
              Hard violations new/resolved <b className="tabular">{df.new_hard_violation_count}</b> / <b className="tabular">{df.resolved_hard_violation_count}</b>
            </div>
          </div>
          <table className="mx-table">
            <thead>
              <tr>
                <th>Order</th>
                <th>Before</th>
                <th>After</th>
                <th className="!text-right">Change</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {df.orders.slice(0, 25).map((o: any) => (
                <tr key={o.order_id}>
                  <td className="code">{o.number}</td>
                  <td className="tabular">{dt(o.before)}</td>
                  <td className="tabular">{dt(o.after)}</td>
                  <td className={`num ${o.delta_minutes > 0 ? "text-red-600" : o.delta_minutes < 0 ? "text-green-600" : ""}`}>{o.delta_minutes === null ? "—" : `${o.delta_minutes > 0 ? "+" : "−"}${duration(Math.abs(o.delta_minutes))}`}</td>
                  <td>
                    {o.status_before !== o.status_after ? (
                      <>
                        <StatusPill status={o.status_before} /> → <StatusPill status={o.status_after} />
                      </>
                    ) : (
                      <StatusPill status={o.status_after} />
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      ))}
    </div>
  );
}

function CloneDialog({ open, onClose, scenarios, onCreated }: { open: boolean; onClose: () => void; scenarios: ScenarioRow[]; onCreated: (s: any) => void }) {
  const toast = useToast();
  const [from, setFrom] = useState("");
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  useEffect(() => {
    if (open) setFrom(scenarios.find((s) => s.is_live)?.id || scenarios[0]?.id || "");
  }, [open, scenarios]);
  const save = async () => {
    try {
      const s = await api(`/scenarios/${from}/clone`, { body: { name, description: desc || null } });
      toast.ok(`Scenario "${s.name}" created`);
      onCreated(s);
      onClose();
    } catch (e) {
      toast.error(e);
    }
  };
  return (
    <Dialog open={open} onClose={onClose} title="New scenario" footer={<><Button onClick={onClose}>Cancel</Button><Button variant="primary" disabled={!name || !from} onClick={save}>Create</Button></>}>
      <div className="space-y-3">
        <Field label="Copy of">
          <Select value={from} onChange={setFrom} className="w-full" options={scenarios.map((s) => ({ value: s.id, label: s.name }))} />
        </Field>
        <Field label="Name">
          <input className="mx-input w-full" value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="Description">
          <input className="mx-input w-full" value={desc} onChange={(e) => setDesc(e.target.value)} />
        </Field>
      </div>
    </Dialog>
  );
}

function WhatIfWizard({ kind, onClose, onCreated }: { kind: string | null; onClose: () => void; onCreated: (s: any) => void }) {
  const { plant } = useSession();
  const toast = useToast();
  const res = useApi<any>(kind ? "/resources" : null, plant ? { plant_id: plant.id } : undefined);
  const items = useApi<any>(kind === "RUSH_ORDER" ? "/master-data/products" : kind === "MATERIAL_DELAY" ? "/master-data/items" : null, { limit: 2000 });
  const [p, setP] = useState<any>({});
  const [busy, setBusy] = useState(false);
  useEffect(() => setP({}), [kind]);
  const w = WHATIFS.find((x) => x.kind === kind);
  const machines = (res.data?.items || []).filter((r: any) => ["MACHINE", "WORK_CENTER", "LINE"].includes(r.kind));
  const pools = (res.data?.items || []).filter((r: any) => ["LABOR_POOL", "TOOL"].includes(r.kind));
  const set = (k: string, v: any) => setP((x: any) => ({ ...x, [k]: v }));

  const build = (): any => {
    switch (kind) {
      case "NIGHT_SHIFT":
        return { resource_ids: p.resource_ids || [], weekdays: p.weekdays || [0, 1, 2, 3, 4] };
      case "ADD_MACHINE":
        return { clone_of: p.clone_of, code: p.code, available_from: p.from ? localInputToIso(p.from) : undefined };
      case "RUSH_ORDER":
        return { item_code: p.item_code, quantity: Number(p.quantity), due: p.due ? localInputToIso(p.due) : undefined, priority: 1, expedite: true };
      case "MATERIAL_DELAY":
        return { material_id: p.material_id, delay_minutes: Math.round(Number(p.days || 0) * 1440) };
      case "BREAKDOWN":
        return { resource_id: p.resource_id, start: p.start ? localInputToIso(p.start) : undefined, end: p.end ? localInputToIso(p.end) : undefined, reason: "what-if breakdown" };
      case "ADD_OPERATOR":
        return { resource_id: p.resource_id, capacity: Number(p.capacity) };
      default:
        return {};
    }
  };
  // the inputs each what-if needs (the server checks them too)
  const ready =
    kind === "NIGHT_SHIFT" ? (p.resource_ids || []).length > 0 && (p.weekdays || [0, 1, 2, 3, 4]).length > 0
    : kind === "ADD_MACHINE" ? !!p.clone_of
    : kind === "RUSH_ORDER" ? !!p.item_code && Number(p.quantity) > 0 && !!p.due
    : kind === "MATERIAL_DELAY" ? !!p.material_id && Number(p.days) > 0
    : kind === "BREAKDOWN" ? !!p.resource_id && !!p.start && !!p.end && p.end > p.start
    : kind === "ADD_OPERATOR" ? !!p.resource_id && Number(p.capacity) > 0
    : !!kind;
  const create = async () => {
    if (!plant || !kind) return;
    setBusy(true);
    try {
      const s = await api("/scenarios/what-if", { body: { plant_id: plant.id, kind, params: build(), name: p.name || undefined, run: true } });
      if (s.run_id) toast.ok(`Scenario "${s.name}" created; planning started.`);
      onCreated(s);
      onClose();
    } catch (e) {
      toast.error(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog open={!!kind} onClose={onClose} title={`What-if: ${w?.label || ""}`} width={560} footer={<><Button onClick={onClose}>Cancel</Button><Button variant="primary" icon="play" busy={busy} disabled={!ready} title={ready ? undefined : "Fill in the fields of this what-if first"} onClick={create}>Create and plan</Button></>}>
      <p className="text-slate-600 mb-3">{w?.desc}. A copy of the live scenario is created with this change and planned; compare it with the live plan afterwards.</p>
      <div className="grid grid-cols-2 gap-3">
        {kind === "NIGHT_SHIFT" && (
          <>
            <Field label="Machines">
              <select multiple className="mx-input w-full !h-[120px]" value={p.resource_ids || []} onChange={(e) => set("resource_ids", Array.from(e.target.selectedOptions).map((o) => o.value))}>
                {machines.map((r: any) => (
                  <option key={r.id} value={r.id}>
                    {r.code} · {r.name}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Weekdays">
              <div className="flex flex-wrap gap-2">
                {["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((d, i) => (
                  <label key={d} className="flex gap-1 items-center">
                    <input type="checkbox" checked={(p.weekdays || [0, 1, 2, 3, 4]).includes(i)} onChange={(e) => set("weekdays", e.target.checked ? [...(p.weekdays || [0, 1, 2, 3, 4]), i] : (p.weekdays || [0, 1, 2, 3, 4]).filter((x: number) => x !== i))} /> {d}
                  </label>
                ))}
              </div>
            </Field>
          </>
        )}
        {kind === "ADD_MACHINE" && (
          <>
            <Field label="Same as">
              <Select value={p.clone_of || ""} onChange={(v) => set("clone_of", v)} className="w-full" options={[{ value: "", label: "—" }, ...machines.map((r: any) => ({ value: r.id, label: r.code }))]} />
            </Field>
            <Field label="New code">
              <input className="mx-input w-full" value={p.code || ""} onChange={(e) => set("code", e.target.value)} />
            </Field>
            <Field label="Available from (plant time)">
              <input className="mx-input w-full" type="datetime-local" value={p.from || ""} onChange={(e) => set("from", e.target.value)} />
            </Field>
          </>
        )}
        {kind === "RUSH_ORDER" && (
          <>
            <Field label="Product">
              <Select value={p.item_code || ""} onChange={(v) => set("item_code", v)} className="w-full" options={[{ value: "", label: "—" }, ...(items.data?.items || []).map((i: any) => ({ value: i.code, label: `${i.code} · ${i.name}` }))]} />
            </Field>
            <Field label="Quantity">
              <input className="mx-input w-full" type="number" min={1} value={p.quantity || ""} onChange={(e) => set("quantity", e.target.value)} />
            </Field>
            <Field label="Due (plant time)">
              <input className="mx-input w-full" type="datetime-local" value={p.due || ""} onChange={(e) => set("due", e.target.value)} />
            </Field>
          </>
        )}
        {kind === "MATERIAL_DELAY" && (
          <>
            <Field label="Material">
              <Select value={p.material_id || ""} onChange={(v) => set("material_id", v)} className="w-full" options={[{ value: "", label: "—" }, ...(items.data?.items || []).filter((i: any) => i.make_or_buy === "BUY").map((i: any) => ({ value: i.id, label: i.code }))]} />
            </Field>
            <Field label="Delay (days)">
              <input className="mx-input w-full" type="number" min={0.25} step={0.25} value={p.days || ""} onChange={(e) => set("days", e.target.value)} />
            </Field>
          </>
        )}
        {kind === "BREAKDOWN" && (
          <>
            <Field label="Resource">
              <Select value={p.resource_id || ""} onChange={(v) => set("resource_id", v)} className="w-full" options={[{ value: "", label: "—" }, ...machines.map((r: any) => ({ value: r.id, label: r.code }))]} />
            </Field>
            <div />
            <Field label="From (plant time)">
              <input className="mx-input w-full" type="datetime-local" value={p.start || ""} onChange={(e) => set("start", e.target.value)} />
            </Field>
            <Field label="To (plant time)">
              <input className="mx-input w-full" type="datetime-local" value={p.end || ""} onChange={(e) => set("end", e.target.value)} />
            </Field>
          </>
        )}
        {kind === "ADD_OPERATOR" && (
          <>
            <Field label="Labour pool / tool">
              <Select value={p.resource_id || ""} onChange={(v) => set("resource_id", v)} className="w-full" options={[{ value: "", label: "—" }, ...pools.map((r: any) => ({ value: r.id, label: `${r.code} (now ${r.capacity})` }))]} />
            </Field>
            <Field label="New capacity">
              <input className="mx-input w-full" type="number" min={1} value={p.capacity || ""} onChange={(e) => set("capacity", e.target.value)} />
            </Field>
          </>
        )}
        <Field label="Scenario name (optional)">
          <input className="mx-input w-full" value={p.name || ""} onChange={(e) => set("name", e.target.value)} />
        </Field>
      </div>
    </Dialog>
  );
}

