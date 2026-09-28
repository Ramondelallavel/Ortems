"use client";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Gantt, ZOOMS, type GanttHandle, type GOp } from "@/components/gantt/Gantt";
import { MovePreview } from "@/components/planning/MovePreview";
import { OperationPanel } from "@/components/planning/OperationPanel";
import { RunDialog } from "@/components/planning/RunDialog";
import { RunProgress } from "@/components/planning/RunProgress";
import { Badge, Button, Empty, ErrorState, Icon, Loading, Select, StatusPill, statusTone, useConfirm, useToast } from "@/components/ui";
import { api, download } from "@/lib/api";
import { dt, num, pct } from "@/lib/format";
import { useApi, useEvents, useHotkeys, useLocalState } from "@/lib/hooks";
import { useScenarioSelection } from "@/lib/plan";
import { useSession } from "@/lib/session";
import { SectionData } from "@/components/data/SectionData";

export default function PlanningBoard() {
  const { t, can, plant } = useSession();
  const router = useRouter();
  const toast = useToast();
  const { confirm, node: confirmNode } = useConfirm();
  const { scenarios, list, selected, select } = useScenarioSelection();
  const [planId, setPlanId] = useState<string | null>(null);
  const plans = useApi<any[]>(selected ? `/scenarios/${selected.id}/plans` : null);
  useEffect(() => {
    setPlanId(selected?.head_plan_id || null);
  }, [selected?.id, selected?.head_plan_id]);
  const header = useApi<any>(planId ? `/plans/${planId}` : null);
  const gantt = useApi<any>(planId ? `/plans/${planId}/gantt` : null);
  const unscheduled = useApi<any[]>(planId ? `/plans/${planId}/unscheduled` : null);

  const [zoom, setZoom] = useLocalState("mx.gantt.zoom", "d");
  const [colorBy, setColorBy] = useLocalState<"family" | "status" | "customer">("mx.gantt.color", "family");
  const [sel, setSel] = useState<GOp | null>(null);
  const [panelOp, setPanelOp] = useState<string | null>(null);
  const [highlightOrder, setHighlightOrder] = useState<string | null>(null);
  const [chain, setChain] = useState<{ operations: string[]; dependencies: { from: string; to: string }[] } | null>(null);
  const [move, setMove] = useState<{ opId: string; resourceId: string; start: string } | null>(null);
  const [runOpen, setRunOpen] = useState(false);
  const [runId, setRunId] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [bottom, setBottom] = useLocalState<"none" | "exceptions">("mx.board.bottom", "exceptions");
  const [busy, setBusy] = useState<string | null>(null);
  const [legend, setLegend] = useState<{ label: string; color: string }[]>([]);
  const gref = useRef<GanttHandle>(null);
  const area = useRef<HTMLDivElement>(null);
  const [h, setH] = useState(500);
  const searchRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const el = area.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setH(el.clientHeight));
    ro.observe(el);
    return () => ro.disconnect();
  }, [gantt.data]);

  useEffect(() => {
    if (!highlightOrder || !planId) {
      setChain(null);
      return;
    }
    api(`/plans/${planId}/order-chain/${highlightOrder}`).then(setChain).catch(() => setChain(null));
  }, [highlightOrder, planId]);

  // someone else changed the plan / a run finished → refresh
  useEvents(
    (type, data) => {
      if ((type === "plan.head_changed" || type === "planning.run.finished") && data?.scenario_id === selected?.id) scenarios.reload();
      if (type === "plan.head_changed" && data?.scenario_id === selected?.id && data?.plan_id !== planId) toast.info("The plan was changed by another user; showing the latest version.");
    },
    plant?.id,
  );

  const allRes = useApi<any>(plant ? "/resources" : null, plant ? { plant_id: plant.id } : undefined);
  const resCodes = useMemo(() => Object.fromEntries([...(allRes.data?.items || []), ...(gantt.data?.resources || [])].map((r: any) => [r.id, r.code])), [gantt.data, allRes.data]);
  const refreshAll = useCallback(() => {
    scenarios.reload();
    plans.reload();
  }, [scenarios, plans]);
  const isHead = header.data?.is_head;
  const editable = can("plan:edit") && isHead && !selected?.locked_by;

  const act = async (name: string, fn: () => Promise<any>, okMsg?: (r: any) => string) => {
    setBusy(name);
    try {
      const r = await fn();
      if (okMsg) toast.ok(okMsg(r));
      refreshAll();
      return r;
    } catch (e) {
      toast.error(e);
    } finally {
      setBusy(null);
    }
  };
  const undo = () => selected && act("undo", () => api(`/scenarios/${selected.id}/undo`, { method: "POST" }), (p) => `Back to ${p.number}`);
  const redo = () => selected && act("redo", () => api(`/scenarios/${selected.id}/redo`, { method: "POST" }), (p) => `Forward to ${p.number}`);
  const publish = async () => {
    if (!planId || !header.data) return;
    const hard = header.data.hard_violations_placed;
    const r = await confirm(`Publish ${header.data.number}?`, {
      body: (
        <div className="space-y-1 text-[12.5px]">
          <p>The shop floor (dispatch lists, operator screens) and subscribed systems will receive this plan.</p>
          {header.data.unscheduled_count > 0 && <p className="text-amber-600">◆ {header.data.unscheduled_count} operations are not scheduled.</p>}
          {hard > 0 && <p className="text-red-600">▲ {hard} hard violations — publishing requires a reason.</p>}
        </div>
      ),
      reason: hard > 0 || header.data.unscheduled_count > 0,
    });
    if (!r.ok) return;
    act("publish", () => api(`/plans/${planId}/publish`, { body: { reason: r.reason, force: hard > 0 || header.data.unscheduled_count > 0 } }), (p) => `${p.number} published`);
  };
  const reschedule = () => selected && act("reschedule", () => api(`/scenarios/${selected.id}/reschedule`, { body: { scope: "LOCAL" } }), (r) => `${r.plan_number}: ${r.affected_operations?.length ?? 0} operations affected, ${r.comparison?.operations_moved ?? 0} moved`);
  const validate = () =>
    planId &&
    act(
      "validate",
      () => api(`/plans/${planId}/validate`, { method: "POST" }),
      (r) => `Validation: ${r.feasible ? "feasible" : "not feasible"} — ${r.hard_count} hard, ${r.soft_count} soft`,
    );
  const lock = (op: string, locked: boolean) => planId && act("lock", () => api(`/plans/${planId}/locks`, { body: { op_ids: [op], locked } }), () => (locked ? `${op} locked` : `${op} unlocked`)).then(() => gantt.reload());

  const find = () => {
    const q = search.trim().toLowerCase();
    if (!q || !gantt.data) return;
    const hit = gantt.data.operations.find((o: GOp) => o.id.toLowerCase().includes(q) || o.order.toLowerCase().includes(q) || (o.item || "").toLowerCase().includes(q));
    if (hit) {
      setSel(hit);
      gref.current?.scrollToOp(hit.id);
    } else toast.warn(`Nothing matches "${search}" in this plan.`);
  };

  useHotkeys({
    "mod+z": () => editable && undo(),
    "mod+y": () => editable && redo(),
    "mod+shift+z": () => editable && redo(),
    "mod+f": () => searchRef.current?.focus(),
    "+": () => gref.current?.zoom(-1),
    "=": () => gref.current?.zoom(-1),
    "-": () => gref.current?.zoom(1),
    escape: () => {
      setPanelOp(null);
      setHighlightOrder(null);
    },
  });

  if (!plant) return <Loading />;
  if (scenarios.error) return <ErrorState error={scenarios.error} onRetry={scenarios.reload} />;
  const hd = header.data;
  const k = hd?.kpis || {};
  const md = hd?.solver || {};

  return (
    <div className="flex flex-col h-full min-h-0">
      {/* toolbar */}
      <div className="flex flex-wrap items-center gap-2 px-3 py-1.5 bg-white border-b border-gray-200">
        <Select ariaLabel={t("common.scenario")} value={selected?.id || ""} onChange={select} options={list.map((s) => ({ value: s.id, label: `${s.is_live ? "● " : ""}${s.name}` }))} className="max-w-[220px]" />
        <Select
          ariaLabel={t("plan.version")}
          value={planId || ""}
          onChange={setPlanId}
          options={(plans.data || []).map((p: any) => ({ value: p.id, label: `${p.number}${p.id === selected?.head_plan_id ? " (current)" : ""} · ${p.kind.toLowerCase()} · ${p.status.toLowerCase()}` }))}
          className="max-w-[330px]"
        />
        {hd && <Badge tone={statusTone(hd.status)}>{hd.status}</Badge>}
        <div className="h-5 w-px bg-gray-200" />
        {can("plan:run") && (
          <Button variant="primary" icon="play" onClick={() => setRunOpen(true)} disabled={!selected || !!runId}>
            {t("plan.optimize")}
          </Button>
        )}
        {can("plan:edit") && (
          <>
            <Button icon="refresh" onClick={reschedule} busy={busy === "reschedule"} disabled={!selected?.head_plan_id} title="Repair the plan after changes (local scope), keeping it stable">
              {t("plan.reschedule")}
            </Button>
            <Button icon="undo" onClick={undo} busy={busy === "undo"} disabled={!editable} aria-label={t("plan.undo")} title={`${t("plan.undo")} (Ctrl+Z)`} />
            <Button icon="redo" onClick={redo} busy={busy === "redo"} disabled={!editable || !hd?.scenario?.redo_available} aria-label={t("plan.redo")} title={`${t("plan.redo")} (Ctrl+Y)`} />
          </>
        )}
        <Button icon="check" onClick={validate} busy={busy === "validate"} disabled={!planId}>
          {t("plan.validate")}
        </Button>
        {can("plan:publish") && selected?.is_live && (
          <Button icon="publish" onClick={publish} busy={busy === "publish"} disabled={!planId || hd?.status === "PUBLISHED"}>
            {t("plan.publish")}
          </Button>
        )}
        <Button icon="compare" onClick={() => router.push(`/planning/scenarios?compare=${planId}`)} disabled={!planId}>
          {t("plan.compare")}
        </Button>
        {can("integration:export") && (
          <Button icon="download" onClick={() => planId && download(`/plans/${planId}/export`, { format: "xlsx" }).catch(toast.error)} disabled={!planId}>
            Excel
          </Button>
        )}
        <SectionData tables={["production-orders", "routing-operations", "routing-operations.resources", "resources", "calendars.shifts", "setup-matrices", "setup-matrices.entries"]} />
        <div className="flex-1" />
        <form
          onSubmit={(e) => {
            e.preventDefault();
            find();
          }}
          className="relative"
        >
          <Icon name="search" size={13} className="absolute left-2 top-[8px] text-slate-400" />
          <input ref={searchRef} className="mx-input pl-7 w-[190px]" placeholder="Find order / operation (Ctrl+F)" aria-label="Find order or operation" value={search} onChange={(e) => setSearch(e.target.value)} />
        </form>
        <Select ariaLabel="Colour by" value={colorBy} onChange={(v) => setColorBy(v as any)} options={[{ value: "family", label: "Colour: family" }, { value: "customer", label: "Colour: customer" }, { value: "status", label: "Colour: status" }]} />
        <div className="flex items-center border border-gray-300 rounded-[3px] overflow-hidden" role="group" aria-label="Zoom">
          {ZOOMS.map((z) => (
            <button key={z.id} onClick={() => setZoom(z.id)} aria-pressed={zoom === z.id} className={`h-7 px-2 text-[11.5px] ${zoom === z.id ? "bg-navy-700 text-white" : "bg-white hover:bg-gray-100"}`}>
              {z.label}
            </button>
          ))}
        </div>
        <Button size="sm" variant="ghost" onClick={() => gref.current?.scrollToTime(Date.now())}>
          Now
        </Button>
      </div>

      {/* KPI strip */}
      {hd && (
        <div className="flex items-center gap-4 px-3 h-9 bg-gray-50 border-b border-gray-200 text-[12px] overflow-x-auto whitespace-nowrap">
          <KpiInline label="OTIF" value={pct(k.otif)} onClick={() => router.push(`/analytics?kpi=otif&plan=${planId}`)} />
          <KpiInline label="Late" value={num(k.late_orders)} tone={k.late_orders ? "bad" : "ok"} onClick={() => router.push(`/planning/orders?status=LATE`)} />
          <KpiInline label="Unscheduled" value={num(k.orders_unscheduled)} tone={k.orders_unscheduled ? "bad" : "ok"} onClick={() => setBottom("exceptions")} />
          <KpiInline label="Utilisation" value={pct(k.utilization)} onClick={() => router.push("/planning/capacity")} />
          <KpiInline label="Setup" value={`${num(k.setup_h, 1)} h`} />
          <KpiInline label="Overtime" value={`${num(k.overtime_h, 1)} h`} />
          <KpiInline label="Material shortages" value={num(k.material_shortages)} tone={k.material_shortages ? "warn" : "ok"} onClick={() => router.push("/planning/materials")} />
          <span className="text-slate-600">
            {t("plan.solver")}: <b className="text-graphite-800">{md.provider}</b> · {md.status}
            {md.gap !== null && md.gap !== undefined ? ` · gap ${(md.gap * 100).toFixed(1)} %` : ""} · {md.runtime_s ?? "—"} s
          </span>
          <span>{hd.feasible ? <Badge tone="ok">{t("plan.feasible")}</Badge> : hd.hard_violations_placed ? <Badge tone="bad">{t("plan.infeasible")}</Badge> : <Badge tone="warn">{t("plan.incomplete")}</Badge>}</span>
          {!isHead && <Badge tone="info">older version (read-only)</Badge>}
        </div>
      )}

      <div className="flex flex-1 min-h-0">
        <div className="flex-1 min-w-0 flex flex-col min-h-0 relative">
          <div ref={area} className="flex-1 min-h-0 relative">
            {runId && (
              <RunProgress
                runId={runId}
                onClose={() => setRunId(null)}
                onDone={(r) => {
                  refreshAll();
                  if (r.status === "SUCCEEDED") setTimeout(() => setRunId(null), 5000);
                  if (r.status === "SUCCEEDED") toast.ok(`Plan ${r.result?.plan_number} ready`);
                  else if (r.status === "FAILED") toast.error(new Error(r.error_message || "Planning failed"));
                }}
              />
            )}
            {!selected?.head_plan_id && !scenarios.loading ? (
              <Empty title={t("dash.noPlan")} icon="board">
                {can("plan:run") && (
                  <Button variant="primary" icon="play" onClick={() => setRunOpen(true)}>
                    {t("dash.runFirst")}
                  </Button>
                )}
              </Empty>
            ) : gantt.error ? (
              <ErrorState error={gantt.error} onRetry={gantt.reload} />
            ) : !gantt.data ? (
              <Loading label="Loading schedule…" />
            ) : (
              <Gantt
                ref={gref}
                data={gantt.data}
                zoom={zoom}
                onZoom={setZoom}
                colorBy={colorBy}
                selectedId={sel?.id || null}
                highlightOps={chain ? new Set(chain.operations) : null}
                deps={chain?.dependencies}
                onSelect={(o) => {
                  setSel(o);
                  if (o) setPanelOp(o.id);
                }}
                onOpen={(o) => setPanelOp(o.id)}
                onMove={editable ? (o, rid, start) => setMove({ opId: o.id, resourceId: rid, start }) : undefined}
                canEdit={!!editable}
                height={h}
                onLegend={setLegend}
              />
            )}
          </div>
          {bottom === "exceptions" && planId && (
            <div className="h-[170px] border-t border-gray-200 bg-white flex flex-col min-h-0">
              <div className="mx-panel-head !min-h-[28px]">
                <span className="flex-1">Exceptions — unscheduled operations ({unscheduled.data?.length ?? "…"})</span>
                <button className="mx-btn mx-btn-ghost mx-btn-sm" aria-label="Hide exceptions" onClick={() => setBottom("none")}>
                  <Icon name="chevronDown" />
                </button>
              </div>
              <div className="overflow-auto mx-scroll flex-1">
                <table className="mx-table">
                  <tbody>
                    {(unscheduled.data || []).map((u: any) => (
                      <tr key={u.op_id} tabIndex={0} className="cursor-pointer" onClick={() => setPanelOp(u.op_id)} onKeyDown={(e) => e.key === "Enter" && setPanelOp(u.op_id)}>
                        <td className="code w-[140px]">{u.op_id}</td>
                        <td className="w-[150px]">
                          <StatusPill status="UNSCHEDULED" label={u.reason} />
                        </td>
                        <td className="whitespace-normal">{u.message}</td>
                      </tr>
                    ))}
                    {!unscheduled.data?.length && (
                      <tr>
                        <td className="text-slate-600">✓ Every operation is scheduled.</td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          )}
          {bottom === "none" && (
            <button className="h-6 border-t border-gray-200 bg-white text-[11.5px] text-slate-600 hover:bg-gray-50" onClick={() => setBottom("exceptions")}>
              ▲ Exceptions ({unscheduled.data?.length ?? 0})
            </button>
          )}
        </div>
        {panelOp && planId && (
          <OperationPanel
            key={panelOp}
            planId={planId}
            opId={panelOp}
            resCodes={resCodes}
            canEdit={!!editable}
            onClose={() => {
              setPanelOp(null);
              setHighlightOrder(null);
            }}
            onHighlightOrder={setHighlightOrder}
            onLock={lock}
          />
        )}
      </div>
      <div className="h-6 px-3 flex items-center gap-4 text-[11px] text-slate-600 border-t border-gray-200 bg-white whitespace-nowrap overflow-hidden">
        <span>
          <span className="inline-block w-4 h-2 align-middle mr-1" style={{ background: "repeating-linear-gradient(45deg,#2a78d6 0 1px,#fff 1px 4px)", border: "1px solid #2a78d6" }} />
          setup
        </span>
        <span>▲ late (red outline)</span>
        <span>◆ material risk (dotted underline)</span>
        <span>🔒︎ locked / frozen</span>
        <span>✕ maintenance / down</span>
        <span className="text-blue-500">│ now</span>
        {legend.map((l) => (
          <span key={l.label} className="flex items-center gap-1">
            <span className="inline-block w-2.5 h-2.5 rounded-sm" style={{ background: l.color }} />
            {l.label}
          </span>
        ))}
        <div className="flex-1" />
        {hd && (
          <span>
            {hd.number} · {hd.operations} ops · created {dt(hd.created_at)} by {hd.created_by}
          </span>
        )}
      </div>
      {selected && <RunDialog open={runOpen} onClose={() => setRunOpen(false)} scenarioId={selected.id} onStarted={setRunId} />}
      {planId && hd && (
        <MovePreview
          planId={planId}
          planVersion={hd.version}
          move={move}
          resCodes={resCodes}
          onClose={() => setMove(null)}
          onApplied={() => {
            setMove(null);
            refreshAll();
          }}
        />
      )}
      {confirmNode}
    </div>
  );
}

function KpiInline({ label, value, tone, onClick }: { label: string; value: string; tone?: "ok" | "bad" | "warn"; onClick?: () => void }) {
  const Tag = onClick ? "button" : "span";
  return (
    <Tag onClick={onClick} className={`flex items-baseline gap-1 ${onClick ? "hover:underline" : ""}`}>
      <span className="text-slate-600">{label}</span>
      <b className={`tabular ${tone === "bad" ? "text-red-600" : tone === "warn" ? "text-amber-600" : "text-graphite-800"}`}>
        {tone === "bad" ? "▲ " : tone === "warn" ? "◆ " : ""}
        {value}
      </b>
    </Tag>
  );
}
