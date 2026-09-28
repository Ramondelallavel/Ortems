"use client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Badge, Button, Empty, ErrorState, Icon, Kpi, Loading, Panel, StatusPill, statusTone, useToast } from "@/components/ui";
import { api } from "@/lib/api";
import { dt, duration, num, pct } from "@/lib/format";
import { useApi, useEvents } from "@/lib/hooks";
import { useSession } from "@/lib/session";

const UNIT_FMT: Record<string, (v: number | null) => string> = {
  "%": (v) => (v === null || v === undefined ? "—" : num(v, 1)),
  h: (v) => (v === null || v === undefined ? "—" : num(v, 1)),
  "": (v) => (v === null || v === undefined ? "—" : num(v)),
};

export default function Dashboard() {
  const { t, plant, can } = useSession();
  const router = useRouter();
  const toast = useToast();
  const d = useApi<any>(plant ? "/dashboard" : null, plant ? { plant_id: plant.id } : undefined);
  useEvents((type) => {
    if (["alert.created", "planning.run.finished", "plan.published", "plan.head_changed"].includes(type)) d.reload();
  }, plant?.id);

  if (!plant) return <Loading />;
  if (d.error && !d.data) return <ErrorState error={d.error} onRetry={d.reload} />;
  if (!d.data) return <Loading />;
  const x = d.data;

  const ack = async (id: string) => {
    try {
      await api(`/alerts/${id}/acknowledge`, { body: {} });
      d.reload();
    } catch (e) {
      toast.error(e);
    }
  };
  const go = (a: any) => {
    const c = a.context || {};
    if (c.order_id) router.push(`/planning/orders?q=${encodeURIComponent(c.order_number || "")}`);
    else if (c.resource_id) router.push(`/planning?scenario=${x.live_scenario_id}`);
    else if (a.type?.startsWith("MATERIAL")) router.push("/planning/materials");
    else if (a.source === "PLAN") router.push("/planning");
    else router.push("/planning/alerts");
  };

  return (
    <div className="flex-1 overflow-auto mx-scroll">
      <div className="px-4 pt-3 pb-2 bg-white border-b border-gray-200 flex flex-wrap items-center gap-x-6 gap-y-1">
        <div>
          <h1 className="text-[16px] font-semibold">
            {t(greetKey(x.greeting.local_time))}, {x.greeting.name}
          </h1>
          <div className="text-[12px] text-slate-600">
            {x.plant.name} · {dt(x.greeting.local_time, { weekday: "long", day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" })} ({x.plant.timezone})
          </div>
        </div>
        <div className="text-[12px] text-slate-600 flex flex-wrap gap-x-4">
          <span>
            {t("dash.currentPlan")}: <b className="text-graphite-800">{x.head_plan?.number || "—"}</b> {x.head_plan && <Badge tone={statusTone(x.head_plan.status)}>{x.head_plan.status}</Badge>}
          </span>
          <span>
            {t("dash.published")}: <b className="text-graphite-800">{x.published_plan?.number || t("dash.none")}</b>
            {x.published_plan?.published_at ? ` · ${dt(x.published_plan.published_at)}` : ""}
          </span>
          {x.last_run && (
            <span>
              {t("dash.lastRun")}: <StatusPill status={x.last_run.status} /> {dt(x.last_run.created_at)} {x.last_run.duration_s ? `· ${num(x.last_run.duration_s, 1)} s` : ""}
            </span>
          )}
        </div>
        <div className="flex-1" />
        <div className="flex gap-2">
          <Button icon="board" onClick={() => router.push("/planning")}>
            {t("nav.board")}
          </Button>
          {can("plan:run") && !x.head_plan && (
            <Button variant="primary" icon="play" onClick={() => router.push("/planning")}>
              {t("dash.runFirst")}
            </Button>
          )}
        </div>
      </div>

      <div className="p-4 space-y-4">
        {x.tiles.length > 0 ? (
          <div className="grid grid-cols-2 sm:grid-cols-4 xl:grid-cols-8 gap-2">
            {x.tiles.map((tl: any) => (
              <Kpi
                key={tl.code}
                label={t(`kpi.${tl.code}`).startsWith("kpi.") ? tl.label : t(`kpi.${tl.code}`)}
                value={(UNIT_FMT[tl.unit] || UNIT_FMT[""])(tl.value)}
                unit={tl.unit || undefined}
                delta={tl.delta !== null && tl.delta !== undefined && tl.delta !== 0 ? `${tl.delta > 0 ? "+" : ""}${num(tl.delta, 1)} ${t("dash.vsPublished")}` : null}
                trend={tl.trend}
                onClick={() => router.push(`/analytics?kpi=${tl.code}`)}
                hint={t("dash.drill")}
              />
            ))}
          </div>
        ) : (
          <Empty title={t("dash.noPlan")} icon="board" />
        )}

        <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
          <Panel title={`${t("dash.attention")} (${x.attention.length})`} className="xl:col-span-2" bodyClass="max-h-[380px] overflow-auto mx-scroll" id="att">
            {!x.attention.length ? (
              <div className="p-4 text-slate-600">✓ {t("dash.nothing")}</div>
            ) : (
              <ul>
                {x.attention.map((a: any, i: number) => (
                  <li key={`${a.source}-${a.id}-${i}`} className="flex items-start gap-2 px-3 py-2 border-b border-gray-100 hover:bg-gray-50">
                    <StatusPill status={a.severity} />
                    <button className="flex-1 min-w-0 text-left" onClick={() => go(a)}>
                      <div className="font-semibold truncate">
                        {a.title}
                        {a.count > 1 ? <span className="text-slate-600 font-normal"> ×{a.count}</span> : null}
                      </div>
                      <div className="text-slate-600 text-[12px] line-clamp-2">{a.message}</div>
                    </button>
                    <span className="text-[11px] text-slate-400 whitespace-nowrap tabular">{a.at ? dt(a.at) : ""}</span>
                    {a.source === "ALERT" && can("alerts:manage") && (
                      <Button size="sm" variant="ghost" onClick={() => ack(a.id)} title={t("dash.ack")}>
                        <Icon name="check" size={13} />
                      </Button>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </Panel>

          <Panel title={t("dash.health")} id="health">
            {x.health ? (
              <dl className="grid grid-cols-2 gap-px bg-gray-100">
                {[
                  [t("dash.hardViolations"), x.health.critical_issues, "bad"],
                  [t("dash.softDeviations"), x.health.warnings, "warn"],
                  [t("dash.ordersAtRisk"), x.health.orders_at_risk, "bad"],
                  [t("dash.overloaded"), x.health.overloaded_resources, "bad"],
                  [t("dash.materialIssues"), x.health.material_issues, "warn"],
                ].map(([l, v, tone]) => (
                  <div key={l as string} className="bg-white px-3 py-2">
                    <dt className="text-[11px] text-slate-600">{l}</dt>
                    <dd className={`text-[18px] font-semibold tabular ${v ? (tone === "bad" ? "text-red-600" : "text-amber-600") : "text-green-600"}`}>
                      {v ? (tone === "bad" ? "▲ " : "◆ ") : "✓ "}
                      {num(v as number)}
                    </dd>
                  </div>
                ))}
                <div className="bg-white px-3 py-2">
                  <dt className="text-[11px] text-slate-600">{t("dash.changes")}</dt>
                  <dd className="text-[12px] tabular">
                    {t("dash.changesLine", { orders: x.changes.new_orders, alerts: x.changes.new_alerts, floor: x.changes.shop_floor_events, versions: x.changes.plan_versions })}
                  </dd>
                </div>
              </dl>
            ) : (
              <div className="p-3 text-slate-600">—</div>
            )}
          </Panel>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          <Panel title={t("dash.lateOrders")} actions={<Link className="text-[11.5px] text-blue-500 hover:underline" href="/planning/orders?status=LATE">{t("dash.all")}</Link>} bodyClass="max-h-[300px] overflow-auto mx-scroll" id="late">
            <table className="mx-table">
              <tbody>
                {x.late_orders.map((o: any) => (
                  <tr key={o.order_id} className="cursor-pointer" onClick={() => router.push(`/planning/orders?q=${o.number}`)}>
                    <td className="code">{o.number}</td>
                    <td>
                      <StatusPill status={o.status} />
                    </td>
                    <td className="num">{o.lateness_minutes ? duration(o.lateness_minutes) : "—"}</td>
                    <td className="text-slate-600 truncate max-w-[140px]" title={o.cause_text}>
                      {o.cause}
                    </td>
                  </tr>
                ))}
                {!x.late_orders.length && (
                  <tr>
                    <td className="text-slate-600">✓ {t("dash.noRisk")}</td>
                  </tr>
                )}
              </tbody>
            </table>
          </Panel>
          <Panel title={t("dash.bottlenecks")} actions={<Link className="text-[11.5px] text-blue-500 hover:underline" href="/planning/capacity">{t("dash.capacity")}</Link>} bodyClass="max-h-[300px] overflow-auto mx-scroll" id="bn">
            <table className="mx-table">
              <tbody>
                {x.bottlenecks.map((b: any) => (
                  <tr key={b.rank}>
                    <td className="num w-6">{b.rank}</td>
                    <td className="code">{b.resource || "—"}</td>
                    <td>
                      <Badge tone={statusTone(b.kind)}>{b.kind.replaceAll("_", " ").toLowerCase()}</Badge>
                    </td>
                    <td className="num">{b.utilization ? pct(b.utilization, 0, true) : ""}</td>
                    <td className="num text-slate-600" title="Waiting induced on other operations">
                      {b.induced_wait_minutes ? duration(b.induced_wait_minutes) : ""}
                    </td>
                  </tr>
                ))}
                {!x.bottlenecks.length && (
                  <tr>
                    <td className="text-slate-600">{t("dash.noBottleneck")}</td>
                  </tr>
                )}
              </tbody>
            </table>
          </Panel>
          <Panel title={t("dash.today")} bodyClass="p-3 text-[12.5px] space-y-1" id="today">
            {x.today?.plan ? (
              <>
                <div>
                  {t("dash.opsStart", { ops: x.today.operations_starting_24h, res: x.today.resources_busy_24h, plan: x.today.plan })}
                </div>
                {x.today.resources_down?.length > 0 && <div className="text-red-600">▲ {t("dash.down")}: {x.today.resources_down.join(", ")}</div>}
                {x.today.maintenance_24h?.map((m: any, i: number) => (
                  <div key={i}>
                    ✕ <span className="code">{m.resource}</span> {m.kind.toLowerCase()} {dt(m.start)}–{dt(m.end, { hour: "2-digit", minute: "2-digit" })} {m.description ? `· ${m.description}` : ""}
                  </div>
                ))}
                {x.material_issues?.length > 0 && (
                  <div className="text-amber-600">
                    ◆ {t("dash.materials")}: <span className="code">{x.material_issues.slice(0, 6).join(", ")}</span>
                  </div>
                )}
                <div className="pt-1 flex gap-2">
                  <Button size="sm" icon="dispatch" onClick={() => router.push("/shopfloor/dispatch")}>
                    {t("nav.dispatch")}
                  </Button>
                  <Button size="sm" icon="eye" onClick={() => router.push("/shopfloor/supervisor")}>
                    {t("nav.supervisor")}
                  </Button>
                </div>
              </>
            ) : (
              <div className="text-slate-600">{t("dash.noPlanYet")}</div>
            )}
          </Panel>
        </div>
      </div>
    </div>
  );
}

function greetKey(iso: string): string {
  // hour in the plant's time zone is encoded in the ISO offset returned by the server
  const h = Number(iso.slice(11, 13));
  return h < 12 ? "dash.morning" : h < 20 ? "dash.afternoon" : "dash.evening";
}
