"use client";
// ECharts wrapper + MonxuPlan chart palette (validated with the categorical checks: lightness band,
// chroma floor, CVD separation ≥ 9 ΔE adjacent, normal-vision floor). Status colours are reserved
// for state (on time / risk / late) and never used as a series colour.
import { useEffect, useRef } from "react";

export const SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#4a3aa7", "#e87ba4"];
export const SEQ_BLUE = ["#eef4fc", "#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"];
export const STATUS = { ok: "#1d7a4a", warn: "#c98a12", bad: "#b8321f", neutral: "#8a94a3" };
export const INK = { primary: "#262c36", secondary: "#4a5565", muted: "#8a94a3", grid: "#eef1f4", axis: "#c6cdd6" };

export const baseOption = {
  textStyle: { fontFamily: "Inter Variable, Inter, system-ui, sans-serif", color: INK.secondary, fontSize: 11 },
  grid: { left: 48, right: 16, top: 28, bottom: 28, containLabel: true },
  tooltip: { trigger: "axis", backgroundColor: "#fff", borderColor: INK.axis, textStyle: { color: INK.primary, fontSize: 12 }, axisPointer: { type: "line", lineStyle: { color: INK.muted } } },
  legend: { top: 0, left: 0, icon: "roundRect", itemWidth: 10, itemHeight: 10, textStyle: { color: INK.secondary, fontSize: 11 } },
  animation: false,
};
export const axisCat = { type: "category", axisLine: { lineStyle: { color: INK.axis } }, axisTick: { show: false }, axisLabel: { color: INK.secondary } };
export const axisVal = { type: "value", splitLine: { lineStyle: { color: INK.grid } }, axisLine: { show: false }, axisLabel: { color: INK.secondary } };

export function Chart({ option, height = 260, onClick, ariaLabel }: { option: any; height?: number | string; onClick?: (p: any) => void; ariaLabel: string }) {
  const el = useRef<HTMLDivElement>(null);
  const inst = useRef<any>(null);
  const clickRef = useRef(onClick);
  clickRef.current = onClick;
  useEffect(() => {
    let disposed = false;
    let ro: ResizeObserver | null = null;
    import("echarts").then((ec) => {
      if (disposed || !el.current) return;
      inst.current = ec.init(el.current, undefined, { renderer: "canvas" });
      inst.current.on("click", (p: any) => clickRef.current?.(p));
      inst.current.setOption({ ...baseOption, ...option }, true);
      ro = new ResizeObserver(() => inst.current?.resize());
      ro.observe(el.current);
    });
    return () => {
      disposed = true;
      ro?.disconnect();
      inst.current?.dispose();
      inst.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => {
    inst.current?.setOption({ ...baseOption, ...option }, true);
  }, [option]);
  return <div ref={el} role="img" aria-label={ariaLabel} style={{ height, width: "100%" }} />;
}
