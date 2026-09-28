"""Stand-alone HTTP wrapper of the Planning Engine (``python -m monxuplan_engine.service``).

The platform calls the engine in-process from its workers; this wrapper lets the engine run as an
independent service (horizontal scaling, other clients) with the same JSON contract.
"""

from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException

from .compile import ENGINE_VERSION, compile_problem
from .contract import Problem, Solution
from .feasibility import check
from .mrp import MrpProblem, run_mrp
from .pipeline import solve
from .providers.mip import AggregateProblem, MipProvider, NotSupported

app = FastAPI(title="MonxuPlan Planning Engine", version=ENGINE_VERSION)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "engine_version": ENGINE_VERSION}


@app.post("/solve", response_model=Solution)
def solve_endpoint(problem: Problem) -> Solution:
    try:
        return solve(problem)
    except NotSupported as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/feasibility")
def feasibility_endpoint(problem: Problem) -> dict:
    return check(compile_problem(problem))


@app.post("/mrp")
def mrp_endpoint(problem: MrpProblem) -> dict:
    return run_mrp(problem)


@app.post("/aggregate")
def aggregate_endpoint(problem: AggregateProblem) -> dict:
    return MipProvider().solve_aggregate(problem)


def main() -> None:  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host=os.environ.get("MONXU_ENGINE_HOST", "0.0.0.0"), port=int(os.environ.get("MONXU_ENGINE_PORT", "8100")))


if __name__ == "__main__":  # pragma: no cover
    main()
