"""Run the APS solver prototype on synthetic HMLV data.

    python run.py                 # default 12-job shop
    python run.py --jobs 20       # heavier load
    python run.py --jobs 16 --seed 7 --time 30
"""

from __future__ import annotations

import argparse

from aps_solver import solve
from aps_solver.data import build_problem
from aps_solver.report import render


def main() -> None:
    ap = argparse.ArgumentParser(description="APS FJSSP solver prototype")
    ap.add_argument("--jobs", type=int, default=12, help="number of work orders")
    ap.add_argument("--seed", type=int, default=42, help="RNG seed")
    ap.add_argument("--time", type=float, default=20.0, help="solver time budget (s)")
    args = ap.parse_args()

    problem = build_problem(n_jobs=args.jobs, seed=args.seed)
    n_ops = len(problem.all_operations())
    print(f"Built shop: {len(problem.jobs)} jobs / {n_ops} operations / "
          f"{len(problem.machines)} machines / {len(problem.operators)} operators")
    sched = solve(problem, max_time_s=args.time)
    print(render(problem, sched))


if __name__ == "__main__":
    main()
