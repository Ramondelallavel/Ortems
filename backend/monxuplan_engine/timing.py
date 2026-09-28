"""Infinite-capacity timing (critical path) on working calendars.

* Forward pass: earliest start / finish of every operation ignoring competition for resources, but
  respecting calendars, release dates, precedences and material supply (each order alone).
  → *earliest achievable* completion per order (deadline-impossible detection).
* Backward pass: latest start / finish to meet the due date → slack, latest start time for the
  DUE_DATE_FIRST rule, backward/hybrid scheduling targets and the requirement load of capacity
  planning.
"""

from __future__ import annotations

from dataclasses import dataclass

from .compile import CompiledProblem
from .materials import MaterialLedger

NEG = -(1 << 50)
POS = 1 << 50


@dataclass
class Timing:
    est: list[int]
    eft: list[int]
    est_mode: list[int]
    lst: list[int]
    lft: list[int]
    lst_mode: list[int]
    nominal: list[int]  # working minutes of the fastest mode
    tail: list[int]  # working minutes from op start to order end along the longest path
    order_eft: list[int | None]
    order_limit: list[str | None]  # what limits the infinite-capacity completion


def initial_ledger(cp: CompiledProblem) -> MaterialLedger:
    ledger = MaterialLedger(len(cp.materials))
    for m in cp.materials:
        for t, q, sid, kind, ref, _firm, supplier in m.supplies:
            ledger.supply(m.idx, t, q, sid, kind=kind, ref=ref, supplier=supplier)
    return ledger


def compute_timing(cp: CompiledProblem) -> Timing:
    n = len(cp.ops)
    ops = cp.ops
    nominal = [min((m.nominal for m in o.modes), default=0) for o in ops]
    est = [0] * n
    eft = [0] * n
    est_mode = [-1] * n
    limit_reason: list[str] = ["START"] * n
    ledger = initial_ledger(cp) if cp.constraints.materials != "IGNORE" else None

    for i in cp.topo:
        o = ops[i]
        order = cp.orders[o.order]
        lb = cp.work_lb
        reason = "START"
        if order.release is not None and order.release > lb:
            lb, reason = order.release, "RELEASE"
        if o.earliest is not None and o.earliest > lb:
            lb, reason = o.earliest, "RELEASE"
        end_lb = NEG
        for p, kind, lag, frac in o.preds:
            po = ops[p]
            if kind in ("FS",):
                t = eft[p] + po.move + po.wait + o.queue + po.buf_after + o.buf_before + lag
            elif kind == "SS":
                t = est[p] + lag
            elif kind == "OVL":
                t = est[p] + int(frac * nominal[p]) + lag
                end_lb = max(end_lb, eft[p] + po.move + int(frac * nominal[i]))
            elif kind == "FF":
                end_lb = max(end_lb, eft[p] + lag)
                continue
            else:  # SF
                end_lb = max(end_lb, est[p] + lag)
                continue
            if t > lb:
                lb, reason = t, "PREDECESSOR"
        if ledger is not None:
            for mi, q in o.materials:
                tm = ledger.earliest(mi, q, lb)
                if tm is None:
                    tm = POS
                if tm > lb:
                    lb, reason = tm, "MATERIAL"
        if o.fixed is not None:
            mi, s, e, *_ = o.fixed
            mode = o.modes[mi]
            est[i] = s
            eft[i] = e if e is not None else (mode.cal.add_work(s, mode.nominal) or POS)
            est_mode[i] = mi
            limit_reason[i] = "FIXED"
            if ledger is not None and o.produces is not None and eft[i] < POS:
                mat, qty = o.produces
                ledger.supply(mat, eft[i] + o.move + o.wait, qty, f"PROD:{i}", kind="PRODUCTION")
            continue
        best_s, best_e, best_m = POS, POS, -1
        for m in o.modes:
            s = m.cal.next_work(lb) if lb < POS else None
            if s is None:
                continue
            if m.sub:
                e = s + m.run
            elif o.interruptible:
                e = m.cal.add_work(s, m.nominal)
            else:
                s2 = m.cal.next_uninterrupted_start(s, m.nominal)
                e = None if s2 is None else s2 + m.nominal
                s = s2 if s2 is not None else s
            if e is None:
                continue
            if e < end_lb:
                shift = end_lb - e
                s2 = m.cal.next_work(s + shift)
                e2 = m.cal.add_work(s2, m.nominal) if s2 is not None else None
                if e2 is None:
                    continue
                s, e = s2, e2
            if e < best_e:
                best_s, best_e, best_m = s, e, m.idx
        est[i], eft[i], est_mode[i] = best_s, best_e, best_m
        limit_reason[i] = reason if best_m >= 0 else "NO_MODE"
        if ledger is not None and o.produces is not None and best_e < POS:
            # component orders feed their parents at infinite capacity too
            mat, qty = o.produces
            ledger.supply(mat, best_e + o.move + o.wait, qty, f"PROD:{i}", kind="PRODUCTION")

    # ---- backward pass
    lst = [POS] * n
    lft = [POS] * n
    lst_mode = [-1] * n
    tail = [0] * n
    for i in reversed(cp.topo):
        o = ops[i]
        order = cp.orders[o.order]
        lf = POS
        tl = nominal[i]
        if o.is_last or not o.succs:
            lf = order.due - order.safety
        for s, kind, lag, frac in o.succs:
            so = ops[s]
            if kind == "FS":
                t = lst[s] - (o.move + o.wait + so.queue + o.buf_after + so.buf_before + lag)
                lf = min(lf, t)
                tl = max(tl, nominal[i] + tail[s])
            elif kind == "OVL":
                lf = min(lf, lft[s] - o.move - int(frac * nominal[s]))
                tl = max(tl, int(frac * nominal[i]) + tail[s])
            elif kind == "SS":
                tl = max(tl, tail[s] + lag)
            elif kind == "FF":
                lf = min(lf, lft[s] - lag)
        tail[i] = tl
        lft[i] = lf
        best, best_m = NEG, -1
        for m in o.modes:
            if lf >= POS:
                continue
            s = m.cal.sub_work(lf, m.nominal) if not m.sub else lf - m.run
            if s is None:
                s = cp.lo
            if s > best:
                best, best_m = s, m.idx
        lst[i] = best if best_m >= 0 else (lf if lf < POS else POS)
        lst_mode[i] = best_m

    order_eft: list[int | None] = []
    order_limit: list[str | None] = []
    for o in cp.orders:
        if not o.last_ops:
            order_eft.append(None)
            order_limit.append(None)
            continue
        e = max(eft[i] for i in o.last_ops)
        order_eft.append(e if e < POS else None)
        # the limiting reason of the critical chain
        crit = max(o.ops, key=lambda i: eft[i]) if o.ops else None
        reason = limit_reason[crit] if crit is not None else None
        # walk back to the first non-predecessor reason
        seen = 0
        while crit is not None and reason == "PREDECESSOR" and seen < 1000:
            seen += 1
            preds = [p for p, *_ in ops[crit].preds]
            if not preds:
                break
            crit = max(preds, key=lambda p: eft[p])
            reason = limit_reason[crit]
        order_limit.append(reason)
    return Timing(est, eft, est_mode, lst, lft, lst_mode, nominal, tail, order_eft, order_limit)
