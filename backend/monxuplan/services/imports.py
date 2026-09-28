"""Import wizard: Upload → Mapping → Validation → Preview → Import → Report.

* Formats: Excel (.xlsx), CSV (delimiter and encoding detected) and JSON (list of objects).
* Every entity has a *template*: typed fields, natural key, aliases used to suggest the column
  mapping (English and Spanish headers), references resolved by code.
* Validation never guesses silently: unknown references, invalid numbers/dates, duplicated keys,
  negative quantities… are row errors. By default a file with errors cannot be imported; the
  planner may explicitly choose to import only the valid rows (``skip_invalid_rows``).
* Import is one transaction: either every accepted row is written or none is. Existing records are
  updated by natural key (``mode``: UPSERT, CREATE_ONLY, UPDATE_ONLY). Nothing is deleted by an
  import. One audit record summarises the job.
"""

from __future__ import annotations

import csv
import io
import json
import re
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .. import models as M
from ..core.errors import NotFound, ValidationFailed
from . import audit
from .context import Ctx

MAX_ROWS = 100_000
PREVIEW_ROWS = 50
MAX_REPORTED_ERRORS = 1000


# =============================================================================================
# templates
# =============================================================================================


@dataclass
class F:
    name: str
    type: str = "str"  # str/int/float/bool/date/datetime/time/enum/kv/list/ref
    required: bool = False
    description: str = ""
    aliases: tuple[str, ...] = ()
    enum: tuple[str, ...] = ()
    ref: str | None = None  # lookup kind for type "ref" (and list of refs)
    min: float | None = None


@dataclass
class Template:
    entity: str
    label: str
    fields: list[F]
    key: tuple[str, ...]
    description: str
    read_perm: str = "masterdata:read"
    write_perm: str = "masterdata:write"
    row_check: Callable[[dict[str, Any]], list[tuple[str, str]]] | None = None
    unique_rows: bool = True  # natural key must be unique inside the file

    def field(self, name: str) -> F:
        return next(f for f in self.fields if f.name == name)


def _chk_items(r: dict) -> list[tuple[str, str]]:
    out = []
    if r.get("min_lot") is not None and r.get("max_lot") is not None and r["min_lot"] > r["max_lot"]:
        out.append(("max_lot", "max_lot is smaller than min_lot"))
    return out


def _chk_resource(r: dict) -> list[tuple[str, str]]:
    out = []
    if r.get("capacity") is not None and r["capacity"] < 1:
        out.append(("capacity", "capacity must be at least 1"))
    if r.get("efficiency") is not None and not (0 < r["efficiency"] <= 5):
        out.append(("efficiency", "efficiency must be between 0 and 5 (1 = 100 %)"))
    return out


def _chk_shift(r: dict) -> list[tuple[str, str]]:
    out = []
    if r.get("weekday") is not None and not (0 <= r["weekday"] <= 6):
        out.append(("weekday", "weekday must be 0 (Monday) … 6 (Sunday) or a day name"))
    if r.get("start") is not None and r.get("start") == r.get("end") and r.get("start") != time(0):
        out.append(("end", "shift start and end are equal"))
    return out


def _chk_bom(r: dict) -> list[tuple[str, str]]:
    if r.get("parent_code") and r.get("parent_code") == r.get("component_code"):
        return [("component_code", "an item cannot be a component of itself")]
    return []


def _chk_order(r: dict) -> list[tuple[str, str]]:
    out = []
    if r.get("release_date") and r.get("due_date") and r["release_date"] > r["due_date"]:
        out.append(("release_date", "release date is after the due date"))
    return out


def _chk_downtime(r: dict) -> list[tuple[str, str]]:
    if r.get("start") and r.get("end") and r["end"] <= r["start"]:
        return [("end", "end must be after start")]
    return []


CODE = ("code", "codigo", "cod", "id", "referencia", "ref")
ITEM = ("item", "item_code", "product", "product_code", "part", "part_number", "sku", "articulo", "producto", "referencia", "material", "material_code")

TEMPLATES: dict[str, Template] = {}


def _t(t: Template) -> None:
    TEMPLATES[t.entity] = t


_t(
    Template(
        "items",
        "Items (products and materials)",
        [
            F("code", required=True, description="Unique item code", aliases=CODE + ITEM),
            F("name", required=True, description="Description", aliases=("description", "descripcion", "nombre", "designation", "denominacion")),
            F("item_type", "enum", enum=("FINISHED", "SEMI_FINISHED", "RAW", "PACKAGING"), description="FINISHED / SEMI_FINISHED / RAW / PACKAGING (default by make/buy)", aliases=("type", "tipo")),
            F("make_or_buy", "enum", enum=("MAKE", "BUY"), description="MAKE or BUY (default by type)", aliases=("procurement", "fabricar_comprar", "aprovisionamiento")),
            F("uom", description="Unit of measure (default pcs)", aliases=("unit", "unidad", "um", "udm")),
            F("family", "ref", ref="family", description="Product family code (created if missing)", aliases=("family_code", "familia", "product_family")),
            F("quantity_type", "enum", enum=("INTEGER", "DECIMAL"), description="INTEGER or DECIMAL quantities"),
            F("lot_policy", "enum", enum=("LFL", "FIXED", "MIN", "MULTIPLE", "MAX", "EOQ"), description="Lot sizing policy"),
            F("min_lot", "float", min=0),
            F("max_lot", "float", min=0),
            F("lot_multiple", "float", min=0),
            F("fixed_lot", "float", min=0),
            F("safety_stock", "float", min=0, aliases=("stock_seguridad",)),
            F("purchase_lead_time_days", "float", min=0, aliases=("lead_time", "lead_time_days", "plazo_compra")),
            F("production_lead_time_days", "float", min=0),
            F("unit_cost", "float", min=0, aliases=("cost", "coste")),
            F("unit_price", "float", min=0, aliases=("price", "precio")),
            F("attributes", "kv", description="Setup attributes, e.g. color=RED; grade=S355", aliases=("atributos",)),
            F("color", description="Shortcut for the 'color' attribute", aliases=("colour",)),
        ],
        key=("code",),
        description="One row per item. Codes are the natural key: existing codes are updated.",
        row_check=_chk_items,
    )
)
_t(
    Template(
        "resources",
        "Resources (machines, lines, tools, labour pools)",
        [
            F("code", required=True, aliases=CODE + ("resource", "machine", "maquina", "recurso")),
            F("name", required=True, aliases=("description", "descripcion", "nombre")),
            F("kind", "enum", enum=("MACHINE", "WORK_CENTER", "LINE", "TOOL", "LABOR_POOL", "SUBCONTRACTOR", "STORAGE", "TRANSPORT"), description="Default MACHINE", aliases=("type", "tipo")),
            F("area", "ref", ref="area", description="Planning area code (created if missing)", aliases=("area_code", "seccion", "department")),
            F("work_center", "ref", ref="work_center", aliases=("work_center_code", "centro_trabajo")),
            F("calendar", "ref", ref="calendar", description="Calendar code (plant default if empty)", aliases=("calendar_code", "calendario")),
            F("capacity", "int", min=1, description="Parallel units (default 1)", aliases=("capacidad",)),
            F("efficiency", "float", description="1.0 = 100 %", aliases=("eficiencia", "oee")),
            F("is_finite", "bool", description="Finite capacity (default yes)"),
            F("cost_per_hour", "float", min=0, aliases=("hourly_cost", "coste_hora")),
            F("overtime_cost_per_hour", "float", min=0),
            F("energy_kw", "float", min=0),
            F("detached_setup", "bool", description="Changeover can be done before the material arrives"),
            F("groups", "list", ref="group", description="Resource group codes, comma separated (created if missing)", aliases=("group", "grupo")),
        ],
        key=("code",),
        description="One row per resource of the selected plant.",
        row_check=_chk_resource,
    )
)
_t(
    Template(
        "calendars",
        "Calendars (shift patterns)",
        [
            F("calendar_code", required=True, aliases=("calendar", "code", "calendario")),
            F("calendar_name", aliases=("name", "nombre")),
            F("timezone", description="IANA time zone (default: plant time zone)", aliases=("tz", "zona_horaria")),
            F("weekday", "int", required=True, description="0 = Monday … 6 = Sunday (or day name)", aliases=("day", "dia", "day_of_week")),
            F("shift_code", aliases=("shift", "turno")),
            F("start", "time", required=True, aliases=("start_time", "inicio", "from")),
            F("end", "time", required=True, aliases=("end_time", "fin", "to")),
            F("kind", "enum", enum=("REGULAR", "OVERTIME"), description="REGULAR or OVERTIME"),
            F("breaks", description="Breaks inside the shift, e.g. 10:00-10:20; 12:00-12:30", aliases=("descansos", "pausas")),
        ],
        key=("calendar_code", "weekday", "shift_code"),
        description="One row per shift. The shift pattern of every calendar present in the file is replaced by the file's rows.",
        row_check=_chk_shift,
    )
)
_t(
    Template(
        "customers",
        "Customers",
        [
            F("code", required=True, aliases=CODE + ("customer", "cliente")),
            F("name", required=True, aliases=("nombre", "razon_social")),
            F("priority", "int", min=1, description="1 (highest) … 9", aliases=("prioridad",)),
            F("is_strategic", "bool", aliases=("strategic", "estrategico")),
            F("country", aliases=("pais",)),
            F("safety_time_minutes", "float", min=0),
        ],
        key=("code",),
        description="One row per customer.",
        read_perm="orders:read",
        write_perm="orders:write",
    )
)
_t(
    Template(
        "suppliers",
        "Suppliers",
        [
            F("code", required=True, aliases=CODE + ("supplier", "vendor", "proveedor")),
            F("name", required=True, aliases=("nombre", "razon_social")),
            F("lead_time_days", "float", min=0, aliases=("lead_time", "plazo")),
            F("reliability_pct", "float", min=0),
            F("is_subcontractor", "bool", aliases=("subcontractor", "subcontratista")),
        ],
        key=("code",),
        description="One row per supplier.",
        read_perm="orders:read",
        write_perm="orders:write",
    )
)
_t(
    Template(
        "boms",
        "Bills of materials",
        [
            F("parent_code", "ref", ref="item", required=True, aliases=("parent", "parent_item", "padre", "item", "product", "producto")),
            F("component_code", "ref", ref="item", required=True, aliases=("component", "componente", "child", "hijo", "material")),
            F("quantity_per", "float", required=True, min=0, aliases=("quantity", "qty", "cantidad", "qty_per")),
            F("scrap_pct", "float", min=0, aliases=("scrap", "merma")),
            F("operation_seq", "int", description="Routing operation sequence that consumes the component", aliases=("operation", "op_seq", "operacion")),
            F("base_quantity", "float", min=0, description="Quantity of parent the lines refer to (default 1)"),
            F("version_code", description="BOM version (default 1)", aliases=("version",)),
        ],
        key=("parent_code", "component_code", "operation_seq"),
        description="One row per BOM line. The lines of every BOM present in the file are replaced by the file's rows.",
        row_check=_chk_bom,
    )
)
_t(
    Template(
        "routings",
        "Routings (operations and resources)",
        [
            F("item_code", "ref", ref="item", required=True, aliases=ITEM),
            F("seq", "int", required=True, min=0, aliases=("sequence", "operation_seq", "secuencia", "op", "fase")),
            F("operation_code", aliases=("op_code", "code", "codigo_operacion")),
            F("operation_name", required=True, aliases=("operation", "name", "descripcion", "operacion")),
            F("resource_code", "ref", ref="resource", required=True, description="Primary resource", aliases=("resource", "machine", "work_center", "maquina", "recurso")),
            F("alternative_resources", "list", ref="resource", description="Alternative resource codes, comma separated", aliases=("alternatives", "alternativas")),
            F("setup_minutes", "float", min=0, aliases=("setup", "preparacion", "setup_time")),
            F("run_minutes_per_unit", "float", min=0, aliases=("run_time", "cycle_time", "tiempo_unitario", "run")),
            F("fixed_minutes", "float", min=0),
            F("batch_size", "float", min=0),
            F("minutes_per_batch", "float", min=0),
            F("teardown_minutes", "float", min=0),
            F("queue_minutes", "float", min=0),
            F("move_minutes", "float", min=0, aliases=("transport_minutes",)),
            F("wait_minutes", "float", min=0, aliases=("cooling_minutes", "curing_minutes")),
            F("overlap_percent", "float", min=0),
            F("transfer_batch", "float", min=0),
            F("interruptible", "bool", description="Can pause over non-working time (default yes)"),
            F("labor_pool", "ref", ref="labor_pool", description="Labour pool code", aliases=("labor_pool_code", "operators")),
            F("labor_units", "int", min=0),
            F("tool", "ref", ref="tool", description="Tool code", aliases=("tool_code", "utillaje")),
            F("setup_attributes", "kv", description="e.g. family=HOUSING; color=RED"),
            F("instructions", aliases=("notes", "instrucciones")),
            F("version_code", aliases=("version",)),
        ],
        key=("item_code", "seq"),
        description="One row per routing operation. Operations are updated by (item, sequence); resources of imported operations are replaced.",
    )
)
_t(
    Template(
        "production-orders",
        "Production orders",
        [
            F("number", required=True, aliases=("order", "order_number", "orden", "of", "work_order", "numero")),
            F("item_code", "ref", ref="item", required=True, aliases=ITEM),
            F("quantity", "float", required=True, min=0, aliases=("qty", "cantidad")),
            F("due_date", "datetime", required=True, aliases=("due", "fecha_entrega", "delivery_date", "deadline")),
            F("release_date", "datetime", aliases=("release", "start_not_before", "fecha_lanzamiento", "earliest_start")),
            F("priority", "int", min=1, description="1 (highest) … 9 (default 5)", aliases=("prioridad",)),
            F("customer_code", "ref", ref="customer", aliases=("customer", "cliente")),
            F("expedite", "bool", aliases=("urgent", "urgente", "rush")),
            F("status", "enum", enum=("PLANNED", "FIRMED", "RELEASED", "IN_PRODUCTION", "BLOCKED", "COMPLETED", "CANCELLED"), description="Default PLANNED"),
            F("erp_ref", aliases=("erp", "external_id")),
            F("notes", aliases=("comments", "observaciones")),
        ],
        key=("number",),
        description="One row per order. New orders get their operations from the item's active routing.",
        read_perm="orders:read",
        write_perm="orders:write",
        row_check=_chk_order,
    )
)
_t(
    Template(
        "inventory",
        "Inventory (on hand)",
        [
            F("item_code", "ref", ref="item", required=True, aliases=ITEM),
            F("location", aliases=("warehouse", "almacen", "ubicacion")),
            F("on_hand", "float", required=True, min=0, aliases=("quantity", "qty", "stock", "cantidad")),
            F("reserved", "float", min=0, aliases=("reservado",)),
            F("blocked", "float", min=0, aliases=("bloqueado",)),
            F("quality_hold", "float", min=0, aliases=("quality", "calidad")),
        ],
        key=("item_code", "location"),
        description="Stock per item and location of the selected plant (replaces the quantities of the rows present).",
        read_perm="orders:read",
        write_perm="orders:write",
    )
)
_t(
    Template(
        "purchase-orders",
        "Purchase orders (open receipts)",
        [
            F("number", required=True, aliases=("po", "po_number", "pedido", "purchase_order")),
            F("line_no", "int", min=0, aliases=("line", "linea", "position")),
            F("supplier_code", "ref", ref="supplier", required=True, aliases=("supplier", "vendor", "proveedor")),
            F("item_code", "ref", ref="item", required=True, aliases=ITEM),
            F("quantity", "float", required=True, min=0, aliases=("qty", "cantidad")),
            F("received_quantity", "float", min=0, aliases=("received", "recibido")),
            F("expected_date", "datetime", required=True, aliases=("expected", "delivery_date", "fecha_prevista", "eta")),
            F("confirmed", "bool", aliases=("confirmado",)),
        ],
        key=("number", "line_no"),
        description="One row per purchase order line.",
        read_perm="orders:read",
        write_perm="orders:write",
    )
)
_t(
    Template(
        "demand",
        "Demand (forecast and independent demand)",
        [
            F("item_code", "ref", ref="item", required=True, aliases=ITEM),
            F("period_start", "date", required=True, aliases=("period", "date", "fecha", "week", "month")),
            F("quantity", "float", required=True, min=0, aliases=("qty", "cantidad", "forecast")),
            F("demand_type", "enum", enum=("FORECAST", "SAFETY", "INTERPLANT", "CUSTOMER"), description="Default FORECAST", aliases=("type", "tipo")),
            F("source", aliases=("origen",)),
        ],
        key=("item_code", "period_start", "demand_type"),
        description="One row per item and period.",
        read_perm="orders:read",
        write_perm="orders:write",
    )
)
_t(
    Template(
        "downtimes",
        "Maintenance and downtime",
        [
            F("resource_code", "ref", ref="resource", required=True, aliases=("resource", "machine", "maquina")),
            F("start", "datetime", required=True, aliases=("from", "inicio")),
            F("end", "datetime", required=True, aliases=("to", "fin")),
            F("kind", "enum", enum=("PREVENTIVE", "CORRECTIVE", "PLANNED", "UNPLANNED"), description="Default PLANNED", aliases=("type", "tipo")),
            F("description", aliases=("reason", "motivo")),
        ],
        key=("resource_code", "start"),
        description="Planned maintenance windows (the resource is unavailable).",
        row_check=_chk_downtime,
    )
)


# =============================================================================================
# parsing
# =============================================================================================


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower().strip()
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


def _cell(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, time):
        return v.strftime("%H:%M")
    if isinstance(v, float) and v.is_integer() and abs(v) < 1e15:
        return int(v)
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


def parse_file(data: bytes, filename: str, sheet: str | None = None) -> tuple[str, list[str], list[list[Any]], list[str]]:
    """→ (format, columns, rows, sheet names)."""
    name = filename.lower()
    if name.endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook

        try:
            wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception as exc:  # noqa: BLE001
            raise ValidationFailed(f"The Excel file could not be read ({exc.__class__.__name__}). Save it as .xlsx and try again.", code="BAD_FILE") from exc
        sheets = wb.sheetnames
        ws = wb[sheet] if sheet and sheet in sheets else wb[sheets[0]]
        it = ws.iter_rows(values_only=True)
        header: list[Any] | None = None
        rows: list[list[Any]] = []
        for r in it:
            vals = [_cell(v) for v in r]
            if header is None:
                if any(v is not None for v in vals):
                    header = vals
                continue
            if any(v is not None for v in vals):
                rows.append(vals)
            if len(rows) > MAX_ROWS:
                raise ValidationFailed(f"The file has more than {MAX_ROWS} rows. Split it or use the API.", code="TOO_MANY_ROWS")
        wb.close()
        if header is None:
            raise ValidationFailed("The sheet is empty.", code="EMPTY_FILE")
        cols = [str(h).strip() if h is not None else f"column_{i + 1}" for i, h in enumerate(header)]
        rows = [(r + [None] * len(cols))[: len(cols)] for r in rows]
        return "xlsx", cols, rows, sheets
    if name.endswith(".json"):
        try:
            obj = json.loads(data.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed(f"Invalid JSON: {exc}", code="BAD_FILE") from exc
        if isinstance(obj, dict):
            obj = obj.get("rows") or obj.get("items") or obj.get("data") or []
        if not isinstance(obj, list) or not all(isinstance(x, dict) for x in obj):
            raise ValidationFailed("JSON must be a list of objects (or {\"rows\": [...]})", code="BAD_FILE")
        cols: list[str] = []
        for x in obj:
            for k in x:
                if k not in cols:
                    cols.append(k)
        rows = [[_cell(x.get(c)) if not isinstance(x.get(c), dict | list) else json.dumps(x.get(c)) for c in cols] for x in obj]
        if len(rows) > MAX_ROWS:
            raise ValidationFailed(f"The file has more than {MAX_ROWS} rows.", code="TOO_MANY_ROWS")
        return "json", cols, rows, []
    if name.endswith((".csv", ".txt", ".tsv")):
        text = None
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        assert text is not None
        try:
            dialect = csv.Sniffer().sniff(text[:20000], delimiters=",;\t|")
            delim = dialect.delimiter
        except csv.Error:
            delim = ";" if text.split("\n", 1)[0].count(";") > text.split("\n", 1)[0].count(",") else ","
        reader = csv.reader(io.StringIO(text), delimiter=delim)
        all_rows = [r for r in reader if any(c.strip() for c in r)]
        if not all_rows:
            raise ValidationFailed("The file is empty.", code="EMPTY_FILE")
        cols = [c.strip() or f"column_{i + 1}" for i, c in enumerate(all_rows[0])]
        rows = [[_cell(v) for v in (r + [""] * len(cols))[: len(cols)]] for r in all_rows[1:]]
        if len(rows) > MAX_ROWS:
            raise ValidationFailed(f"The file has more than {MAX_ROWS} rows.", code="TOO_MANY_ROWS")
        return "csv", cols, rows, []
    raise ValidationFailed("Unsupported file type. Use .xlsx, .csv or .json.", code="UNSUPPORTED_FORMAT")


def suggest_mapping(tpl: Template, columns: list[str]) -> dict[str, str | None]:
    norm_cols = {_norm(c): c for c in columns}
    used: set[str] = set()
    out: dict[str, str | None] = {}
    # exact field names first, then aliases (so "code" beats "item" as alias of another field)
    for f in tpl.fields:
        c = norm_cols.get(_norm(f.name))
        if c and c not in used:
            out[f.name] = c
            used.add(c)
    for f in tpl.fields:
        if f.name in out:
            continue
        for a in f.aliases:
            c = norm_cols.get(_norm(a))
            if c and c not in used:
                out[f.name] = c
                used.add(c)
                break
        else:
            out[f.name] = None
    return out


# =============================================================================================
# value conversion
# =============================================================================================

DAY_NAMES = {
    **{d: i for i, d in enumerate(["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"])},
    **{d: i for i, d in enumerate(["mon", "tue", "wed", "thu", "fri", "sat", "sun"])},
    **{d: i for i, d in enumerate(["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"])},
    **{d: i for i, d in enumerate(["lun", "mar", "mie", "jue", "vie", "sab", "dom"])},
}
ENUM_SYNONYMS = {
    "FG": "FINISHED", "PT": "FINISHED", "PRODUCTO_TERMINADO": "FINISHED", "SEMI": "SEMI_FINISHED", "SF": "SEMI_FINISHED", "SEMIELABORADO": "SEMI_FINISHED",
    "RM": "RAW", "MP": "RAW", "MATERIA_PRIMA": "RAW", "RAW_MATERIAL": "RAW", "COMPONENT": "RAW", "PACK": "PACKAGING", "EMBALAJE": "PACKAGING",
    "FABRICAR": "MAKE", "M": "MAKE", "COMPRAR": "BUY", "B": "BUY", "P": "BUY", "PURCHASE": "BUY",
}


class ConvError(ValueError):
    pass


def _to_float(v: Any) -> float:
    if isinstance(v, bool):
        raise ConvError("not a number")
    if isinstance(v, int | float):
        return float(v)
    s = str(v).strip().replace(" ", "").replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError as exc:
        raise ConvError(f"'{v}' is not a number") from exc


def _to_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, int | float):
        return v != 0
    s = _norm(str(v))
    if s in ("1", "true", "yes", "y", "si", "s", "x", "verdadero", "oui", "ja", "sim"):
        return True
    if s in ("0", "false", "no", "n", "falso", "non", "nein", "nao"):
        return False
    raise ConvError(f"'{v}' is not yes/no")


_DT_FORMATS_DMY = ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y", "%d-%m-%Y %H:%M", "%d-%m-%Y", "%d.%m.%Y %H:%M", "%d.%m.%Y", "%d/%m/%y")
_DT_FORMATS_MDY = ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y")


def _to_datetime(v: Any, tz: ZoneInfo, date_format: str) -> datetime:
    if isinstance(v, int | float) and 20000 < float(v) < 80000:  # Excel serial date
        dt = datetime(1899, 12, 30) + timedelta(days=float(v))
    else:
        s = str(v).strip()
        dt = None
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            for fmt in _DT_FORMATS_MDY if date_format == "MDY" else _DT_FORMATS_DMY:
                try:
                    dt = datetime.strptime(s, fmt)
                    break
                except ValueError:
                    continue
        if dt is None:
            raise ConvError(f"'{v}' is not a date (use YYYY-MM-DD[ HH:MM] or {'MM/DD/YYYY' if date_format == 'MDY' else 'DD/MM/YYYY'})")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(UTC)


def _to_time(v: Any) -> time:
    if isinstance(v, int | float) and 0 <= float(v) < 1:  # Excel time fraction
        m = round(float(v) * 1440)
        return time(m // 60 % 24, m % 60)
    s = str(v).strip()
    m = re.fullmatch(r"(\d{1,2})[:h.]?(\d{2})?(?::(\d{2}))?", s)
    if not m:
        raise ConvError(f"'{v}' is not a time (HH:MM)")
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if h == 24 and mi == 0:
        return time(0)
    if not (0 <= h < 24 and 0 <= mi < 60):
        raise ConvError(f"'{v}' is not a valid time")
    return time(h, mi)


def _to_kv(v: Any) -> dict[str, Any]:
    if isinstance(v, dict):
        return v
    s = str(v).strip()
    if s.startswith("{"):
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError as exc:
            raise ConvError("invalid JSON object") from exc
    out = {}
    for part in re.split(r"[;|]", s):
        if not part.strip():
            continue
        if "=" not in part and ":" not in part:
            raise ConvError(f"'{part.strip()}' must be key=value")
        k, val = re.split(r"[=:]", part, maxsplit=1)
        out[k.strip()] = val.strip()
    return out


def convert(f: F, raw: Any, tz: ZoneInfo, date_format: str) -> Any:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    t = f.type
    if t in ("str", "ref"):
        v: Any = str(int(raw)) if isinstance(raw, float) and raw.is_integer() else str(raw).strip()
    elif t == "int":
        if f.name == "weekday" and isinstance(raw, str) and _norm(raw) in DAY_NAMES:
            return DAY_NAMES[_norm(raw)]
        x = _to_float(raw)
        if not float(x).is_integer():
            raise ConvError(f"'{raw}' must be a whole number")
        v = int(x)
    elif t == "float":
        v = _to_float(raw)
    elif t == "bool":
        v = _to_bool(raw)
    elif t in ("date", "datetime"):
        dt = _to_datetime(raw, tz, date_format)
        v = dt.astimezone(tz).date() if t == "date" else dt
    elif t == "time":
        v = _to_time(raw)
    elif t == "enum":
        s = _norm(str(raw)).upper()
        s = ENUM_SYNONYMS.get(s, s)
        if s not in f.enum:
            raise ConvError(f"'{raw}' is not one of {', '.join(f.enum)}")
        v = s
    elif t == "kv":
        v = _to_kv(raw)
    elif t == "list":
        v = [x.strip() for x in re.split(r"[,;|]", str(raw)) if x.strip()]
    else:
        v = raw
    if f.min is not None and isinstance(v, int | float) and not isinstance(v, bool) and v < f.min:
        raise ConvError(f"must be ≥ {f.min:g}")
    return v


# =============================================================================================
# reference lookups
# =============================================================================================

CREATABLE_REFS = {"family", "area", "group"}


class Lookups:
    def __init__(self, s: Session, plant: M.Plant):
        self.s = s
        self.plant = plant
        self.cache: dict[str, dict[str, uuid.UUID]] = {}

    def _load(self, kind: str) -> dict[str, uuid.UUID]:
        if kind in self.cache:
            return self.cache[kind]
        s, pid = self.s, self.plant.id
        q = {
            "item": select(M.Item.code, M.Item.id),
            "resource": select(M.Resource.code, M.Resource.id).where(M.Resource.plant_id == pid, M.Resource.kind.notin_(["TOOL", "LABOR_POOL"])),
            "tool": select(M.Resource.code, M.Resource.id).where(M.Resource.plant_id == pid, M.Resource.kind == "TOOL"),
            "customer": select(M.Customer.code, M.Customer.id),
            "supplier": select(M.Supplier.code, M.Supplier.id),
            "calendar": select(M.Calendar.code, M.Calendar.id),
            "family": select(M.ProductFamily.code, M.ProductFamily.id),
            "area": select(M.PlanningArea.code, M.PlanningArea.id).where(M.PlanningArea.plant_id == pid),
            "work_center": select(M.WorkCenter.code, M.WorkCenter.id).where(M.WorkCenter.plant_id == pid),
            "labor_pool": select(M.LaborPool.code, M.LaborPool.id).where(M.LaborPool.plant_id == pid),
            "group": select(M.ResourceGroup.code, M.ResourceGroup.id),
        }[kind]
        d = {str(c): i for c, i in s.execute(q)}
        self.cache[kind] = d
        return d

    def get(self, kind: str, code: str) -> uuid.UUID | None:
        d = self._load(kind)
        if code in d:
            return d[code]
        low = code.lower()
        for c, i in d.items():
            if c.lower() == low:
                return i
        return None

    def add(self, kind: str, code: str, id_: uuid.UUID) -> None:
        self._load(kind)[code] = id_


# =============================================================================================
# workflow
# =============================================================================================


def _job(s: Session, ctx: Ctx, job_id: uuid.UUID) -> M.ImportJob:
    job = s.get(M.ImportJob, job_id)
    if job is None:
        raise NotFound("Import job not found", code="IMPORT_NOT_FOUND")
    tpl = TEMPLATES[job.entity]
    ctx.require("integration:import")
    ctx.require(tpl.write_perm)
    return job


def _plant(s: Session, ctx: Ctx, job: M.ImportJob) -> M.Plant:
    pid = (job.options or {}).get("plant_id")
    plant = s.get(M.Plant, uuid.UUID(pid)) if pid else s.scalar(select(M.Plant).order_by(M.Plant.code).limit(1))
    if plant is None:
        raise ValidationFailed("No plant selected", code="NO_PLANT")
    ctx.require_plant(plant.id)
    return plant


def job_dict(job: M.ImportJob, detail: bool = False) -> dict[str, Any]:
    out = {
        "id": str(job.id),
        "entity": job.entity,
        "entity_label": TEMPLATES[job.entity].label if job.entity in TEMPLATES else job.entity,
        "filename": job.filename,
        "format": job.file_format,
        "status": job.status,
        "source": job.source,
        "rows": len(job.rows or []),
        "stats": job.stats or {},
        "options": job.options or {},
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "created_by": job.created_by,
    }
    if detail:
        tpl = TEMPLATES.get(job.entity)
        out.update(
            columns=job.columns,
            mapping=job.mapping,
            fields=[{"name": f.name, "type": f.type, "required": f.required, "description": f.description, "enum": list(f.enum), "ref": f.ref} for f in tpl.fields] if tpl else [],
            sample=[dict(zip(job.columns, r, strict=False)) for r in (job.rows or [])[:10]],
            errors=(job.errors or [])[:MAX_REPORTED_ERRORS],
            warnings=(job.warnings or [])[:MAX_REPORTED_ERRORS],
            preview=(job.stats or {}).get("preview", []),
        )
        out["stats"] = {k: v for k, v in (job.stats or {}).items() if k != "preview"}
    return out


def upload(s: Session, ctx: Ctx, entity: str, filename: str, data: bytes, options: dict[str, Any] | None = None, source: str = "UPLOAD") -> dict[str, Any]:
    ctx.require("integration:import")
    tpl = TEMPLATES.get(entity)
    if tpl is None:
        raise ValidationFailed(f"Unknown import type '{entity}'. Available: {', '.join(TEMPLATES)}", code="UNKNOWN_ENTITY")
    ctx.require(tpl.write_perm)
    opts = dict(options or {})
    fmt, cols, rows, sheets = parse_file(data, filename, opts.get("sheet"))
    if not rows:
        raise ValidationFailed("The file has a header but no data rows.", code="EMPTY_FILE")
    job = M.ImportJob(tenant_id=ctx.tenant_id, entity=entity, filename=filename[:300], file_format=fmt, status="UPLOADED", columns=cols, rows=rows, options={**opts, "sheets": sheets}, source=source)
    job.mapping = suggest_mapping(tpl, cols)
    s.add(job)
    s.flush()
    return job_dict(job, detail=True)


def set_mapping(s: Session, ctx: Ctx, job_id: uuid.UUID, mapping: dict[str, str | None], options: dict[str, Any] | None = None) -> dict[str, Any]:
    job = _job(s, ctx, job_id)
    if job.status == "IMPORTED":
        raise ValidationFailed("This import was already committed.", code="ALREADY_IMPORTED")
    tpl = TEMPLATES[job.entity]
    names = {f.name for f in tpl.fields}
    bad = [k for k in mapping if k not in names]
    if bad:
        raise ValidationFailed(f"Unknown field(s): {', '.join(bad)}", code="UNKNOWN_FIELD")
    missing_cols = [c for c in mapping.values() if c and c not in job.columns]
    if missing_cols:
        raise ValidationFailed(f"Column(s) not in the file: {', '.join(missing_cols)}", code="UNKNOWN_COLUMN")
    job.mapping = {f.name: mapping.get(f.name) for f in tpl.fields}
    if options:
        job.options = {**(job.options or {}), **options}
    job.status = "MAPPED"
    job.errors, job.warnings, job.stats = [], [], {}
    return job_dict(job, detail=True)


def _converted_rows(s: Session, job: M.ImportJob, plant: M.Plant) -> tuple[list[tuple[int, dict[str, Any]]], list[dict[str, Any]], list[dict[str, Any]]]:
    """→ ([(file row, values)], errors, warnings). Values use typed Python objects; refs are resolved
    to ids under '<field>__id'."""
    tpl = TEMPLATES[job.entity]
    tz = ZoneInfo(plant.timezone)
    date_format = (job.options or {}).get("date_format", "DMY")
    col_idx = {c: i for i, c in enumerate(job.columns)}
    fmap = {f.name: col_idx.get(job.mapping.get(f.name)) if job.mapping.get(f.name) else None for f in tpl.fields}
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    missing_required = [f.name for f in tpl.fields if f.required and fmap.get(f.name) is None]
    if missing_required:
        errors.append({"row": None, "field": ",".join(missing_required), "value": None, "message": f"Required field(s) not mapped: {', '.join(missing_required)}"})
        return [], errors, warnings
    lk = Lookups(s, plant)
    out: list[tuple[int, dict[str, Any]]] = []
    seen: dict[tuple, int] = {}
    to_create: dict[str, set[str]] = {}
    for n, raw in enumerate(job.rows or [], start=2):
        vals: dict[str, Any] = {}
        row_errs: list[dict[str, Any]] = []
        for f in tpl.fields:
            idx = fmap.get(f.name)
            rv = raw[idx] if idx is not None and idx < len(raw) else None
            try:
                v = convert(f, rv, tz, date_format)
            except ConvError as exc:
                row_errs.append({"row": n, "field": f.name, "value": rv, "message": str(exc)})
                continue
            if v is None:
                if f.required:
                    row_errs.append({"row": n, "field": f.name, "value": rv, "message": "required value is empty"})
                continue
            vals[f.name] = v
            if f.ref and f.type == "ref":
                rid = lk.get(f.ref, v)
                if rid is None:
                    if f.ref in CREATABLE_REFS:
                        to_create.setdefault(f.ref, set()).add(v)
                    else:
                        row_errs.append({"row": n, "field": f.name, "value": rv, "message": f"unknown {f.ref.replace('_', ' ')} '{v}'"})
                        continue
                vals[f.name + "__id"] = rid
            elif f.ref and f.type == "list":
                ids = []
                for code in v:
                    rid = lk.get(f.ref, code)
                    if rid is None:
                        if f.ref in CREATABLE_REFS:
                            to_create.setdefault(f.ref, set()).add(code)
                        else:
                            row_errs.append({"row": n, "field": f.name, "value": code, "message": f"unknown {f.ref.replace('_', ' ')} '{code}'"})
                    ids.append(rid)
                vals[f.name + "__ids"] = ids
        if not row_errs and tpl.row_check:
            for fld, msg in tpl.row_check(vals):
                row_errs.append({"row": n, "field": fld, "value": vals.get(fld), "message": msg})
        if not row_errs and tpl.unique_rows:
            key = tuple(str(vals.get(k)).lower() if vals.get(k) is not None else None for k in tpl.key)
            if key in seen:
                row_errs.append({"row": n, "field": ",".join(tpl.key), "value": "/".join(str(x) for x in key), "message": f"duplicated in the file (first at row {seen[key]})"})
            else:
                seen[key] = n
        if row_errs:
            errors.extend(row_errs)
        else:
            out.append((n, vals))
    for kind, codes in to_create.items():
        warnings.append({"row": None, "field": kind, "value": ", ".join(sorted(codes)[:20]), "message": f"{len(codes)} new {kind.replace('_', ' ')}(s) will be created: {', '.join(sorted(codes)[:10])}{'…' if len(codes) > 10 else ''}"})
    return out, errors, warnings


def _existing_keys(s: Session, job: M.ImportJob, plant: M.Plant, rows: list[tuple[int, dict[str, Any]]]) -> set[tuple]:
    """Natural keys (lower-cased) that already exist → UPDATE."""
    e = job.entity
    if e == "items":
        return {(c.lower(),) for c in s.scalars(select(M.Item.code))}
    if e == "resources":
        return {(c.lower(),) for c in s.scalars(select(M.Resource.code))}
    if e == "customers":
        return {(c.lower(),) for c in s.scalars(select(M.Customer.code))}
    if e == "suppliers":
        return {(c.lower(),) for c in s.scalars(select(M.Supplier.code))}
    if e == "production-orders":
        return {(c.lower(),) for c in s.scalars(select(M.ProductionOrder.number))}
    if e == "purchase-orders":
        return {(n.lower(), str(ln)) for n, ln in s.execute(select(M.PurchaseOrder.number, M.PurchaseOrderLine.line_no).join(M.PurchaseOrderLine, M.PurchaseOrderLine.purchase_order_id == M.PurchaseOrder.id))}
    if e == "inventory":
        codes = {i: c for c, i in s.execute(select(M.Item.code, M.Item.id))}
        return {(codes.get(i, "").lower(), (loc or "").lower()) for i, loc in s.execute(select(M.Inventory.item_id, M.Inventory.location).where(M.Inventory.plant_id == plant.id))}
    if e == "routings":
        codes = {i: c for c, i in s.execute(select(M.Item.code, M.Item.id))}
        return {(codes.get(i, "").lower(), str(seq)) for i, seq in s.execute(select(M.Routing.item_id, M.RoutingOperation.seq).join(M.RoutingOperation, M.RoutingOperation.routing_id == M.Routing.id).where(M.Routing.is_active.is_(True)))}
    if e == "calendars":
        return {(c.lower(),) for c in s.scalars(select(M.Calendar.code))}
    if e == "boms":
        codes = {i: c for c, i in s.execute(select(M.Item.code, M.Item.id))}
        return {(codes.get(i, "").lower(),) for i in s.scalars(select(M.Bom.item_id).where(M.Bom.is_active.is_(True)))}
    return set()


def _row_key(job: M.ImportJob, vals: dict[str, Any]) -> tuple:
    e = job.entity
    if e in ("items", "resources", "customers", "suppliers"):
        return (str(vals["code"]).lower(),)
    if e == "production-orders":
        return (str(vals["number"]).lower(),)
    if e == "purchase-orders":
        return (str(vals["number"]).lower(), str(vals.get("line_no") or 1))
    if e == "inventory":
        return (str(vals["item_code"]).lower(), str(vals.get("location") or "MAIN").lower())
    if e == "routings":
        return (str(vals["item_code"]).lower(), str(vals["seq"]))
    if e == "calendars":
        return (str(vals["calendar_code"]).lower(),)
    if e == "boms":
        return (str(vals["parent_code"]).lower(),)
    return ()


def _preview_value(v: Any) -> Any:
    if isinstance(v, datetime | date | time):
        return v.isoformat()
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, list):
        return [_preview_value(x) for x in v]
    return v


def validate(s: Session, ctx: Ctx, job_id: uuid.UUID) -> dict[str, Any]:
    job = _job(s, ctx, job_id)
    if job.status == "IMPORTED":
        raise ValidationFailed("This import was already committed.", code="ALREADY_IMPORTED")
    plant = _plant(s, ctx, job)
    rows, errors, warnings = _converted_rows(s, job, plant)
    existing = _existing_keys(s, job, plant, rows)
    mode = (job.options or {}).get("mode", "UPSERT")
    creates = updates = skipped = 0
    preview = []
    for n, vals in rows:
        exists = _row_key(job, vals) in existing
        action = "UPDATE" if exists else "CREATE"
        if job.entity in ("calendars", "boms") and exists:
            action = "REPLACE"
        if mode == "CREATE_ONLY" and exists or mode == "UPDATE_ONLY" and not exists:
            action = "SKIP"
            skipped += 1
        elif action == "CREATE":
            creates += 1
        else:
            updates += 1
        if len(preview) < PREVIEW_ROWS:
            preview.append({"row": n, "action": action, "values": {k: _preview_value(v) for k, v in vals.items() if not k.endswith(("__id", "__ids"))}})
    if mode == "UPDATE_ONLY" and skipped:
        warnings.append({"row": None, "field": None, "value": skipped, "message": f"{skipped} row(s) do not exist yet and will be skipped (mode UPDATE_ONLY)"})
    if mode == "CREATE_ONLY" and skipped:
        warnings.append({"row": None, "field": None, "value": skipped, "message": f"{skipped} row(s) already exist and will be skipped (mode CREATE_ONLY)"})
    error_rows = len({e["row"] for e in errors if e["row"] is not None})
    blocking = bool(errors) and not (job.options or {}).get("skip_invalid_rows") or any(e["row"] is None for e in errors)
    job.errors = errors[:MAX_REPORTED_ERRORS]
    job.warnings = warnings[:MAX_REPORTED_ERRORS]
    job.stats = {
        "rows": len(job.rows or []),
        "valid_rows": len(rows),
        "error_rows": error_rows,
        "errors": len(errors),
        "warnings": len(warnings),
        "to_create": creates,
        "to_update": updates,
        "to_skip": skipped,
        "can_import": not blocking and len(rows) > 0,
        "blocking_reason": ("Fix the errors or choose to import only the valid rows." if errors else None) if blocking else None,
        "preview": preview,
    }
    job.status = "VALIDATED" if not blocking else "INVALID"
    return job_dict(job, detail=True)


def commit(s: Session, ctx: Ctx, job_id: uuid.UUID) -> dict[str, Any]:
    job = _job(s, ctx, job_id)
    if job.status == "IMPORTED":
        raise ValidationFailed("This import was already committed.", code="ALREADY_IMPORTED")
    validate(s, ctx, job_id)
    if not job.stats.get("can_import"):
        raise ValidationFailed(job.stats.get("blocking_reason") or "Nothing to import.", code="IMPORT_BLOCKED", context={"errors": job.stats.get("errors")})
    plant = _plant(s, ctx, job)
    rows, _errors, _warnings = _converted_rows(s, job, plant)
    mode = (job.options or {}).get("mode", "UPSERT")
    ap = _Applier(s, ctx, plant, mode)
    with s.begin_nested():
        getattr(ap, "apply_" + job.entity.replace("-", "_"))([v for _n, v in rows])
        s.flush()
    stats = {k: v for k, v in job.stats.items() if k != "preview"}
    stats.update(created=ap.created, updated=ap.updated, skipped=ap.skipped, related_created=ap.related, operations_generated=ap.ops_generated)
    job.stats = stats
    job.status = "IMPORTED"
    audit.record(s, ctx, "IMPORT", "import_job", job.id, f"{job.entity}: {job.filename}", after={k: v for k, v in stats.items() if isinstance(v, int | str | bool | dict)}, reason=(job.options or {}).get("reason"))
    return job_dict(job, detail=True)


def list_jobs(s: Session, ctx: Ctx, limit: int = 50) -> list[dict[str, Any]]:
    ctx.require("integration:import")
    return [job_dict(j) for j in s.scalars(select(M.ImportJob).order_by(M.ImportJob.created_at.desc()).limit(limit))]


def get_job(s: Session, ctx: Ctx, job_id: uuid.UUID) -> dict[str, Any]:
    return job_dict(_job(s, ctx, job_id), detail=True)


# =============================================================================================
# appliers (one per template)
# =============================================================================================


class _Applier:
    def __init__(self, s: Session, ctx: Ctx, plant: M.Plant, mode: str):
        self.s, self.ctx, self.plant, self.mode = s, ctx, plant, mode
        self.created = self.updated = self.skipped = self.ops_generated = 0
        self.related: dict[str, int] = {}
        self.lk = Lookups(s, plant)

    # helpers -----------------------------------------------------------------------------
    def _decide(self, obj: Any) -> bool:
        """True if the row must be written."""
        if obj is None and self.mode == "UPDATE_ONLY" or obj is not None and self.mode == "CREATE_ONLY":
            self.skipped += 1
            return False
        return True

    def _set(self, obj: Any, vals: dict[str, Any], fields: list[str], rename: dict[str, str] | None = None) -> None:
        rename = rename or {}
        for f in fields:
            if f in vals:
                setattr(obj, rename.get(f, f), vals[f])

    def _count(self, created: bool) -> None:
        if created:
            self.created += 1
        else:
            self.updated += 1

    def _ref(self, kind: str, code: str | None) -> uuid.UUID | None:
        if not code:
            return None
        rid = self.lk.get(kind, code)
        if rid is not None:
            return rid
        s, t = self.s, self.ctx.tenant_id
        if kind == "family":
            obj = M.ProductFamily(tenant_id=t, code=code, name=code)
        elif kind == "area":
            n = s.scalar(select(func.count()).select_from(M.PlanningArea).where(M.PlanningArea.plant_id == self.plant.id)) or 0
            obj = M.PlanningArea(tenant_id=t, plant_id=self.plant.id, code=code, name=code, sort_order=(n + 1) * 10)
        elif kind == "group":
            obj = M.ResourceGroup(tenant_id=t, plant_id=self.plant.id, code=code, name=code)
        else:
            raise ValidationFailed(f"Unknown {kind} '{code}'")
        s.add(obj)
        s.flush()
        self.lk.add(kind, code, obj.id)
        self.related[kind] = self.related.get(kind, 0) + 1
        return obj.id

    def _by_code(self, model: Any, code: str, *where: Any) -> Any:
        obj = self.s.scalar(select(model).where(model.code == code, *where))
        if obj is None:
            obj = self.s.scalar(select(model).where(func.lower(model.code) == code.lower(), *where))
        return obj

    # entities ----------------------------------------------------------------------------
    def apply_items(self, rows: list[dict]) -> None:
        for v in rows:
            obj = self._by_code(M.Item, v["code"])
            if not self._decide(obj):
                continue
            created = obj is None
            if created:
                obj = M.Item(tenant_id=self.ctx.tenant_id, code=v["code"])
                self.s.add(obj)
            t = v.get("item_type") or (obj.item_type if not created else None)
            mob = v.get("make_or_buy") or (obj.make_or_buy if not created else None)
            if t is None:
                t = "RAW" if mob == "BUY" else "FINISHED"
            if mob is None:
                mob = "BUY" if t in ("RAW", "PACKAGING") else "MAKE"
            obj.item_type, obj.make_or_buy = t, mob
            self._set(obj, v, ["name", "uom", "quantity_type", "lot_policy", "min_lot", "max_lot", "lot_multiple", "fixed_lot", "safety_stock", "purchase_lead_time_days", "production_lead_time_days", "unit_cost", "unit_price"])
            if created and not v.get("uom"):
                obj.uom = "pcs"
            if "family" in v:
                obj.family_id = self._ref("family", v["family"])
            attrs = dict(obj.attributes or {})
            attrs.update(v.get("attributes") or {})
            if v.get("color"):
                attrs["color"] = v["color"]
            obj.attributes = attrs
            self._count(created)
            self.s.flush()
            self.lk.add("item", obj.code, obj.id)

    def apply_resources(self, rows: list[dict]) -> None:
        for v in rows:
            obj = self._by_code(M.Resource, v["code"])
            if obj is not None and obj.plant_id != self.plant.id:
                raise ValidationFailed(f"Resource {v['code']} belongs to another plant", code="WRONG_PLANT")
            if not self._decide(obj):
                continue
            created = obj is None
            if created:
                obj = M.Resource(tenant_id=self.ctx.tenant_id, plant_id=self.plant.id, code=v["code"], kind=v.get("kind") or "MACHINE", calendar_id=self.plant.default_calendar_id)
                self.s.add(obj)
            self._set(obj, v, ["name", "kind", "capacity", "efficiency", "is_finite", "cost_per_hour", "overtime_cost_per_hour", "energy_kw", "detached_setup"])
            if "area" in v:
                obj.area_id = self._ref("area", v["area"])
            if "work_center" in v:
                obj.work_center_id = v.get("work_center__id")
            if "calendar" in v:
                obj.calendar_id = v.get("calendar__id")
            self.s.flush()
            if v.get("groups"):
                for code in v["groups"]:
                    gid = self._ref("group", code)
                    if self.s.scalar(select(M.ResourceGroupMember).where(M.ResourceGroupMember.group_id == gid, M.ResourceGroupMember.resource_id == obj.id)) is None:
                        self.s.add(M.ResourceGroupMember(tenant_id=self.ctx.tenant_id, group_id=gid, resource_id=obj.id))
            self._count(created)
            self.lk.add("resource" if obj.kind not in ("TOOL", "LABOR_POOL") else obj.kind.lower(), obj.code, obj.id)

    def apply_calendars(self, rows: list[dict]) -> None:
        by_cal: dict[str, list[dict]] = {}
        for v in rows:
            by_cal.setdefault(v["calendar_code"], []).append(v)
        for code, shifts in by_cal.items():
            cal = self._by_code(M.Calendar, code)
            if not self._decide(cal):
                continue
            created = cal is None
            first = shifts[0]
            if created:
                cal = M.Calendar(tenant_id=self.ctx.tenant_id, code=code, name=first.get("calendar_name") or code, timezone=first.get("timezone") or self.plant.timezone)
                self.s.add(cal)
                self.s.flush()
            else:
                if first.get("calendar_name"):
                    cal.name = first["calendar_name"]
                if first.get("timezone"):
                    cal.timezone = first["timezone"]
                self.s.execute(delete(M.CalendarShift).where(M.CalendarShift.calendar_id == cal.id))
            try:
                ZoneInfo(cal.timezone)
            except Exception as exc:  # noqa: BLE001
                raise ValidationFailed(f"Unknown time zone '{cal.timezone}' in calendar {code}", code="BAD_TIMEZONE") from exc
            for i, v in enumerate(shifts):
                brk = []
                for part in re.split(r"[;,]", v.get("breaks") or ""):
                    if not part.strip():
                        continue
                    a, _, b = part.partition("-")
                    try:
                        brk.append({"start": _to_time(a).strftime("%H:%M"), "end": _to_time(b).strftime("%H:%M")})
                    except ConvError as exc:
                        raise ValidationFailed(f"Calendar {code}: invalid break '{part.strip()}'", code="BAD_BREAK") from exc
                self.s.add(M.CalendarShift(tenant_id=self.ctx.tenant_id, calendar_id=cal.id, weekday=v["weekday"], shift_code=v.get("shift_code") or f"S{i + 1}", start_time=v["start"], end_time=v["end"], kind=v.get("kind") or "REGULAR", breaks=brk))
            self._count(created)

    def apply_customers(self, rows: list[dict]) -> None:
        for v in rows:
            obj = self._by_code(M.Customer, v["code"])
            if not self._decide(obj):
                continue
            created = obj is None
            if created:
                obj = M.Customer(tenant_id=self.ctx.tenant_id, code=v["code"])
                self.s.add(obj)
            self._set(obj, v, ["name", "priority", "is_strategic", "country", "safety_time_minutes"])
            self._count(created)

    def apply_suppliers(self, rows: list[dict]) -> None:
        for v in rows:
            obj = self._by_code(M.Supplier, v["code"])
            if not self._decide(obj):
                continue
            created = obj is None
            if created:
                obj = M.Supplier(tenant_id=self.ctx.tenant_id, code=v["code"])
                self.s.add(obj)
            self._set(obj, v, ["name", "lead_time_days", "reliability_pct", "is_subcontractor"])
            self._count(created)

    def apply_boms(self, rows: list[dict]) -> None:
        by_parent: dict[uuid.UUID, list[dict]] = {}
        for v in rows:
            by_parent.setdefault(v["parent_code__id"], []).append(v)
        for item_id, lines in by_parent.items():
            ver = str(lines[0].get("version_code") or "1")
            bom = self.s.scalar(select(M.Bom).where(M.Bom.item_id == item_id, M.Bom.version_code == ver))
            if not self._decide(bom):
                continue
            created = bom is None
            if created:
                for other in self.s.scalars(select(M.Bom).where(M.Bom.item_id == item_id, M.Bom.is_active.is_(True))):
                    other.is_active = False
                bom = M.Bom(tenant_id=self.ctx.tenant_id, item_id=item_id, version_code=ver, base_quantity=lines[0].get("base_quantity") or 1, is_active=True)
                self.s.add(bom)
                self.s.flush()
            else:
                if lines[0].get("base_quantity"):
                    bom.base_quantity = lines[0]["base_quantity"]
                self.s.execute(delete(M.BomLine).where(M.BomLine.bom_id == bom.id))
            for pos, v in enumerate(lines, start=1):
                self.s.add(M.BomLine(tenant_id=self.ctx.tenant_id, bom_id=bom.id, component_id=v["component_code__id"], quantity_per=v["quantity_per"], scrap_pct=v.get("scrap_pct") or 0, operation_seq=v.get("operation_seq"), position=pos * 10))
            self._count(created)
        self.s.flush()
        # a BOM must not create a cycle
        edges = [(b.item_id, ln.component_id) for b, ln in self.s.execute(select(M.Bom, M.BomLine).join(M.BomLine, M.BomLine.bom_id == M.Bom.id).where(M.Bom.is_active.is_(True)))]
        from .dataquality import _cycle_nodes

        cyc = _cycle_nodes(edges)
        if cyc:
            codes = sorted(c for c, i in self.s.execute(select(M.Item.code, M.Item.id).where(M.Item.id.in_(list(cyc)))))
            raise ValidationFailed(f"The imported BOMs create a cycle between {', '.join(codes[:8])}", code="BOM_CYCLE")

    def apply_routings(self, rows: list[dict]) -> None:
        by_item: dict[uuid.UUID, list[dict]] = {}
        for v in rows:
            by_item.setdefault(v["item_code__id"], []).append(v)
        for item_id, ops in by_item.items():
            ver = str(ops[0].get("version_code") or "1")
            routing = self.s.scalar(select(M.Routing).where(M.Routing.item_id == item_id, M.Routing.version_code == ver))
            if routing is None:
                for other in self.s.scalars(select(M.Routing).where(M.Routing.item_id == item_id, M.Routing.is_active.is_(True))):
                    other.is_active = False
                routing = M.Routing(tenant_id=self.ctx.tenant_id, item_id=item_id, plant_id=self.plant.id, version_code=ver, is_active=True)
                self.s.add(routing)
                self.s.flush()
                self.related["routing"] = self.related.get("routing", 0) + 1
            for v in sorted(ops, key=lambda x: x["seq"]):
                ro = self.s.scalar(select(M.RoutingOperation).where(M.RoutingOperation.routing_id == routing.id, M.RoutingOperation.seq == v["seq"]))
                if not self._decide(ro):
                    continue
                created = ro is None
                if created:
                    ro = M.RoutingOperation(tenant_id=self.ctx.tenant_id, routing_id=routing.id, seq=v["seq"])
                    self.s.add(ro)
                ro.code = v.get("operation_code") or ro.code or f"OP{v['seq']}"
                self._set(ro, v, ["setup_minutes", "run_minutes_per_unit", "fixed_minutes", "batch_size", "minutes_per_batch", "teardown_minutes", "queue_minutes", "move_minutes", "wait_minutes", "overlap_percent", "transfer_batch", "interruptible", "labor_units", "setup_attributes", "instructions"])
                ro.name = v["operation_name"]
                if "labor_pool" in v:
                    ro.labor_pool_id = v.get("labor_pool__id")
                if "tool" in v:
                    ro.tool_id = v.get("tool__id")
                self.s.flush()
                self.s.execute(delete(M.OperationResource).where(M.OperationResource.routing_operation_id == ro.id))
                self.s.add(M.OperationResource(tenant_id=self.ctx.tenant_id, routing_operation_id=ro.id, resource_id=v["resource_code__id"], role="PRIMARY", preference=0))
                for k, rid in enumerate(v.get("alternative_resources__ids") or [], start=1):
                    if rid is not None and rid != v["resource_code__id"]:
                        self.s.add(M.OperationResource(tenant_id=self.ctx.tenant_id, routing_operation_id=ro.id, resource_id=rid, role="ALTERNATIVE", preference=k))
                self._count(created)

    def apply_production_orders(self, rows: list[dict]) -> None:
        from .masterdata import generate_order_operations

        for v in rows:
            obj = self.s.scalar(select(M.ProductionOrder).where(M.ProductionOrder.number == v["number"]))
            if obj is not None and obj.plant_id != self.plant.id:
                raise ValidationFailed(f"Order {v['number']} belongs to another plant", code="WRONG_PLANT")
            if not self._decide(obj):
                continue
            created = obj is None
            if created:
                obj = M.ProductionOrder(tenant_id=self.ctx.tenant_id, plant_id=self.plant.id, number=v["number"], item_id=v["item_code__id"], source="IMPORT")
                self.s.add(obj)
            elif obj.item_id != v["item_code__id"] and obj.status not in ("PLANNED",):
                raise ValidationFailed(f"Order {obj.number}: the item cannot change once the order is {obj.status}", code="ORDER_ITEM_LOCKED")
            obj.item_id = v["item_code__id"]
            self._set(obj, v, ["quantity", "due_date", "release_date", "priority", "expedite", "status", "erp_ref", "notes"])
            if "customer_code" in v:
                obj.customer_id = v.get("customer_code__id")
            if created and not obj.status:
                obj.status = "PLANNED"
            self.s.flush()
            if created:
                self.ops_generated += generate_order_operations(self.s, self.ctx, obj)
            self._count(created)

    def apply_inventory(self, rows: list[dict]) -> None:
        for v in rows:
            loc = v.get("location") or "MAIN"
            obj = self.s.scalar(select(M.Inventory).where(M.Inventory.item_id == v["item_code__id"], M.Inventory.plant_id == self.plant.id, M.Inventory.location == loc))
            if not self._decide(obj):
                continue
            created = obj is None
            if created:
                obj = M.Inventory(tenant_id=self.ctx.tenant_id, item_id=v["item_code__id"], plant_id=self.plant.id, location=loc)
                self.s.add(obj)
            self._set(obj, v, ["on_hand", "reserved", "blocked", "quality_hold"])
            for f in ("reserved", "blocked", "quality_hold"):
                if getattr(obj, f) is None:
                    setattr(obj, f, 0)
            self._count(created)

    def apply_purchase_orders(self, rows: list[dict]) -> None:
        for v in rows:
            po = self.s.scalar(select(M.PurchaseOrder).where(M.PurchaseOrder.number == v["number"]))
            if po is None:
                po = M.PurchaseOrder(tenant_id=self.ctx.tenant_id, number=v["number"], supplier_id=v["supplier_code__id"], plant_id=self.plant.id, status="OPEN")
                self.s.add(po)
                self.s.flush()
                self.related["purchase_order"] = self.related.get("purchase_order", 0) + 1
            line_no = v.get("line_no") or 1
            ln = self.s.scalar(select(M.PurchaseOrderLine).where(M.PurchaseOrderLine.purchase_order_id == po.id, M.PurchaseOrderLine.line_no == line_no))
            if not self._decide(ln):
                continue
            created = ln is None
            if created:
                ln = M.PurchaseOrderLine(tenant_id=self.ctx.tenant_id, purchase_order_id=po.id, line_no=line_no, item_id=v["item_code__id"], quantity=v["quantity"], expected_date=v["expected_date"], original_date=v["expected_date"], status="OPEN")
                self.s.add(ln)
            elif ln.expected_date and v["expected_date"] and ln.original_date is None:
                ln.original_date = ln.expected_date
            ln.item_id = v["item_code__id"]
            self._set(ln, v, ["quantity", "received_quantity", "expected_date", "confirmed"])
            if ln.received_quantity is None:
                ln.received_quantity = 0
            if ln.received_quantity >= ln.quantity:
                ln.status = "RECEIVED"
            self._count(created)

    def apply_demand(self, rows: list[dict]) -> None:
        for v in rows:
            dtype = v.get("demand_type") or "FORECAST"
            start = v["period_start"]
            obj = self.s.scalar(select(M.Demand).where(M.Demand.item_id == v["item_code__id"], M.Demand.plant_id == self.plant.id, M.Demand.period_start == start, M.Demand.demand_type == dtype))
            if not self._decide(obj):
                continue
            created = obj is None
            if created:
                obj = M.Demand(tenant_id=self.ctx.tenant_id, item_id=v["item_code__id"], plant_id=self.plant.id, period_start=start, demand_type=dtype)
                self.s.add(obj)
            obj.quantity = v["quantity"]
            obj.source = v.get("source") or obj.source or "IMPORT"
            self._count(created)

    def apply_downtimes(self, rows: list[dict]) -> None:
        for v in rows:
            obj = self.s.scalar(select(M.Maintenance).where(M.Maintenance.resource_id == v["resource_code__id"], M.Maintenance.start == v["start"]))
            if not self._decide(obj):
                continue
            created = obj is None
            if created:
                obj = M.Maintenance(tenant_id=self.ctx.tenant_id, resource_id=v["resource_code__id"], start=v["start"], status="PLANNED")
                self.s.add(obj)
            obj.end = v["end"]
            obj.kind = v.get("kind") or obj.kind or "PLANNED"
            if v.get("description"):
                obj.description = v["description"]
            self._count(created)


# =============================================================================================
# templates & exports with the same columns
# =============================================================================================


def template_file(entity: str, fmt: str = "xlsx") -> tuple[bytes, str, str]:
    tpl = TEMPLATES.get(entity)
    if tpl is None:
        raise NotFound(f"No template for '{entity}'")
    cols = [f.name for f in tpl.fields]
    if fmt == "csv":
        return ("﻿" + ",".join(cols) + "\n").encode(), "text/csv; charset=utf-8", f"monxuplan-template-{entity}.csv"
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = entity[:30]
    ws.append(cols)
    for i, f in enumerate(tpl.fields, start=1):
        c = ws.cell(row=1, column=i)
        c.font = Font(bold=True, color="FFFFFF" if f.required else "12203A")
        c.fill = PatternFill("solid", fgColor="12203A" if f.required else "DDE2E8")
        ws.column_dimensions[c.column_letter].width = max(12, len(f.name) + 4)
    info = wb.create_sheet("Fields")
    info.append(["Field", "Required", "Type", "Allowed values / reference", "Description", "Also recognised headers"])
    for c in info[1]:
        c.font = Font(bold=True)
    for f in tpl.fields:
        info.append([f.name, "yes" if f.required else "", f.type, ", ".join(f.enum) if f.enum else (f.ref or ""), f.description, ", ".join(f.aliases[:8])])
    info.append([])
    info.append([tpl.description])
    info.append(["Dates: YYYY-MM-DD [HH:MM] or DD/MM/YYYY [HH:MM] in the plant's local time. Numbers accept '.' or ',' as decimal separator."])
    for col, w in zip("ABCDEF", (26, 10, 10, 40, 60, 50), strict=True):
        info.column_dimensions[col].width = w
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", f"monxuplan-template-{entity}.xlsx"


def template_rows(s: Session, entity: str, plant_id: uuid.UUID | None) -> list[dict[str, Any]]:
    """Current data in the template's columns (round-trip: export → edit → import)."""
    items = {i.id: i for i in s.scalars(select(M.Item))}
    res = {r.id: r for r in s.scalars(select(M.Resource))}
    out: list[dict[str, Any]] = []
    if entity == "items":
        fams = {f.id: f.code for f in s.scalars(select(M.ProductFamily))}
        for i in sorted(items.values(), key=lambda x: x.code):
            attrs = {k: v for k, v in (i.attributes or {}).items() if k != "color"}
            out.append({**{k: getattr(i, k) for k in ("code", "name", "item_type", "make_or_buy", "uom", "quantity_type", "lot_policy", "min_lot", "max_lot", "lot_multiple", "fixed_lot", "safety_stock", "purchase_lead_time_days", "production_lead_time_days", "unit_cost", "unit_price")}, "family": fams.get(i.family_id), "attributes": "; ".join(f"{k}={v}" for k, v in attrs.items()) or None, "color": (i.attributes or {}).get("color")})
    elif entity == "resources":
        areas = {a.id: a.code for a in s.scalars(select(M.PlanningArea))}
        cals = {c.id: c.code for c in s.scalars(select(M.Calendar))}
        wcs = {w.id: w.code for w in s.scalars(select(M.WorkCenter))}
        grp = {g.id: g.code for g in s.scalars(select(M.ResourceGroup))}
        mem: dict[uuid.UUID, list[str]] = {}
        for m in s.scalars(select(M.ResourceGroupMember)):
            mem.setdefault(m.resource_id, []).append(grp.get(m.group_id, ""))
        for r in sorted(res.values(), key=lambda x: x.code):
            if plant_id and r.plant_id != plant_id:
                continue
            out.append({**{k: getattr(r, k) for k in ("code", "name", "kind", "capacity", "efficiency", "is_finite", "cost_per_hour", "overtime_cost_per_hour", "energy_kw", "detached_setup")}, "area": areas.get(r.area_id), "work_center": wcs.get(r.work_center_id), "calendar": cals.get(r.calendar_id), "groups": ", ".join(mem.get(r.id, [])) or None})
    elif entity == "calendars":
        for c in s.scalars(select(M.Calendar).order_by(M.Calendar.code)):
            for sh in s.scalars(select(M.CalendarShift).where(M.CalendarShift.calendar_id == c.id).order_by(M.CalendarShift.weekday, M.CalendarShift.start_time)):
                out.append({"calendar_code": c.code, "calendar_name": c.name, "timezone": c.timezone, "weekday": sh.weekday, "shift_code": sh.shift_code, "start": sh.start_time.strftime("%H:%M"), "end": sh.end_time.strftime("%H:%M"), "kind": sh.kind, "breaks": "; ".join(f"{b['start']}-{b['end']}" for b in (sh.breaks or [])) or None})
    elif entity == "customers":
        out = [{k: getattr(c, k) for k in ("code", "name", "priority", "is_strategic", "country", "safety_time_minutes")} for c in s.scalars(select(M.Customer).order_by(M.Customer.code))]
    elif entity == "suppliers":
        out = [{k: getattr(c, k) for k in ("code", "name", "lead_time_days", "reliability_pct", "is_subcontractor")} for c in s.scalars(select(M.Supplier).order_by(M.Supplier.code))]
    elif entity == "boms":
        for b, ln in s.execute(select(M.Bom, M.BomLine).join(M.BomLine, M.BomLine.bom_id == M.Bom.id).where(M.Bom.is_active.is_(True)).order_by(M.Bom.item_id, M.BomLine.position)):
            out.append({"parent_code": items[b.item_id].code, "component_code": items[ln.component_id].code, "quantity_per": ln.quantity_per, "scrap_pct": ln.scrap_pct, "operation_seq": ln.operation_seq, "base_quantity": b.base_quantity, "version_code": b.version_code})
        out.sort(key=lambda r: r["parent_code"])
    elif entity == "routings":
        pools = {p.id: p.code for p in s.scalars(select(M.LaborPool))}
        ors: dict[uuid.UUID, list[M.OperationResource]] = {}
        for o in s.scalars(select(M.OperationResource).order_by(M.OperationResource.preference)):
            ors.setdefault(o.routing_operation_id, []).append(o)
        for rt, ro in s.execute(select(M.Routing, M.RoutingOperation).join(M.RoutingOperation, M.RoutingOperation.routing_id == M.Routing.id).where(M.Routing.is_active.is_(True))):
            if plant_id and rt.plant_id not in (None, plant_id):
                continue
            rr = [o for o in ors.get(ro.id, []) if o.resource_id in res]
            prim = next((o for o in rr if o.role == "PRIMARY"), rr[0] if rr else None)
            out.append(
                {
                    "item_code": items[rt.item_id].code,
                    "seq": ro.seq,
                    "operation_code": ro.code,
                    "operation_name": ro.name,
                    "resource_code": res[prim.resource_id].code if prim else None,
                    "alternative_resources": ", ".join(res[o.resource_id].code for o in rr if o is not prim) or None,
                    **{k: getattr(ro, k) for k in ("setup_minutes", "run_minutes_per_unit", "fixed_minutes", "batch_size", "minutes_per_batch", "teardown_minutes", "queue_minutes", "move_minutes", "wait_minutes", "overlap_percent", "transfer_batch", "interruptible", "labor_units", "instructions")},
                    "labor_pool": pools.get(ro.labor_pool_id),
                    "tool": res[ro.tool_id].code if ro.tool_id in res else None,
                    "setup_attributes": "; ".join(f"{k}={v}" for k, v in (ro.setup_attributes or {}).items()) or None,
                    "version_code": rt.version_code,
                }
            )
        out.sort(key=lambda r: (r["item_code"], r["seq"]))
    elif entity == "production-orders":
        custs = {c.id: c.code for c in s.scalars(select(M.Customer))}
        q = select(M.ProductionOrder).order_by(M.ProductionOrder.due_date)
        if plant_id:
            q = q.where(M.ProductionOrder.plant_id == plant_id)
        for o in s.scalars(q):
            out.append({"number": o.number, "item_code": items[o.item_id].code, "quantity": o.quantity, "due_date": o.due_date, "release_date": o.release_date, "priority": o.priority, "customer_code": custs.get(o.customer_id), "expedite": o.expedite, "status": o.status, "erp_ref": o.erp_ref, "notes": o.notes})
    elif entity == "inventory":
        q = select(M.Inventory)
        if plant_id:
            q = q.where(M.Inventory.plant_id == plant_id)
        out = [{"item_code": items[i.item_id].code, "location": i.location, "on_hand": i.on_hand, "reserved": i.reserved, "blocked": i.blocked, "quality_hold": i.quality_hold} for i in s.scalars(q)]
        out.sort(key=lambda r: r["item_code"])
    elif entity == "purchase-orders":
        sups = {x.id: x.code for x in s.scalars(select(M.Supplier))}
        for po, ln in s.execute(select(M.PurchaseOrder, M.PurchaseOrderLine).join(M.PurchaseOrderLine, M.PurchaseOrderLine.purchase_order_id == M.PurchaseOrder.id).order_by(M.PurchaseOrder.number, M.PurchaseOrderLine.line_no)):
            if plant_id and po.plant_id not in (None, plant_id):
                continue
            out.append({"number": po.number, "line_no": ln.line_no, "supplier_code": sups.get(po.supplier_id), "item_code": items[ln.item_id].code, "quantity": ln.quantity, "received_quantity": ln.received_quantity, "expected_date": ln.expected_date, "confirmed": ln.confirmed})
    elif entity == "demand":
        q = select(M.Demand).order_by(M.Demand.period_start)
        if plant_id:
            q = q.where(M.Demand.plant_id == plant_id)
        out = [{"item_code": items[d.item_id].code, "period_start": d.period_start, "quantity": d.quantity, "demand_type": d.demand_type, "source": d.source} for d in s.scalars(q)]
    elif entity == "downtimes":
        for m in s.scalars(select(M.Maintenance).order_by(M.Maintenance.start)):
            r = res.get(m.resource_id)
            if r is None or plant_id and r.plant_id != plant_id:
                continue
            out.append({"resource_code": r.code, "start": m.start, "end": m.end, "kind": m.kind, "description": m.description})
    return out
