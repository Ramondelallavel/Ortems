"""Command line: ``python -m monxuplan_engine solve problem.json [-o solution.json]``."""

from __future__ import annotations

import argparse
import json
import sys

from .contract import Problem
from .pipeline import solve


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="monxuplan_engine")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("solve", help="solve a problem JSON file")
    s.add_argument("problem")
    s.add_argument("-o", "--output")
    s.add_argument("--provider")
    s.add_argument("--time-limit", type=float)
    sub.add_parser("schema", help="print the JSON schema of the Problem contract")
    args = ap.parse_args(argv)
    if args.cmd == "schema":
        json.dump(Problem.model_json_schema(), sys.stdout, indent=2)
        return 0
    with open(args.problem, encoding="utf-8") as fh:
        problem = Problem.model_validate_json(fh.read())
    if args.provider:
        problem.solver.provider = args.provider
    if args.time_limit:
        problem.solver.time_limit_s = args.time_limit

    def progress(step, frac, detail):
        print(f"[{step}] {detail or ''}", file=sys.stderr)

    sol = solve(problem, progress=progress)
    out = sol.model_dump_json(indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(out)
    else:
        print(out)
    md = sol.solver_metadata
    print(f"status={md.status} feasible={sol.feasible} objective={md.objective} gap={md.gap} runtime={md.runtime_s}s", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
