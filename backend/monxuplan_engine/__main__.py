"""Command line: ``python -m monxuplan_engine solve problem.json [-o solution.json]``."""

from __future__ import annotations

import argparse
import json
import sys

from pydantic import ValidationError

from .contract import Problem
from .pipeline import solve


def _positive(v: str) -> float:
    x = float(v)
    if x <= 0:
        raise argparse.ArgumentTypeError("must be a positive number of seconds")
    return x


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="monxuplan_engine")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("solve", help="solve a problem JSON file")
    s.add_argument("problem")
    s.add_argument("-o", "--output")
    # mip plans aggregate volumes only (MPS screen / API), it cannot build a detailed schedule
    s.add_argument("--provider", choices=["heuristic", "cpsat", "hybrid"])
    s.add_argument("--time-limit", type=_positive)
    sub.add_parser("schema", help="print the JSON schema of the Problem contract")
    args = ap.parse_args(argv)
    if args.cmd == "schema":
        json.dump(Problem.model_json_schema(), sys.stdout, indent=2)
        return 0
    try:
        with open(args.problem, encoding="utf-8") as fh:
            raw = fh.read()
        data = json.loads(raw)
        if args.provider:
            data.setdefault("solver", {})["provider"] = args.provider
        if args.time_limit:
            data.setdefault("solver", {})["time_limit_s"] = args.time_limit
        problem = Problem.model_validate(data)  # overrides are validated with the rest of the problem
    except OSError as exc:
        print(f"error: cannot read {args.problem}: {exc.strerror}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"error: {args.problem} is not valid JSON: {exc}", file=sys.stderr)
        return 2
    except ValidationError as exc:
        print(f"error: {args.problem} is not a valid problem ({exc.error_count()} errors):", file=sys.stderr)
        for e in exc.errors()[:20]:
            print(f"  {'.'.join(str(x) for x in e['loc'])}: {e['msg']}", file=sys.stderr)
        return 2

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
