"use client";
import { useState } from "react";
import { Badge, Button, ErrorState, Loading, PageHeader, Panel, StatusPill } from "@/components/ui";
import { dt } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import { useSession } from "@/lib/session";

/** Data quality center: critical errors block planning; warnings are shown in the plan. */
export default function DataQuality() {
  const { t, plant } = useSession();
  const dq = useApi<any>(plant ? "/data-quality" : null, plant ? { plant_id: plant.id } : undefined);
  const [open, setOpen] = useState<string | null>(null);
  if (!plant) return <Loading />;
  const d = dq.data;
  return (
    <div className="flex flex-col h-full min-h-0">
      <PageHeader
        title={t("nav.dataQuality")}
        subtitle={d ? `checked ${dt(d.checked_at)} · ${d.summary.ok} OK · ${d.summary.warnings} warnings · ${d.summary.errors} errors` : undefined}
        actions={
          <Button icon="refresh" onClick={dq.reload} busy={dq.loading}>
            Check again
          </Button>
        }
      />
      <div className="flex-1 overflow-auto mx-scroll p-3 space-y-2">
        <ErrorState error={dq.error} onRetry={dq.reload} />
        {!d && <Loading />}
        {d?.planning_blocked && <div className="p-2 border border-red-600/40 bg-red-100 text-red-600 rounded-[3px]">▲ Planning is blocked until the errors below are fixed (a plan built on broken data would not be executable).</div>}
        {d?.checks.map((c: any) => (
          <Panel
            key={c.code}
            title={
              <span className="flex items-center gap-2">
                <StatusPill status={c.status === "OK" ? "OK" : c.status === "ERROR" ? "CRITICAL" : "WARNING"} label={c.status === "OK" ? "OK" : c.status.toLowerCase()} />
                {c.title}
                {c.count > 0 && <Badge tone="neutral" glyph={false}>{c.count}</Badge>}
              </span>
            }
            actions={
              c.count > 0 && (
                <Button size="sm" variant="ghost" onClick={() => setOpen(open === c.code ? null : c.code)}>
                  {open === c.code ? "Hide" : "Show"}
                </Button>
              )
            }
          >
            {c.count > 0 && <div className="px-3 py-1.5 text-[12px] text-slate-600">{c.hint}</div>}
            {open === c.code && (
              <table className="mx-table">
                <thead>
                  <tr>
                    {Object.keys(c.examples[0] || {}).map((k) => (
                      <th key={k}>{k}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {c.examples.map((e: any, i: number) => (
                    <tr key={i}>
                      {Object.values(e).map((v: any, j) => (
                        <td key={j} className="whitespace-normal">
                          {typeof v === "object" ? JSON.stringify(v) : String(v ?? "")}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Panel>
        ))}
      </div>
    </div>
  );
}
