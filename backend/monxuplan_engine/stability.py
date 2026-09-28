"""Plan stability / plan difference metrics."""

from __future__ import annotations

from collections import defaultdict
from typing import Any


def stability(cp, placements, baseline: dict[int, tuple[int, int, int, int]] | None = None, tolerance: int = 1) -> dict[str, Any]:
    """Compare a schedule with a baseline {op: (res, setup_start, start, end)}.

    * operations_moved: start shifted by more than ``tolerance`` minutes or resource changed
    * average_shift_h / max_shift_h over moved operations
    * changed_resources: operations on another resource
    * changed_sequences: operations whose predecessor on its resource changed
    * new_operations / removed_operations
    """
    base = baseline if baseline is not None else cp.baseline
    if not base:
        return {
            "baseline_operations": 0,
            "operations_moved": 0,
            "average_shift_h": 0.0,
            "max_shift_h": 0.0,
            "changed_resources": 0,
            "changed_sequences": 0,
            "new_operations": sum(1 for p in placements if p is not None),
            "removed_operations": 0,
            "moved": [],
        }
    moved = []
    shifts = []
    changed_res = 0
    new_ops = 0
    for p in placements:
        if p is None:
            continue
        b = base.get(p.op)
        if b is None:
            new_ops += 1
            continue
        b_res, _b_ss, b_start, _b_end = b
        shift = p.start - b_start
        res_changed = b_res != p.res
        if res_changed:
            changed_res += 1
        if abs(shift) > tolerance or res_changed:
            moved.append({"op_id": cp.ops[p.op].id, "shift_minutes": shift, "resource_changed": res_changed, "from_resource": cp.resources[b_res].id, "to_resource": cp.resources[p.res].id})
            shifts.append(abs(shift))
    removed = sum(1 for i in base if placements[i] is None) if base else 0
    # sequence changes: predecessor on the same resource
    def seq_prev(entries):
        by_res = defaultdict(list)
        for op, res, start in entries:
            by_res[res].append((start, op))
        prev = {}
        for _res, lst in by_res.items():
            lst.sort()
            for k, (_s, op) in enumerate(lst):
                prev[op] = lst[k - 1][1] if k > 0 else None
        return prev

    now_prev = seq_prev([(p.op, p.res, p.start) for p in placements if p is not None])
    base_prev = seq_prev([(op, b[0], b[2]) for op, b in base.items()])
    changed_seq = sum(1 for op, pv in now_prev.items() if op in base_prev and base_prev[op] != pv)
    return {
        "baseline_operations": len(base),
        "operations_moved": len(moved),
        "average_shift_h": round(sum(shifts) / len(shifts) / 60.0, 2) if shifts else 0.0,
        "max_shift_h": round(max(shifts) / 60.0, 2) if shifts else 0.0,
        "changed_resources": changed_res,
        "changed_sequences": changed_seq,
        "new_operations": new_ops,
        "removed_operations": removed,
        "moved": sorted(moved, key=lambda m: -abs(m["shift_minutes"]))[:200],
    }
