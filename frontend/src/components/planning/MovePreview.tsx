"use client";
import { useEffect, useState } from "react";
import { Button, Dialog, ErrorState, Field, Loading, Select, StatusPill, useToast } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { dt, duration } from "@/lib/format";
import { useSession } from "@/lib/session";

const MODES = ["NO_REPLAN", "THIS_ORDER", "DOWNSTREAM", "RESOURCE", "AREA", "SCENARIO"];
const KPI_SHOW = ["late_orders", "otif", "orders_unscheduled", "setup_h", "average_delay_h", "utilization"];

/** Impact preview of a manual move. The backend re-validates every hard constraint; nothing is
 * applied until the planner confirms. */
export function MovePreview({ planId, planVersion, move, resCodes, onClose, onApplied }: { planId: string; planVersion: number; move: { opId: string; resourceId: string; start: string } | null; resCodes: Record<string, string>; onClose: () => void; onApplied: (plan: any) => void }) {
  const { t, can } = useSession();
  const toast = useToast();
  const [mode, setMode] = useState("DOWNSTREAM");
  const [preview, setPreview] = useState<any>(null);
  const [error, setError] = useState<ApiError | undefined>();
  const [loading, setLoading] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [allowFrozen, setAllowFrozen] = useState(false);

  useEffect(() => {
    if (!move) return;
    // a newer request (other replan mode, other drop) cancels the previous one: an old answer never
    // replaces the preview of what is on screen
    const ctl = new AbortController();
    setLoading(true);
    setError(undefined);
    setPreview(null);
    api(`/plans/${planId}/moves/preview`, { body: { op_id: move.opId, resource_id: move.resourceId, start: move.start, replan: mode, allow_frozen: allowFrozen }, signal: ctl.signal })
      .then((p) => !ctl.signal.aborted && setPreview(p))
      .catch((e) => !ctl.signal.aborted && e?.name !== "AbortError" && setError(e))
      .finally(() => !ctl.signal.aborted && setLoading(false));
    return () => ctl.abort();
  }, [move, mode, planId, allowFrozen]);

  const apply = async () => {
    if (!move) return;
    setBusy(true);
    try {
      const p = await api(`/plans/${planId}/moves`, { body: { op_id: move.opId, resource_id: move.resourceId, start: move.start, replan: mode, reason: reason || undefined, expected_version: planVersion, accept_violations: !preview?.feasible, allow_frozen: allowFrozen } });
      toast.ok(t("{plan} created (manual edit). Undo with Ctrl+Z.", { plan: p.number }));
      onApplied(p);
    } catch (e) {
      toast.error(e);
    } finally {
      setBusy(false);
    }
  };
  const cmp = preview?.comparison;
  // overriding (frozen zone, new hard violations) is an explained decision: the server requires a reason
  const needsReason = allowFrozen || (!!preview && !preview.feasible && !!cmp?.new_hard_violation_count);
  return (
    <Dialog
      open={!!move}
      onClose={onClose}
      width={640}
      title={`${t("plan.movePreview")} — ${move?.opId || ""}`}
      footer={
        <>
          <Button onClick={onClose}>{t("common.cancel")}</Button>
          <Button variant={preview && !preview.feasible ? "danger" : "primary"} busy={busy} disabled={!preview || loading || (needsReason && !reason.trim())} title={needsReason && !reason.trim() ? t("Give a reason first") : undefined} onClick={apply}>
            {preview && !preview.feasible ? t("Apply with violations") : t("plan.applyMove")}
          </Button>
        </>
      }
    >
      {move && (
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <Field label={t("Target")}>
              <div className="tabular">
                <span className="code">{resCodes[move.resourceId] || move.resourceId}</span> · {dt(move.start)}
              </div>
            </Field>
            <Field label={t("plan.replanMode")}>
              <Select value={mode} onChange={setMode} className="w-full" options={MODES.map((m) => ({ value: m, label: t(`replan.${m}`) }))} />
            </Field>
          </div>
          {error?.code === "FROZEN_OPERATION" && can("plan:frozen") && (
            <label className="flex gap-2 items-center text-amber-600">
              <input type="checkbox" checked={allowFrozen} onChange={(e) => setAllowFrozen(e.target.checked)} /> {t("Change the frozen zone (requires a reason; recorded in the audit log)")}
            </label>
          )}
          {loading && <Loading label={t("Re-scheduling and validating every hard constraint…")} />}
          <ErrorState error={error} />
          {preview && (
            <>
              <div className={`p-2 rounded-[3px] border ${preview.feasible ? "border-green-600/40 bg-green-100" : "border-red-600/40 bg-red-100"}`}>
                <b>{preview.feasible ? `✓ ${t("Feasible")}` : `▲ ${t("Would violate hard constraints")}`}</b>
                {preview.planned && (
                  <span className="tabular">
                    {" "}
                    — {t("planned {from} → {to} on", { from: dt(preview.planned.setup_start), to: dt(preview.planned.end) })} <span className="code">{resCodes[preview.planned.resource_id] || preview.planned.resource_id}</span>
                    {Math.abs(new Date(preview.planned.setup_start).getTime() - new Date(move.start).getTime()) > 60000 && <span className="text-slate-600"> ({t("earliest valid position after the requested time")})</span>}
                  </span>
                )}
                {preview.hard_violations?.slice(0, 6).map((v: any, i: number) => (
                  <div key={i} className="text-red-600 text-[12px]">
                    ▲ {v.message}
                  </div>
                ))}
                {preview.messages?.map((m: string, i: number) => (
                  <div key={i} className="text-[12px] text-slate-600">
                    {m}
                  </div>
                ))}
              </div>
              {cmp && (
                <div className="grid grid-cols-2 gap-3">
                  <table className="mx-table">
                    <thead>
                      <tr>
                        <th>KPI</th>
                        <th className="!text-right">{t("Before")}</th>
                        <th className="!text-right">{t("After")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {KPI_SHOW.filter((k) => cmp.kpis[k]).map((k) => {
                        const v = cmp.kpis[k];
                        return (
                          <tr key={k}>
                            <td>{t(`kpi.${k}`).startsWith("kpi.") ? k.replaceAll("_", " ") : t(`kpi.${k}`)}</td>
                            <td className="num">{v.before ?? "—"}</td>
                            <td className={`num ${v.better === true ? "text-green-600" : v.better === false ? "text-red-600" : ""}`}>
                              {v.after ?? "—"} {v.better === true ? "✓" : v.better === false ? "!" : ""}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                  <div className="text-[12.5px] space-y-1">
                    <div>
                      {t("Operations moved:")} <b className="tabular">{cmp.operations_moved}</b>
                    </div>
                    <div>
                      {t("Orders delayed / advanced:")} <b className="tabular">{cmp.orders_delayed}</b> / <b className="tabular">{cmp.orders_advanced}</b>
                    </div>
                    <div>
                      {t("Setup change:")} <b className="tabular">{duration(cmp.setup_delta_minutes)}</b>
                    </div>
                    <div>
                      {t("Sequence changes:")} <b className="tabular">{cmp.sequence_changes}</b>
                    </div>
                    {cmp.orders
                      .filter((o: any) => o.delta_minutes)
                      .slice(0, 5)
                      .map((o: any) => (
                        <div key={o.order_id} className="text-[12px]">
                          <span className="code">{o.number}</span> {o.delta_minutes > 0 ? `▼ ${t("later")}` : `▲ ${t("earlier")}`} {duration(Math.abs(o.delta_minutes))}{" "}
                          {o.status_after !== o.status_before && <StatusPill status={o.status_after} />}
                        </div>
                      ))}
                  </div>
                </div>
              )}
              <Field label={needsReason ? t("Reason (required, audit log)") : t("Reason (audit log)")} error={needsReason && !reason.trim() ? t("Changing the frozen zone or accepting hard violations needs a reason.") : undefined}>
                <input className="mx-input w-full" value={reason} onChange={(e) => setReason(e.target.value)} placeholder={t("e.g. customer called, expedite")} />
              </Field>
            </>
          )}
        </div>
      )}
    </Dialog>
  );
}
