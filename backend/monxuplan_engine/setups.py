"""Sequence-dependent setup (changeover) model.

For a unary resource with setup matrices::

    setup(prev, next) = combine_M  M[prev[attr_M]][next[attr_M]]  +  Σ matching setup rules

* ``same_minutes`` when both values are equal and no explicit entry exists,
* ``default_minutes`` for unlisted pairs or unknown previous state,
* ``combine`` is MAX (parallel changeover tasks) or SUM (sequential tasks), per resource.

When a resource has no matrix, the routing's standard setup (``base``) applies to every job — the
setup is then sequence independent. Setup rules (e.g. *cleaning after family Y before family X*) add
minutes in both cases.
"""

from __future__ import annotations

from typing import Any

from .contract import SetupMatrixSpec, SetupRuleSpec

StateKey = tuple[tuple[str, str], ...]


def state_key(state: dict[str, Any] | None) -> StateKey | None:
    if state is None:
        return None
    return tuple(sorted((str(k), _norm(v)) for k, v in state.items()))


def _norm(v: Any) -> str:
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v)


class CompiledMatrix:
    __slots__ = ("id", "attribute", "same", "default", "table")

    def __init__(self, spec: SetupMatrixSpec) -> None:
        self.id = spec.id
        self.attribute = spec.attribute
        self.same = int(round(spec.same_minutes))
        self.default = int(round(spec.default_minutes))
        self.table = {(_norm(e.from_value), _norm(e.to_value)): int(round(e.minutes)) for e in spec.entries}

    def value(self, prev: dict[str, str] | None, nxt: dict[str, str]) -> int:
        b = nxt.get(self.attribute)
        if prev is None:
            return self.default
        a = prev.get(self.attribute)
        if a is None or b is None:
            return self.default
        v = self.table.get((a, b))
        if v is not None:
            return v
        return self.same if a == b else self.default


class SetupModel:
    def __init__(self, matrices: list[SetupMatrixSpec], rules: list[SetupRuleSpec]) -> None:
        self.matrices = {m.id: CompiledMatrix(m) for m in matrices}
        self.rules = rules
        self._cache: dict[tuple, int] = {}
        # per resource index: (matrix list, combine, resource id)
        self._res: dict[int, tuple[list[CompiledMatrix], str, str]] = {}

    def register_resource(self, res_idx: int, res_id: str, matrix_ids: list[str], combine: str) -> list[str]:
        """Returns unknown matrix ids (reported as data issues)."""
        mats = [self.matrices[m] for m in matrix_ids if m in self.matrices]
        unknown = [m for m in matrix_ids if m not in self.matrices]
        self._res[res_idx] = (mats, combine, res_id)
        return unknown

    def has_matrices(self, res_idx: int) -> bool:
        entry = self._res.get(res_idx)
        return bool(entry and entry[0])

    def setup(self, res_idx: int, prev: StateKey | None, nxt: StateKey | None, base: int) -> int:
        key = (res_idx, prev, nxt, base)
        v = self._cache.get(key)
        if v is not None:
            return v
        v = self._compute(res_idx, prev, nxt, base)
        self._cache[key] = v
        return v

    def _compute(self, res_idx: int, prev: StateKey | None, nxt: StateKey | None, base: int) -> int:
        mats, combine, res_id = self._res.get(res_idx, ([], "MAX", ""))
        p = dict(prev) if prev is not None else None
        n = dict(nxt) if nxt is not None else {}
        if mats:
            vals = [m.value(p, n) for m in mats]
            total = max(vals) if combine == "MAX" else sum(vals)
        else:
            total = base
        for r in self.rules:
            if r.resource_ids and res_id not in r.resource_ids:
                continue
            if p is None and r.when_prev:
                continue
            if _match(p or {}, r.when_prev) and _match(n, r.when_next):
                total += int(round(r.add_minutes))
        return max(0, int(total))

    def breakdown(self, res_idx: int, prev: StateKey | None, nxt: StateKey | None, base: int) -> list[dict[str, Any]]:
        """Human-readable composition of a setup value (for explanations)."""
        mats, combine, res_id = self._res.get(res_idx, ([], "MAX", ""))
        p = dict(prev) if prev is not None else None
        n = dict(nxt) if nxt is not None else {}
        out: list[dict[str, Any]] = []
        if mats:
            for m in mats:
                out.append(
                    {
                        "source": "matrix",
                        "matrix": m.id,
                        "attribute": m.attribute,
                        "from": None if p is None else p.get(m.attribute),
                        "to": n.get(m.attribute),
                        "minutes": m.value(p, n),
                    }
                )
            out.append({"source": "combine", "mode": combine})
        else:
            out.append({"source": "routing", "minutes": base})
        for r in self.rules:
            if r.resource_ids and res_id not in r.resource_ids:
                continue
            if p is None and r.when_prev:
                continue
            if _match(p or {}, r.when_prev) and _match(n, r.when_next):
                out.append({"source": "rule", "rule": r.id, "description": r.description, "minutes": r.add_minutes})
        return out


def _match(state: dict[str, str], cond: dict[str, Any]) -> bool:
    for k, v in cond.items():
        sv = state.get(k)
        if isinstance(v, list):
            if sv not in {_norm(x) for x in v}:
                return False
        elif sv != _norm(v):
            return False
    return True


def matches(state: StateKey | None, cond: dict[str, Any]) -> bool:
    if state is None:
        return False
    return _match(dict(state), cond)
