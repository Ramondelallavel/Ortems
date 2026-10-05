"use client";
import { useState } from "react";
import { Button, DataTable, Dialog, ErrorState, Field, Loading, PageHeader, Select, StatusPill, useToast } from "@/components/ui";
import { api } from "@/lib/api";
import { dt } from "@/lib/format";
import { useApi, useEvents } from "@/lib/hooks";
import { useSession } from "@/lib/session";
import { alertText } from "@/lib/alerts";

export default function AlertsPage() {
  const { t, plant, can } = useSession();
  const toast = useToast();
  const [status, setStatus] = useState("OPEN");
  const alerts = useApi<any[]>(plant ? "/alerts" : null, plant ? { plant_id: plant.id, status } : undefined);
  const [ack, setAck] = useState<any>(null);
  const [note, setNote] = useState("");
  useEvents((type) => type === "alert.created" && alerts.reload(), plant?.id);
  const submit = async (resolve: boolean) => {
    try {
      await api(`/alerts/${ack.id}/acknowledge`, { body: { note: note || null, resolve } });
      setAck(null);
      setNote("");
      alerts.reload();
    } catch (e) {
      toast.error(e);
    }
  };
  if (!plant) return <Loading />;
  return (
    <div className="flex flex-col h-full min-h-0">
      <PageHeader
        title={t("nav.alerts")}
        subtitle={t("Generated from each new plan and from shop-floor events. Severity is never shown by colour alone.")}
        actions={<Select ariaLabel={t("common.status")} value={status} onChange={setStatus} options={["OPEN", "ACKNOWLEDGED", "RESOLVED", "ALL"].map((s) => ({ value: s, label: s === "ALL" ? t("common.all") : t(`status.${s}`) }))} />}
      />
      <div className="flex-1 min-h-0 bg-white">
        {alerts.error ? (
          <ErrorState error={alerts.error} onRetry={alerts.reload} />
        ) : !alerts.data ? (
          <Loading />
        ) : (
          <DataTable
            rows={alerts.data}
            rowKey={(r) => r.id}
            exportName="alerts"
            onRowClick={can("alerts:manage") ? setAck : undefined}
            columns={[
              { key: "severity", label: t("Severity"), width: 100, render: (r) => <StatusPill status={r.severity} /> },
              { key: "type", label: t("Type"), width: 170 },
              { key: "title", label: t("Title"), value: (r) => alertText(t, dt, r, "title") },
              { key: "message", label: t("Message"), value: (r) => alertText(t, dt, r, "message") },
              { key: "count", label: t("Count"), align: "right", width: 60 },
              { key: "created_at", label: t("Created"), width: 130, render: (r) => dt(r.created_at) },
              { key: "status", label: t("common.status"), width: 110, render: (r) => <StatusPill status={r.status} /> },
              { key: "acknowledged_by", label: t("By"), width: 100 },
            ]}
          />
        )}
      </div>
      <Dialog
        open={!!ack}
        onClose={() => setAck(null)}
        title={ack ? alertText(t, dt, ack, "title") : ""}
        footer={
          <>
            <Button onClick={() => setAck(null)}>{t("common.cancel")}</Button>
            {ack?.status === "OPEN" && <Button onClick={() => submit(false)}>{t("Acknowledge")}</Button>}
            {ack?.status !== "RESOLVED" && (
              <Button variant="primary" onClick={() => submit(true)}>
                {t("Resolve")}
              </Button>
            )}
          </>
        }
      >
        <p className="mb-2">{ack ? alertText(t, dt, ack, "message") : ""}</p>
        {ack?.acknowledged_by && <p className="mb-2 text-[12px] text-slate-600">{t("{status} by {who} · {at}", { status: t(`status.${ack.status}`), who: ack.acknowledged_by, at: dt(ack.acknowledged_at) })}{ack.note ? ` — ${ack.note}` : ""}</p>}
        <Field label={t("Note")}>
          <textarea className="mx-input w-full" rows={3} value={note} onChange={(e) => setNote(e.target.value)} />
        </Field>
      </Dialog>
    </div>
  );
}
