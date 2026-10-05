"use client";
import { useState } from "react";
import { Badge, Button, ErrorState, Icon, Loading, StatusPill, Tabs } from "@/components/ui";
import { dt, duration } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import { useSession } from "@/lib/session";
import { unscheduledText } from "@/lib/alerts";

const REASON_ICON: Record<string, string> = { PREDECESSOR: "chevronRight", MATERIAL: "materials", RESOURCE: "factory", CALENDAR: "clock", SETUP: "wrench", LABOR: "user", TOOL: "wrench", RELEASE: "clock", FROZEN: "lock" };

/** Everything the planner needs to understand one operation, straight from the stored plan. */
export function OperationPanel({ planId, opId, resCodes, onClose, onHighlightOrder, onLock, canEdit }: { planId: string; opId: string; resCodes: Record<string, string>; onClose: () => void; onHighlightOrder: (orderId: string | null) => void; onLock: (op: string, locked: boolean) => Promise<unknown> | unknown; canEdit: boolean }) {
  const { t } = useSession();
  const [tab, setTab] = useState("why");
  const detail = useApi<any>(`/plans/${planId}/operations/${encodeURIComponent(opId)}`);
  const explore = useApi<any>(tab === "explore" ? `/plans/${planId}/operations/${encodeURIComponent(opId)}/explore` : null);
  const orderKey = detail.data?.scheduled?.order_key || detail.data?.unscheduled?.order_id;
  const order = useApi<any>(tab === "order" && orderKey ? `/plans/${planId}/order-detail/${orderKey}` : null);
  const d = detail.data;
  const sch = d?.scheduled;
  const ex = d?.explanation;
  const rc = (id?: string | null) => (id ? resCodes[id] || id.slice(0, 8) : "—");

  return (
    <div className="flex flex-col h-full min-h-0 bg-white border-l border-gray-200 w-[380px] max-w-full">
      <div className="flex items-center gap-2 h-9 px-3 border-b border-gray-200 bg-gray-100">
        <span className="code font-semibold truncate flex-1">{opId}</span>
        {sch && canEdit && (
          <Button size="sm" variant="ghost" icon={sch.is_locked ? "unlock" : "lock"} onClick={async () => {
              await onLock(opId, !sch.is_locked);
              detail.reload(); // the lock state shown here comes from the stored plan
            }} title={sch.is_locked ? t("Unlock") : t("Lock position (kept by the next runs)")}>
            {sch.is_locked ? t("Unlock") : t("Lock")}
          </Button>
        )}
        <button className="mx-btn mx-btn-ghost mx-btn-sm" aria-label={t("Close")} onClick={onClose}>
          <Icon name="x" />
        </button>
      </div>
      <Tabs
        value={tab}
        onChange={setTab}
        tabs={[
          { id: "why", label: t("plan.whyHere") },
          { id: "explore", label: t("plan.explorer") },
          { id: "order", label: t("plan.orderChain") },
          { id: "peg", label: t("plan.pegging") },
        ]}
      />
      <div className="flex-1 overflow-auto mx-scroll p-3 text-[12.5px]">
        {detail.loading && !d && <Loading />}
        <ErrorState error={detail.error} onRetry={detail.reload} />
        {d && tab === "why" && (
          <div className="space-y-3">
            {d.order && (
              <div>
                <div className="font-semibold">
                  {d.order.number} · <span className="code">{d.order.item}</span>
                </div>
                <div className="text-slate-600 truncate">{d.order.item_name}</div>
                <div className="flex flex-wrap gap-1 mt-1">
                  <Badge tone="neutral" glyph={false}>
                    {t("qty {n}", { n: d.order.quantity })}
                  </Badge>
                  <Badge tone="neutral" glyph={false}>
                    {t("due {at}", { at: dt(d.order.due) })}
                  </Badge>
                  <Badge tone="neutral" glyph={false}>
                    {t("prio {n}", { n: d.order.priority })}
                  </Badge>
                  {d.order.expedite && <Badge tone="warn">{t("expedite")}</Badge>}
                  {d.operation && <StatusPill status={d.operation.status} />}
                </div>
              </div>
            )}
            {d.unscheduled && (
              <div className="border border-red-600/40 bg-red-100 rounded-[3px] p-2">
                <div className="font-semibold text-red-600">▲ {t("Not scheduled — {reason}", { reason: t(`reason.${d.unscheduled.reason}`).startsWith("reason.") ? d.unscheduled.reason : t(`reason.${d.unscheduled.reason}`) })}</div>
                <div>{unscheduledText(t, { ...d.unscheduled, op_id: d.unscheduled.op_id || opId })}</div>
                {(d.unscheduled.details?.materials || []).map((m: any) => (
                  <div key={m.material_id} className="text-[12px] mt-1">
                    ◆ <span className="code">{m.material}</span>: {t("need {need}, available {avail}, shortfall {short} {uom}", { need: m.required, avail: m.available ?? "—", short: m.shortfall, uom: m.uom || "" })}
                  </div>
                ))}
              </div>
            )}
            {sch && (
              <table className="w-full text-[12px] tabular">
                <tbody>
                  {[
                    [t("common.resource"), rc(sch.resource_key)],
                    [t("Setup"), `${dt(sch.setup_start)} · ${duration(sch.setup_minutes)}`],
                    [t("Run"), `${dt(sch.start)} → ${dt(sch.end)} (${t("{d} working time", { d: duration(sch.run_minutes) })})`],
                    [t("Zone"), t(`zone.${sch.zone}`).startsWith("zone.") ? sch.zone : t(`zone.${sch.zone}`)],
                    [t("Secondary"), (sch.secondary || []).map((x: any) => `${rc(x.resource_id)}×${x.units}`).join(", ") || "—"],
                  ].map(([k, v]) => (
                    <tr key={k as string}>
                      <td className="text-slate-600 py-0.5 pr-2 w-[80px] align-top">{k}</td>
                      <td>{v}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {sch?.binding?.type && sch.binding.type !== "NONE" && (
              <div className="border-l-2 border-navy-700 pl-2">
                <div className="text-[11px] uppercase text-slate-600 font-semibold">{t("Binding constraint")}</div>
                <div className="font-semibold">{sch.binding.type}</div>
                <div>{sch.binding.detail}</div>
                {!!sch.binding.wait_minutes && <div className="text-slate-600">{t("waited {d} (working time)", { d: duration(sch.binding.wait_minutes) })}</div>}
              </div>
            )}
            {ex?.reasons?.length > 0 && (
              <div>
                <div className="text-[11px] uppercase text-slate-600 font-semibold mb-1">{t("Why it starts here")}</div>
                <ul className="space-y-1">
                  {ex.reasons.map((r: any, i: number) => (
                    <li key={i} className="flex gap-1.5">
                      <Icon name={REASON_ICON[r.code] || "info"} size={13} className="mt-0.5 shrink-0 text-slate-600" />
                      <span>{r.text}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {ex?.alternatives?.length > 0 && (
              <div>
                <div className="text-[11px] uppercase text-slate-600 font-semibold mb-1">{t("Alternatives considered")}</div>
                <table className="mx-table">
                  <thead>
                    <tr>
                      <th>{t("common.resource")}</th>
                      <th>{t("Finish")}</th>
                      <th className="!text-right">Δ</th>
                    </tr>
                  </thead>
                  <tbody>
                    {ex.alternatives.map((a: any, i: number) => (
                      <tr key={i}>
                        <td>
                          {a.chosen ? "✓ " : ""}
                          <span className="code">{rc(a.resource_id)}</span>
                        </td>
                        <td>{a.feasible ? dt(a.end) : <span className="text-red-600">▲ {a.blocking?.[0]?.text || t("not feasible")}</span>}</td>
                        <td className="num">{a.delta_finish_minutes !== null && a.delta_finish_minutes !== undefined ? duration(a.delta_finish_minutes) : ""}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {ex?.rules_applied?.length > 0 && <div className="text-[11.5px] text-slate-600">{t("Rules applied: {rules}", { rules: ex.rules_applied.join(", ") })}</div>}
            {d.violations?.length > 0 && (
              <div>
                <div className="text-[11px] uppercase text-slate-600 font-semibold mb-1">{t("Deviations")}</div>
                {d.violations.map((v: any) => (
                  <div key={v.id} className={v.hardness === "HARD" ? "text-red-600" : "text-amber-600"}>
                    {v.hardness === "HARD" ? "▲" : "◆"} {v.message}
                  </div>
                ))}
              </div>
            )}
            {d.operation?.instructions && <div className="text-slate-600 border-t border-gray-200 pt-2">{d.operation.instructions}</div>}
          </div>
        )}
        {tab === "explore" && (
          <div>
            {explore.loading && <Loading label={t("Re-evaluating every alternative against the rest of the schedule…")} />}
            <ErrorState error={explore.error} onRetry={explore.reload} />
            {explore.data && (
              <div className="space-y-2">
                <div className="text-slate-600">
                  {t("Earliest start from predecessors/release:")} <b className="text-graphite-800">{dt(explore.data.earliest_start_from_predecessors)}</b> ({explore.data.lower_bound?.type}
                  {explore.data.lower_bound?.detail ? ` — ${explore.data.lower_bound.detail}` : ""})
                </div>
                {explore.data.alternatives.map((a: any) => (
                  <div key={a.resource_id} className={`border rounded-[3px] p-2 ${a.resource_id === explore.data.recommended ? "border-green-600" : "border-gray-200"}`}>
                    <div className="flex items-center gap-2">
                      <span className="code font-semibold">{a.resource}</span>
                      {a.resource_id === explore.data.current_resource && <Badge tone="info">{t("current")}</Badge>}
                      {a.resource_id === explore.data.recommended && <Badge tone="ok">{t("earliest finish")}</Badge>}
                      {!a.feasible && <Badge tone="bad">{t("not possible")}</Badge>}
                    </div>
                    {a.feasible && (
                      <div className="tabular text-[12px]">
                        {dt(a.start)} → {dt(a.end)} · {t("setup")} {duration(a.setup_minutes)}
                      </div>
                    )}
                    {a.reasons?.map((r: any, i: number) => (
                      <div key={i} className="text-[12px] text-slate-600">
                        • {r.text}
                      </div>
                    ))}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
        {tab === "order" && (
          <div className="space-y-2">
            {order.loading && <Loading />}
            <ErrorState error={order.error} onRetry={order.reload} />
            {order.data && (
              <>
                <div className="flex items-center gap-2">
                  <StatusPill status={order.data.result?.status} />
                  <span className="tabular">
                    {t("end {end} · due {due}", { end: dt(order.data.result?.end), due: dt(order.data.result?.due) })}
                  </span>
                </div>
                <Button size="sm" icon="eye" onClick={() => onHighlightOrder(orderKey)}>
                  {t("Highlight order chain in Gantt")}
                </Button>
                {order.data.root_cause?.length > 0 && (
                  <div>
                    <div className="text-[11px] uppercase text-slate-600 font-semibold mt-2 mb-1">{t("Root-cause chain")}</div>
                    <ol className="space-y-1 border-l-2 border-gray-300 pl-2">
                      {order.data.root_cause.map((s: any, i: number) => (
                        <li key={i}>
                          <span className="text-[10.5px] text-slate-400 uppercase">{s.level}</span> {s.text}
                        </li>
                      ))}
                    </ol>
                  </div>
                )}
                {order.data.deadline && (
                  <div className="text-[12px] text-slate-600">
                    {t("Earliest possible with unlimited capacity:")} <b>{dt(order.data.deadline.earliest_possible_infinite_capacity)}</b>
                    {order.data.deadline.required_additional_capacity_h ? ` · ${t("extra capacity needed ≈ {h} h", { h: order.data.deadline.required_additional_capacity_h })}` : ""}
                  </div>
                )}
                <table className="mx-table mt-2">
                  <thead>
                    <tr>
                      <th>{t("Operation")}</th>
                      <th>{t("common.resource")}</th>
                      <th>{t("common.start")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {order.data.operations.map((o: any) => (
                      <tr key={o.op_key}>
                        <td className="code">{o.op_key}</td>
                        <td className="code">{rc(o.resource_key)}</td>
                        <td className="tabular">{dt(o.start)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            )}
          </div>
        )}
        {tab === "peg" && d && (
          <div className="space-y-1">
            {!d.pegging?.length && <div className="text-slate-600">{t("This operation consumes no tracked material.")}</div>}
            {d.pegging?.map((p: any, i: number) => {
              const late = p.supply_time && p.need_time && p.supply_time > p.need_time;
              return (
                <div key={i} className="border border-gray-200 rounded-[3px] p-2">
                  <div className="flex gap-2 items-center">
                    <span className="code">{p.material || p.material_id}</span>
                    <Badge tone={late ? "warn" : "ok"}>{p.supply_kind}</Badge>
                    <span className="tabular">{p.quantity}</span>
                  </div>
                  <div className="text-[12px] text-slate-600">
                    {p.supply_ref || p.supply_order_id || ""} · {t("available {a} · needed {n}", { a: dt(p.supply_time), n: dt(p.need_time) })}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
