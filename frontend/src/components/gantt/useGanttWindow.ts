"use client";
// Viewport loading for large plans (a 100 000-order day holds ~200 000 operations): the resource rows
// are loaded once, the operations of the visible rows and time span on demand. Views denser than
// OPS_BUDGET operations are drawn as busy blocks per machine (a click on a block zooms into it).
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";
import type { GanttData, GanttViewport, GBlock, GOp } from "./Gantt";

export const LARGE_PLAN_OPS = 30_000; // same threshold as the server's windowed Gantt
const OPS_BUDGET = 12_000; // operations per viewport request

type Loaded = { a: number; b: number; ids: Set<string>; mode: "ops" | "blocks" };

export function useGanttWindow(planId: string | null, plan: { operations: number; horizon_start: string; horizon_end: string } | null, enabled: boolean) {
  const [base, setBase] = useState<GanttData | null>(null);
  const [error, setError] = useState<ApiError>();
  const [ops, setOps] = useState<GOp[]>([]);
  const [blocks, setBlocks] = useState<Record<string, GBlock[]> | null>(null);
  const [loading, setLoading] = useState(false);
  const [tick, setTick] = useState(0);
  const loaded = useRef<Loaded | null>(null);
  const seq = useRef(0);
  const last = useRef<GanttViewport | null>(null);

  const span = useMemo(() => {
    if (!plan) return null;
    const a = Date.parse(plan.horizon_start) - 86400e3;
    const b = Date.parse(plan.horizon_end) + 2 * 86400e3;
    return { a, b };
  }, [plan]);

  // rows (calendars, unavailability, plan utilisation) once per plan version
  useEffect(() => {
    setBase(null);
    setOps([]);
    setBlocks(null);
    setError(undefined);
    loaded.current = null;
    if (!enabled || !planId || !span) return;
    const ctl = new AbortController();
    api<GanttData>(`/plans/${planId}/gantt`, { query: { operations: false, start: new Date(span.a).toISOString(), end: new Date(span.b).toISOString() }, signal: ctl.signal })
      .then((d) => setBase(d))
      .catch((e) => e?.name !== "AbortError" && setError(e instanceof ApiError ? e : new ApiError(0, "ERROR", String(e))));
    return () => ctl.abort();
  }, [enabled, planId, span, tick]);

  const onViewport = useCallback(
    (v: GanttViewport) => {
      last.current = v;
      if (!enabled || !planId || !plan || !base || !v.rowIds.length) return;
      const rows = Math.max(1, base.resources.length);
      const horizon = Math.max(1, Date.parse(plan.horizon_end) - Date.parse(plan.horizon_start));
      const density = plan.operations / rows / horizon; // operations per row and millisecond
      const w = v.end - v.start;
      const a = v.start - w * 0.5;
      const b = v.end + w * 0.5;
      const mode: Loaded["mode"] = density * (b - a) * v.rowIds.length > OPS_BUDGET ? "blocks" : "ops";
      const L = loaded.current;
      if (L && L.mode === mode && L.a <= v.start && L.b >= v.end && v.rowIds.every((id) => L.ids.has(id))) return;
      const my = ++seq.current;
      const query = { start: new Date(a).toISOString(), end: new Date(b).toISOString(), resource_ids: v.rowIds };
      setLoading(true);
      const done = () => my === seq.current && setLoading(false);
      if (mode === "ops") {
        api<GanttData>(`/plans/${planId}/gantt`, { query: { ...query, resources: false } })
          .then((d) => {
            if (my !== seq.current) return;
            loaded.current = { a, b, ids: new Set(v.rowIds), mode };
            setOps(d.operations);
            setBlocks(null);
          })
          .catch((e) => my === seq.current && setError(e instanceof ApiError ? e : new ApiError(0, "ERROR", String(e))))
          .finally(done);
      } else {
        // merge gaps shorter than ~4 px at the current zoom
        const resolution = Math.max(1, Math.round(4 / v.pxPerMin));
        api<{ lanes: Record<string, [string, string, number, number, number][]> }>(`/plans/${planId}/gantt/blocks`, { query: { ...query, resolution_minutes: resolution } })
          .then((d) => {
            if (my !== seq.current) return;
            loaded.current = { a, b, ids: new Set(v.rowIds), mode };
            const out: Record<string, GBlock[]> = {};
            for (const [rid, lane] of Object.entries(d.lanes)) out[rid] = lane.map(([s, e, n, late, locked]) => [Date.parse(s), Date.parse(e), n, late, locked]);
            setBlocks(out);
            setOps([]);
          })
          .catch((e) => my === seq.current && setError(e instanceof ApiError ? e : new ApiError(0, "ERROR", String(e))))
          .finally(done);
      }
    },
    [enabled, planId, plan, base],
  );

  // the rows arrived after the first viewport notice: load that viewport now
  useEffect(() => {
    if (base && last.current) onViewport(last.current);
  }, [base, onViewport]);

  const data: GanttData | null = useMemo(() => (base && span ? { ...base, window: { start: new Date(span.a).toISOString(), end: new Date(span.b).toISOString() }, operations: ops, blocks } : null), [base, span, ops, blocks]);
  const reload = useCallback(() => {
    loaded.current = null;
    if (last.current) onViewport(last.current);
  }, [onViewport]);
  const reloadAll = useCallback(() => setTick((t) => t + 1), []);
  return { data, error, loading, onViewport, reload, reloadAll };
}
