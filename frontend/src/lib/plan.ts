"use client";
import { useEffect, useMemo, useState } from "react";
import { useApi, useLocalState } from "./hooks";
import { useSession } from "./session";
import { loc } from "./loc";

export type ScenarioRow = {
  id: string;
  name: string;
  is_live: boolean;
  kind: string;
  status: string;
  description: string | null;
  head_plan_id: string | null;
  head_plan: { id: string; number: string; status: string; kpis: Record<string, number>; feasible: boolean; created_at: string } | null;
  last_run: { id: string; status: string; kind: string; created_at: string; finished_at: string | null; error: string | null } | null;
  changes?: { id: string; seq: number; type: string; description: string; payload: any; is_active: boolean }[];
  parent: { id: string; name: string } | null;
  locked_by: string | null;
  config: Record<string, any>;
  plans: number;
  owner: string | null;
  redo_stack?: string[];
};

/** Scenario list of the current plant + the selected scenario (default: live) and its head plan. */
export function useScenarioSelection() {
  const { plant } = useSession();
  const scenarios = useApi<ScenarioRow[]>(plant ? "/scenarios" : null, plant ? { plant_id: plant.id } : undefined);
  const [stored, setStored] = useLocalState<Record<string, string>>("mx.scenario", {});
  const [explicit, setExplicit] = useState<string | null>(null);
  useEffect(() => {
    const q = new URLSearchParams(loc.search()).get("scenario");
    if (q) setExplicit(q);
  }, []);
  const list = scenarios.data || [];
  const selected = useMemo(() => {
    if (!plant) return null;
    const want = explicit || stored[plant.id];
    return list.find((s) => s.id === want) || list.find((s) => s.is_live) || list[0] || null;
  }, [list, plant, stored, explicit]);
  const select = (id: string) => {
    if (!plant) return;
    setExplicit(null);
    setStored({ ...stored, [plant.id]: id });
  };
  return { scenarios, list, selected, select };
}
