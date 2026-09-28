"use client";
import { useState } from "react";
import { Button, DataTable, Dialog, ErrorState, Field, Loading, PageHeader, Select, StatusPill, useToast } from "@/components/ui";
import { api } from "@/lib/api";
import { dt } from "@/lib/format";
import { useApi, useEvents } from "@/lib/hooks";
import { useSession } from "@/lib/session";

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
        subtitle="Generated from each new plan and from shop-floor events. Severity is never shown by colour alone."
        actions={<Select ariaLabel="Status" value={status} onChange={setStatus} options={["OPEN", "ACKNOWLEDGED", "RESOLVED", "ALL"].map((s) => ({ value: s, label: s.toLowerCase() }))} />}
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
              { key: "severity", label: "Severity", width: 100, render: (r) => <StatusPill status={r.severity} /> },
              { key: "type", label: "Type", width: 170 },
              { key: "title", label: "Title" },
              { key: "message", label: "Message" },
              { key: "count", label: "Count", align: "right", width: 60 },
              { key: "created_at", label: "Created", width: 130, render: (r) => dt(r.created_at) },
              { key: "status", label: "Status", width: 110, render: (r) => <StatusPill status={r.status} label={r.status.toLowerCase()} /> },
              { key: "acknowledged_by", label: "By", width: 100 },
            ]}
          />
        )}
      </div>
      <Dialog
        open={!!ack}
        onClose={() => setAck(null)}
        title={ack?.title || ""}
        footer={
          <>
            <Button onClick={() => setAck(null)}>Cancel</Button>
            <Button onClick={() => submit(false)}>Acknowledge</Button>
            <Button variant="primary" onClick={() => submit(true)}>
              Resolve
            </Button>
          </>
        }
      >
        <p className="mb-2">{ack?.message}</p>
        <Field label="Note">
          <textarea className="mx-input w-full" rows={3} value={note} onChange={(e) => setNote(e.target.value)} />
        </Field>
      </Dialog>
    </div>
  );
}
