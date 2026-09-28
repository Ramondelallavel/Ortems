"use client";
import { useEffect, useState } from "react";
import { Badge, Button, Dialog, ErrorState, Field, Loading, Select, useToast } from "@/components/ui";
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
    setLoading(true);
    setError(undefined);
    setPreview(null);
    api(`/plans/${planId}/moves/preview`, { body: { op_id: move.opId, resource_id: move.resourceId, start: move.start, replan: mode, allow_frozen: allowFrozen } })
      .then(setPreview)
      .catch(setError)
      .finally(() => setLoading(false));
  }, [move, mode, planId, allowFrozen]);

  const apply = async () => {
    if (!move) return;
    setBusy(true);
    try {
      const p = await api(`/plans/${planId}/moves`, { body: { op_id: move.opId, resource_id: move.resourceId, start: move.start, replan: mode, reason: reason || undefined, expected_version: planVersion, accept_violations: !preview?.feasible, allow_frozen: allowFrozen } });
      toast.ok(`${p.number} created (manual edit). Undo with Ctrl+Z.`);
      onApplied(p);
    } catch (e) {
      toast.error(e);
    } finally {
      setBusy(false);
    }
  };
  const cmp = preview?.comparison;
  return (
    <Dialog
      open={!!move}
      onClose={onClose}
      width={640}
      title={`${t("plan.movePreview")} — ${move?.opId || ""}`}
      footer={
        <>
          <Button onClick={onClose}>{t("common.cancel")}</Button>
          <Button variant={preview && !preview.feasible ? "danger" : "primary"} busy={busy} disabled={!preview || loading} onClick={apply}>
            {preview && !preview.feasible ? "Apply with violations" : t("plan.applyMove")}
          </Button>
        </>
      }
    >
      {move && (
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <Field label="Target">
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
              <input type="checkbox" checked={allowFrozen} onChange={(e) => setAllowFrozen(e.target.checked)} /> Change the frozen zone (requires a reason; recorded in the audit log)
            </label>
          )}
          {loading && <Loading label="Re-scheduling and validating every hard constraint…" />}
          <ErrorState error={error} />
          {preview && (
            <>
              <div className={`p-2 rounded-[3px] border ${preview.feasible ? "border-green-600/40 bg-green-100" : "border-red-600/40 bg-red-100"}`}>
                <b>{preview.feasible ? "✓ Feasible" : "▲ Would violate hard constraints"}</b>
                {preview.planned && (
                  <span className="tabular">
                    {" "}
                    — planned {dt(preview.planned.setup_start)} → {dt(preview.planned.end)} on <span className="code">{resCodes[preview.planned.resource_id] || preview.planned.resource_id}</span>
                    {preview.planned.start !== move.start && Math.abs(new Date(preview.planned.start).getTime() - new Date(move.start).getTime()) > 60000 && <span className="text-slate-600"> (earliest valid position after the requested time)</span>}
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
                        <th className="!text-right">Before</th>
                        <th className="!text-right">After</th>
                      </tr>
                    </thead>
                    <tbody>
                      {KPI_SHOW.filter((k) => cmp.kpis[k]).map((k) => {
                        const v = cmp.kpis[k];
                        return (
                          <tr key={k}>
                            <td>{k.replaceAll("_", " ")}</td>
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
                      Operations moved: <b className="tabular">{cmp.operations_moved}</b>
                    </div>
                    <div>
                      Orders delayed / advanced: <b className="tabular">{cmp.orders_delayed}</b> / <b className="tabular">{cmp.orders_advanced}</b>
                    </div>
                    <div>
                      Setup change: <b className="tabular">{duration(cmp.setup_delta_minutes)}</b>
                    </div>
                    <div>
                      Sequence changes: <b className="tabular">{cmp.sequence_changes}</b>
                    </div>
                    {cmp.orders
                      .filter((o: any) => o.delta_minutes)
                      .slice(0, 5)
                      .map((o: any) => (
                        <div key={o.order_id} className="text-[12px]">
                          <span className="code">{o.number}</span> {o.delta_minutes > 0 ? "▼ later" : "▲ earlier"} {duration(Math.abs(o.delta_minutes))}{" "}
                          {o.status_after !== o.status_before && <Badge tone={o.status_after === "LATE" ? "bad" : "ok"}>{o.status_after}</Badge>}
                        </div>
                      ))}
                  </div>
                </div>
              )}
              <Field label="Reason (audit log)">
                <input className="mx-input w-full" value={reason} onChange={(e) => setReason(e.target.value)} placeholder="e.g. customer called, expedite" />
              </Field>
            </>
          )}
        </div>
      )}
    </Dialog>
  );
}
