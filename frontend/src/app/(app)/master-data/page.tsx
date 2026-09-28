"use client";
import Link from "next/link";
import { Button, ErrorState, Loading, PageHeader, Panel, useToast } from "@/components/ui";
import { download } from "@/lib/api";
import { useApi } from "@/lib/hooks";
import { useSession } from "@/lib/session";

export default function MasterDataIndex() {
  const { t, can, plant } = useSession();
  const toast = useToast();
  const ents = useApi<any>("/master-data");
  const groups: Record<string, any[]> = {};
  for (const e of ents.data?.entities || []) (groups[e.group] ||= []).push(e);
  return (
    <div className="flex flex-col h-full min-h-0">
      <PageHeader
        title={t("nav.masterData")}
        subtitle={t("md.indexHelp")}
        actions={
          <>
            {can("integration:export") && (
              <Button icon="download" onClick={() => download("/exports/workbook", { plant_id: plant?.id }).catch(toast.error)}>
                {t("wb.download")}
              </Button>
            )}
            {can("integration:import") && (
              <Link className="mx-btn" href="/integrations?tab=workbook">
                {t("wb.importLink")}
              </Link>
            )}
            {can("integration:manage") && (
              <Link className="mx-btn" href="/integrations?tab=database">
                {t("int.database")}
              </Link>
            )}
          </>
        }
      />
      <div className="flex-1 overflow-auto mx-scroll p-3">
        <ErrorState error={ents.error} onRetry={ents.reload} />
        {!ents.data && <Loading />}
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-3">
          {Object.entries(groups).map(([g, list]) => (
            <Panel key={g} title={g}>
              <ul className="py-1">
                {list.map((e) => (
                  <li key={e.name}>
                    <Link className="block px-3 py-1.5 hover:bg-gray-50 text-[12.5px]" href={`/master-data/${e.name}`}>
                      {e.label}
                      <span className="text-slate-400 code ml-2">{e.name}</span>
                    </Link>
                  </li>
                ))}
              </ul>
            </Panel>
          ))}
        </div>
      </div>
    </div>
  );
}
