"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { Badge, Button, DataTable, Dialog, ErrorState, Field, Loading, NoPermission, PageHeader, Panel, Select, Tabs, useConfirm, useToast } from "@/components/ui";
import { api } from "@/lib/api";
import { dt } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import { LOCALES } from "@/lib/i18n";
import { useSession } from "@/lib/session";

export default function AdminPage() {
  const { t, can } = useSession();
  const [tab, setTab] = useState("users");
  if (!can("admin:users") && !can("admin:audit") && !can("admin:config")) return <NoPermission />;
  return (
    <div className="flex flex-col h-full min-h-0">
      <PageHeader title={t("nav.admin")} />
      <Tabs
        value={tab}
        onChange={setTab}
        tabs={[
          ...(can("admin:users") ? [{ id: "users", label: "Users & roles" }, { id: "keys", label: "API keys" }] : []),
          ...(can("admin:audit") ? [{ id: "audit", label: "Audit log" }] : []),
          ...(can("admin:config") ? [{ id: "settings", label: "Plant settings" }, { id: "config", label: "Rules & profiles" }] : []),
        ]}
      />
      <div className="flex-1 min-h-0 overflow-auto mx-scroll p-3">
        {tab === "users" && <Users />}
        {tab === "keys" && <Keys />}
        {tab === "audit" && <Audit />}
        {tab === "settings" && <Settings />}
        {tab === "config" && (
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3 max-w-4xl">
            {[
              ["planning-rules", "Planning rules", "Conditions → actions (priority weights, safety time, resource preferences). Validated before saving; every applied rule is shown in the operation explanation."],
              ["optimization-profiles", "Optimisation profiles", "Objective weights or lexicographic levels, constraint settings and solver budget per scenario."],
              ["setup-matrices", "Setup matrices", "Sequence-dependent changeover times by attribute (colour, family, grade…)."],
              ["sequence-rules", "Sequence rules", "Forbidden or preferred transitions."],
              ["calendars", "Calendars", "Shifts, breaks, holidays and overtime windows in plant local time."],
              ["resources", "Resources", "Machines, lines, tools, labour pools and subcontractors."],
            ].map(([e, l, d]) => (
              <Link key={e} href={`/master-data/${e}`} className="mx-panel p-3 hover:border-navy-600">
                <div className="font-semibold">{l}</div>
                <div className="text-[12px] text-slate-600 mt-1">{d}</div>
              </Link>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function Users() {
  const toast = useToast();
  const { me } = useSession();
  const users = useApi<any[]>("/users");
  const roles = useApi<any>("/roles");
  const [edit, setEdit] = useState<any>(null);
  const save = async () => {
    try {
      if (edit.id) {
        await api(`/users/${edit.id}`, { method: "PATCH", body: { full_name: edit.full_name, email: edit.email, is_active: edit.is_active, roles: edit.roles, plant_ids: edit.plant_ids, locale: edit.locale, unlock: edit.unlock, version: edit.version } });
        // a new password ends the user's open sessions and tokens
        if (edit.new_password) await api(`/users/${edit.id}/password`, { body: { new_password: edit.new_password } });
      } else await api("/users", { body: { username: edit.username, email: edit.email, full_name: edit.full_name, password: edit.password, roles: edit.roles, plant_ids: edit.plant_ids || [], locale: edit.locale || "en" } });
      toast.ok("User saved");
      setEdit(null);
      users.reload();
    } catch (e) {
      toast.error(e);
    }
  };
  if (users.error) return <ErrorState error={users.error} onRetry={users.reload} />;
  if (!users.data || !roles.data) return <Loading />;
  const roleCodes: string[] = roles.data.roles.map((r: any) => r.code);
  return (
    <div className="space-y-3">
      <div className="h-[calc(100vh-260px)] bg-white border border-gray-200">
        <DataTable
          rows={users.data}
          rowKey={(r) => r.id}
          onRowClick={(r) => setEdit({ ...r })}
          toolbar={
            <Button size="sm" variant="primary" onClick={() => setEdit({ roles: ["PLANNER"], plant_ids: [], is_active: true })}>
              New user
            </Button>
          }
          columns={[
            { key: "username", label: "Username", mono: true },
            { key: "full_name", label: "Name" },
            { key: "email", label: "Email" },
            { key: "roles", label: "Roles", value: (r) => r.roles.join(", ") },
            { key: "plant_ids", label: "Plants", value: (r) => (r.plant_ids.length ? `${r.plant_ids.length} plants` : "all") },
            { key: "is_active", label: "Active", render: (r) => (r.is_active ? <Badge tone="ok">active</Badge> : <Badge tone="neutral">inactive</Badge>) },
            { key: "locked", label: "", render: (r) => (r.locked ? <Badge tone="bad">locked</Badge> : "") },
            { key: "last_login_at", label: "Last login", render: (r) => dt(r.last_login_at) },
          ]}
        />
      </div>
      <Panel title="Roles and permissions">
        <div className="overflow-auto">
          <table className="mx-table">
            <thead>
              <tr>
                <th>Permission</th>
                {roleCodes.map((c) => (
                  <th key={c} className="!text-center">
                    {c.replace("_", " ").toLowerCase()}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {Object.entries(roles.data.permissions).map(([p, label]: any) => (
                <tr key={p}>
                  <td title={p}>{label}</td>
                  {roles.data.roles.map((r: any) => (
                    <td key={r.code} className="text-center">
                      {r.permissions.includes(p) ? "✓" : ""}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
      <Dialog open={!!edit} onClose={() => setEdit(null)} title={edit?.id ? `User ${edit.username}` : "New user"} width={560} footer={<><Button onClick={() => setEdit(null)}>Cancel</Button><Button variant="primary" onClick={save}>Save</Button></>}>
        {edit && (
          <div className="grid grid-cols-2 gap-3">
            <Field label="Username">
              <input className="mx-input w-full" disabled={!!edit.id} value={edit.username || ""} onChange={(e) => setEdit({ ...edit, username: e.target.value })} />
            </Field>
            <Field label="Full name">
              <input className="mx-input w-full" value={edit.full_name || ""} onChange={(e) => setEdit({ ...edit, full_name: e.target.value })} />
            </Field>
            <Field label="Email">
              <input className="mx-input w-full" value={edit.email || ""} onChange={(e) => setEdit({ ...edit, email: e.target.value })} />
            </Field>
            <Field label="Language">
              <Select value={edit.locale || "en"} onChange={(v) => setEdit({ ...edit, locale: v })} className="w-full" options={LOCALES.map((l) => ({ value: l.code, label: l.label }))} />
            </Field>
            {!edit.id ? (
              <Field label="Initial password" hint="≥ 10 characters, upper and lower case, a digit">
                <input className="mx-input w-full" type="password" autoComplete="new-password" value={edit.password || ""} onChange={(e) => setEdit({ ...edit, password: e.target.value })} />
              </Field>
            ) : edit.id !== me?.id ? (
              <Field label="Set a new password (optional)" hint="Signs the user out everywhere. ≥ 10 characters, upper and lower case, a digit">
                <input className="mx-input w-full" type="password" autoComplete="new-password" value={edit.new_password || ""} onChange={(e) => setEdit({ ...edit, new_password: e.target.value })} />
              </Field>
            ) : (
              <div className="text-[12px] text-slate-600 pt-5">Change your own password under My account (your name in the top bar).</div>
            )}
            <Field label="Roles">
              <div className="flex flex-col gap-1">
                {roleCodes.map((c) => (
                  <label key={c} className="flex gap-2 items-center">
                    <input type="checkbox" checked={(edit.roles || []).includes(c)} onChange={(e) => setEdit({ ...edit, roles: e.target.checked ? [...(edit.roles || []), c] : edit.roles.filter((x: string) => x !== c) })} /> {c}
                  </label>
                ))}
              </div>
            </Field>
            <Field label="Plants" hint="None selected = access to all plants">
              <div className="flex flex-col gap-1">
                {(me?.plants || []).map((p) => (
                  <label key={p.id} className="flex gap-2 items-center">
                    <input type="checkbox" checked={(edit.plant_ids || []).includes(p.id)} onChange={(e) => setEdit({ ...edit, plant_ids: e.target.checked ? [...(edit.plant_ids || []), p.id] : edit.plant_ids.filter((x: string) => x !== p.id) })} /> {p.code}
                  </label>
                ))}
              </div>
            </Field>
            {edit.id && (
              <div className="flex flex-col gap-1 pt-5">
                <label className="flex gap-2 items-center">
                  <input type="checkbox" checked={!!edit.is_active} onChange={(e) => setEdit({ ...edit, is_active: e.target.checked })} /> Active
                </label>
                {edit.locked && (
                  <label className="flex gap-2 items-center">
                    <input type="checkbox" checked={!!edit.unlock} onChange={(e) => setEdit({ ...edit, unlock: e.target.checked })} /> Unlock (failed logins)
                  </label>
                )}
              </div>
            )}
          </div>
        )}
      </Dialog>
    </div>
  );
}

function Keys() {
  const toast = useToast();
  const { confirm, node } = useConfirm();
  const keys = useApi<any[]>("/api-keys");
  const [open, setOpen] = useState(false);
  const [f, setF] = useState<any>({ name: "", role_code: "INTEGRATION_SERVICE", days: "" });
  const [created, setCreated] = useState<any>(null);
  const create = async () => {
    try {
      const k = await api("/api-keys", { body: { name: f.name, role_code: f.role_code, days: f.days ? Number(f.days) : null } });
      setCreated(k);
      setOpen(false);
      keys.reload();
    } catch (e) {
      toast.error(e);
    }
  };
  const revoke = async (k: any) => {
    const r = await confirm(`Revoke key "${k.name}"?`, { danger: true, body: "Systems using it will be rejected immediately." });
    if (!r.ok) return;
    try {
      await api(`/api-keys/${k.id}`, { method: "DELETE" });
      keys.reload();
    } catch (e) {
      toast.error(e);
    }
  };
  return (
    <div className="max-w-4xl space-y-3">
      <Panel title="API keys (Authorization: Bearer mxk_…) — only a hash is stored" actions={<Button size="sm" variant="primary" onClick={() => setOpen(true)}>New key</Button>}>
        <table className="mx-table">
          <tbody>
            {(keys.data || []).map((k) => (
              <tr key={k.id}>
                <td>{k.name}</td>
                <td className="code">mxk_{k.prefix}_…</td>
                <td>{k.role_code}</td>
                <td>{k.is_active ? <Badge tone="ok">active</Badge> : <Badge tone="neutral">revoked</Badge>}</td>
                <td>last used {dt(k.last_used_at)}</td>
                <td>{k.expires_at ? `expires ${dt(k.expires_at)}` : "no expiry"}</td>
                <td>
                  {k.is_active && (
                    <Button size="sm" variant="ghost" onClick={() => revoke(k)}>
                      Revoke
                    </Button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>
      <Dialog open={open} onClose={() => setOpen(false)} title="New API key" footer={<><Button onClick={() => setOpen(false)}>Cancel</Button><Button variant="primary" disabled={!f.name} onClick={create}>Create</Button></>}>
        <div className="grid grid-cols-3 gap-3">
          <Field label="Name">
            <input className="mx-input w-full" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} />
          </Field>
          <Field label="Role">
            <Select value={f.role_code} onChange={(v) => setF({ ...f, role_code: v })} className="w-full" options={["INTEGRATION_SERVICE", "VIEWER", "SUPERVISOR", "PLANNER"].map((r) => ({ value: r, label: r }))} />
          </Field>
          <Field label="Expires in (days)">
            <input className="mx-input w-full" type="number" min={1} value={f.days} onChange={(e) => setF({ ...f, days: e.target.value })} />
          </Field>
        </div>
      </Dialog>
      <Dialog open={!!created} onClose={() => setCreated(null)} title="API key created" footer={<Button onClick={() => setCreated(null)}>Done</Button>}>
        <p className="mb-2">{created?.note}</p>
        <pre className="code bg-gray-50 border border-gray-200 p-2 select-all break-all whitespace-pre-wrap">{created?.key}</pre>
      </Dialog>
      {node}
    </div>
  );
}

function Audit() {
  const [typed, setTyped] = useState("");
  const [q, setQ] = useState("");
  const [action, setAction] = useState("");
  useEffect(() => {
    const h = setTimeout(() => setQ(typed.trim()), 300);
    return () => clearTimeout(h);
  }, [typed]);
  const actions = useApi<string[]>("/audit/actions");
  const log = useApi<any>("/audit", { q: q || undefined, action: action || undefined, limit: 500 });
  const [open, setOpen] = useState<any>(null);
  return (
    <div className="space-y-2">
      <div className="flex gap-2">
        <input className="mx-input w-[240px]" placeholder="Search label / reason" value={typed} onChange={(e) => setTyped(e.target.value)} aria-label="Search audit" />
        <Select ariaLabel="Action" value={action} onChange={setAction} options={[{ value: "", label: "All actions" }, ...(actions.data || []).map((a) => ({ value: a, label: a }))]} />
        {log.data && <span className="self-center text-[11.5px] text-slate-600 tabular">{log.data.total > log.data.items.length ? `${log.data.items.length} most recent of ${log.data.total}` : `${log.data.total} entries`}</span>}
      </div>
      {log.error && <ErrorState error={log.error} />}
      <div className="h-[calc(100vh-240px)] bg-white border border-gray-200">
        {!log.data ? (
          <Loading />
        ) : (
          <DataTable
            rows={log.data.items}
            rowKey={(r) => r.id}
            onRowClick={setOpen}
            exportName="audit-log"
            columns={[
              { key: "at", label: "When", render: (r) => dt(r.at, { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" }) },
              { key: "user", label: "User" },
              { key: "action", label: "Action", mono: true },
              { key: "entity_type", label: "Entity" },
              { key: "entity_label", label: "Record" },
              { key: "reason", label: "Why" },
              { key: "ip", label: "IP", mono: true },
            ]}
          />
        )}
      </div>
      <Dialog open={!!open} onClose={() => setOpen(null)} title={`${open?.action} ${open?.entity_type} ${open?.entity_label || ""}`} width={720}>
        {open && (
          <div className="grid grid-cols-2 gap-3">
            <div>
              <div className="mx-label">Before</div>
              <pre className="code text-[11px] bg-gray-50 border border-gray-200 p-2 overflow-auto max-h-[400px]">{JSON.stringify(open.before, null, 1)}</pre>
            </div>
            <div>
              <div className="mx-label">After</div>
              <pre className="code text-[11px] bg-gray-50 border border-gray-200 p-2 overflow-auto max-h-[400px]">{JSON.stringify(open.after, null, 1)}</pre>
            </div>
          </div>
        )}
      </Dialog>
    </div>
  );
}

function Settings() {
  const { plant } = useSession();
  const toast = useToast();
  const st = useApi<any>(plant ? `/plants/${plant.id}/settings` : null);
  const types = useApi<any>("/events/types");
  const [draft, setDraft] = useState<any>(null);
  const cur = draft || st.data?.settings || {};
  const ar = cur.auto_reschedule || { events: [], scope: "LOCAL" };
  const save = async () => {
    try {
      await api(`/plants/${plant!.id}/settings`, { method: "PUT", body: { auto_reschedule: ar, publish_requires_validation: !!cur.publish_requires_validation } });
      toast.ok("Settings saved");
      setDraft(null);
      st.reload();
    } catch (e) {
      toast.error(e);
    }
  };
  if (!st.data) return st.error ? <ErrorState error={st.error} /> : <Loading />;
  return (
    <div className="max-w-3xl space-y-3">
      <Panel title={`Publication — ${plant?.code}`}>
        <div className="p-3 space-y-2 text-[12.5px]">
          <label className="flex gap-2 items-start">
            <input type="checkbox" className="mt-0.5" checked={!!cur.publish_requires_validation} onChange={(e) => setDraft({ ...cur, publish_requires_validation: e.target.checked })} />
            <span>
              Require validation before publishing
              <span className="block text-slate-600">A plan can only be published after “Validate” has checked it against the current data (otherwise the publication gate lists it as a blocker that needs an explicit override).</span>
            </span>
          </label>
        </div>
      </Panel>
      <Panel title={`Automatic rescheduling — ${plant?.code}`}>
        <div className="p-3 space-y-3 text-[12.5px]">
          <p className="text-slate-600">When one of these shop-floor events arrives, MonxuPlan repairs the live plan automatically with the chosen scope (the result is a new plan version, never published automatically). Otherwise the planner gets an alert.</p>
          <div className="grid grid-cols-2 gap-1">
            {(types.data?.inbound || []).map((ev: string) => (
              <label key={ev} className="flex gap-2 items-center code">
                <input type="checkbox" checked={ar.events.includes(ev)} onChange={(e) => setDraft({ ...cur, auto_reschedule: { ...ar, events: e.target.checked ? [...ar.events, ev] : ar.events.filter((x: string) => x !== ev) } })} /> {ev}
              </label>
            ))}
          </div>
          <Field label="Scope">
            <Select value={ar.scope} onChange={(v) => setDraft({ ...cur, auto_reschedule: { ...ar, scope: v } })} options={["LOCAL", "REGIONAL", "GLOBAL"].map((x) => ({ value: x, label: x.toLowerCase() }))} />
          </Field>
        </div>
      </Panel>
      <div className="flex gap-2">
        <Button variant="primary" onClick={save} disabled={!draft}>
          Save
        </Button>
        {draft && (
          <Button variant="ghost" onClick={() => setDraft(null)}>
            Discard changes
          </Button>
        )}
      </div>
    </div>
  );
}
