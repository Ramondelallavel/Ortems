"""Static pegging of make-items (sub-assemblies, semi-finished goods).

Component production orders are linked to the parent operations that consume their output, FIFO by
the parent's need (due date, then weight). Each link becomes a finish-to-start synchronisation edge
(child order → parent operation), so the builder always schedules the child before the parent; the
material ledger then enforces the exact quantities. Stock on hand and firm receipts are allocated
first. Demand that no supply covers is reported (and the ledger will block the operation).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .compile import CMat, COp, COrd, Issue, StaticPeg

EPS = 1e-9


def static_pegging(ops: list[COp], orders: list[COrd], materials: list[CMat], issues: list[Issue]) -> list[StaticPeg]:
    from .compile import Issue, StaticPeg

    pegs: list[StaticPeg] = []
    for m in materials:
        if not m.producers or not m.consumers:
            continue
        demands = []
        for oi in m.consumers:
            op = ops[oi]
            qty = sum(q for mi, q in op.materials if mi == m.idx)
            o = orders[op.order]
            demands.append((o.due - o.safety, -o.weight, o.number, op.seq, oi, qty))
        demands.sort()
        # supplies: stock/receipts by time, then production orders by due date
        supplies: list[list] = []
        for t, q, sid, kind, ref, _firm, _sup in sorted(m.supplies, key=lambda s: s[0]):
            supplies.append([q, kind, ref or sid, None])
        for pi in sorted(m.producers, key=lambda i: (orders[i].due, orders[i].number)):
            if not orders[pi].ops:
                continue
            supplies.append([orders[pi].qty, "PRODUCTION", orders[pi].number, pi])
        si = 0
        for *_k, oi, need in demands:
            while need > EPS and si < len(supplies):
                left, kind, ref, producer = supplies[si]
                take = min(left, need)
                if take > EPS:
                    pegs.append(StaticPeg(m.idx, oi, take, kind, ref, producer))
                    need -= take
                    supplies[si][0] -= take
                if supplies[si][0] <= EPS:
                    si += 1
            if need > EPS:
                pegs.append(StaticPeg(m.idx, oi, need, "UNCOVERED", None, None))
                issues.append(
                    Issue(
                        "WARNING",
                        "COMPONENT_NOT_COVERED",
                        f"{need:g} {m.uom} of {m.code} required by {ops[oi].id} are not covered by stock, receipts or component orders",
                        {"material_id": m.id, "op_id": ops[oi].id, "quantity": need},
                    )
                )
    return pegs
