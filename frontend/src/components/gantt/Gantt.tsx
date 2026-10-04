"use client";
// Canvas Gantt for thousands of operations. The canvas only draws what the server computed:
// dropping a bar never changes the plan by itself — it asks the backend for an impact preview.
import { forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState } from "react";
import { dt, duration, localParts } from "@/lib/format";
import { SERIES } from "../charts/Chart";

export type GRes = { id: string; code: string; name: string; kind: string; area: string | null; area_name: string | null; groups: string[]; status: string | null; non_working: [string, string][]; unavailability: { start: string; end: string; kind: string; reason: string | null }[]; utilization: number | null };
export type GOp = {
  id: string;
  order_id: string;
  order: string;
  item: string | null;
  item_name?: string | null;
  family: string | null;
  customer: string | null;
  priority: number | null;
  expedite: boolean;
  due: string | null;
  resource_id: string;
  setup_start: string;
  start: string;
  end: string;
  setup_minutes: number;
  run_minutes: number;
  quantity: number;
  fixed: boolean;
  fixed_reason: string | null;
  locked: boolean;
  late: boolean;
  zone: string;
  subcontracted: boolean;
  binding: { type?: string; ref?: string; detail?: string; wait_minutes?: number } | null;
  order_status: string | null;
  material_status: string | null;
};
/** Busy block of a lane in zoomed-out views of dense plans: [start ms, end ms, operations, late, locked]. */
export type GBlock = [number, number, number, number, number];
export type GanttData = {
  plan: { id: string; number: string };
  timezone: string;
  window: { start: string; end: string };
  now: string;
  frozen_until: string | null;
  resources: GRes[];
  operations: GOp[];
  blocks?: Record<string, GBlock[]> | null;
};
/** What the chart shows: visible time span and resource rows (with some rows of margin). */
export type GanttViewport = { start: number; end: number; rowIds: string[]; pxPerMin: number };
type OpT = { o: GOp; ss: number; st: number; en: number };
type Lane = { list: OpT[]; maxDur: number };

function firstFrom(list: OpT[], t: number): number {
  let lo = 0;
  let hi = list.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (list[mid].ss < t) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

export const ZOOMS = [
  { id: "15m", label: "15 min", pxPerMin: 4 },
  { id: "h", label: "Hour", pxPerMin: 1.2 },
  { id: "shift", label: "Shift", pxPerMin: 0.45 },
  { id: "d", label: "Day", pxPerMin: 0.16 },
  { id: "w", label: "Week", pxPerMin: 0.045 },
  { id: "m", label: "Month", pxPerMin: 0.012 },
];

const LABEL_W = 176;
const HEAD_H = 40;
const ROW_H = 28;
const GROUP_H = 20;
const BAR_H = 16;

type Row = { type: "group"; label: string } | { type: "res"; res: GRes };
type Hit = { op: GOp; x: number; y: number; w: number; h: number };
type BlockHit = { res: string; a: number; b: number; x: number; y: number; w: number; h: number };

export type GanttHandle = { scrollToTime: (ms: number) => void; scrollToOp: (id: string) => void; scrollTo: (resourceId: string, ms: number) => void; zoom: (dir: 1 | -1) => void };

export const Gantt = forwardRef<GanttHandle, {
  data: GanttData;
  zoom: string;
  onZoom: (z: string) => void;
  colorBy: "family" | "status" | "customer";
  selectedId: string | null;
  highlightOps?: Set<string> | null;
  deps?: { from: string; to: string }[];
  onSelect: (op: GOp | null) => void;
  onOpen?: (op: GOp) => void;
  onMove?: (op: GOp, resourceId: string, startIso: string) => void;
  canEdit: boolean;
  height: number;
  onLegend?: (items: { label: string; color: string }[]) => void;
  onViewport?: (v: GanttViewport) => void;
}>(function Gantt({ data, zoom, onZoom, colorBy, selectedId, highlightOps, deps, onSelect, onOpen, onMove, canEdit, height, onLegend, onViewport }, ref) {
  const wrap = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const [width, setWidth] = useState(1000);
  const z = ZOOMS.find((x) => x.id === zoom) || ZOOMS[3];
  const t0 = useMemo(() => new Date(data.window.start).getTime(), [data.window.start]);
  const t1 = useMemo(() => new Date(data.window.end).getTime(), [data.window.end]);
  const nowMs = useMemo(() => new Date(data.now).getTime(), [data.now]);
  const [viewStart, setViewStart] = useState(() => Math.max(t0, nowMs - 4 * 3600e3));
  const [scrollY, setScrollY] = useState(0);
  const [hover, setHover] = useState<{ op: GOp; x: number; y: number } | null>(null);
  const [drag, setDrag] = useState<{ op: GOp; dx: number; dy: number; x0: number; y0: number; moved: boolean } | null>(null);
  const hits = useRef<Hit[]>([]);
  const blockHits = useRef<BlockHit[]>([]);
  const [live, setLive] = useState("");

  // times parsed once per data set (not once per frame), lanes sorted for binary search
  const opsByRes = useMemo(() => {
    const m = new Map<string, Lane>();
    for (const o of data.operations) {
      const ss = Date.parse(o.setup_start);
      const en = Date.parse(o.end);
      let lane = m.get(o.resource_id);
      if (!lane) m.set(o.resource_id, (lane = { list: [], maxDur: 0 }));
      lane.list.push({ o, ss, st: Date.parse(o.start), en });
      if (en - ss > lane.maxDur) lane.maxDur = en - ss;
    }
    for (const lane of m.values()) lane.list.sort((a, b) => a.ss - b.ss);
    return m;
  }, [data.operations]);
  const resTimes = useMemo(() => {
    const m = new Map<string, { nw: [number, number][]; un: { a: number; b: number; kind: string }[] }>();
    for (const r of data.resources) m.set(r.id, { nw: r.non_working.map(([a, b]) => [Date.parse(a), Date.parse(b)] as [number, number]), un: r.unavailability.map((u) => ({ a: Date.parse(u.start), b: Date.parse(u.end), kind: u.kind })) });
    return m;
  }, [data.resources]);

  const rows: Row[] = useMemo(() => {
    const out: Row[] = [];
    let last: string | null | undefined;
    for (const r of data.resources) {
      const g = r.area_name || r.area || (r.kind === "SUBCONTRACTOR" ? "Subcontracting" : "Other");
      if (g !== last) {
        out.push({ type: "group", label: g });
        last = g;
      }
      out.push({ type: "res", res: r });
    }
    return out;
  }, [data.resources]);
  const rowY = useMemo(() => {
    const ys: number[] = [];
    let y = 0;
    for (const r of rows) {
      ys.push(y);
      y += r.type === "group" ? GROUP_H : ROW_H;
    }
    ys.push(y);
    return ys;
  }, [rows]);
  const totalH = rowY[rowY.length - 1];
  const resRowIndex = useMemo(() => {
    const m = new Map<string, number>();
    rows.forEach((r, i) => r.type === "res" && m.set(r.res.id, i));
    return m;
  }, [rows]);

  const families = useMemo(() => {
    const key = colorBy === "customer" ? "customer" : "family";
    const cnt = new Map<string, number>();
    for (const o of data.operations) {
      const k = (o as any)[key];
      if (k) cnt.set(k, (cnt.get(k) || 0) + 1);
    }
    // fixed order: the most frequent keys get the validated series hues, all others fold into "other"
    return [...cnt.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).slice(0, SERIES.length).map(([k]) => k).sort();
  }, [data.operations, colorBy]);

  useEffect(() => {
    onLegend?.(colorBy === "status" ? [{ label: "on time", color: "#1f3a64" }, { label: "late order", color: "#7a8699" }] : [...families.map((f, i) => ({ label: f, color: SERIES[i] })), { label: "other", color: "#8a94a3" }]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [families, colorBy]);

  const pxPerMs = z.pxPerMin / 60000;
  const xOf = useCallback((ms: number) => LABEL_W + (ms - viewStart) * pxPerMs, [viewStart, pxPerMs]);
  const msOf = useCallback((x: number) => viewStart + (x - LABEL_W) / pxPerMs, [viewStart, pxPerMs]);

  useEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setWidth(el.clientWidth));
    ro.observe(el);
    setWidth(el.clientWidth);
    return () => ro.disconnect();
  }, []);

  const clampY = useCallback((y: number) => Math.max(0, Math.min(y, Math.max(0, totalH - (height - HEAD_H) + 20))), [totalH, height]);
  const clampX = useCallback((ms: number) => Math.max(t0 - 86400e3, Math.min(ms, t1)), [t0, t1]);

  useImperativeHandle(ref, () => ({
    scrollToTime: (ms) => setViewStart(clampX(ms - ((width - LABEL_W) / pxPerMs) * 0.15)),
    scrollToOp: (id) => {
      const o = data.operations.find((x) => x.id === id);
      if (!o) return;
      setViewStart(clampX(new Date(o.setup_start).getTime() - ((width - LABEL_W) / pxPerMs) * 0.3));
      const ri = resRowIndex.get(o.resource_id);
      if (ri !== undefined) setScrollY(clampY(rowY[ri] - (height - HEAD_H) / 3));
    },
    scrollTo: (resourceId, ms) => {
      setViewStart(clampX(ms - ((width - LABEL_W) / pxPerMs) * 0.3));
      const ri = resRowIndex.get(resourceId);
      if (ri !== undefined) setScrollY(clampY(rowY[ri] - (height - HEAD_H) / 3));
    },
    zoom: (dir) => {
      const i = ZOOMS.findIndex((x) => x.id === zoom);
      const n = ZOOMS[Math.max(0, Math.min(ZOOMS.length - 1, i + dir))];
      onZoom(n.id);
    },
  }));

  // keep the time under the centre fixed when zooming
  const prevZoom = useRef(z.pxPerMin);
  useEffect(() => {
    if (prevZoom.current !== z.pxPerMin) {
      const centre = viewStart + ((width - LABEL_W) / 2) * (60000 / prevZoom.current);
      setViewStart(clampX(centre - ((width - LABEL_W) / 2) * (60000 / z.pxPerMin)));
      prevZoom.current = z.pxPerMin;
    }
  }, [z.pxPerMin, viewStart, width, clampX]);

  // ------------------------------------------------------------------ draw
  useEffect(() => {
    const c = canvas.current;
    if (!c) return;
    const dpr = window.devicePixelRatio || 1;
    c.width = Math.floor(width * dpr);
    c.height = Math.floor(height * dpr);
    c.style.width = `${width}px`;
    c.style.height = `${height}px`;
    const g = c.getContext("2d")!;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, width, height);
    g.font = "11px Inter Variable, Inter, system-ui, sans-serif";
    g.textBaseline = "middle";
    const viewEnd = msOf(width);
    const newHits: Hit[] = [];
    const newBlocks: BlockHit[] = [];

    const hatch = (x: number, y: number, w: number, h: number, color: string, gap = 5) => {
      g.save();
      g.beginPath();
      g.rect(x, y, w, h);
      g.clip();
      g.strokeStyle = color;
      g.lineWidth = 1;
      for (let k = -h; k < w; k += gap) {
        g.beginPath();
        g.moveTo(x + k, y + h);
        g.lineTo(x + k + h, y);
        g.stroke();
      }
      g.restore();
    };

    // body rows
    g.save();
    g.beginPath();
    g.rect(0, HEAD_H, width, height - HEAD_H);
    g.clip();
    const first = Math.max(0, rowY.findIndex((y, i) => rowY[i + 1] > scrollY) );
    for (let i = first; i < rows.length; i++) {
      const y = HEAD_H + rowY[i] - scrollY;
      if (y > height) break;
      const r = rows[i];
      if (r.type === "group") {
        g.fillStyle = "#eef1f4";
        g.fillRect(0, y, width, GROUP_H);
        g.fillStyle = "#4a5565";
        g.font = "600 10.5px Inter Variable, Inter, system-ui, sans-serif";
        g.fillText(r.label.toUpperCase(), 8, y + GROUP_H / 2);
        g.font = "11px Inter Variable, Inter, system-ui, sans-serif";
        continue;
      }
      const res = r.res;
      const rt = resTimes.get(res.id);
      // non-working time
      g.fillStyle = "#eceff3";
      for (const [a, b] of rt?.nw || []) {
        const xa = xOf(a);
        const xb = xOf(b);
        if (xb < LABEL_W || xa > width) continue;
        g.fillRect(Math.max(LABEL_W, xa), y, Math.min(width, xb) - Math.max(LABEL_W, xa), ROW_H);
      }
      // maintenance / downtime (striped + label)
      for (const u of rt?.un || []) {
        const xa = xOf(u.a);
        const xb = xOf(u.b);
        if (xb < LABEL_W || xa > width) continue;
        const x = Math.max(LABEL_W, xa);
        const w = Math.min(width, xb) - x;
        g.fillStyle = "rgba(184,50,31,0.07)";
        g.fillRect(x, y + 1, w, ROW_H - 2);
        hatch(x, y + 1, w, ROW_H - 2, "rgba(184,50,31,0.35)", 6);
        if (w > 60) {
          g.fillStyle = "#b8321f";
          g.fillText(`✕ ${u.kind.toLowerCase()}`, x + 4, y + 7);
        }
      }
      g.strokeStyle = "#eef1f4";
      g.beginPath();
      g.moveTo(LABEL_W, y + ROW_H - 0.5);
      g.lineTo(width, y + ROW_H - 0.5);
      g.stroke();

      // busy blocks (zoomed-out view of a dense plan): one bar per busy stretch of the machine
      const blocks = data.blocks?.[res.id];
      if (blocks) {
        const by = y + (ROW_H - BAR_H) / 2;
        for (const [a, b, n, late, locked] of blocks) {
          if (b < viewStart || a > viewEnd) continue;
          const xa = Math.max(LABEL_W, xOf(a));
          const w = Math.max(1.5, Math.min(width, xOf(b)) - xa);
          g.fillStyle = "#5b6b82";
          g.fillRect(xa, by, w, BAR_H);
          if (late) {
            g.fillStyle = "#b8321f";
            g.fillRect(xa, by, w, 3);
          }
          if (w > 44) {
            g.save();
            g.beginPath();
            g.rect(xa, by, w, BAR_H);
            g.clip();
            g.fillStyle = "#ffffff";
            g.fillText(`${locked ? "🔒︎ " : ""}${n} op${n === 1 ? "" : "s"}${late ? ` · ${late} ▲` : ""}`, xa + 4, by + BAR_H / 2 + 0.5);
            g.restore();
          }
          newBlocks.push({ res: res.id, a, b, x: xa, y: by, w, h: BAR_H });
        }
      }
      // bars
      const lane = opsByRes.get(res.id);
      const list = lane ? lane.list : [];
      for (let k = lane ? firstFrom(list, viewStart - lane.maxDur) : 0; k < list.length; k++) {
        const { o, ss, st, en } = list[k];
        if (ss > viewEnd) break;
        if (en < viewStart) continue;
        const xs = xOf(ss);
        const xr = xOf(st);
        const xe = xOf(en);
        const by = y + (ROW_H - BAR_H) / 2;
        const w = Math.max(2, xe - xs);
        const dim = highlightOps && !highlightOps.has(o.id);
        g.globalAlpha = dim ? 0.25 : 1;
        let fill = "#5b6b82";
        if (colorBy === "status") fill = o.order_status === "LATE" ? "#7a8699" : "#1f3a64";
        else {
          const k = colorBy === "customer" ? o.customer : o.family;
          const idx = k ? families.indexOf(k) : -1;
          fill = idx >= 0 ? SERIES[idx] : "#8a94a3";
        }
        // setup: hatched segment before the run
        if (o.setup_minutes > 0 && xr > xs + 1) {
          g.fillStyle = "#ffffff";
          g.fillRect(xs, by, xr - xs, BAR_H);
          hatch(xs, by, xr - xs, BAR_H, fill, 4);
          g.strokeStyle = fill;
          g.strokeRect(xs + 0.5, by + 0.5, xr - xs - 1, BAR_H - 1);
        }
        g.fillStyle = fill;
        const rx = Math.max(xr, xs);
        g.beginPath();
        g.roundRect(rx, by, Math.max(2, xe - rx), BAR_H, 2);
        g.fill();
        // frozen / fixed: lock glyph; late: red outline + ▲; material risk: dotted underline + ◆
        if (o.late) {
          g.strokeStyle = "#b8321f";
          g.lineWidth = 2;
          g.strokeRect(xs + 1, by - 1, w - 2, BAR_H + 2);
          g.lineWidth = 1;
        }
        if (o.material_status === "SHORTAGE" || o.material_status === "LATE_SUPPLY" || o.material_status === "RISK") {
          g.strokeStyle = "#c98a12";
          g.setLineDash([2, 2]);
          g.beginPath();
          g.moveTo(xs, by + BAR_H + 2.5);
          g.lineTo(xe, by + BAR_H + 2.5);
          g.stroke();
          g.setLineDash([]);
        }
        if (o.id === selectedId) {
          g.strokeStyle = "#0b1422";
          g.lineWidth = 2;
          g.strokeRect(xs - 1.5, by - 2.5, w + 3, BAR_H + 5);
          g.lineWidth = 1;
        }
        const glyphs = `${o.late ? "▲" : ""}${o.locked || o.fixed ? "🔒︎" : ""}${o.material_status && ["SHORTAGE", "LATE_SUPPLY"].includes(o.material_status) ? "◆" : ""}${o.subcontracted ? "⇄" : ""}`;
        if (xe - rx > 34) {
          g.save();
          g.beginPath();
          g.rect(rx, by, xe - rx, BAR_H);
          g.clip();
          g.fillStyle = "#ffffff";
          g.fillText(`${glyphs ? glyphs + " " : ""}${o.id}`, rx + 4, by + BAR_H / 2 + 0.5);
          g.restore();
        } else if (glyphs && xe - xs > 8) {
          g.fillStyle = o.late ? "#b8321f" : "#262c36";
          g.fillText(glyphs.charAt(0), xe + 2, by + BAR_H / 2);
        }
        g.globalAlpha = 1;
        newHits.push({ op: o, x: xs, y: by, w, h: BAR_H });
      }
    }
    // frozen zone
    if (data.frozen_until) {
      const xf = xOf(new Date(data.frozen_until).getTime());
      const xa = Math.max(LABEL_W, xOf(t0));
      if (xf > LABEL_W) {
        hatch(xa, HEAD_H, Math.min(width, xf) - xa, height - HEAD_H, "rgba(31,58,100,0.08)", 10);
        g.strokeStyle = "#1f3a64";
        g.setLineDash([4, 3]);
        g.beginPath();
        g.moveTo(xf, HEAD_H);
        g.lineTo(xf, height);
        g.stroke();
        g.setLineDash([]);
      }
    }
    // dependencies of the selected order
    if (deps && deps.length) {
      const pos = new Map(newHits.map((h) => [h.op.id, h]));
      g.strokeStyle = "#0b1422";
      g.fillStyle = "#0b1422";
      for (const d of deps) {
        const a = pos.get(d.from);
        const b = pos.get(d.to);
        if (!a || !b) continue;
        const x1 = a.x + a.w;
        const y1 = a.y + a.h / 2;
        const x2 = b.x;
        const y2 = b.y + b.h / 2;
        g.beginPath();
        g.moveTo(x1, y1);
        g.lineTo(x1 + 6, y1);
        g.lineTo(x1 + 6, y2);
        g.lineTo(x2 - 3, y2);
        g.stroke();
        g.beginPath();
        g.moveTo(x2, y2);
        g.lineTo(x2 - 5, y2 - 3);
        g.lineTo(x2 - 5, y2 + 3);
        g.fill();
      }
    }
    // drag ghost
    if (drag && drag.moved) {
      const h = newHits.find((x) => x.op.id === drag.op.id);
      if (h) {
        g.globalAlpha = 0.6;
        g.fillStyle = "#2f6fb3";
        g.fillRect(h.x + drag.dx, h.y + drag.dy, h.w, h.h);
        g.globalAlpha = 1;
        g.strokeStyle = "#2f6fb3";
        g.setLineDash([3, 2]);
        g.strokeRect(h.x + drag.dx - 0.5, h.y + drag.dy - 0.5, h.w + 1, h.h + 1);
        g.setLineDash([]);
        const targetMs = msOf(h.x + drag.dx);
        g.fillStyle = "#0b1422";
        g.fillText(dt(new Date(targetMs)), h.x + drag.dx, h.y + drag.dy - 8);
      }
    }
    // now line
    const xn = xOf(nowMs);
    if (xn > LABEL_W && xn < width) {
      g.strokeStyle = "#2f6fb3";
      g.lineWidth = 1.5;
      g.beginPath();
      g.moveTo(xn, HEAD_H);
      g.lineTo(xn, height);
      g.stroke();
      g.lineWidth = 1;
    }
    g.restore();

    // resource labels (fixed column)
    g.fillStyle = "#ffffff";
    g.fillRect(0, HEAD_H, LABEL_W, height - HEAD_H);
    g.save();
    g.beginPath();
    g.rect(0, HEAD_H, LABEL_W, height - HEAD_H);
    g.clip();
    for (let i = first; i < rows.length; i++) {
      const y = HEAD_H + rowY[i] - scrollY;
      if (y > height) break;
      const r = rows[i];
      if (r.type === "group") {
        g.fillStyle = "#eef1f4";
        g.fillRect(0, y, LABEL_W, GROUP_H);
        g.fillStyle = "#4a5565";
        g.font = "600 10.5px Inter Variable, Inter, system-ui, sans-serif";
        g.fillText(r.label.toUpperCase(), 8, y + GROUP_H / 2);
        g.font = "11px Inter Variable, Inter, system-ui, sans-serif";
        continue;
      }
      const res = r.res;
      const down = res.status === "DOWN" || res.status === "MAINTENANCE";
      g.fillStyle = "#262c36";
      g.font = "500 11.5px IBM Plex Mono, monospace";
      g.fillText(`${down ? "✕ " : ""}${res.code}`, 8, y + ROW_H / 2 - 5);
      g.font = "10px Inter Variable, Inter, system-ui, sans-serif";
      g.fillStyle = "#667081";
      const util = res.utilization !== null ? ` · ${Math.round(res.utilization * 100)} %` : "";
      g.fillText(`${res.name}`.slice(0, 22) + util, 8, y + ROW_H / 2 + 7);
      g.font = "11px Inter Variable, Inter, system-ui, sans-serif";
      if (res.utilization !== null) {
        const u = Math.min(1, res.utilization);
        g.fillStyle = "#eef1f4";
        g.fillRect(LABEL_W - 34, y + ROW_H / 2 - 2, 26, 4);
        g.fillStyle = res.utilization > 0.85 ? "#1c5cab" : "#86b6ef";
        g.fillRect(LABEL_W - 34, y + ROW_H / 2 - 2, 26 * u, 4);
      }
      g.strokeStyle = "#eef1f4";
      g.beginPath();
      g.moveTo(0, y + ROW_H - 0.5);
      g.lineTo(LABEL_W, y + ROW_H - 0.5);
      g.stroke();
    }
    g.restore();
    g.strokeStyle = "#dde2e8";
    g.beginPath();
    g.moveTo(LABEL_W - 0.5, 0);
    g.lineTo(LABEL_W - 0.5, height);
    g.stroke();

    // time header (plant local time)
    g.fillStyle = "#f6f7f9";
    g.fillRect(0, 0, width, HEAD_H);
    g.strokeStyle = "#dde2e8";
    g.beginPath();
    g.moveTo(0, HEAD_H - 0.5);
    g.lineTo(width, HEAD_H - 0.5);
    g.stroke();
    // label column: plan number and the time zone every time on the board is shown in
    g.save();
    g.beginPath();
    g.rect(0, 0, LABEL_W - 4, HEAD_H);
    g.clip();
    g.fillStyle = "#4a5565";
    g.font = "600 11px Inter Variable, Inter, system-ui, sans-serif";
    g.fillText(data.plan.number, 8, 12);
    g.font = "11px Inter Variable, Inter, system-ui, sans-serif";
    g.fillStyle = "#667081";
    g.fillText(data.timezone, 8, 30);
    g.restore();
    g.font = "11px Inter Variable, Inter, system-ui, sans-serif";
    const minorMin = z.pxPerMin >= 3 ? 15 : z.pxPerMin >= 1 ? 60 : z.pxPerMin >= 0.4 ? 240 : z.pxPerMin >= 0.1 ? 1440 : z.pxPerMin >= 0.03 ? 1440 : 10080;
    // iterate plant-local hours (DST aware via Intl)
    const stepMs = Math.min(minorMin, 60) * 60000;
    let tt = Math.floor(viewStart / stepMs) * stepMs;
    const endMs = msOf(width);
    let lastDay = "";
    g.save();
    g.beginPath();
    g.rect(LABEL_W, 0, width - LABEL_W, height);
    g.clip();
    let guard = 0;
    let firstDayX: number | null = null;
    while (tt <= endMs && guard++ < 5000) {
      const p = localParts(tt);
      const x = xOf(tt);
      const dayKey = `${p.y}-${p.mo}-${p.d}`;
      const minuteOfDay = p.h * 60 + p.mi;
      if (dayKey !== lastDay && p.h === 0 && p.mi === 0) {
        g.strokeStyle = "#c6cdd6";
        g.beginPath();
        g.moveTo(x, 18);
        g.lineTo(x, height);
        g.stroke();
        if (firstDayX === null && x >= LABEL_W) firstDayX = x;
        if (z.pxPerMin * 1440 > 40 || p.wd === 0) {
          g.fillStyle = p.wd >= 5 ? "#8a94a3" : "#262c36";
          g.font = "600 11px Inter Variable, Inter, system-ui, sans-serif";
          const lbl = z.pxPerMin * 1440 > 90 ? dt(new Date(tt), { weekday: "short", day: "2-digit", month: "short" }) : dt(new Date(tt), { day: "2-digit", month: "short" });
          g.fillText(lbl, x + 4, 12);
          g.font = "11px Inter Variable, Inter, system-ui, sans-serif";
        }
        lastDay = dayKey;
      } else if (minorMin < 1440 && minuteOfDay % minorMin === 0) {
        g.strokeStyle = "#eef1f4";
        g.beginPath();
        g.moveTo(x, HEAD_H);
        g.lineTo(x, height);
        g.stroke();
        g.fillStyle = "#667081";
        g.fillText(`${String(p.h).padStart(2, "0")}:${String(p.mi).padStart(2, "0")}`, x + 3, 30);
      }
      tt += stepMs;
      if (minorMin >= 1440) tt = Math.floor(tt / 3600e3) * 3600e3; // hourly stepping for day zoom
    }
    // the day the view starts in stays readable even when its midnight is scrolled out of view
    {
      g.font = "600 11px Inter Variable, Inter, system-ui, sans-serif";
      const lbl = dt(new Date(viewStart), z.pxPerMin * 1440 > 90 ? { weekday: "short", day: "2-digit", month: "short" } : { day: "2-digit", month: "short" });
      const w = g.measureText(lbl).width + 8;
      if (firstDayX === null || firstDayX - LABEL_W > w + 4) {
        g.fillStyle = "#f6f7f9";
        g.fillRect(LABEL_W, 0, w, 17);
        g.fillStyle = "#262c36";
        g.fillText(lbl, LABEL_W + 4, 12);
      }
      g.font = "11px Inter Variable, Inter, system-ui, sans-serif";
    }
    if (xn > LABEL_W && xn < width) {
      g.fillStyle = "#2f6fb3";
      g.fillRect(xn - 1, 18, 2, HEAD_H - 18);
    }
    if (data.frozen_until) {
      const xf = xOf(new Date(data.frozen_until).getTime());
      if (xf > LABEL_W + 40 && xf < width) {
        g.fillStyle = "#1f3a64";
        g.fillText("🔒︎ frozen", xf - 58, 30);
      }
    }
    g.restore();
    hits.current = newHits;
    blockHits.current = newBlocks;
  }, [data, rows, rowY, width, height, viewStart, scrollY, z.pxPerMin, xOf, msOf, opsByRes, resTimes, colorBy, families, selectedId, highlightOps, deps, drag, nowMs, t0]);

  // tell the page what is on screen (debounced): large plans load the operations of the viewport only
  useEffect(() => {
    if (!onViewport) return;
    const h = setTimeout(() => {
      const top = scrollY - 10 * ROW_H;
      const bottom = scrollY + (height - HEAD_H) + 10 * ROW_H;
      const ids: string[] = [];
      for (let i = 0; i < rows.length; i++) {
        const r = rows[i];
        if (r.type === "res" && rowY[i + 1] > top && rowY[i] < bottom) ids.push(r.res.id);
      }
      onViewport({ start: viewStart, end: msOf(width), rowIds: ids, pxPerMin: z.pxPerMin });
    }, 120);
    return () => clearTimeout(h);
  }, [onViewport, viewStart, scrollY, width, height, rows, rowY, msOf, z.pxPerMin]);

  // ------------------------------------------------------------------ interaction
  const hitAt = (x: number, y: number): GOp | null => {
    for (let i = hits.current.length - 1; i >= 0; i--) {
      const h = hits.current[i];
      if (x >= h.x - 2 && x <= h.x + h.w + 2 && y >= h.y - 3 && y <= h.y + h.h + 3) return h.op;
    }
    return null;
  };
  const rowAt = (y: number): GRes | null => {
    const yy = y - HEAD_H + scrollY;
    for (let i = 0; i < rows.length; i++) {
      if (yy >= rowY[i] && yy < rowY[i + 1]) {
        const r = rows[i];
        return r.type === "res" ? r.res : null;
      }
    }
    return null;
  };
  const local = (e: React.MouseEvent | MouseEvent) => {
    const r = canvas.current!.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  };

  const blockAt = (x: number, y: number): BlockHit | null => blockHits.current.find((b) => x >= b.x - 1 && x <= b.x + b.w + 1 && y >= b.y - 2 && y <= b.y + b.h + 2) || null;

  const onMouseDown = (e: React.MouseEvent) => {
    const { x, y } = local(e);
    if (y < HEAD_H || x < LABEL_W) return;
    const op = hitAt(x, y);
    const block = op ? null : blockAt(x, y);
    if (block) {
      // a busy block opens at hour zoom, where its operations are drawn one by one
      const hour = ZOOMS.find((q) => q.id === "h")!;
      prevZoom.current = hour.pxPerMin; // position set here, not re-centred by the zoom effect
      onZoom("h");
      setViewStart(clampX(block.a - 30 * 60000));
      return;
    }
    if (op) {
      onSelect(op);
      setLive(describe(op));
      if (canEdit && onMove && e.button === 0) setDrag({ op, dx: 0, dy: 0, x0: x, y0: y, moved: false });
    } else {
      onSelect(null);
      // pan
      const startView = viewStart;
      const startScroll = scrollY;
      const x0 = e.clientX;
      const y0 = e.clientY;
      const mm = (ev: MouseEvent) => {
        setViewStart(clampX(startView - (ev.clientX - x0) / pxPerMs));
        setScrollY(clampY(startScroll - (ev.clientY - y0)));
      };
      const mu = () => {
        window.removeEventListener("mousemove", mm);
        window.removeEventListener("mouseup", mu);
      };
      window.addEventListener("mousemove", mm);
      window.addEventListener("mouseup", mu);
    }
  };

  useEffect(() => {
    if (!drag) return;
    const snapMin = z.pxPerMin >= 1 ? 5 : z.pxPerMin >= 0.4 ? 15 : 60;
    const mm = (ev: MouseEvent) => {
      const { x, y } = local(ev);
      const dxRaw = x - drag.x0;
      const snapPx = snapMin * z.pxPerMin;
      const dx = Math.round(dxRaw / snapPx) * snapPx;
      const target = rowAt(y);
      const srcIdx = resRowIndex.get(drag.op.resource_id)!;
      const tgtIdx = target ? resRowIndex.get(target.id)! : srcIdx;
      const dy = rowY[tgtIdx] - rowY[srcIdx];
      setDrag((d) => (d ? { ...d, dx, dy, moved: d.moved || Math.abs(dxRaw) > 4 || Math.abs(y - d.y0) > 6 } : d));
    };
    const mu = (ev: MouseEvent) => {
      const d = drag;
      setDrag(null);
      if (!d || !d.moved) return;
      const { y } = local(ev);
      const target = rowAt(y) || data.resources.find((r) => r.id === d.op.resource_id)!;
      // the bar's left edge is the setup start, and a move asks for the setup start: the job lands
      // where it was dropped (its setup may then change on the new resource / neighbour)
      const newSetupStart = new Date(d.op.setup_start).getTime() + d.dx / pxPerMs;
      onMove?.(d.op, target.id, new Date(newSetupStart).toISOString());
    };
    const key = (ev: KeyboardEvent) => ev.key === "Escape" && setDrag(null);
    window.addEventListener("mousemove", mm);
    window.addEventListener("mouseup", mu);
    window.addEventListener("keydown", key);
    return () => {
      window.removeEventListener("mousemove", mm);
      window.removeEventListener("mouseup", mu);
      window.removeEventListener("keydown", key);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [drag?.op.id, drag?.x0, z.pxPerMin, pxPerMs]);

  const onMouseMove = (e: React.MouseEvent) => {
    if (drag) return;
    const { x, y } = local(e);
    const op = y > HEAD_H && x > LABEL_W ? hitAt(x, y) : null;
    const block = !op && y > HEAD_H && x > LABEL_W ? blockAt(x, y) : null;
    setHover(op ? { op, x, y } : null);
    if (canvas.current) canvas.current.style.cursor = op ? (canEdit && onMove ? "grab" : "pointer") : block ? "zoom-in" : "default";
  };

  const onWheel = useCallback(
    (e: WheelEvent) => {
      e.preventDefault();
      if (e.ctrlKey || e.metaKey) {
        const i = ZOOMS.findIndex((x) => x.id === zoom);
        const n = ZOOMS[Math.max(0, Math.min(ZOOMS.length - 1, i + (e.deltaY > 0 ? 1 : -1)))];
        onZoom(n.id);
      } else if (e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
        setViewStart((v) => clampX(v + (e.deltaX || e.deltaY) / pxPerMs));
      } else {
        setScrollY((s) => clampY(s + e.deltaY));
      }
    },
    [zoom, onZoom, pxPerMs, clampX, clampY],
  );
  useEffect(() => {
    const c = canvas.current;
    if (!c) return;
    c.addEventListener("wheel", onWheel, { passive: false });
    return () => c.removeEventListener("wheel", onWheel);
  }, [onWheel]);

  // keyboard: ←/→ previous/next operation on the resource, ↑/↓ other resources, Enter opens
  const onKeyDown = (e: React.KeyboardEvent) => {
    const sel = data.operations.find((o) => o.id === selectedId);
    const visibleSpan = (width - LABEL_W) / pxPerMs;
    if (!sel) {
      if (e.key === "ArrowRight") setViewStart((v) => clampX(v + visibleSpan * 0.25));
      else if (e.key === "ArrowLeft") setViewStart((v) => clampX(v - visibleSpan * 0.25));
      else if (e.key === "ArrowDown") setScrollY((s) => clampY(s + ROW_H * 3));
      else if (e.key === "ArrowUp") setScrollY((s) => clampY(s - ROW_H * 3));
      else if (e.key === "Home") setViewStart(clampX(nowMs - visibleSpan * 0.1));
      else return;
      e.preventDefault();
      return;
    }
    const list = (opsByRes.get(sel.resource_id)?.list || []).map((x) => x.o);
    const idx = list.findIndex((o) => o.id === sel.id);
    let next: GOp | undefined;
    if (e.key === "ArrowRight") next = list[idx + 1];
    else if (e.key === "ArrowLeft") next = list[idx - 1];
    else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      const ri = rows.findIndex((r) => r.type === "res" && r.res.id === sel.resource_id);
      const step = e.key === "ArrowDown" ? 1 : -1;
      for (let i = ri + step; i >= 0 && i < rows.length; i += step) {
        const r = rows[i];
        if (r.type !== "res") continue;
        const cand = (opsByRes.get(r.res.id)?.list || []).map((x) => x.o);
        if (!cand.length) continue;
        const ref = new Date(sel.start).getTime();
        next = cand.reduce((a, b) => (Math.abs(new Date(b.start).getTime() - ref) < Math.abs(new Date(a.start).getTime() - ref) ? b : a));
        break;
      }
    } else if (e.key === "Enter") {
      onOpen?.(sel);
    } else if (e.key === "Escape") {
      onSelect(null);
    } else return;
    e.preventDefault();
    if (next) {
      onSelect(next);
      setLive(describe(next));
      const xs = xOf(new Date(next.setup_start).getTime());
      if (xs < LABEL_W + 20 || xs > width - 40) setViewStart(clampX(new Date(next.setup_start).getTime() - visibleSpan * 0.3));
      const ri = resRowIndex.get(next.resource_id);
      if (ri !== undefined) {
        const yy = rowY[ri] - scrollY;
        if (yy < 0 || yy > height - HEAD_H - ROW_H) setScrollY(clampY(rowY[ri] - (height - HEAD_H) / 3));
      }
    }
  };

  return (
    <div ref={wrap} className="relative select-none" style={{ height }}>
      <canvas
        ref={canvas}
        tabIndex={0}
        role="application"
        aria-roledescription="Gantt chart"
        aria-label={`Gantt chart of ${data.plan.number}: ${data.operations.length} operations on ${data.resources.length} resources. Arrow keys move between operations, Enter opens the detail, Escape clears the selection.`}
        onMouseDown={onMouseDown}
        onMouseMove={onMouseMove}
        onMouseLeave={() => setHover(null)}
        onDoubleClick={(e) => {
          const { x, y } = local(e);
          const op = hitAt(x, y);
          if (op) onOpen?.(op);
        }}
        onKeyDown={onKeyDown}
      />
      <div aria-live="polite" className="sr-only">
        {live}
      </div>
      {hover && !drag && (
        <div className="absolute z-20 pointer-events-none bg-white border border-gray-300 rounded-[3px] shadow-lg px-2.5 py-2 text-[12px] w-[280px]" style={{ left: Math.min(hover.x + 14, width - 290), top: Math.min(hover.y + 14, height - 170) }}>
          <div className="code font-semibold text-graphite-800">{hover.op.id}</div>
          <div className="text-slate-600 truncate">
            {hover.op.item} {hover.op.item_name ? `· ${hover.op.item_name}` : ""}
          </div>
          <div className="grid grid-cols-[70px_1fr] gap-x-2 mt-1 tabular">
            <span className="text-slate-600">Qty</span>
            <span>{hover.op.quantity}</span>
            <span className="text-slate-600">Setup</span>
            <span>
              {dt(hover.op.setup_start)} · {duration(hover.op.setup_minutes)}
            </span>
            <span className="text-slate-600">Run</span>
            <span>
              {dt(hover.op.start)} → {dt(hover.op.end)}
            </span>
            <span className="text-slate-600">Due</span>
            <span className={hover.op.late ? "text-red-600 font-semibold" : ""}>
              {dt(hover.op.due)} {hover.op.late ? "▲ late" : ""}
            </span>
            {hover.op.customer && (
              <>
                <span className="text-slate-600">Customer</span>
                <span className="truncate">{hover.op.customer}</span>
              </>
            )}
            {hover.op.binding?.type && (
              <>
                <span className="text-slate-600">Bound by</span>
                <span className="truncate">{hover.op.binding.detail || hover.op.binding.type}</span>
              </>
            )}
          </div>
          {(hover.op.locked || hover.op.fixed) && <div className="mt-1 text-navy-700">🔒︎ {hover.op.fixed_reason || "locked"}</div>}
        </div>
      )}

    </div>
  );
});

function describe(o: GOp): string {
  return `${o.id}, ${o.item || ""}, ${dt(o.start)} to ${dt(o.end)}${o.late ? ", late" : ""}${o.locked ? ", locked" : ""}`;
}
