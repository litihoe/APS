"""Synthetic high-mix / low-volume (HMLV) precision-shop data generator.

Produces a ``ShopProblem`` that resembles a small CNC/EDM job shop: a handful of
machine cells, a few multi-skilled operators on shifts, limited fixtures, and
many *different* small-quantity work orders (the defining trait of HMLV).

Deterministic given a seed, so runs are reproducible and testable.
"""

from __future__ import annotations

import random

from .model import Job, Machine, Operation, Operator, ShopProblem, Tool

# A routing template: list of (skill, tool, machine-family, base_run_minutes).
# Machine-family maps to the set of machines able to do that step.
_ROUTING_TEMPLATES = [
    # Turned part: turn -> mill -> deburr -> inspect
    [("turning", None, "lathe", 90),
     ("milling", "fixtureA", "mill", 120),
     ("deburr", None, "bench", 30),
     ("inspect", None, "cmm", 45)],
    # Milled bracket: mill -> mill -> deburr -> inspect
    [("milling", "fixtureA", "mill", 150),
     ("milling", "fixtureB", "mill", 100),
     ("deburr", None, "bench", 25),
     ("inspect", None, "cmm", 40)],
    # EDM detail: mill -> edm -> inspect
    [("milling", "fixtureB", "mill", 80),
     ("edm", None, "edm", 200),
     ("inspect", None, "cmm", 50)],
    # Simple shaft: turn -> inspect
    [("turning", None, "lathe", 60),
     ("inspect", None, "cmm", 30)],
]

_MACHINE_FAMILIES = {
    "lathe": [("LATHE-1", 1.0), ("LATHE-2", 0.9)],
    "mill": [("MILL-1", 1.0), ("MILL-2", 0.95), ("MILL-3", 0.85)],
    "edm": [("EDM-1", 0.9)],
    "bench": [("BENCH-1", 1.0)],
    "cmm": [("CMM-1", 1.0)],
}


def build_problem(
    n_jobs: int = 12,
    seed: int = 42,
    horizon: int = 8 * 60 * 7,  # one week of 8h-ish days in minutes
) -> ShopProblem:
    rng = random.Random(seed)

    # --- Machines (with OEE: performance + a maintenance window) ----------- #
    machines: dict[str, Machine] = {}
    for family, members in _MACHINE_FAMILIES.items():
        for m_id, perf in members:
            maint = []
            # Give one mill a planned maintenance block to exercise the feature.
            if m_id == "MILL-3":
                maint = [(2 * 480, 2 * 480 + 120)]  # day 3 morning, 2h
            machines[m_id] = Machine(
                id=m_id, name=m_id, performance_factor=perf, maintenance_windows=maint
            )

    # --- Operators (multi-skilled, two shifts of two people) --------------- #
    # Shift A 08:00-16:00, Shift B 16:00-24:00 across the horizon's days.
    days = horizon // (24 * 60) + 1
    shift_a = [(d * 1440 + 8 * 60, d * 1440 + 16 * 60) for d in range(days)]
    shift_b = [(d * 1440 + 16 * 60, d * 1440 + 24 * 60) for d in range(days)]
    operators = {
        "OP-Ana": Operator("OP-Ana", "Ana",
                           frozenset({"turning", "milling", "deburr"}), shift_a),
        "OP-Ben": Operator("OP-Ben", "Ben",
                           frozenset({"milling", "edm", "inspect"}), shift_a),
        "OP-Cy": Operator("OP-Cy", "Cy",
                          frozenset({"turning", "edm", "deburr"}), shift_b),
        "OP-Dee": Operator("OP-Dee", "Dee",
                          frozenset({"milling", "inspect", "deburr"}), shift_b),
    }

    # --- Tools / fixtures (the usual HMLV bottleneck) ---------------------- #
    tools = {
        "fixtureA": Tool("fixtureA", "Fixture A", quantity=1),
        "fixtureB": Tool("fixtureB", "Fixture B", quantity=2),
    }

    # --- Jobs: many small, varied work orders ----------------------------- #
    jobs: dict[str, Job] = {}
    for j in range(n_jobs):
        template = rng.choice(_ROUTING_TEMPLATES)
        job_id = f"WO-{1000 + j}"
        qty = rng.randint(1, 8)  # low volume
        ops = []
        for idx, (skill, tool, family, base_run) in enumerate(template):
            # Quantity scales run; setup is a fixed changeover cost.
            run = base_run + qty * rng.randint(3, 9)
            setup = rng.choice([20, 30, 45, 60]) if idx == 0 else rng.choice([10, 15, 25])
            eligible = tuple(m for m, _ in _MACHINE_FAMILIES[family])
            ops.append(
                Operation(
                    id=f"{job_id}-{idx}",
                    job_id=job_id,
                    index=idx,
                    eligible_machines=eligible,
                    run_time=run,
                    setup_time=setup,
                    required_skill=skill,
                    required_tool=tool,
                )
            )
        # Material readiness: some jobs gated a day or two out.
        release = rng.choice([0, 0, 0, 480, 960])
        # Due dates spread across the week, some tight (drives tardiness).
        due = release + rng.randint(600, horizon - 480)
        weight = rng.choice([1, 1, 1, 3, 5])  # a few hot/priority jobs
        jobs[job_id] = Job(
            id=job_id, name=job_id, operations=tuple(ops),
            due_date=due, release_time=release, weight=weight,
        )

    return ShopProblem(
        machines=machines, operators=operators, tools=tools, jobs=jobs,
        horizon=horizon,
    )
