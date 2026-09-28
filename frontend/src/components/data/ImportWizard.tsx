"use client";
import { useEffect, useMemo, useState } from "react";
import { Badge, Button, Field, Panel, Select, StatusPill, useToast } from "@/components/ui";
import { api, download } from "@/lib/api";
import { useApi } from "@/lib/hooks";
import { useSession } from "@/lib/session";
import { invalidateRefOptions } from "./FieldInput";

export type ImportChoice = { entity: string; label: string };

/** Upload → mapping → validation (a real dry run) → preview → import → report, for any template or table. */
export function ImportWizard({ choices, initialEntity, onImported, compact }: { choices?: ImportChoice[]; initialEntity?: string; onImported?: () => void; compact?: boolean }) {
  const { plant, t } = useSession();
  const toast = useToast();
  const templates = useApi<any[]>(choices ? null : "/imports/templates");
  const tables = useApi<any[]>(choices ? null : "/imports/tables");
  const options = useMemo(() => {
    if (choices) return choices.map((c) => ({ value: c.entity, label: c.label }));
    const a = (templates.data || []).map((x) => ({ value: x.entity, label: `${t("imp.guided")}: ${x.label}` }));
    const b = (tables.data || []).filter((x) => x.writable).map((x) => ({ value: x.entity, label: `${t("imp.table")}: ${x.label} (${x.table})` }));
    return [...a, ...b];
  }, [choices, templates.data, tables.data, t]);
  const [step, setStep] = useState(0);
  const [entity, setEntity] = useState(initialEntity || "items");
  useEffect(() => {
    if (options.length && !options.some((o) => o.value === entity)) setEntity(options[0].value);
  }, [options, entity]);
  const [file, setFile] = useState<File | null>(null);
  const [job, setJob] = useState<any>(null);
  const [mapping, setMapping] = useState<Record<string, string | null>>({});
  const [opts, setOpts] = useState<any>({ mode: "UPSERT", date_format: "DMY", skip_invalid_rows: false });
  const [busy, setBusy] = useState(false);
  const isTable = entity.startsWith("table:");
  const steps = [t("imp.s.upload"), t("imp.s.mapping"), t("imp.s.validation"), t("imp.s.preview"), t("imp.s.import"), t("imp.s.report")];

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    try {
      await fn();
    } catch (e) {
      toast.error(e);
    } finally {
      setBusy(false);
    }
  };
  const upload = () =>
    run(async () => {
      const fd = new FormData();
      fd.append("entity", entity);
      fd.append("file", file!);
      if (plant) fd.append("plant_id", plant.id);
      const j = await api("/imports", { form: fd });
      setJob(j);
      setMapping(j.mapping);
      setStep(1);
    });
  const validate = () =>
    run(async () => {
      await api(`/imports/${job.id}/mapping`, { method: "PUT", body: { mapping, options: opts } });
      const v = await api(`/imports/${job.id}/validate`, { method: "POST" });
      setJob(v);
      setStep(2);
    });
  const commit = () =>
    run(async () => {
      const c = await api(`/imports/${job.id}/commit`, { method: "POST" });
      setJob(c);
      setStep(5);
      invalidateRefOptions();
      toast.ok(t("imp.done", { c: c.stats.created ?? 0, u: c.stats.updated ?? 0 }));
      onImported?.();
    });
  const reset = () => {
    setStep(0);
    setJob(null);
    setFile(null);
  };
  const toWrite = job ? (job.stats.to_create || 0) + (job.stats.to_update || 0) + (job.stats.to_delete || 0) : 0;

  return (
    <div className={compact ? "space-y-3" : "max-w-5xl space-y-3"}>
      <ol className="flex gap-1" aria-label={t("imp.steps")}>
        {steps.map((s, i) => (
          <li key={s} aria-current={i === step ? "step" : undefined} className={`flex-1 h-8 flex items-center justify-center text-[12px] rounded-[3px] border ${i === step ? "bg-navy-700 text-white border-navy-700" : i < step ? "bg-green-100 border-green-600/30 text-green-600" : "bg-white border-gray-200 text-slate-600"}`}>
            {i < step ? "✓ " : `${i + 1}. `}
            <span className="hidden sm:inline">{s}</span>
          </li>
        ))}
      </ol>
      {step === 0 && (
        <Panel title={`1. ${t("imp.s.upload")}`}>
          <div className="p-3 grid grid-cols-1 sm:grid-cols-2 gap-3">
            <Field label={t("imp.what")}>
              <Select value={entity} onChange={setEntity} className="w-full" options={options} ariaLabel={t("imp.what")} />
            </Field>
            <Field label={t("imp.file")} hint={t("imp.fileHint")}>
              <input type="file" accept=".xlsx,.xlsm,.csv,.txt,.tsv,.json" className="mx-input w-full pt-1" onChange={(e) => setFile(e.target.files?.[0] || null)} aria-label={t("imp.file")} />
            </Field>
            <p className="sm:col-span-2 text-slate-600 text-[12.5px]">{isTable ? t("imp.tableHelp") : t("imp.guidedHelp")}</p>
            <div className="sm:col-span-2 flex flex-wrap gap-2">
              <Button variant="primary" icon="upload" busy={busy} disabled={!file} onClick={upload}>
                {t("imp.upload")}
              </Button>
              {isTable ? (
                <Button icon="download" onClick={() => download(`/exports/${entity}`, { format: "xlsx", plant_id: plant?.id }).catch(toast.error)}>
                  {t("imp.currentData")}
                </Button>
              ) : null}
              <Button icon="download" onClick={() => download(`/imports/templates/${entity}`, { format: "xlsx" }).catch(toast.error)}>
                {t("imp.templateXlsx")}
              </Button>
              <Button icon="download" onClick={() => download(`/imports/templates/${entity}`, { format: "csv" }).catch(toast.error)}>
                {t("imp.templateCsv")}
              </Button>
            </div>
          </div>
        </Panel>
      )}
      {step === 1 && job && (
        <Panel title={`2. ${t("imp.s.mapping")} — ${job.filename} (${job.rows} ${t("imp.rows")}, ${job.columns.length} ${t("imp.columns")})`}>
          <div className="p-3 space-y-3">
            <p className="text-[12.5px] text-slate-600">{t("imp.mappingHelp")}</p>
            <div className="overflow-x-auto max-h-[46vh] overflow-y-auto mx-scroll border border-gray-200">
              <table className="mx-table">
                <thead>
                  <tr>
                    <th>{t("imp.field")}</th>
                    <th>{t("imp.type")}</th>
                    <th>{t("imp.column")}</th>
                    <th>{t("imp.sample")}</th>
                  </tr>
                </thead>
                <tbody>
                  {job.fields.map((f: any) => (
                    <tr key={f.name}>
                      <td>
                        <span className="code">{f.name}</span> {f.required && <Badge tone="bad">{t("imp.required")}</Badge>}
                        <div className="text-[11px] text-slate-600 whitespace-normal">{f.description}</div>
                      </td>
                      <td className="text-slate-600">{f.enum?.length ? f.enum.join(" / ") : f.ref ? `${t("imp.codeOf")} ${f.ref}` : f.type}</td>
                      <td>
                        <select className="mx-select" aria-label={`${t("imp.column")} ${f.name}`} value={mapping[f.name] || ""} onChange={(e) => setMapping({ ...mapping, [f.name]: e.target.value || null })} aria-invalid={f.required && !mapping[f.name]}>
                          <option value="">{t("imp.notMapped")}</option>
                          {job.columns.map((c: string) => (
                            <option key={c} value={c}>
                              {c}
                            </option>
                          ))}
                        </select>
                      </td>
                      <td className="text-slate-600 truncate max-w-[180px]">{mapping[f.name] ? String(job.sample?.[0]?.[mapping[f.name]!] ?? "") : ""}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
              <Field label={t("imp.mode")}>
                <Select value={opts.mode} onChange={(v) => setOpts({ ...opts, mode: v })} className="w-full" options={[{ value: "UPSERT", label: t("imp.mode.upsert") }, { value: "CREATE_ONLY", label: t("imp.mode.create") }, { value: "UPDATE_ONLY", label: t("imp.mode.update") }]} />
              </Field>
              <Field label={t("imp.dateFormat")}>
                <Select value={opts.date_format} onChange={(v) => setOpts({ ...opts, date_format: v })} className="w-full" options={[{ value: "DMY", label: "DD/MM/YYYY" }, { value: "MDY", label: "MM/DD/YYYY" }]} />
              </Field>
              <label className="flex gap-2 items-center sm:pt-5 text-[12.5px]">
                <input type="checkbox" checked={opts.skip_invalid_rows} onChange={(e) => setOpts({ ...opts, skip_invalid_rows: e.target.checked })} /> {t("imp.onlyValid")}
              </label>
            </div>
            <div className="flex gap-2">
              <Button onClick={reset}>{t("common.back")}</Button>
              <Button variant="primary" busy={busy} onClick={validate} disabled={job.fields.some((f: any) => f.required && !mapping[f.name])}>
                {t("imp.validate")}
              </Button>
            </div>
          </div>
        </Panel>
      )}
      {(step === 2 || step === 3) && job && (
        <Panel title={step === 2 ? `3. ${t("imp.s.validation")}` : `4. ${t("imp.s.preview")}`}>
          <div className="p-3 space-y-3">
            <div className="flex flex-wrap gap-2 text-[12.5px]">
              <Badge tone="neutral" glyph={false}>
                {job.stats.rows} {t("imp.rows")}
              </Badge>
              <Badge tone="ok">
                {job.stats.valid_rows} {t("imp.valid")}
              </Badge>
              <Badge tone={job.stats.error_rows ? "bad" : "ok"}>
                {job.stats.error_rows} {t("imp.withErrors")}
              </Badge>
              <Badge tone="info">
                {job.stats.to_create} {t("imp.toCreate")}
              </Badge>
              <Badge tone="info">
                {job.stats.to_update} {t("imp.toUpdate")}
              </Badge>
              {job.stats.unchanged > 0 && (
                <Badge tone="neutral" glyph={false}>
                  {job.stats.unchanged} {t("imp.unchanged")}
                </Badge>
              )}
              {job.stats.to_delete > 0 && (
                <Badge tone="warn">
                  {job.stats.to_delete} {t("imp.toDelete")}
                </Badge>
              )}
              {job.stats.to_skip > 0 && (
                <Badge tone="warn">
                  {job.stats.to_skip} {t("imp.skipped")}
                </Badge>
              )}
              <StatusPill status={job.status} />
            </div>
            {isTable && <p className="text-[12px] text-slate-600">{t("imp.dryRun")}</p>}
            {job.stats.blocking_reason && <div className="text-red-600">▲ {job.stats.blocking_reason}</div>}
            {job.warnings?.map((w: any, i: number) => (
              <div key={i} className="text-amber-600 text-[12.5px]">
                ◆ {w.message}
              </div>
            ))}
            {step === 2 && job.errors?.length > 0 && <ErrorTable errors={job.errors} />}
            {step === 3 && (
              <div className="max-h-[380px] overflow-auto mx-scroll border border-gray-200">
                <table className="mx-table">
                  <thead>
                    <tr>
                      <th>{t("imp.row")}</th>
                      <th>{t("imp.action")}</th>
                      {Object.keys(job.preview?.[0]?.values || {})
                        .slice(0, 8)
                        .map((k) => (
                          <th key={k}>{k}</th>
                        ))}
                    </tr>
                  </thead>
                  <tbody>
                    {(job.preview || []).map((p: any) => (
                      <tr key={p.row}>
                        <td className="num">{p.row}</td>
                        <td>
                          <Badge tone={p.action === "CREATE" || p.action === "CREATED" ? "ok" : p.action === "SKIP" || p.action === "SKIPPED" || p.action === "DELETED" || p.action === "DEACTIVATED" ? "warn" : p.action === "ERROR" ? "bad" : "info"}>{p.action}</Badge>
                        </td>
                        {Object.keys(job.preview[0].values)
                          .slice(0, 8)
                          .map((k) => (
                            <td key={k} className="tabular">
                              {typeof p.values[k] === "object" && p.values[k] !== null ? JSON.stringify(p.values[k]) : String(p.values[k] ?? "")}
                            </td>
                          ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <div className="flex gap-2">
              <Button onClick={() => setStep(1)}>{t("imp.backMapping")}</Button>
              {step === 2 && (
                <Button variant="primary" disabled={!job.stats.can_import} onClick={() => setStep(3)}>
                  {t("imp.s.preview")}
                </Button>
              )}
              {step === 3 && (
                <Button variant="primary" busy={busy} disabled={!job.stats.can_import} onClick={commit}>
                  {t("imp.importN", { n: toWrite })}
                </Button>
              )}
            </div>
          </div>
        </Panel>
      )}
      {step === 5 && job && (
        <Panel title={`6. ${t("imp.s.report")}`}>
          <div className="p-3 space-y-2 text-[12.5px]">
            <div className="text-green-600 font-semibold">✓ {t("imp.committed", { what: job.entity_label })}</div>
            <div>
              {t("imp.created")} <b>{job.stats.created ?? 0}</b> · {t("imp.updated")} <b>{job.stats.updated ?? 0}</b>
              {job.stats.unchanged ? (
                <>
                  {" "}
                  · {t("imp.unchanged")} <b>{job.stats.unchanged}</b>
                </>
              ) : null}
              {job.stats.deleted ? (
                <>
                  {" "}
                  · {t("imp.deleted")} <b>{job.stats.deleted}</b>
                </>
              ) : null}
              {job.stats.deactivated ? (
                <>
                  {" "}
                  · {t("imp.deactivated")} <b>{job.stats.deactivated}</b>
                </>
              ) : null}{" "}
              · {t("imp.skipped")} <b>{job.stats.skipped ?? 0}</b>
              {job.stats.operations_generated ? (
                <>
                  {" "}
                  · {t("imp.opsGenerated")} <b>{job.stats.operations_generated}</b>
                </>
              ) : null}
            </div>
            {job.stats.related_created && Object.keys(job.stats.related_created).length > 0 && (
              <div>
                {t("imp.alsoCreated")}: {Object.entries(job.stats.related_created)
                  .map(([k, v]) => `${v} ${k}`)
                  .join(", ")}
              </div>
            )}
            {job.stats.error_rows > 0 && <div className="text-amber-600">◆ {t("imp.invalidNotImported", { n: job.stats.error_rows })}</div>}
            <p className="text-slate-600">{t("imp.nextRun")}</p>
            <Button onClick={reset}>{t("imp.new")}</Button>
          </div>
        </Panel>
      )}
    </div>
  );
}

export function ErrorTable({ errors }: { errors: any[] }) {
  const { t } = useSession();
  return (
    <div className="max-h-[320px] overflow-auto mx-scroll border border-gray-200">
      <table className="mx-table">
        <thead>
          <tr>
            <th>{t("imp.row")}</th>
            <th>{t("imp.field")}</th>
            <th>{t("imp.value")}</th>
            <th>{t("imp.problem")}</th>
          </tr>
        </thead>
        <tbody>
          {errors.map((e: any, i: number) => (
            <tr key={i}>
              <td className="num">{e.row ?? "—"}</td>
              <td className="code">{e.field}</td>
              <td className="truncate max-w-[160px]">{String(e.value ?? "")}</td>
              <td className="text-red-600 whitespace-normal">▲ {e.message}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
