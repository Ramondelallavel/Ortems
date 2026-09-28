"""Planning rule engine (configuration over code).

A rule is ``IF condition THEN actions`` stored as data and edited from the administration UI.

Condition DSL (JSON)::

    {"all": [ {"field": "order.family", "op": "eq", "value": "A"},
              {"field": "op.resource_groups", "op": "contains", "value": "CNC"} ]}
    {"any": [...]}   {"not": {...}}
    {"field": "resource.attributes.max_diameter", "op": "lt", "value_field": "order.attributes.diameter"}

Operators: eq, ne, gt, gte, lt, lte, in, not_in, contains, exists.

Fields: ``order.*`` (number, item_code, family, priority, customer_id, customer_priority, strategic,
expedite, quantity, attributes.<k>), ``op.*`` (code, name, seq, quantity, resource_groups,
resources), ``resource.*`` (code, kind, groups, area, attributes.<k>) — resource fields are only
available to mode-level actions.

Actions::

    {"type": "ADD_WEIGHT", "value": 30}            order priority weight += value
    {"type": "MULTIPLY_WEIGHT", "value": 2}
    {"type": "SET_WEIGHT", "value": 5}
    {"type": "ADD_SAFETY_TIME", "minutes": 240}     plan to finish earlier than the due date
    {"type": "PREFER_RESOURCE", "resource": "CNC-03"}
    {"type": "AVOID_RESOURCE", "resource": "CNC-01", "penalty": 2}
    {"type": "FORBID_RESOURCE", "resource": "CNC-01"}   (or mode-level with resource.* condition)
    {"type": "ADD_SETUP_MINUTES", "value": 15}
    {"type": "SET_SETUP_ATTRIBUTE", "key": "color", "value": "RAL9005"}

Every rule that fires is recorded (order and operation ``rules_applied``) and shown in explanations.
"""

from __future__ import annotations

from typing import Any

from .contract import RuleSpec

ORDER_ACTIONS = {"ADD_WEIGHT", "MULTIPLY_WEIGHT", "SET_WEIGHT", "ADD_SAFETY_TIME"}
OP_ACTIONS = {"PREFER_RESOURCE", "AVOID_RESOURCE", "FORBID_RESOURCE", "ADD_SETUP_MINUTES", "SET_SETUP_ATTRIBUTE"}
KNOWN_ACTIONS = ORDER_ACTIONS | OP_ACTIONS
KNOWN_OPS = {"eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "contains", "exists"}


class RuleError(ValueError):
    pass


def validate_rule(rule: RuleSpec) -> list[str]:
    """Static validation used by the API before saving a rule."""
    errors: list[str] = []

    def walk(c: Any, path: str) -> None:
        if not c:
            return
        if not isinstance(c, dict):
            errors.append(f"{path}: condition must be an object")
            return
        if "all" in c or "any" in c:
            key = "all" if "all" in c else "any"
            if not isinstance(c[key], list):
                errors.append(f"{path}.{key}: must be a list")
                return
            for i, sub in enumerate(c[key]):
                walk(sub, f"{path}.{key}[{i}]")
        elif "not" in c:
            walk(c["not"], f"{path}.not")
        else:
            if "field" not in c:
                errors.append(f"{path}: missing 'field'")
            if c.get("op", "eq") not in KNOWN_OPS:
                errors.append(f"{path}: unknown operator {c.get('op')!r}")

    walk(rule.condition, "condition")
    for i, a in enumerate(rule.actions):
        t = a.get("type")
        if t not in KNOWN_ACTIONS:
            errors.append(f"actions[{i}]: unknown action type {t!r}")
        if t in {"PREFER_RESOURCE", "AVOID_RESOURCE", "FORBID_RESOURCE"} and not a.get("resource"):
            errors.append(f"actions[{i}]: 'resource' is required")
        if t in {"ADD_WEIGHT", "MULTIPLY_WEIGHT", "SET_WEIGHT", "ADD_SETUP_MINUTES"} and not isinstance(
            a.get("value"), int | float
        ):
            errors.append(f"actions[{i}]: numeric 'value' is required")
    return errors


def uses_resource_fields(cond: Any) -> bool:
    if not isinstance(cond, dict):
        return False
    if "all" in cond or "any" in cond:
        return any(uses_resource_fields(c) for c in cond.get("all", cond.get("any", [])))
    if "not" in cond:
        return uses_resource_fields(cond["not"])
    return str(cond.get("field", "")).startswith("resource.") or str(cond.get("value_field", "")).startswith("resource.")


def evaluate(cond: Any, ctx: dict[str, Any]) -> bool:
    if not cond:
        return True
    if "all" in cond:
        return all(evaluate(c, ctx) for c in cond["all"])
    if "any" in cond:
        return any(evaluate(c, ctx) for c in cond["any"])
    if "not" in cond:
        return not evaluate(cond["not"], ctx)
    left = resolve(ctx, cond["field"])
    op = cond.get("op", "eq")
    if "value_field" in cond:
        right = resolve(ctx, cond["value_field"])
    else:
        right = cond.get("value")
    return _compare(left, op, right)


def resolve(ctx: dict[str, Any], path: str) -> Any:
    cur: Any = ctx
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            cur = getattr(cur, part, None)
        if cur is None:
            return None
    return cur


def _compare(left: Any, op: str, right: Any) -> bool:
    try:
        if op == "exists":
            return (left is not None) == bool(right if right is not None else True)
        if left is None:
            return op in {"ne", "not_in"}
        if op == "eq":
            return _eq(left, right)
        if op == "ne":
            return not _eq(left, right)
        if op == "gt":
            return float(left) > float(right)
        if op == "gte":
            return float(left) >= float(right)
        if op == "lt":
            return float(left) < float(right)
        if op == "lte":
            return float(left) <= float(right)
        if op == "in":
            return any(_eq(left, r) for r in (right or []))
        if op == "not_in":
            return not any(_eq(left, r) for r in (right or []))
        if op == "contains":
            if isinstance(left, list | tuple | set):
                return any(_eq(x, right) for x in left)
            return str(right) in str(left)
    except (TypeError, ValueError):
        return False
    return False


def _eq(a: Any, b: Any) -> bool:
    if isinstance(a, int | float) and isinstance(b, int | float):
        return float(a) == float(b)
    return str(a) == str(b)


def active_rules(rules: list[RuleSpec]) -> list[RuleSpec]:
    return sorted((r for r in rules if r.active), key=lambda r: (r.priority, r.id))
