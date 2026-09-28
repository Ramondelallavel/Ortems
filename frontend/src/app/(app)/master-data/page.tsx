"use client";
import Link from "next/link";
import { ErrorState, Loading, PageHeader, Panel } from "@/components/ui";
import { useApi } from "@/lib/hooks";
import { useSession } from "@/lib/session";

export default function MasterDataIndex() {
  const { t } = useSession();
  const ents = useApi<any>("/master-data");
  const groups: Record<string, any[]> = {};
  for (const e of ents.data?.entities || []) (groups[e.group] ||= []).push(e);
  return (
    <div className="flex flex-col h-full min-h-0">
      <PageHeader title={t("nav.masterData")} subtitle="Every table is editable here or importable from Excel/CSV (Integrations → Import)." />
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
