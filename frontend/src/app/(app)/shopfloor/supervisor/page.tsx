"use client";
import { useState } from "react";
import { ErrorState, Loading, PageHeader, Select, StatusPill } from "@/components/ui";
import { dt, time } from "@/lib/format";
import { useApi, useEvents } from "@/lib/hooks";
import { useSession } from "@/lib/session";
import { SectionData } from "@/components/data/SectionData";

function More({ shown, total }: { shown: number; total: number | undefined }) {
  const { t } = useSession();
  return total && total > shown ? <div className="text-slate-600 text-[11.5px]">… {t("{n} more", { n: total - shown })}</div> : null;
}

export default function SupervisorPage() {
  const { t, plant } = useSession();
  const [area, setArea] = useState("");
  const areas = useApi<any>(plant ? "/master-data/areas" : null, plant ? { plant_id: plant.id, limit: 500 } : undefined);
  const d = useApi<any>(plant ? "/supervisor" : null, plant ? { plant_id: plant.id, area_id: area || undefined } : undefined);
  useEvents((type) => (type.startsWith("event.") || type === "plan.published") && d.reload(), plant?.id);
  if (!plant) return <Loading />;
  return (
    <div className="flex flex-col h-full min-h-0">
      <PageHeader
        title={t("nav.supervisor")}
        subtitle={d.data ? `${d.data.plan.number} · ${dt(d.data.at)} · ${t("{n} resources", { n: d.data.resources.length })}` : undefined}
        actions={
          <>
            <Select ariaLabel={t("Area")} value={area} onChange={setArea} options={[{ value: "", label: t("All areas") }, ...(areas.data?.items || []).map((a: any) => ({ value: a.id, label: `${a.code} · ${a.name}` }))]} />
            <SectionData tables={["actual-production", "order-operations", "downtimes", "maintenance"]} />
          </>
        }
      />
      <div className="flex-1 overflow-auto mx-scroll p-3">
        {d.error ? (
          <ErrorState error={d.error} onRetry={d.reload} />
        ) : !d.data ? (
          <Loading />
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4 gap-3">
            {d.data.resources.map((r: any) => {
              const problem = r.status !== "AVAILABLE" || r.delayed.length || r.blocked.length || r.material_issues.length;
              return (
                <section key={r.resource_id} className={`mx-panel ${problem ? "border-amber-500" : ""}`} aria-label={r.resource}>
                  <header className="mx-panel-head">
                    <span className="code flex-1">{r.resource}</span>
                    <StatusPill status={r.status} />
                  </header>
                  <div className="p-2.5 text-[12.5px] space-y-1.5">
                    <div>
                      <span className="text-slate-600">{t("Now:")} </span>
                      {r.now ? (
                        <>
                          <span className="code">{r.now.op_id}</span> {r.now.operation} · {t("until {when}", { when: time(r.now.end) })} <StatusPill status={r.now.status} />
                        </>
                      ) : (
                        <span className="text-slate-400">{t("idle")}</span>
                      )}
                    </div>
                    {r.next.map((n: any) => (
                      <div key={n.op_id} className="text-slate-600">
                        {t("Next")} {time(n.start)} · <span className="code">{n.op_id}</span> · {t("qty")} {n.quantity}
                      </div>
                    ))}
                    {r.delayed.map((x: any) => (
                      <div key={x.op_id} className="text-red-600">
                        ▲ <span className="code">{x.op_id}</span> {t(x.reason)}
                      </div>
                    ))}
                    <More shown={r.delayed.length} total={r.delayed_total} />
                    {r.material_issues.map((x: any) => (
                      <div key={x.op_id} className="text-amber-600">
                        ◆ {t("material risk")} <span className="code">{x.op_id}</span>
                      </div>
                    ))}
                    <More shown={r.material_issues.length} total={r.material_issues_total} />
                    {r.blocked.map((x: any) => (
                      <div key={x.op_id} className="text-red-600">
                        ✕ {t("blocked order")} <span className="code">{x.order}</span>
                      </div>
                    ))}
                    <More shown={r.blocked.length} total={r.blocked_total} />
                  </div>
                </section>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
