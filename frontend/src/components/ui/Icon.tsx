// MonxuPlan's own line icon set (24×24 grid, 1.6 px stroke). Drawn for this product.
import type { SVGProps } from "react";

const P: Record<string, string> = {
  dashboard: "M4 4h7v7H4zM13 4h7v4h-7zM13 10h7v10h-7zM4 13h7v7H4z",
  board: "M3 5h18M3 12h18M3 19h18M7 3v4M14 10v4M10 17v4",
  orders: "M6 3h9l4 4v14H6zM15 3v4h4M9 11h7M9 15h7M9 19h4",
  capacity: "M4 20V10M9 20V4M14 20v-8M19 20v-5M3 20h18",
  materials: "M12 3l8 4.5v9L12 21l-8-4.5v-9zM12 12l8-4.5M12 12v9M12 12L4 7.5",
  mps: "M4 5h16v15H4zM4 9h16M8 3v4M16 3v4M8 13h3M13 13h3M8 16h3",
  scenarios: "M6 3v6a4 4 0 004 4h4a4 4 0 014 4v4M6 3L3 6M6 3l3 3M18 21l-3-3M18 21l3-3",
  alert: "M12 3l9 16H3zM12 10v4M12 17v.5",
  dispatch: "M4 6h11M4 12h16M4 18h8M18 4l3 2-3 2",
  analytics: "M4 19l5-6 4 3 7-9M4 4v16h16",
  database: "M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3zM4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3",
  plug: "M9 3v5M15 3v5M6 8h12v3a6 6 0 01-12 0zM12 17v4",
  settings: "M12 9a3 3 0 100 6 3 3 0 000-6zM19 12l2-1-1-3-2 .3-1.3-1.3.3-2-3-1-1 2h-2L9 3 6 4l.3 2L5 7.3 3 7l-1 3 2 1v2l-2 1 1 3 2-.3L6.3 18 6 20l3 1 1-2h2l1 2 3-1-.3-2 1.3-1.3 2 .3 1-3-2-1z",
  check: "M4 12l5 5L20 6",
  quality: "M12 3l7 3v6c0 4.5-3 8-7 9-4-1-7-4.5-7-9V6zM9 12l2 2 4-4",
  chat: "M4 5h16v11H9l-5 4z",
  search: "M11 4a7 7 0 100 14 7 7 0 000-14zM16 16l5 5",
  play: "M7 4l13 8-13 8z",
  stop: "M6 6h12v12H6z",
  undo: "M9 7L4 12l5 5M4 12h11a5 5 0 010 10h-2",
  redo: "M15 7l5 5-5 5M20 12H9a5 5 0 000 10h2",
  lock: "M6 11h12v10H6zM8 11V8a4 4 0 018 0v3",
  unlock: "M6 11h12v10H6zM8 11V8a4 4 0 017.5-2",
  x: "M5 5l14 14M19 5L5 19",
  plus: "M12 5v14M5 12h14",
  chevronDown: "M6 9l6 6 6-6",
  chevronRight: "M9 6l6 6-6 6",
  chevronLeft: "M15 6l-6 6 6 6",
  download: "M12 4v12M7 11l5 5 5-5M4 20h16",
  upload: "M12 20V8M7 13l5-5 5 5M4 4h16",
  info: "M12 3a9 9 0 100 18 9 9 0 000-18zM12 11v6M12 7.5v.5",
  refresh: "M20 11a8 8 0 10-2.3 5.7M20 4v7h-7",
  zoomIn: "M11 4a7 7 0 100 14 7 7 0 000-14zM16 16l5 5M8 11h6M11 8v6",
  zoomOut: "M11 4a7 7 0 100 14 7 7 0 000-14zM16 16l5 5M8 11h6",
  publish: "M12 16V4M7 9l5-5 5 5M5 20h14",
  compare: "M8 4v16M16 4v16M4 8h4M16 16h4M4 16h4M16 8h4",
  target: "M12 3a9 9 0 100 18 9 9 0 000-18zM12 8a4 4 0 100 8 4 4 0 000-8zM12 11.5v1",
  user: "M12 4a4 4 0 100 8 4 4 0 000-8zM4 21c1-4 4.5-6 8-6s7 2 8 6",
  logout: "M10 4H5v16h5M14 8l4 4-4 4M18 12H9",
  menu: "M4 6h16M4 12h16M4 18h16",
  factory: "M3 21V10l6 4V10l6 4V6l6 3v12zM3 21h18",
  keyboard: "M3 6h18v12H3zM6 9h1M9 9h1M12 9h1M15 9h1M18 9h1M7 15h10",
  filter: "M4 5h16l-6 7v6l-4 2v-8z",
  eye: "M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12zM12 9a3 3 0 100 6 3 3 0 000-6z",
  clock: "M12 3a9 9 0 100 18 9 9 0 000-18zM12 7v5l3 2",
  wrench: "M14 6a4 4 0 005 5l-8 8a2 2 0 01-3-3l8-8a4 4 0 01-2-2zM15 4l3 3",
  print: "M7 8V3h10v5M5 17H3V9h18v8h-2M7 14h10v7H7z",
};

export type IconName = keyof typeof P;

export function Icon({ name, size = 16, ...rest }: { name: IconName | string; size?: number } & SVGProps<SVGSVGElement>) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false" {...rest}>
      <path d={P[name] || P.info} />
    </svg>
  );
}
