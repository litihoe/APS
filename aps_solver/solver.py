"""CP-SAT model for the Flexible Job-Shop Scheduling Problem (FJSSP) with
machines, operators+skills, material gating and tooling/fixtures.

Modelling summary
-----------------
For every operation we create a global ``start``/``end`` pair and then layer the
four resource families on top:

* Machines (flexible + precedence + OEE):
    - One optional interval per *eligible* machine; exactly one is selected.
    - Duration on machine m = setup + ceil(run / performance_factor[m])  (OEE
      Performance lengthens the run; Quality/scrap is already folded into run).
    - ``NoOverlap`` per machine, with planned-maintenance windows (OEE
      Availability) injected as fixed blocking intervals.
    - Routing precedence: op[i].end <= op[i+1].start within a job.

* Operators (human time + skills + shifts):
    - An operation that needs a skill is attended *during setup only*; the run
      proceeds unattended (lights-out), so one operator can cover several
      machines.
    - One optional setup-interval per *qualified* operator; exactly one chosen.
    - ``NoOverlap`` per operator, with off-shift time injected as blocking
      intervals (so setups only happen on-shift).

* Material readiness:
    - Every operation starts no earlier than its job's ``release_time``.

* Tooling / fixtures:
    - An operation holds one unit of its required tool for its whole duration.
    - ``AddCumulative`` per tool type with capacity = quantity.

Objective: minimise weighted tardiness (on-time delivery), with makespan as a
secondary tie-breaker.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ortools.sat.python import cp_model

from .model import Operation, ShopProblem


@dataclass
class ScheduledOp:
    op_id: str
    job_id: str
    machine_id: str
    operator_id: str | None
    tool_id: str | None
    start: int
    setup_end: int
    end: int


@dataclass
class Schedule:
    status: str
    objective: float
    weighted_tardiness: int
    makespan: int
    operations: list[ScheduledOp]
    job_tardiness: dict[str, int]
    solve_time_s: float


def _effective_run(op: Operation, performance_factor: float) -> int:
    """OEE Performance: a slower machine inflates the nominal run time."""
    if performance_factor <= 0:
        raise ValueError(f"performance_factor must be > 0 for op {op.id}")
    return math.ceil(op.run_time / performance_factor)


def _off_shift_windows(
    shifts: list[tuple[int, int]], horizon: int
) -> list[tuple[int, int]]:
    """Complement of the shift windows over [0, horizon] -> blocked time."""
    if not shifts:
        return []  # no shift info => treat as always available
    blocked: list[tuple[int, int]] = []
    cursor = 0
    for start, end in sorted(shifts):
        if start > cursor:
            blocked.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < horizon:
        blocked.append((cursor, horizon))
    return blocked


def solve(problem: ShopProblem, max_time_s: float = 20.0) -> Schedule:
    model = cp_model.CpModel()
    H = problem.horizon

    # Per-machine and per-operator interval buckets for NoOverlap.
    machine_intervals: dict[str, list] = {m: [] for m in problem.machines}
    operator_intervals: dict[str, list] = {p: [] for p in problem.operators}
    tool_intervals: dict[str, list] = {t: [] for t in problem.tools}

    # Decision variables, keyed by operation id.
    start: dict[str, cp_model.IntVar] = {}
    end: dict[str, cp_model.IntVar] = {}
    mach_lit: dict[str, dict[str, cp_model.IntVar]] = {}
    oper_lit: dict[str, dict[str, cp_model.IntVar]] = {}

    for op in problem.all_operations():
        job = problem.jobs[op.job_id]
        s = model.NewIntVar(job.release_time, H, f"start_{op.id}")
        e = model.NewIntVar(0, H, f"end_{op.id}")
        size = model.NewIntVar(0, H, f"size_{op.id}")
        model.Add(size == e - s)
        start[op.id] = s
        end[op.id] = e

        # ---- Machine selection (flexible) + OEE Performance duration -------- #
        lits = {}
        setup_end = model.NewIntVar(0, H, f"setupend_{op.id}")
        for m_id in op.eligible_machines:
            machine = problem.machines[m_id]
            dur = op.setup_time + _effective_run(op, machine.performance_factor)
            lit = model.NewBoolVar(f"on_{op.id}_{m_id}")
            lits[m_id] = lit
            iv = model.NewOptionalIntervalVar(s, dur, e, lit, f"mi_{op.id}_{m_id}")
            machine_intervals[m_id].append(iv)
            # When this machine is chosen, pin the total duration and setup end.
            model.Add(e == s + dur).OnlyEnforceIf(lit)
            model.Add(setup_end == s + op.setup_time).OnlyEnforceIf(lit)
        model.AddExactlyOne(lits.values())
        mach_lit[op.id] = lits

        # ---- Operator selection (skill) attended during setup -------------- #
        oper_lit[op.id] = {}
        if op.required_skill is not None:
            qualified = [
                p_id
                for p_id, p in problem.operators.items()
                if op.required_skill in p.skills
            ]
            if not qualified:
                raise ValueError(
                    f"No operator has skill '{op.required_skill}' for op {op.id}"
                )
            olits = {}
            for p_id in qualified:
                olit = model.NewBoolVar(f"by_{op.id}_{p_id}")
                olits[p_id] = olit
                # Operator busy from start to setup_end (the attended setup).
                oiv = model.NewOptionalIntervalVar(
                    s, op.setup_time, setup_end, olit, f"oi_{op.id}_{p_id}"
                )
                operator_intervals[p_id].append(oiv)
            model.AddExactlyOne(olits.values())
            oper_lit[op.id] = olits

        # ---- Tooling / fixture (held for whole operation) ------------------ #
        if op.required_tool is not None:
            if op.required_tool not in problem.tools:
                raise ValueError(f"Unknown tool '{op.required_tool}' for op {op.id}")
            tiv = model.NewIntervalVar(s, size, e, f"ti_{op.id}")
            tool_intervals[op.required_tool].append(tiv)

    # ---- Routing precedence within each job -------------------------------- #
    for job in problem.jobs.values():
        ordered = sorted(job.operations, key=lambda o: o.index)
        for prev, nxt in zip(ordered, ordered[1:]):
            model.Add(end[prev.id] <= start[nxt.id])

    # ---- Machine NoOverlap + maintenance (OEE Availability) ---------------- #
    for m_id, machine in problem.machines.items():
        ivs = list(machine_intervals[m_id])
        for w_start, w_end in machine.maintenance_windows:
            ivs.append(
                model.NewIntervalVar(
                    w_start, w_end - w_start, w_end, f"maint_{m_id}_{w_start}"
                )
            )
        model.AddNoOverlap(ivs)

    # ---- Operator NoOverlap + off-shift blocking --------------------------- #
    for p_id, operator in problem.operators.items():
        ivs = list(operator_intervals[p_id])
        for w_start, w_end in _off_shift_windows(operator.shifts, H):
            ivs.append(
                model.NewIntervalVar(
                    w_start, w_end - w_start, w_end, f"off_{p_id}_{w_start}"
                )
            )
        model.AddNoOverlap(ivs)

    # ---- Tool cumulative (limited fixtures) -------------------------------- #
    for t_id, tool in problem.tools.items():
        ivs = tool_intervals[t_id]
        if ivs:
            model.AddCumulative(ivs, [1] * len(ivs), tool.quantity)

    # ---- Objective: weighted tardiness (primary) + makespan (secondary) ---- #
    tardiness_terms = []
    job_tardiness_vars: dict[str, cp_model.IntVar] = {}
    last_ends = []
    for job in problem.jobs.values():
        last_op = max(job.operations, key=lambda o: o.index)
        tard = model.NewIntVar(0, H, f"tard_{job.id}")
        model.Add(tard >= end[last_op.id] - job.due_date)
        job_tardiness_vars[job.id] = tard
        tardiness_terms.append(job.weight * tard)
        last_ends.append(end[last_op.id])

    makespan = model.NewIntVar(0, H, "makespan")
    model.AddMaxEquality(makespan, last_ends)

    weighted_tardiness = model.NewIntVar(0, H * 10_000, "weighted_tardiness")
    model.Add(weighted_tardiness == sum(tardiness_terms))
    # Lexicographic-ish: tardiness dominates, makespan breaks ties.
    model.Minimize(weighted_tardiness * (H + 1) + makespan)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max_time_s
    solver.parameters.num_search_workers = 8
    status = solver.Solve(model)

    status_name = solver.StatusName(status)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return Schedule(
            status=status_name,
            objective=float("inf"),
            weighted_tardiness=-1,
            makespan=-1,
            operations=[],
            job_tardiness={},
            solve_time_s=solver.WallTime(),
        )

    scheduled: list[ScheduledOp] = []
    for op in problem.all_operations():
        chosen_m = next(
            m for m, lit in mach_lit[op.id].items() if solver.Value(lit) == 1
        )
        chosen_p = None
        if oper_lit[op.id]:
            chosen_p = next(
                p for p, lit in oper_lit[op.id].items() if solver.Value(lit) == 1
            )
        s = solver.Value(start[op.id])
        e = solver.Value(end[op.id])
        scheduled.append(
            ScheduledOp(
                op_id=op.id,
                job_id=op.job_id,
                machine_id=chosen_m,
                operator_id=chosen_p,
                tool_id=op.required_tool,
                start=s,
                setup_end=s + op.setup_time,
                end=e,
            )
        )

    return Schedule(
        status=status_name,
        objective=solver.ObjectiveValue(),
        weighted_tardiness=solver.Value(weighted_tardiness),
        makespan=solver.Value(makespan),
        operations=sorted(scheduled, key=lambda x: x.start),
        job_tardiness={
            j: solver.Value(v) for j, v in job_tardiness_vars.items()
        },
        solve_time_s=solver.WallTime(),
    )
