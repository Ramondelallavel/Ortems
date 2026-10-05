# MonxuPlan — Design System & Screens

MonxuPlan is used for full working days by production planners, supply chain managers, plant managers
and industrial engineers. The UI optimises **Information → Decision → Action**: high density,
predictable layout, keyboard first, direct manipulation, minimal clicks. No decoration that does not
carry information.

## 1. Principles

1. A planner looking at the Planning Board for five seconds must answer: *what is happening, what is
   at risk, where is the bottleneck, what must I decide?*
2. Colour is never the only carrier of state: every status also has a glyph, a pattern or a label
   (late = red outline + ▲ marker, frozen = hatch + lock glyph, material risk = dotted underline + ◆).
3. Numbers are right aligned with tabular figures; units always visible.
4. Every metric is navigable (drill-down) — no dead numbers.
5. Every screen handles: loading, empty, error, partial data, success, warning, no permission,
   offline and calculating states.

## 2. Tokens

| Token | Value | Use |
|---|---|---|
| `--mx-navy-950` | `#0b1422` | Application bar, sidebar |
| `--mx-navy-900` | `#12203a` | Sidebar hover, headers |
| `--mx-navy-700` | `#1f3a64` | Primary action |
| `--mx-graphite-800` | `#262c36` | Primary text on light surfaces |
| `--mx-slate-600` | `#4a5565` | Secondary text |
| `--mx-slate-400` | `#8a94a3` | Tertiary text, disabled |
| `--mx-gray-200` | `#dde2e8` | Borders |
| `--mx-gray-100` | `#eef1f4` | Table stripes, panel headers |
| `--mx-gray-50` | `#f6f7f9` | Page background |
| `--mx-white` | `#ffffff` | Panels |
| `--mx-green-600` | `#1d7a4a` | OK / on time |
| `--mx-amber-500` | `#c98a12` | Warning / at risk |
| `--mx-red-600` | `#b8321f` | Critical / late / overload |
| `--mx-blue-500` | `#2f6fb3` | Info, selection |

Typography: **Inter** (UI) with `font-variant-numeric: tabular-nums` for all figures, **IBM Plex
Mono** for codes (WO numbers, item codes). Base size 13 px, table rows 26 px, controls 28 px.
Radius 3 px (dialogs 4 px). Shadows only on floating layers (menus, dialogs, tooltips).

## 3. Components (`frontend/src/components/ui`)

Button (primary / secondary / ghost / danger, with confirmation for dangerous actions) · IconButton ·
Input · NumberInput · Select · Combobox · DateTimeInput · Checkbox · Switch · Tabs · Panel ·
Toolbar · Badge · StatusPill · Tooltip · Dialog · Drawer · ContextMenu · Kbd · KPI (value, unit,
delta, drill link) · AlertItem · EmptyState / ErrorState / NoPermission / Calculating ·
DataGrid (virtual scroll, sort, filter, group, resize, pin, saved views, CSV/Excel export,
conditional formatting, inline edit) · FilterBar (typed expressions, saved filters) ·
Gantt (canvas) · LoadChart · Heatmap · Timeline · ProgressSteps · Form fields.

## 4. Navigation

```
Overview        Command Center
Planning        Demand · MPS · Capacity Plan · Detailed Plan (Planning Board)
Scheduling      Planning Board · Gantt · Dispatch · Exceptions
Orders          Production orders · Sales orders · Purchase orders
Materials       Availability · Projected inventory · Shortages · Pegging
Capacity        Load chart · Heatmap · Bottlenecks
Resources       Machines · Labour · Tools · Maintenance
Scenarios       Scenarios · Compare · What-if
Analytics       KPIs · Root cause · Plan vs actual · Reports
Master Data     Products · Materials · BOM · Routings · Resources · Calendars · Setup matrices · …
Integrations    Import wizard · Exports · Webhooks · Connectors · Jobs
Administration  Users & roles · Optimisation profiles · Planning rules · Data quality · Audit
```

## 5. Key screens

* **Command Center** (`/dashboard`) — greeting line with plant, date, current plan, last
  optimisation; *Attention Required* list; Orders at risk; Critical bottlenecks; Material shortages;
  Capacity load (14-day heatmap strip); OTIF with drill-down; Production today; Plan health counts.
* **Planning Board** (`/planning`) — toolbar (scenario, plan version, Plan / Optimize / Reschedule /
  Validate / Publish / Compare / Export, undo/redo), left panel (filters, resources, orders, materials,
  alerts), KPI bar, canvas Gantt, exceptions panel, contextual right panel (Why here? · Constraint
  explorer · order chain · pegging), KPI footer, optimisation progress overlay.
* **Gantt** — zoom 5 min · 15 min · hour · day · week · month; rows by resource grouped by area /
  group; setup segment hatched; non-working time shaded; maintenance striped; frozen zone tinted with
  label; now line; dependencies of the selected order; baseline ghosts; actual bars; drag & drop with
  impact preview and replan-mode choice.
* **Capacity** — load chart (capacity, load, overload, available) by day/week/month and by
  resource/group/area; resource × date heatmap (underloaded, balanced, high, overloaded, unavailable).
* **Materials** — availability status per order (OK / risk / shortage / late supply), shortages
  with impact, projected inventory, pegging tree both directions.
* **Scenarios** — list with lineage and changes; what-if wizards (night shift, extra machine,
  rush order, supplier delay, breakdown); comparison table and plan diff.
* **Dispatch / Supervisor / Operator** — progressively simpler views of the published plan.

## 6. Accessibility & localisation

Target WCAG 2.2 AA: contrast ≥ 4.5:1 for text, visible focus rings, full keyboard operation of grids
and Gantt (arrows, Enter, Esc, Delete, Ctrl/Cmd+Z/Y/S/F), labelled controls, ARIA roles on grids.
Strings live in `frontend/src/lib/i18n/` (English and Spanish complete; French, German and Portuguese
translate the navigation and fall back to English). Keys are either named (`en.ts` / `es.ts`) or the
English phrase itself (`es-phrases.ts`); `npm run i18n:check` lists any text without translation. Texts
generated by the server carry an English template and its values, which the interface translates;
the planning engine's detailed explanations and some server messages stay in English (stated in the
account dialog). Dates, numbers, currency and units are formatted per user
locale and plant timezone; metric/imperial unit systems.
