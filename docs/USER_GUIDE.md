# MonxuPlan — User guide

For production planners, plant and production managers, supervisors and operators. Screens are in
English and Spanish (French, German and Portuguese navigation, English fallback); switch with the
language selector in the top bar or under **My account** (your name in the top bar), which also keeps
your default plant, changes your password and signs you out on every device. The engine's detailed
explanations ("Why here?", constraint explorer, order chain) are in English in every language. Press **?** anywhere for keyboard shortcuts.

## 1. Daily loop

1. **Command Center** (`Alt+1`) — what needs attention: alerts (critical first), orders at risk with
   their first cause, bottlenecks, material shortages, what happens on the floor in the next 24 h and
   what changed since your last visit. Every figure opens its detail.
2. **Planning Board** (`Alt+2`) — the schedule of the selected scenario:
   * **Optimize** runs the planner (choose objective preset, solver, time budget, material and
     overtime policy). Progress shows the 17 pipeline steps as they happen; *Stop and keep best plan
     found* is always available.
   * **Reschedule** repairs the current plan after changes (new orders, breakdowns, delays) while
     moving as little as possible.
   * **Validate** re-checks every hard constraint of the displayed version.
   * **Publish** sends the plan to dispatch lists, operator screens and subscribed systems. Publishing
     an incomplete plan or one with violations requires a reason (audit log).
   * **Undo / Redo** (`Ctrl+Z` / `Ctrl+Y`) move between plan versions of the scenario.
3. **Scenarios** — try decisions on a copy before touching the live plan (§4).
4. **Dispatch / Supervisor / Operator** — the published plan as the floor sees it.

## 2. Reading the Gantt

* Rows are resources grouped by planning area; the small bar after each name is its utilisation.
* **Hatched segment** before a bar = setup/changeover; shaded background = non-working time
  (nights, weekends, holidays); red hatching with ✕ = maintenance or breakdown; dashed navy line and
  hatch = frozen zone; blue line = now.
* **Red outline and ▲** = the order is late; **◆ with dotted underline** = material risk (shortage or
  late supply); **🔒** = locked or frozen; **⇄** = subcontracted. Colour is never the only signal.
* Colour by family, customer or status (toolbar). Zoom 15 min … month (`+`/`−`, `Ctrl`+wheel),
  scroll with the wheel / `Shift`+wheel or by dragging the background, **Now** jumps to today.
* Keyboard: click the chart, then arrows move between operations, `Enter` opens the detail, `Esc`
  clears. `Ctrl+F` finds an order, operation or item.
* **Large plans** (more than 30 000 operations, e.g. a plant with 100 000 orders a day): the rows are
  loaded once and the operations of the visible rows and time span are fetched as you scroll or zoom.
  When a view would hold too many bars to read (a machine running hundreds of jobs in the visible
  span), each machine shows its **busy blocks** instead, labelled with their number of operations, a
  red top edge and "▲" count when some are late, 🔒 when some are locked; click a block to zoom into it. The search box looks the operation up on the server
  and jumps to it.

## 3. Understanding a decision

Select a bar (or double-click, or `Enter`) to open the side panel:

* **Why here?** — binding constraint (what the operation waited for, in working minutes), all reasons
  (predecessor, material, calendar, labour, tool, setup, rules applied) and the alternatives that were
  considered with their finish time.
* **Constraint explorer** — every possible resource re-evaluated against the rest of the schedule:
  earliest feasible slot or the exact reason it is impossible.
* **Order chain** — root-cause chain of the order, earliest possible end at infinite capacity (if even
  that is after the due date, the date is impossible — capacity will not help), estimated extra
  capacity needed, and **Highlight order chain** to see the order and its component orders with their
  dependencies in the Gantt.
* **Pegging** — which stock or receipt each material requirement uses and whether it arrives late.

**Lock** keeps an operation where it is for future runs.

## 4. Changing the plan

**Drag & drop a bar** to another time or resource. Nothing changes yet: MonxuPlan re-schedules with the
chosen **replan mode** (only this operation, this order, downstream operations, whole resource, area
or scenario), validates every hard constraint and shows the impact — KPI before/after, orders delayed
or advanced, setup change, sequence changes and any violation. If the requested time is impossible
the preview shows the earliest valid position. **Apply** creates a new plan version (undoable); the
moved operation is locked. Changes inside the frozen zone need the *frozen zone* permission and a
reason. On a very large plan the preview and the apply take tens of seconds rather than being
instant (the whole version is re-validated and stored; see OPTIMIZATION.md for measured times).

## 5. Scenarios and what-ifs

*Scenarios → What-if…* offers: night shift, add a machine, rush order, supplier delay, machine
breakdown, more operators or tool copies, allow overtime. Each creates a copy of the live scenario with
the change recorded in its change list, plans it and lets you compare. Tick two or more scenarios and
open **Compare**: KPI table and chart (✓ better / ! worse than the first column), and the order-by-order
differences. The live plan is never modified by a scenario.

## 6. Capacity and materials

* **Capacity** — load chart per resource/group/area (scheduled, overload, requirement to meet due
  dates, capacity) by hour, shift, day, week or month; heatmap of requirement ÷ capacity (▲ overloaded,
  ✕ no capacity); ranked bottlenecks with measured overload, induced waiting and causes.
* **Materials** — shortages (demand no stock or receipt covers), operations waiting for late supply,
  order material status, open receipts, projected stock per material and which orders/customers depend
  on it (reverse pegging). *What-if delay…* evaluates a supplier delay.
* **MPS** — weekly netting of forecast, customer orders and dependent demand; planned orders (firm the
  MAKE proposals you accept), MRP exceptions, rough-cut capacity and the aggregate family plan (linear
  program with overtime and shadow prices). Demand beyond the chosen horizon is reported, not added.

## 7. Your data: edit, Excel and databases

**Every screen has a *Data* button** (Orders, Materials, Capacity, MPS, Planning Board, Scenarios,
Dispatch, Supervisor, Plan vs actual). It lists the tables behind that screen; for each one you can
*Edit* it, download it to *Excel* with all its columns, or *Import* a changed file back.

*Master data* lists every table (44, from plants and calendars to order operations, material lots and
production reports). Each table can be edited in two ways:

* **List** → click a row → the record opens with all its fields and its child rows (BOM lines, shifts,
  routing operations, order operations…), which you can edit, add and remove before saving.
* **Edit as table** → a spreadsheet: change any cell, *Add row*, select rows and *Delete selected*, then
  *Save* once. Rows that cannot be saved keep your changes and show the reason in red.

Edits are versioned: if someone else saved the record meanwhile, you are asked to reload instead of
overwriting their change. Records still used by other data are deactivated instead of deleted.

**Excel round trip.** *Excel* downloads the table with an `id` column. Change values, add rows (leave
`id` empty), add a column `delete` and write *yes* on rows to delete, then *Import* the file.
*Master data → Download all data (Excel)* gives the whole data set in one workbook (one sheet per table);
*Integrations → Excel workbook* imports it back, validating all sheets together (a new item and the order
that uses it can be in the same workbook).

**Import wizard** (*Integrations → Import wizard*, or *Import* in any section): Upload → Columns
(recognised automatically, English/Spanish headers) → Validation → Preview → Import → Report.
Validation applies every row inside a transaction that is then undone, so it shows exactly what the
import will do: created, updated, unchanged, deleted and every error by row and field. A file with
errors cannot be imported unless you explicitly choose to import only the valid rows.

**Databases** (*Integrations → Databases*, administrators): connect PostgreSQL, MySQL/MariaDB, SQL
Server, Oracle (or a SQLite file). *Test connection* lists the tables and views. Add *sources*: which
MonxuPlan table each one feeds and where its rows come from (a table/view or a `SELECT` query), then
*Preview and map columns*. *Sync now* reads the sources and imports them with the same validation as
Excel; sources with errors wait for review in the import history. Optional automatic sync every N
minutes. Only reading is allowed; use a database user with read-only permissions.

*Data quality* runs before every planning run: critical errors (e.g. routing without resource, BOM
cycle, order without quantity) block planning; warnings are shown in the plan.

## 8. Shop floor

* **Dispatch list** — operations of the published plan for the next hours, per resource; printable.
* **Supervisor** — each machine: current job, next jobs, delayed, blocked and material issues.
* **Operator** — large touch targets: current job with setup instructions, *Start*, *Report quantity*,
  *Pause*, *Finish* (good and scrap). Reports are stored as actuals, never overwrite the plan, and feed
  *Plan vs actual*.

## 9. Assistant

`Ctrl+J` opens the assistant. Ask *why is WO-10042 late?*, *what is the bottleneck?*, *which orders
depend on BRG-6205?*, *what if CNC-03 breaks down 8 hours?*. Answers are built from the plan's stored
data and list their sources; the assistant never changes the plan — for what-ifs it offers a button
that creates the scenario for you to evaluate.

## 10. Roles

Planner (plan, edit, publish, import), Production manager (plan, publish, frozen zone, audit), Supervisor
(read plan, report execution, alerts), Operator (own screen and reports), Viewer (read only), Company
admin (users, settings, rules), Integration service (API keys for ERP/MES). The full matrix is in
*Administration → Users & roles*.
