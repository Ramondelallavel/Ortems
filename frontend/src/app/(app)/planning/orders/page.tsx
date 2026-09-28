"use client";
import { useEffect, useMemo, useState } from "react";
import { Badge, Button, Dialog, Drawer, ErrorState, Field, Loading, PageHeader, Select, StatusPill, useToast, type Column } from "@/components/ui";
import { api } from "@/lib/api";
import { date, dt, duration, localInputToIso } from "@/lib/format";
import { useApi, useQueryParam } from "@/lib/hooks";
import { useSession } from "@/lib/session";
import { RemoteTable } from "@/components/data/RemoteTable";
import { SectionData } from "@/components/data/SectionData";

type Row = Record<string, any>;

// server sort keys of the sortable columns (the order book is sorted and paged by the server)
const SORT_KEYS: Record<string, string> = {
  number: "number",
  quantity: "quantity",
  priority: "priority",
  due_date: "due_date",
  planned_end: "planned_end",
  lateness_minutes: "lateness_minutes",
  plan_status: "plan_status",
  material_status: "material_status",
  status: "status",
};

export default function OrdersPage() {
  const { t, plant, can } = useSession();
  const qParam = useQueryParam("q");
  const statusParam = useQueryParam("status");
  const [scope, setScope] = useState("open");
  const [planFilter, setPlanFilter] = useState("");
  const [open, setOpen] = useState<Row | null>(null);
  const [creating, setCreating] = useState(false);
  const [meta, setMeta] = useState<{ total: number; plan_id: string | null; plan_number: string | null } | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  useEffect(() => {
    if (statusParam) setPlanFilter(statusParam);
  }, [statusParam]);
  // a link to one order (?q=number) opens it directly
  const exact = useApi<any>(plant && qParam ? "/orders" : null, plant && qParam ? { plant_id: plant.id, q: qParam, all: 1, limit: 5 } : undefined);
  useEffect(() => {
    const hit = (exact.data?.items || []).find((o: Row) => o.number.toLowerCase() === (qParam || "").toLowerCase());
    if (hit) setOpen(hit);
  }, [exact.data, qParam]);
  const query = useMemo(() => (plant ? { plant_id: plant.id, all: scope === "all" ? 1 : undefined, plan_status: planFilter || undefined } : undefined), [plant, scope, planFilter]);

  const cols: Column<Row>[] = [
    { key: "number", label: t("common.order"), mono: true, width: 110 },
    { key: "item", label: t("common.item"), mono: true, width: 120 },
    { key: "item_name", label: "Description" },
    { key: "family", label: "Family", width: 80 },
    { key: "quantity", label: t("common.quantity"), align: "right", width: 70 },
    { key: "customer", label: t("common.customer") },
    { key: "priority", label: "Prio", align: "right", width: 44, render: (r) => <span>{r.expedite ? "⚡" : ""}{r.priority}</span> },
    { key: "due_date", label: t("common.due"), width: 120, render: (r) => dt(r.due_date), value: (r) => r.due_date },
    { key: "planned_end", label: "Planned end", width: 120, render: (r) => dt(r.planned_end), value: (r) => r.planned_end },
    { key: "lateness_minutes", label: "Delay", align: "right", width: 80, render: (r) => (r.lateness_minutes > 0 ? <span className="text-red-600">{duration(r.lateness_minutes)}</span> : "—") },
    { key: "plan_status", label: "Plan status", width: 110, render: (r) => <StatusPill status={r.plan_status} /> },
    { key: "material_status", label: "Material", width: 100, render: (r) => <StatusPill status={r.material_status} /> },
    { key: "cause", label: "Delay cause", value: (r) => r.cause?.category, render: (r) => (r.cause ? <span title={r.cause.text}>{r.cause.category}</span> : "") },
    { key: "status", label: "ERP status", width: 90 },
  ];

  if (!plant) return <Loading />;
  return (
    <div className="flex flex-col h-full min-h-0">
      <PageHeader
        title={t("nav.orders")}
        subtitle={meta ? `${meta.total.toLocaleString()} production orders · plan ${meta.plan_number || "—"}` : undefined}
        actions={
          <>
            <SectionData tables={["production-orders", "production-orders.operations", "sales-orders", "sales-orders.lines", "demands", "customers"]} onChanged={() => setReloadKey((k) => k + 1)} />
            <Select ariaLabel="Scope" value={scope} onChange={setScope} options={[{ value: "open", label: "Open orders" }, { value: "all", label: "All orders" }]} />
            <Select
              ariaLabel="Plan status"
              value={planFilter}
              onChange={setPlanFilter}
              options={[{ value: "", label: "Any plan status" }, ...["ON_TIME", "LATE", "UNSCHEDULED", "SHORTAGE", "LATE_SUPPLY", "RISK"].map((s) => ({ value: s, label: t(`status.${s}`) }))]}
            />
            {can("orders:write") && (
              <Button variant="primary" icon="plus" onClick={() => setCreating(true)}>
                New order
              </Button>
            )}
          </>
        }
      />
      <div className="flex-1 min-h-0 bg-white">
        <RemoteTable
          path="/orders"
          query={query}
          columns={cols}
          rowKey={(r) => r.id}
          onRowClick={setOpen}
          selectedKey={open?.id}
          sortKeys={SORT_KEYS}
          initialSort={{ key: "due_date", dir: 1 }}
          onMeta={(d) => setMeta({ total: d.total, plan_id: d.plan_id ?? null, plan_number: d.plan_number ?? null })}
          reloadKey={reloadKey}
          searchPlaceholder="Order, item or description…"
        />
      </div>
      <Drawer open={!!open} onClose={() => setOpen(null)} title={open ? `${open.number} · ${open.item}` : ""} width={520}>
        {open && <OrderDetail order={open} planId={meta?.plan_id ?? exact.data?.plan_id ?? null} />}
      </Drawer>
      <NewOrder open={creating} onClose={() => setCreating(false)} onCreated={() => setReloadKey((k) => k + 1)} />
    </div>
  );
}

function OrderDetail({ order, planId }: { order: Row; planId: string | null }) {
  const detail = useApi<any>(planId ? `/plans/${planId}/order-detail/${order.id}` : null);
  const peg = useApi<any>(planId ? `/pegging/order/${order.id}` : null, planId ? { plan_id: planId } : undefined);
  return (
    <div className="p-3 space-y-3 text-[12.5px]">
      <div className="grid grid-cols-2 gap-2">
        <Info k="Item" v={`${order.item} · ${order.item_name}`} />
        <Info k="Quantity" v={`${order.quantity} (done ${order.completed_quantity})`} />
        <Info k="Customer" v={order.customer || "—"} />
        <Info k="Priority" v={`${order.priority}${order.expedite ? " · expedite" : ""}${order.strategic ? " · strategic customer" : ""}`} />
        <Info k="Release" v={dt(order.release_date)} />
        <Info k="Due" v={dt(order.due_date)} />
        <Info k="Planned" v={`${dt(order.planned_start)} → ${dt(order.planned_end)}`} />
        <Info k="Status" v={<StatusPill status={order.plan_status} />} />
      </div>
      {!planId && <div className="text-slate-600">No plan yet for this plant.</div>}
      {detail.loading && <Loading />}
      <ErrorState error={detail.error} />
      {detail.data && (
        <>
          {detail.data.root_cause?.length > 0 && (
            <section>
              <h3 className="mx-label">Why this result</h3>
              <ol className="border-l-2 border-gray-300 pl-2 space-y-1">
                {detail.data.root_cause.map((s: any, i: number) => (
                  <li key={i}>{s.text}</li>
                ))}
              </ol>
              {detail.data.deadline?.earliest_possible_infinite_capacity && (
                <div className="mt-1 text-slate-600">
                  Earliest possible with unlimited capacity: <b>{dt(detail.data.deadline.earliest_possible_infinite_capacity)}</b>
                  {detail.data.deadline.required_additional_capacity_h ? ` · extra capacity ≈ ${detail.data.deadline.required_additional_capacity_h} h` : ""}
                </div>
              )}
            </section>
          )}
          <section>
            <h3 className="mx-label">Operations</h3>
            <table className="mx-table">
              <tbody>
                {detail.data.operations.map((o: any) => (
                  <tr key={o.op_key}>
                    <td className="code">{o.op_key}</td>
                    <td className="tabular">{dt(o.setup_start)}</td>
                    <td className="tabular">{dt(o.end)}</td>
                    <td>{o.is_late ? <Badge tone="bad">late</Badge> : ""}</td>
                  </tr>
                ))}
                {detail.data.unscheduled.map((u: any) => (
                  <tr key={u.op_id}>
                    <td className="code">{u.op_id}</td>
                    <td colSpan={3} className="text-red-600 whitespace-normal">
                      ▲ {u.message}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        </>
      )}
      {peg.data && (
        <section>
          <h3 className="mx-label">Pegging (demand → supply)</h3>
          <PegTree node={peg.data} depth={0} />
        </section>
      )}
    </div>
  );
}

function PegTree({ node, depth }: { node: any; depth: number }) {
  const label =
    node.type === "SALES_ORDER" ? `Sales order ${node.number} · ${node.customer || ""} · line ${node.line}` : node.type === "PRODUCTION_ORDER" ? `${node.number} · ${node.item || ""} × ${node.quantity ?? ""}` : `${node.material} · ${node.quantity?.toFixed?.(2) ?? node.quantity} from ${node.supply_kind}${node.supply_ref ? ` ${node.supply_ref}` : ""}`;
  return (
    <div style={{ paddingLeft: depth ? 14 : 0 }} className={depth ? "border-l border-gray-200" : ""}>
      <div className={`py-0.5 ${node.late ? "text-amber-600" : ""}`}>
        {node.type === "MATERIAL" ? "◇" : node.type === "SALES_ORDER" ? "■" : "▸"} {label}
        {node.late ? " ◆ late supply" : ""}
        {node.supply_time ? <span className="text-slate-600"> · {dt(node.supply_time)}</span> : null}
      </div>
      {node.children?.map((c: any, i: number) => (
        <PegTree key={i} node={c} depth={depth + 1} />
      ))}
    </div>
  );
}

function Info({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div>
      <div className="text-[11px] text-slate-600">{k}</div>
      <div className="truncate">{v}</div>
    </div>
  );
}

function NewOrder({ open, onClose, onCreated }: { open: boolean; onClose: () => void; onCreated: () => void }) {
  const { plant } = useSession();
  const toast = useToast();
  const items = useApi<any>(open ? "/master-data/products" : null, { limit: 2000 });
  const custs = useApi<any>(open ? "/master-data/customers" : null, { limit: 2000 });
  const [f, setF] = useState<any>({ number: "", item_id: "", quantity: "10", due: "", priority: "5", customer_id: "", expedite: false });
  const [busy, setBusy] = useState(false);
  const save = async () => {
    setBusy(true);
    try {
      const o = await api("/orders", { body: { number: f.number, item_id: f.item_id, quantity: Number(f.quantity), due_date: localInputToIso(f.due), priority: Number(f.priority), customer_id: f.customer_id || null, expedite: f.expedite, plant_id: plant!.id, source: "MANUAL" } });
      toast.ok(`${o.number} created with ${o.operations?.length ?? 0} operations. Reschedule to include it.`);
      onCreated();
      onClose();
    } catch (e) {
      toast.error(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="New production order"
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" busy={busy} disabled={!f.number || !f.item_id || !f.due} onClick={save}>
            Create
          </Button>
        </>
      }
    >
      <div className="grid grid-cols-2 gap-3">
        <Field label="Number">
          <input className="mx-input w-full" value={f.number} onChange={(e) => setF({ ...f, number: e.target.value })} />
        </Field>
        <Field label="Product">
          <select className="mx-select w-full" value={f.item_id} onChange={(e) => setF({ ...f, item_id: e.target.value })}>
            <option value="">—</option>
            {(items.data?.items || []).map((i: any) => (
              <option key={i.id} value={i.id}>
                {i.code} · {i.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Quantity">
          <input className="mx-input w-full" type="number" min={0} value={f.quantity} onChange={(e) => setF({ ...f, quantity: e.target.value })} />
        </Field>
        <Field label="Due (plant time)">
          <input className="mx-input w-full" type="datetime-local" value={f.due} onChange={(e) => setF({ ...f, due: e.target.value })} />
        </Field>
        <Field label="Priority (1 = highest)">
          <input className="mx-input w-full" type="number" min={1} max={9} value={f.priority} onChange={(e) => setF({ ...f, priority: e.target.value })} />
        </Field>
        <Field label="Customer">
          <select className="mx-select w-full" value={f.customer_id} onChange={(e) => setF({ ...f, customer_id: e.target.value })}>
            <option value="">—</option>
            {(custs.data?.items || []).map((c: any) => (
              <option key={c.id} value={c.id}>
                {c.code} · {c.name}
              </option>
            ))}
          </select>
        </Field>
        <label className="flex gap-2 items-center">
          <input type="checkbox" checked={f.expedite} onChange={(e) => setF({ ...f, expedite: e.target.checked })} /> Expedite
        </label>
      </div>
      <p className="text-[11.5px] text-slate-600 mt-3">Operations are generated from the product&apos;s active routing. The order is planned at the next optimisation or reschedule. Today is {date(new Date())}.</p>
    </Dialog>
  );
}
