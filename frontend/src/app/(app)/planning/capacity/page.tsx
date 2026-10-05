"use client";
import { useEffect, useMemo, useState } from "react";
import { Chart, INK, SEQ_BLUE, STATUS, axisCat, axisVal } from "@/components/charts/Chart";
import { Badge, ErrorState, Loading, PageHeader, Panel, Select, statusTone } from "@/components/ui";
import { num } from "@/lib/format";
import { useApi, useQueryParam } from "@/lib/hooks";
import { useScenarioSelection } from "@/lib/plan";
import { useSession } from "@/lib/session";
import { SectionData } from "@/components/data/SectionData";

const STATE_GLYPH: Record<string, string> = { UNDERLOADED: "○", BALANCED: "●", HIGH_LOAD: "◆", OVERLOADED: "▲", UNAVAILABLE: "✕" };
const PAGE = 100;

export default function CapacityPage() {
  const { t, plant } = useSession();
  const { selected } = useScenarioSelection();
  const planId = selected?.head_plan_id;
  const [bucket, setBucket] = useState("day");
  const [groupBy, setGroupBy] = useState("resource");
  const [focus, setFocus] = useState<string | null>(null);
  // a plant with hundreds of machines is paged (the heat-map draws one cell per machine and period)
  const [page, setPage] = useState(0);
  const [typed, setTyped] = useState("");
  const [q, setQ] = useState("");
  const [sort, setSort] = useState<"code" | "load">("code");
  // deep link from an alert: /planning/capacity?resource=<code> shows that resource
  const resParam = useQueryParam("resource");
  useEffect(() => {
    if (resParam) {
      setGroupBy("resource");
      setTyped(resParam);
    }
  }, [resParam]);
  useEffect(() => {
    const h = setTimeout(() => {
      setQ(typed.trim());
      setPage(0);
    }, 300);
    return () => clearTimeout(h);
  }, [typed]);
  const load = useApi<any>(planId ? "/capacity/load" : null, planId ? { plan_id: planId, bucket, group_by: groupBy, offset: page * PAGE, limit: PAGE, q: q || undefined, sort } : undefined);
  const bn = useApi<any>(planId ? "/bottlenecks" : null, planId ? { plan_id: planId } : undefined);
  const totalRows: number = load.data?.rows_total ?? 0;

  const rows = useMemo(() => (load.data?.rows || []).filter((r: any) => !["LABOR_POOL", "TOOL"].includes(r.kind) || r.scheduled > 0 || r.requirement > 0), [load.data]);
  const sel = rows.find((r: any) => r.id === focus) || (resParam && rows.find((r: any) => r.code === resParam)) || rows.find((r: any) => r.requirement > r.capacity) || rows[0];
  const labels = (load.data?.buckets || []).map((b: any) => b.label);

  const loadOption = useMemo(() => {
    if (!sel) return {};
    const h = (m: number) => Math.round((m / 60) * 10) / 10;
    return {
      legend: { top: 0, left: 0, data: [t("Scheduled"), t("Overload"), t("Requirement"), t("Capacity")] },
      grid: { left: 40, right: 16, top: 36, bottom: 24, containLabel: true },
      xAxis: { ...axisCat, data: labels },
      yAxis: { ...axisVal, axisLabel: { color: INK.secondary, formatter: "{value} h" } },
      series: [
        { name: t("Scheduled"), type: "bar", stack: "load", barMaxWidth: 22, itemStyle: { color: SEQ_BLUE[5], borderRadius: [0, 0, 0, 0] }, data: sel.buckets.map((b: any) => h(Math.min(b.scheduled, b.capacity || b.scheduled))) },
        { name: t("Overload"), type: "bar", stack: "load", barMaxWidth: 22, itemStyle: { color: STATUS.bad, borderRadius: [3, 3, 0, 0] }, data: sel.buckets.map((b: any) => h(b.overload || 0)) },
        { name: t("Requirement"), type: "line", step: "middle", symbol: "none", lineStyle: { color: INK.primary, width: 2, type: "dashed" }, itemStyle: { color: INK.primary }, data: sel.buckets.map((b: any) => h(b.requirement)) },
        { name: t("Capacity"), type: "line", step: "middle", symbol: "none", lineStyle: { color: INK.muted, width: 2 }, itemStyle: { color: INK.muted }, data: sel.buckets.map((b: any) => h(b.capacity)) },
      ],
      tooltip: { trigger: "axis", valueFormatter: (v: number) => `${num(v, 1)} h` },
    };
  }, [sel, labels, t]);

  if (!plant) return <Loading />;
  return (
    <div className="flex flex-col h-full min-h-0">
      <PageHeader
        title={t("nav.capacity")}
        subtitle={selected ? `${selected.name} · ${selected.head_plan?.number || t("no plan")} · ${t("load = scheduled work; requirement = work needed to meet due dates")}` : undefined}
        actions={
          <>
            <SectionData tables={["resources", "calendars", "calendars.shifts", "calendars.exceptions", "maintenance", "downtimes", "resource-groups.members", "labor-pools", "operators", "operator-absences"]} />
            <Select ariaLabel={t("cap.bucket")} value={bucket} onChange={setBucket} options={["hour", "shift", "day", "week", "month"].map((b) => ({ value: b, label: t(`bucket.${b}`) }))} />
            <Select
              ariaLabel={t("cap.groupBy")}
              value={groupBy}
              onChange={(v) => {
                setGroupBy(v);
                setFocus(null);
                setPage(0);
              }}
              options={["resource", "group", "area", "plant"].map((b) => ({ value: b, label: t(`by.${b}`) }))}
            />
            {groupBy === "resource" && (
              <>
                <input className="mx-input w-[160px]" placeholder={t("Find resource…")} aria-label={t("Find resource")} value={typed} onChange={(e) => setTyped(e.target.value)} />
                <Select
                  ariaLabel={t("Order")}
                  value={sort}
                  onChange={(v) => {
                    setSort(v as "code" | "load");
                    setPage(0);
                  }}
                  options={[
                    { value: "code", label: t("by code") },
                    { value: "load", label: t("most loaded first") },
                  ]}
                />
              </>
            )}
          </>
        }
      />
      <div className="flex-1 overflow-auto mx-scroll p-3 space-y-3">
        {!planId && <div className="text-slate-600 p-4">{t("No plan yet.")}</div>}
        <ErrorState error={load.error} onRetry={load.reload} />
        {planId && !load.data && <Loading />}
        {load.data && (
          <>
            <Panel title={sel ? `${t("cap.load")} — ${sel.code}${sel.name && sel.name !== sel.code ? ` · ${sel.name}` : ""}` : t("cap.load")} id="load">
              <div className="p-2">{sel && <Chart option={loadOption} height={260} ariaLabel={t("Load chart of {code}", { code: sel.code })} />}</div>
            </Panel>
            <Panel
              title={t("cap.heatmap")}
              id="heat"
              actions={
                <div className="flex gap-2 text-[11px] font-normal">
                  {load.data.states.map((s: string) => (
                    <span key={s} className="flex items-center gap-1">
                      <span aria-hidden="true">{STATE_GLYPH[s]}</span>
                      {t(`heat.${s}`)}
                    </span>
                  ))}
                </div>
              }
            >
              <div className="overflow-auto mx-scroll max-h-[520px]">
                <table className="border-separate border-spacing-[2px] text-[11px]" role="grid" aria-label={t("Capacity heatmap: utilisation per resource and period")}>
                  <thead>
                    <tr>
                      <th className="sticky left-0 top-0 z-10 bg-white text-left px-1 min-w-[120px]">{t(`by.${groupBy}`)}</th>
                      {labels.map((l: string, i: number) => (
                        <th key={i} className="sticky top-0 bg-white font-normal text-slate-600 px-0.5 whitespace-nowrap">
                          {l}
                        </th>
                      ))}
                      <th className="sticky top-0 bg-white px-1 text-right">{t("Total")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((r: any) => (
                      <tr key={r.id} aria-selected={sel?.id === r.id}>
                        <th scope="row" className={`sticky left-0 bg-white text-left px-1 font-normal code cursor-pointer ${sel?.id === r.id ? "underline" : ""}`} onClick={() => setFocus(r.id)}>
                          {r.code}
                        </th>
                        {r.buckets.map((b: any, i: number) => {
                          const u = b.requirement_utilization ?? b.utilization;
                          const bg = b.state === "UNAVAILABLE" ? "#dde2e8" : u === null || u === undefined ? "#f6f7f9" : SEQ_BLUE[Math.min(SEQ_BLUE.length - 1, Math.floor(Math.min(u, 1) * (SEQ_BLUE.length - 1)))];
                          const dark = u !== null && u !== undefined && u > 0.55;
                          const over = b.state === "OVERLOADED";
                          return (
                            <td
                              key={i}
                              title={`${r.code} · ${labels[i]}: ${t(`heat.${b.state}`)} — ${t("scheduled {s} h, requirement {r} h, capacity {c} h", { s: num(b.scheduled / 60, 1), r: num(b.requirement / 60, 1), c: num(b.capacity / 60, 1) })}`}
                              onClick={() => setFocus(r.id)}
                              className="min-w-[34px] h-[22px] text-center tabular cursor-pointer rounded-[2px]"
                              style={{ background: bg, color: dark ? "#fff" : INK.primary, outline: over ? `2px solid ${STATUS.bad}` : undefined, outlineOffset: -2 }}
                            >
                              {b.state === "UNAVAILABLE" ? "✕" : u === null || u === undefined ? "" : `${over ? "▲" : ""}${Math.round(u * 100)}`}
                            </td>
                          );
                        })}
                        <td className="text-right px-1 tabular">{r.capacity ? `${Math.round((100 * r.scheduled) / r.capacity)} %` : "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="px-2 py-1 text-[11px] text-slate-600 border-t border-gray-200 flex items-center gap-3">
                <span className="flex-1">{t("Cell value = requirement ÷ capacity in %, blue scale by magnitude; ▲ with red outline = overloaded; ✕ = no capacity (calendar or maintenance).")}</span>
                {totalRows > PAGE && (
                  <span className="flex items-center gap-1 whitespace-nowrap tabular">
                    <button className="mx-btn mx-btn-ghost mx-btn-sm" disabled={page === 0} onClick={() => setPage((p) => Math.max(0, p - 1))} aria-label={t("Previous rows")}>
                      ‹
                    </button>
                    {t("{a}–{b} of {n}", { a: page * PAGE + 1, b: Math.min(totalRows, (page + 1) * PAGE), n: totalRows.toLocaleString() })}
                    <button className="mx-btn mx-btn-ghost mx-btn-sm" disabled={(page + 1) * PAGE >= totalRows} onClick={() => setPage((p) => p + 1)} aria-label={t("Next rows")}>
                      ›
                    </button>
                  </span>
                )}
              </div>
            </Panel>
          </>
        )}
        {bn.data && (
          <Panel title={t("dash.bottlenecks")} id="bn">
            <table className="mx-table">
              <thead>
                <tr>
                  <th>#</th>
                  <th>{t("Constraint")}</th>
                  <th>{t("Kind")}</th>
                  <th className="!text-right">{t("Utilisation")}</th>
                  <th className="!text-right">{t("Overload")}</th>
                  <th className="!text-right">{t("Induced waiting")}</th>
                  <th className="!text-right">{t("Late orders")}</th>
                  <th>{t("Main causes")}</th>
                </tr>
              </thead>
              <tbody>
                {bn.data.bottlenecks.map((b: any) => {
                  const code = rows.find((r: any) => r.id === b.resource_id)?.code || b.ref;
                  return (
                    <tr
                      key={b.rank}
                      onClick={() => {
                        if (!b.resource_id) return;
                        setFocus(b.resource_id);
                        // the machine may be on another page of the heat-map: filter to it
                        if (!rows.some((r: any) => r.id === b.resource_id) && code) setTyped(code);
                      }}
                      className={b.resource_id ? "cursor-pointer" : ""}
                    >
                      <td className="num">{b.rank}</td>
                      <td className="code">{code}</td>
                      <td>
                        <Badge tone={statusTone(b.kind)}>{t(`bn.${b.kind}`).startsWith("bn.") ? b.kind.replaceAll("_", " ").toLowerCase() : t(`bn.${b.kind}`)}</Badge>
                      </td>
                      <td className="num">{b.resource_id ? `${Math.round(b.utilization * 100)} %` : "—"}</td>
                      <td className="num">{b.overload_minutes ? `${num(b.overload_minutes / 60, 1)} h` : "—"}</td>
                      <td className="num">{b.induced_wait_minutes ? `${num(b.induced_wait_minutes / 60, 1)} h` : "—"}</td>
                      <td className="num">{b.orders_affected || "—"}</td>
                      <td className="text-slate-600">{(b.causes || []).map((c: any) => `${c.kind === "FAMILY" ? t("Orders {ref}", { ref: c.ref }) : c.kind === "MAINTENANCE" ? t("Maintenance / downtime") : c.kind === "SETUP" ? t("Setup") : c.label} ${Math.round(c.share * 100)} %`).join(" · ")}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </Panel>
        )}
      </div>
    </div>
  );
}
