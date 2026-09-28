"""MIP provider — aggregate production planning (OR-Tools linear solver, SCIP).

Plans production quantities per product family (or product) and period under the capacity of
resource groups, before detailed scheduling:

    min  Σ h_f·I[f,t] + b_f·B[f,t] + o_g·O[g,t] + c_f·P[f,t]
    s.t. I[f,t-1] − B[f,t-1] + P[f,t] − D[f,t] = I[f,t] − B[f,t]
         Σ_f hours[f,g]·P[f,t] ≤ cap[g,t] + O[g,t]          (capacity per group and period)
         0 ≤ O[g,t] ≤ ot_max[g,t];  I[f,t] ≥ ss_f − S[f,t]  (S = safety-stock shortfall, penalised)
         P[f,t] ≥ 0 (integer when ``integer`` is set)

This provider does not sequence operations: for detailed scheduling it raises
:class:`NotSupported` instead of pretending — use ``heuristic``, ``cpsat`` or ``hybrid``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .base import OptimizationProvider, ProviderCapabilities


class NotSupported(RuntimeError):
    pass


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AggFamily(_M):
    id: str
    demand: list[float]
    initial_inventory: float = 0
    safety_stock: float = 0
    holding_cost: float = 1.0
    backlog_cost: float = 10.0
    production_cost: float = 0.0
    hours_per_unit: dict[str, float] = Field(default_factory=dict)


class AggGroup(_M):
    id: str
    capacity_hours: list[float]
    overtime_max_hours: list[float] | None = None
    overtime_cost_per_hour: float = 50.0


class AggregateProblem(_M):
    periods: list[str]
    families: list[AggFamily]
    groups: list[AggGroup]
    integer: bool = False
    safety_stock_penalty: float = 5.0
    time_limit_s: float = 30.0


class MipProvider(OptimizationProvider):
    name = "mip"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(detailed_scheduling=False, aggregate_planning=True, proves_optimality=True)

    def solve(self, cp, timing, ctx):  # noqa: ARG002
        raise NotSupported("The MIP provider performs aggregate (family × period) planning only; use 'heuristic', 'cpsat' or 'hybrid' for detailed scheduling.")

    def solve_aggregate(self, pb: AggregateProblem) -> dict[str, Any]:
        from ortools.linear_solver import pywraplp

        solver = pywraplp.Solver.CreateSolver("SCIP" if pb.integer else "GLOP")
        if solver is None:  # pragma: no cover
            raise RuntimeError("linear solver backend not available")
        T = len(pb.periods)
        inf = solver.infinity()
        P, I, B, S = {}, {}, {}, {}
        O = {}
        for f in pb.families:
            if len(f.demand) != T:
                raise ValueError(f"family {f.id}: demand has {len(f.demand)} periods, expected {T}")
            for t in range(T):
                P[f.id, t] = solver.IntVar(0, inf, f"P_{f.id}_{t}") if pb.integer else solver.NumVar(0, inf, f"P_{f.id}_{t}")
                I[f.id, t] = solver.NumVar(0, inf, f"I_{f.id}_{t}")
                B[f.id, t] = solver.NumVar(0, inf, f"B_{f.id}_{t}")
                S[f.id, t] = solver.NumVar(0, inf, f"S_{f.id}_{t}")
        for g in pb.groups:
            for t in range(T):
                ub = g.overtime_max_hours[t] if g.overtime_max_hours else 0.0
                O[g.id, t] = solver.NumVar(0, ub, f"O_{g.id}_{t}")
        for f in pb.families:
            for t in range(T):
                prev_i = I[f.id, t - 1] if t else f.initial_inventory
                prev_b = B[f.id, t - 1] if t else 0
                solver.Add(prev_i - prev_b + P[f.id, t] - f.demand[t] == I[f.id, t] - B[f.id, t])
                solver.Add(I[f.id, t] + S[f.id, t] >= f.safety_stock)
        caps = {}
        for g in pb.groups:
            for t in range(T):
                expr = sum(f.hours_per_unit.get(g.id, 0.0) * P[f.id, t] for f in pb.families)
                caps[g.id, t] = solver.Add(expr <= g.capacity_hours[t] + O[g.id, t])
        obj = solver.Objective()
        for f in pb.families:
            for t in range(T):
                obj.SetCoefficient(I[f.id, t], f.holding_cost)
                obj.SetCoefficient(B[f.id, t], f.backlog_cost)
                obj.SetCoefficient(P[f.id, t], f.production_cost)
                obj.SetCoefficient(S[f.id, t], pb.safety_stock_penalty)
        for g in pb.groups:
            for t in range(T):
                obj.SetCoefficient(O[g.id, t], g.overtime_cost_per_hour)
        obj.SetMinimization()
        solver.SetTimeLimit(int(pb.time_limit_s * 1000))
        status = solver.Solve()
        names = {pywraplp.Solver.OPTIMAL: "OPTIMAL", pywraplp.Solver.FEASIBLE: "FEASIBLE", pywraplp.Solver.INFEASIBLE: "INFEASIBLE"}
        st = names.get(status, "NO_SOLUTION")
        out: dict[str, Any] = {"status": st, "objective": None, "families": [], "groups": [], "periods": pb.periods}
        if st not in ("OPTIMAL", "FEASIBLE"):
            return out
        out["objective"] = round(obj.Value(), 4)
        if pb.integer:
            out["best_bound"] = round(obj.BestBound(), 4)
        for f in pb.families:
            out["families"].append(
                {
                    "id": f.id,
                    "demand": f.demand,
                    "production": [round(P[f.id, t].solution_value(), 3) for t in range(T)],
                    "inventory": [round(I[f.id, t].solution_value(), 3) for t in range(T)],
                    "backlog": [round(B[f.id, t].solution_value(), 3) for t in range(T)],
                    "safety_shortfall": [round(S[f.id, t].solution_value(), 3) for t in range(T)],
                }
            )
        for g in pb.groups:
            load = [sum(f.hours_per_unit.get(g.id, 0.0) * P[f.id, t].solution_value() for f in pb.families) for t in range(T)]
            out["groups"].append(
                {
                    "id": g.id,
                    "capacity_hours": g.capacity_hours,
                    "load_hours": [round(v, 2) for v in load],
                    "overtime_hours": [round(O[g.id, t].solution_value(), 2) for t in range(T)],
                    "utilization": [round(load[t] / g.capacity_hours[t], 4) if g.capacity_hours[t] else None for t in range(T)],
                    "shadow_price": [round(caps[g.id, t].dual_value(), 4) if not pb.integer else None for t in range(T)],
                }
            )
        return out
