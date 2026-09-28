"use client";
import { Fragment, useState } from "react";
import { Badge, Button, Field, Panel, StatusPill, useToast } from "@/components/ui";
import { api, download } from "@/lib/api";
import { useSession } from "@/lib/session";
import { invalidateRefOptions } from "./FieldInput";
import { ErrorTable } from "./ImportWizard";

/** The whole data set as one Excel workbook (one sheet per table): download, edit, import back. */
export function WorkbookImport() {
  const { t, plant, can } = useSession();
  const toast = useToast();
  const [file, setFile] = useState<File | null>(null);
  const [batch, setBatch] = useState<any>(null);
  const [check, setCheck] = useState<any>(null);
  const [skip, setSkip] = useState(false);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<any>(null);
  const [open, setOpen] = useState<string | null>(null);

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
      fd.append("file", file!);
      if (plant) fd.append("plant_id", plant.id);
      const b = await api("/imports/workbook", { form: fd });
      setBatch(b);
      setDone(null);
      const v = await api("/imports/batch/validate", { body: { job_ids: b.jobs.map((j: any) => j.id), options: { skip_invalid_rows: skip } } });
      setCheck(v);
    });
  const revalidate = () =>
    run(async () => {
      setCheck(await api("/imports/batch/validate", { body: { job_ids: batch.jobs.map((j: any) => j.id), options: { skip_invalid_rows: skip } } }));
    });
  const commit = () =>
    run(async () => {
      const c = await api("/imports/batch/commit", { body: { job_ids: batch.jobs.map((j: any) => j.id), options: { skip_invalid_rows: skip } } });
      setDone(c);
      invalidateRefOptions();
      toast.ok(t("wb.done"));
    });
  const jobs = done?.jobs || check?.jobs || [];
  return (
    <div className="max-w-5xl space-y-3">
      <Panel title={t("wb.title")}>
        <div className="p-3 space-y-3 text-[12.5px]">
          <p className="text-slate-600">{t("wb.help")}</p>
          <div className="flex flex-wrap gap-2 items-end">
            {can("integration:export") && (
              <Button icon="download" onClick={() => download("/exports/workbook", { plant_id: plant?.id }).catch(toast.error)}>
                {t("wb.download")}
              </Button>
            )}
            <Field label={t("wb.file")}>
              <input type="file" accept=".xlsx,.xlsm" className="mx-input pt-1" onChange={(e) => setFile(e.target.files?.[0] || null)} aria-label={t("wb.file")} />
            </Field>
            <label className="flex gap-2 items-center pb-2">
              <input type="checkbox" checked={skip} onChange={(e) => setSkip(e.target.checked)} /> {t("imp.onlyValid")}
            </label>
            <Button variant="primary" icon="upload" busy={busy} disabled={!file} onClick={upload}>
              {t("wb.check")}
            </Button>
          </div>
          {batch?.empty_sheets?.length > 0 && (
            <p className="text-slate-600">
              {t("wb.empty")}: <span className="code">{batch.empty_sheets.join(", ")}</span>
            </p>
          )}
          {batch?.ignored_sheets?.length > 0 && (
            <p className="text-slate-600">
              {t("wb.ignored")}: <span className="code">{batch.ignored_sheets.join(", ")}</span>
            </p>
          )}
        </div>
      </Panel>
      {jobs.length > 0 && (
        <Panel
          title={done ? t("wb.imported") : t("wb.result")}
          actions={
            !done && (
              <div className="flex gap-2">
                <Button size="sm" onClick={revalidate} busy={busy}>
                  {t("imp.validate")}
                </Button>
                <Button size="sm" variant="primary" disabled={!check?.can_import} busy={busy} onClick={commit}>
                  {t("wb.importAll")}
                </Button>
              </div>
            )
          }
        >
          <table className="mx-table">
            <thead>
              <tr>
                <th>{t("wb.sheet")}</th>
                <th>{t("imp.rows")}</th>
                <th>{done ? t("imp.created") : t("imp.toCreate")}</th>
                <th>{done ? t("imp.updated") : t("imp.toUpdate")}</th>
                <th>{done ? t("imp.deleted") : t("imp.toDelete")}</th>
                <th>{t("imp.unchanged")}</th>
                <th>{t("imp.withErrors")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {jobs.map((j: any) => (
                <Fragment key={j.id}>
                  <tr>
                    <td>
                      <div className="font-medium">{j.entity_label}</div>
                      <div className="code text-[11px] text-slate-500">{j.options?.sheet}</div>
                    </td>
                    <td className="num">{j.stats.rows}</td>
                    <td className="num">{done ? j.stats.created : j.stats.to_create}</td>
                    <td className="num">{done ? j.stats.updated : j.stats.to_update}</td>
                    <td className="num">{done ? (j.stats.deleted || 0) + (j.stats.deactivated || 0) : j.stats.to_delete}</td>
                    <td className="num text-slate-500">{j.stats.unchanged ?? 0}</td>
                    <td className="num">{j.stats.error_rows ? <Badge tone="bad">{j.stats.error_rows}</Badge> : "0"}</td>
                    <td>
                      <StatusPill status={j.status} />{" "}
                      {j.errors?.length > 0 && (
                        <Button size="sm" variant="ghost" onClick={() => setOpen(open === j.id ? null : j.id)}>
                          {t("wb.errors")}
                        </Button>
                      )}
                    </td>
                  </tr>
                  {open === j.id && (
                    <tr>
                      <td colSpan={8}>
                        <ErrorTable errors={j.errors} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
          {!done && check && !check.can_import && <p className="p-3 text-red-600 text-[12.5px]">▲ {t("wb.blocked")}</p>}
        </Panel>
      )}
    </div>
  );
}
