"use client";
import Link from "next/link";
import { useMemo, useState } from "react";
import { Button, Dialog, useToast } from "@/components/ui";
import { download } from "@/lib/api";
import { useApi } from "@/lib/hooks";
import { useSession } from "@/lib/session";
import { ImportWizard } from "./ImportWizard";

/**
 * "Data" button for a screen: the tables behind it, each one editable (opens its editor),
 * exportable to Excel with all columns, and importable from Excel/CSV (round trip).
 * `tables` are master-data entity names, optionally with a child table: "calendars.shifts".
 */
export function SectionData({ tables, onChanged, label }: { tables: string[]; onChanged?: () => void; label?: string }) {
  const { t, plant, can } = useSession();
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [importing, setImporting] = useState<string | null>(null);
  const meta = useApi<any[]>(open && can("integration:import") ? "/imports/tables" : null);
  const byTable = useMemo(() => Object.fromEntries((meta.data || []).map((m) => [m.table, m])), [meta.data]);
  if (!can("masterdata:read") && !can("orders:read")) return null;
  const rows = tables.map((tb) => ({ table: tb, meta: byTable[tb], parent: tb.split(".")[0] }));
  const canImport = can("integration:import");
  const canExport = can("integration:export");
  const close = () => {
    setOpen(false);
    setImporting(null);
  };
  return (
    <>
      <Button icon="database" onClick={() => setOpen(true)} title={t("data.title")}>
        {label || t("data.button")}
      </Button>
      <Dialog open={open} onClose={close} title={importing ? `${t("data.import")}: ${byTable[importing]?.label || importing}` : t("data.title")} width={importing ? 900 : 640}>
        {importing ? (
          <div className="space-y-2">
            <button className="text-blue-500 hover:underline text-[12.5px]" onClick={() => setImporting(null)}>
              ← {t("data.back")}
            </button>
            <ImportWizard
              compact
              choices={[{ entity: `table:${importing}`, label: byTable[importing]?.label || importing }]}
              initialEntity={`table:${importing}`}
              onImported={() => onChanged?.()}
            />
          </div>
        ) : (
          <div className="space-y-3">
            <p className="text-[12.5px] text-slate-600">{t("data.help")}</p>
            <table className="mx-table">
              <tbody>
                {rows.map((r) => (
                  <tr key={r.table}>
                    <td>
                      <div className="font-medium">{r.meta?.label || r.table}</div>
                      <div className="code text-[11px] text-slate-500">{r.table}</div>
                    </td>
                    <td className="text-right whitespace-nowrap">
                      <Link className="mx-btn mx-btn-ghost mx-btn-sm" href={`/master-data/${r.parent}`} onClick={close}>
                        {t("data.edit")}
                      </Link>
                      {canExport && (
                        <Button size="sm" variant="ghost" icon="download" onClick={() => download(`/exports/table:${r.table}`, { format: "xlsx", plant_id: plant?.id }).catch(toast.error)}>
                          Excel
                        </Button>
                      )}
                      {canImport && r.meta?.writable !== false && (
                        <Button size="sm" variant="ghost" icon="upload" onClick={() => setImporting(r.table)}>
                          {t("data.import")}
                        </Button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="text-[12px] text-slate-600">
              {t("data.more")}{" "}
              <Link className="text-blue-500 hover:underline" href="/integrations" onClick={close}>
                {t("nav.integrations")}
              </Link>
            </p>
          </div>
        )}
      </Dialog>
    </>
  );
}
