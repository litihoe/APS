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
    # Perturbation vs. the previous plan (reactive re-solves only; 0 otherwise).
    deviation: int = 0
    reassigned_ops: int = 0


@dataclass
class ReschedulePolicy:
    """Turns a static solve into a *minimal-perturbation* reactive re-solve.

    A dynamic shop re-plans constantly (a machine breaks, a rush order lands,
    actuals drift). Re-optimising from scratch is correct but *nervous*: it can
    reshuffle the whole floor for a trivial gain, which operators rightly
    distrust. This policy keeps the new plan close to the one already on the
    floor:

    * **Freeze.** Operations that have started, or start within
      ``freeze_horizon`` of ``now`` in the previous plan, are pinned to their
      previous machine / operator / start time — you cannot un-start a job.
    * **Warm start.** Every other operation hints CP-SAT toward its previous
      assignment, so the solver explores near the incumbent first.
    * **Stability objective.** Changing an operation's machine, or shifting its
      start, is penalised. This sits *below* tardiness (on-time delivery still
      wins) but *above* makespan, so the plan only churns when it actually
      buys due-date performance.
    """

    now: int = 0
    freeze_horizon: int = 0
    previous: "Schedule | None" = None
    pinned_op_ids: frozenset[str] = frozenset()
    machine_change_penalty: int = 100
    start_shift_penalty: int = 1

    def resolve_pins(self) -> set[str]:
        pinned = set(self.pinned_op_ids)
        if self.previous is not None:
            cutoff = self.now + self.freeze_horizon
            for so in self.previous.operations:
                if so.start < cutoff:
                    pinned.add(so.op_id)
        return pinned

    def prior_by_op(self) -> dict[str, "ScheduledOp"]:
        if self.previous is None:
            return {}
        return {so.op_id: so for so in self.previous.operations}


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


def solve(
    problem: ShopProblem,
    max_time_s: float = 20.0,
    policy: "ReschedulePolicy | None" = None,
) -> Schedule:
    model = cp_model.CpModel()
    H = problem.horizon

    pinned = policy.resolve_pins() if policy else set()
    prior = policy.prior_by_op() if policy else {}
    now = policy.now if policy else 0

    # Per-machine and per-operator interval buckets for NoOverlap.
    machine_intervals: dict[str, list] = {m: [] for m in problem.machines}
    operator_intervals: dict[str, list] = {p: [] for p in problem.operators}
    tool_intervals: dict[str, list] = {t: [] for t in problem.tools}

    # Decision variables, keyed by operation id.
    start: dict[str, cp_model.IntVar] = {}
    end: dict[str, cp_model.IntVar] = {}
    mach_lit: dict[str, dict[str, cp_model.IntVar]] = {}
    oper_lit: dict[str, dict[str, cp_model.IntVar]] = {}
    # Stability (deviation-from-previous-plan) penalty terms.
    deviation_terms: list = []
    reassign_lits: list = []

    for op in problem.all_operations():
        job = problem.jobs[op.job_id]
        prev = prior.get(op.id)
        is_pinned = op.id in pinned and prev is not None
        # Non-pinned work can't be scheduled into the past; pinned work keeps
        # its (possibly already-started) previous start, so it floors at 0.
        lo = 0 if is_pinned else max(job.release_time, now)
        s = model.NewIntVar(lo, H, f"start_{op.id}")
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

        # ---- Reactive rescheduling: pin / warm-start / penalise drift ------ #
        if prev is not None:
            prev_m_ok = prev.machine_id in lits
            if is_pinned and prev_m_ok:
                # Freeze this operation exactly where it already is.
                model.Add(s == prev.start)
                model.Add(lits[prev.machine_id] == 1)
                if prev.operator_id and prev.operator_id in oper_lit[op.id]:
                    model.Add(oper_lit[op.id][prev.operator_id] == 1)
            else:
                # Warm start from the incumbent, then penalise moving away.
                model.AddHint(s, min(max(prev.start, lo), H))
                if prev_m_ok:
                    for m_id, lit in lits.items():
                        model.AddHint(lit, 1 if m_id == prev.machine_id else 0)
                    # Machine reassignment penalty (1 - stayed-on-prev-machine).
                    if policy.machine_change_penalty:
                        deviation_terms.append(
                            policy.machine_change_penalty
                            * (1 - lits[prev.machine_id])
                        )
                        reassign_lits.append(1 - lits[prev.machine_id])
                # Absolute start-time shift penalty.
                if policy.start_shift_penalty:
                    shift = model.NewIntVar(0, H, f"shift_{op.id}")
                    model.AddAbsEquality(shift, s - prev.start)
                    deviation_terms.append(policy.start_shift_penalty * shift)

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

    # Stability term (0 for a from-scratch solve, so the static objective is
    # recovered exactly). Bounded so its coefficient can sit strictly between
    # tardiness and makespan in the lexicographic objective.
    n_ops = len(problem.all_operations())
    dev_ub = (
        n_ops * (policy.machine_change_penalty + policy.start_shift_penalty * H)
        if policy
        else 0
    )
    deviation = model.NewIntVar(0, max(dev_ub, 1), "deviation")
    model.Add(deviation == (sum(deviation_terms) if deviation_terms else 0))

    n_reassigned = model.NewIntVar(0, n_ops, "n_reassigned")
    model.Add(n_reassigned == (sum(reassign_lits) if reassign_lits else 0))

    # Lexicographic: tardiness  >>  stability  >>  makespan.
    # k_dev makes one unit of deviation outweigh any makespan (<= H); k_tard
    # makes one unit of weighted tardiness outweigh all deviation + makespan.
    k_dev = H + 1
    k_tard = dev_ub * k_dev + H + 1
    model.Minimize(weighted_tardiness * k_tard + deviation * k_dev + makespan)

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
        deviation=solver.Value(deviation),
        reassigned_ops=solver.Value(n_reassigned),
    )
