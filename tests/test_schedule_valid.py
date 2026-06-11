"""Validate that solver output actually honours every constraint family.

A schedule that *looks* plausible isn't enough — these tests independently
re-check the four constraint families against the returned solution, so the
CP-SAT model can't silently drift.

Run with:  python -m pytest tests/ -v     (or)     python tests/test_schedule_valid.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aps_solver import solve  # noqa: E402
from aps_solver.data import build_problem  # noqa: E402
from aps_solver.solver import _off_shift_windows  # noqa: E402


def _overlap(a_start, a_end, b_start, b_end) -> bool:
    return a_start < b_end and b_start < a_end


def _solved():
    problem = build_problem(n_jobs=12, seed=42)
    sched = solve(problem, max_time_s=15.0)
    assert sched.status in ("OPTIMAL", "FEASIBLE"), sched.status
    return problem, sched


def test_machines_no_overlap_and_maintenance():
    problem, sched = _solved()
    by_machine: dict[str, list] = {}
    for so in sched.operations:
        by_machine.setdefault(so.machine_id, []).append(so)
    for m_id, ops in by_machine.items():
        ops = sorted(ops, key=lambda x: x.start)
        for a, b in zip(ops, ops[1:]):
            assert not _overlap(a.start, a.end, b.start, b.end), (
                f"machine {m_id} double-booked: {a.op_id} vs {b.op_id}")
        # No op overlaps a maintenance window (OEE Availability).
        for w_s, w_e in problem.machines[m_id].maintenance_windows:
            for so in ops:
                assert not _overlap(so.start, so.end, w_s, w_e), (
                    f"{so.op_id} runs during maintenance on {m_id}")


def test_operators_no_overlap_skill_and_shift():
    problem, sched = _solved()
    by_op: dict[str, list] = {}
    for so in sched.operations:
        if so.operator_id is None:
            continue
        by_op.setdefault(so.operator_id, []).append(so)
    op_index = {o.id: o for o in problem.all_operations()}
    for p_id, ops in by_op.items():
        operator = problem.operators[p_id]
        off = _off_shift_windows(operator.shifts, problem.horizon)
        setups = sorted(((s.start, s.setup_end, s.op_id) for s in ops))
        # Operators only busy during setup; setups must not overlap each other.
        for (s1, e1, id1), (s2, e2, id2) in zip(setups, setups[1:]):
            assert not _overlap(s1, e1, s2, e2), (
                f"operator {p_id} double-booked: {id1} vs {id2}")
        for s, e, op_id in setups:
            # Skill check.
            assert op_index[op_id].required_skill in operator.skills, (
                f"{p_id} lacks skill for {op_id}")
            # On-shift check (setup must not touch any off-shift window).
            for w_s, w_e in off:
                assert not _overlap(s, e, w_s, w_e), (
                    f"{p_id} setup for {op_id} falls off-shift")


def test_material_gate_and_precedence():
    problem, sched = _solved()
    start_of = {so.op_id: so.start for so in sched.operations}
    end_of = {so.op_id: so.end for so in sched.operations}
    for job in problem.jobs.values():
        for op in job.operations:
            assert start_of[op.id] >= job.release_time, (
                f"{op.id} starts before material release")
        ordered = sorted(job.operations, key=lambda o: o.index)
        for prev, nxt in zip(ordered, ordered[1:]):
            assert end_of[prev.id] <= start_of[nxt.id], (
                f"precedence violated {prev.id} -> {nxt.id}")


def test_tooling_capacity():
    problem, sched = _solved()
    # For each tool, no point in time exceeds its quantity.
    for t_id, tool in problem.tools.items():
        intervals = [(s.start, s.end) for s in sched.operations
                     if s.tool_id == t_id]
        edges = sorted({t for iv in intervals for t in iv})
        for t in edges:
            concurrent = sum(1 for s, e in intervals if s <= t < e)
            assert concurrent <= tool.quantity, (
                f"tool {t_id} over-allocated ({concurrent} > {tool.quantity}) at {t}")


if __name__ == "__main__":
    test_machines_no_overlap_and_maintenance()
    test_operators_no_overlap_skill_and_shift()
    test_material_gate_and_precedence()
    test_tooling_capacity()
    print("All constraint-validation tests passed.")
