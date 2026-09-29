"use client";
import { useEffect, useState } from "react";
import { Badge, Button, Icon, ProgressBar, statusTone } from "@/components/ui";
import { api } from "@/lib/api";
import { useEvents } from "@/lib/hooks";

/** Terminal run states. STALE: computed, but the data or the current plan changed meanwhile — the
 * result is kept as a separate version and was not applied. */
export const FINAL = ["SUCCEEDED", "FAILED", "CANCELLED", "STALE"];

/** Live progress of a planning run: the 17 pipeline steps with their real status and details. */
export function RunProgress({ runId, onDone, onClose }: { runId: string; onDone: (run: any) => void; onClose: () => void }) {
  const [run, setRun] = useState<any>(null);
  const [t0] = useState(Date.now());
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    let stop = false;
    const tick = async () => {
      try {
        const r = await api(`/planning/runs/${runId}`);
        if (stop) return;
        setRun(r);
        if (FINAL.includes(r.status)) {
          onDone(r);
          return;
        }
      } catch {
        /* retry */
      }
      if (!stop) setTimeout(tick, 1000);
    };
    tick();
    const iv = setInterval(() => setElapsed(Math.round((Date.now() - t0) / 1000)), 500);
    return () => {
      stop = true;
      clearInterval(iv);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId]);
  useEvents((type, data) => {
    if (type === "planning.run.progress" && data?.run_id === runId)
      setRun((r: any) => (r ? { ...r, status: r.status === "QUEUED" ? "RUNNING" : r.status, progress: data.progress, current_step: data.current_step } : r));
  });
  const steps: any[] = run?.progress || [];
  const done = steps.filter((s) => s.status === "DONE").length;
  const finished = run && FINAL.includes(run.status);
  return (
    <div className="absolute right-3 top-3 z-30 w-[380px] mx-panel shadow-xl" role="status" aria-live="polite">
      <div className="mx-panel-head">
        <Icon name="refresh" size={14} className={finished ? "" : "animate-spin"} />
        <span className="flex-1">Planning run</span>
        {run && <Badge tone={statusTone(run.status)}>{run.status}</Badge>}
        <button className="mx-btn mx-btn-ghost mx-btn-sm" aria-label="Close" onClick={onClose}>
          <Icon name="x" />
        </button>
      </div>
      <div className="p-2.5 space-y-2">
        <div className="flex justify-between text-[11.5px] text-slate-600 tabular">
          <span>{run?.current_step || "Queued"}</span>
          <span>{run?.duration_s ?? elapsed} s</span>
        </div>
        <ProgressBar value={steps.length ? done / steps.length : 0} indeterminate={!finished && !steps.length} />
        <ol className="max-h-[260px] overflow-auto mx-scroll text-[11.5px] space-y-0.5">
          {steps.map((s) => (
            <li key={s.step} className="flex gap-1.5">
              <span aria-hidden="true" className={s.status === "DONE" ? "text-green-600" : s.status === "RUNNING" ? "text-blue-500" : s.status === "FAILED" ? "text-red-600" : "text-slate-400"}>
                {s.status === "DONE" ? "✓" : s.status === "RUNNING" ? "▸" : s.status === "FAILED" ? "▲" : s.status === "SKIPPED" ? "–" : "○"}
              </span>
              <span className={s.status === "PENDING" ? "text-slate-400" : ""}>
                {s.step}
                {s.detail && <span className="text-slate-600"> — {s.detail}</span>}
              </span>
            </li>
          ))}
        </ol>
        {run?.error_message && <div className="text-red-600 text-[12px]">▲ {run.error_message}</div>}
        {run?.status === "STALE" && run.result && (
          <div className="text-amber-600 text-[12px]" role="alert">
            ▲ {run.result.stale} ({run.result.plan_number})
          </div>
        )}
        {run?.status === "SUCCEEDED" && run.result && (
          <div className="text-[12px]">
            ✓ {run.result.plan_number} · solver {run.solver_status}
            {run.gap !== null && run.gap !== undefined ? ` · gap ${(run.gap * 100).toFixed(1)} %` : ""}
            {(run.result.messages || []).slice(0, 3).map((m: string, i: number) => (
              <div key={i} className="text-slate-600">
                {m}
              </div>
            ))}
          </div>
        )}
        {!finished && (
          <Button size="sm" variant="danger" icon="stop" onClick={() => api(`/planning/runs/${runId}/cancel`, { method: "POST" }).catch(() => {})}>
            Stop and keep best plan found
          </Button>
        )}
      </div>
    </div>
  );
}
