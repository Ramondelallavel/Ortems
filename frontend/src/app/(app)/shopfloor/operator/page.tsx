"use client";
import { useState } from "react";
import { Button, ErrorState, Field, Loading, Select, StatusPill, useToast } from "@/components/ui";
import { api } from "@/lib/api";
import { dt, duration, time } from "@/lib/format";
import { useApi, useLocalState } from "@/lib/hooks";
import { useSession } from "@/lib/session";

/** Operator terminal: large touch targets, current and next jobs, start / stop / report quantities. */
export default function OperatorPage() {
  const { t, plant, can } = useSession();
  const toast = useToast();
  const [resId, setResId] = useLocalState<string>("mx.operator.resource", "");
  const resources = useApi<any>(plant ? "/resources" : null, plant ? { plant_id: plant.id } : undefined);
  const view = useApi<any>(plant && resId ? "/operator" : null, plant && resId ? { plant_id: plant.id, resource_id: resId } : undefined);
  const [good, setGood] = useState("");
  const [scrap, setScrap] = useState("");
  const [busy, setBusy] = useState(false);
  const machines = (resources.data?.items || []).filter((r: any) => ["MACHINE", "WORK_CENTER", "LINE"].includes(r.kind));
  const cur = view.data?.current;

  const report = async (action: string) => {
    if (!cur?.order_operation_id) return;
    setBusy(true);
    try {
      const g = qty(good);
      const sc = qty(scrap);
      if (Number.isNaN(g) || Number.isNaN(sc) || g < 0 || sc < 0) {
        toast.warn(t("Enter quantities as positive numbers (for example 12 or 12,5)."));
        return;
      }
      await api("/execution/report", { body: { order_operation_id: cur.order_operation_id, action, resource_id: resId, good_quantity: g, scrap_quantity: sc } });
      toast.ok(t(`op.reported.${action}`, { op: cur.op_id }));
      setGood("");
      setScrap("");
      view.reload();
    } catch (e) {
      toast.error(e);
    } finally {
      setBusy(false);
    }
  };
  if (!plant) return <Loading />;
  return (
    <div className="flex-1 overflow-auto mx-scroll p-4 text-[15px]">
      <div className="max-w-3xl mx-auto space-y-4">
        <div className="flex items-end gap-3">
          <Field label={t("nav.operator") + " — " + t("common.resource")}>
            <Select value={resId} onChange={setResId} className="!h-10 min-w-[220px] !text-[15px]" options={[{ value: "", label: t("Select your machine") }, ...machines.map((r: any) => ({ value: r.id, label: `${r.code} · ${r.name}` }))]} />
          </Field>
          {view.data && (
            <div className="pb-2 text-slate-600 text-[13px]">
              {view.data.plan_number} · <StatusPill status={view.data.resource.status} />
            </div>
          )}
        </div>
        <ErrorState error={view.error} onRetry={view.reload} />
        {resId && !view.data && !view.error && <Loading />}
        {cur ? (
          <section className="mx-panel p-4 space-y-3" aria-label={t("Current job")}>
            <div className="flex items-center gap-3">
              <div className="text-[22px] font-semibold code">{cur.order}</div>
              <StatusPill status={cur.status} />
            </div>
            <div className="text-[17px]">
              {cur.operation} · <span className="code">{cur.product}</span> {cur.product_name}
            </div>
            <div className="grid grid-cols-3 gap-3 tabular">
              <div>
                <div className="mx-label">{t("Quantity")}</div>
                <div className="text-[20px] font-semibold">
                  {cur.completed_quantity} / {cur.quantity}
                </div>
              </div>
              <div>
                <div className="mx-label">{t("Setup")}</div>
                {time(cur.setup_start)} · {duration(cur.setup_minutes)}
              </div>
              <div>
                <div className="mx-label">{t("Run")}</div>
                {time(cur.start)} → {dt(cur.end)}
              </div>
            </div>
            {cur.setup_instructions && <div className="border-l-4 border-navy-700 pl-3 text-[14px]">{cur.setup_instructions}</div>}
            {cur.quality && <div className="text-amber-600 text-[14px]">◆ {cur.quality}</div>}
            {can("execution:report") && (
              <div className="space-y-3 pt-2 border-t border-gray-200">
                <div className="grid grid-cols-2 gap-3">
                  <Field label={t("Good quantity")}>
                    <input className="mx-input w-full !h-11 !text-[17px]" inputMode="decimal" value={good} onChange={(e) => setGood(e.target.value)} />
                  </Field>
                  <Field label={t("Scrap")}>
                    <input className="mx-input w-full !h-11 !text-[17px]" inputMode="decimal" value={scrap} onChange={(e) => setScrap(e.target.value)} />
                  </Field>
                </div>
                <div className="flex flex-wrap gap-2">
                  {cur.status !== "IN_PROGRESS" ? (
                    <Button variant="primary" className="!h-12 !px-6 !text-[15px]" busy={busy} onClick={() => report("START")}>
                      ▶ {t("Start")}
                    </Button>
                  ) : (
                    <>
                      <Button className="!h-12 !px-6 !text-[15px]" busy={busy} onClick={() => report("QUANTITY")} disabled={!good && !scrap}>
                        {t("Report quantity")}
                      </Button>
                      <Button className="!h-12 !px-6 !text-[15px]" busy={busy} onClick={() => report("PAUSE")}>
                        ❚❚ {t("Pause")}
                      </Button>
                      <Button variant="primary" className="!h-12 !px-6 !text-[15px]" busy={busy} onClick={() => report("FINISH")}>
                        ✓ {t("Finish")}
                      </Button>
                    </>
                  )}
                </div>
              </div>
            )}
          </section>
        ) : (
          view.data && <div className="mx-panel p-6 text-center text-slate-600">{t("No job planned on this machine.")}</div>
        )}
        {view.data?.next?.length > 0 && (
          <section className="mx-panel" aria-label={t("Next jobs")}>
            <div className="mx-panel-head">{t("Next")}</div>
            {view.data.next.map((j: any) => (
              <div key={j.op_id} className="px-3 py-2 border-b border-gray-100 flex gap-3 tabular">
                <span className="w-[120px]">{dt(j.setup_start)}</span>
                <span className="code">{j.order}</span>
                <span className="flex-1 truncate">{j.operation}</span>
                <span>× {j.quantity}</span>
              </div>
            ))}
          </section>
        )}
      </div>
    </div>
  );
}

/** Quantity typed on the terminal: empty = 0, decimal comma or point. */
function qty(v: string): number {
  const t = v.trim().replace(/\s/g, "").replace(",", ".");
  return t === "" ? 0 : Number(t);
}
