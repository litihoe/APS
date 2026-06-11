"""Reactive rescheduling tests for the minimal-perturbation re-solve.

Scenario: solve a small shop, then disrupt it (a machine goes down mid-shift)
and re-solve with a ReschedulePolicy. We assert the three guarantees a dynamic
APS must give the floor:

  1. Frozen work does not move (pinned ops keep machine + start).
  2. Nothing is scheduled into the past.
  3. The re-solve is *stable*: with no disruption it reproduces the incumbent
     (zero deviation), and a disruption only moves what it must.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aps_solver import (  # noqa: E402
    Job,
    Machine,
    Operation,
    Operator,
    ReschedulePolicy,
    ShopProblem,
    Tool,
    solve,
)


def _problem(machines=("M1", "M2")) -> ShopProblem:
    mids = {
        m: Machine(id=m, name=m, performance_factor=1.0) for m in machines
    }
    ops_per_job = {}
    jobs = {}
    for j in range(4):
        ops = (
            Operation(
                id=f"J{j}::1", job_id=f"J{j}", index=1,
                eligible_machines=tuple(machines), run_time=60, setup_time=10,
                required_skill="mill", required_tool="FIX",
            ),
            Operation(
                id=f"J{j}::2", job_id=f"J{j}", index=2,
                eligible_machines=tuple(machines), run_time=45, setup_time=10,
                required_skill="mill", required_tool="FIX",
            ),
        )
        ops_per_job[f"J{j}"] = ops
        jobs[f"J{j}"] = Job(
            id=f"J{j}", name=f"J{j}", operations=ops,
            due_date=600, release_time=0, weight=1,
        )
    return ShopProblem(
        machines=mids,
        operators={
            "OP1": Operator(id="OP1", name="OP1", skills=frozenset({"mill"})),
            "OP2": Operator(id="OP2", name="OP2", skills=frozenset({"mill"})),
        },
        tools={"FIX": Tool(id="FIX", name="FIX", quantity=2)},
        jobs=jobs,
        horizon=2000,
    )


def test_no_disruption_is_stable():
    """Re-solving an unchanged shop must not churn the plan (zero deviation)."""
    base = solve(_problem(), max_time_s=10.0)
    assert base.status in ("OPTIMAL", "FEASIBLE")

    policy = ReschedulePolicy(now=0, freeze_horizon=0, previous=base)
    resolved = solve(_problem(), max_time_s=10.0, policy=policy)
    assert resolved.status in ("OPTIMAL", "FEASIBLE")

    # Same delivery performance, and the stability term kept assignments put.
    assert resolved.weighted_tardiness == base.weighted_tardiness
    assert resolved.reassigned_ops == 0
    base_machine = {so.op_id: so.machine_id for so in base.operations}
    for so in resolved.operations:
        assert so.machine_id == base_machine[so.op_id]


def test_freeze_window_pins_in_progress_work():
    """Work inside the freeze window keeps its machine and start exactly."""
    base = solve(_problem(), max_time_s=10.0)
    now = 70  # mid-flight
    freeze = 30  # anything starting before t=100 is committed

    base_by_op = {so.op_id: so for so in base.operations}
    policy = ReschedulePolicy(now=now, freeze_horizon=freeze, previous=base)
    resolved = solve(_problem(), max_time_s=10.0, policy=policy)
    assert resolved.status in ("OPTIMAL", "FEASIBLE")

    res_by_op = {so.op_id: so for so in resolved.operations}
    pinned = {oid for oid, so in base_by_op.items() if so.start < now + freeze}
    assert pinned, "expected at least one op in the freeze window"
    for oid in pinned:
        assert res_by_op[oid].start == base_by_op[oid].start
        assert res_by_op[oid].machine_id == base_by_op[oid].machine_id

    # Non-frozen work may move, but never into the past.
    for oid, so in res_by_op.items():
        if oid not in pinned:
            assert so.start >= now


def test_machine_breakdown_reroutes_minimally():
    """If M2 dies, its work must move to M1 and nothing schedules onto M2."""
    base = solve(_problem(machines=("M1", "M2")), max_time_s=10.0)
    assert any(so.machine_id == "M2" for so in base.operations)

    # M2 is gone for the rest of the horizon.
    broken = _problem(machines=("M1", "M2"))
    broken.machines["M2"] = Machine(
        id="M2", name="M2", performance_factor=1.0,
        maintenance_windows=[(0, broken.horizon)],
    )
    policy = ReschedulePolicy(now=0, freeze_horizon=0, previous=base)
    resolved = solve(broken, max_time_s=15.0, policy=policy)
    assert resolved.status in ("OPTIMAL", "FEASIBLE")

    assert all(so.machine_id == "M1" for so in resolved.operations)
    # Only the ops that were on M2 should count as reassigned.
    on_m2 = sum(1 for so in base.operations if so.machine_id == "M2")
    assert resolved.reassigned_ops == on_m2


if __name__ == "__main__":
    test_no_disruption_is_stable()
    test_freeze_window_pins_in_progress_work()
    test_machine_breakdown_reroutes_minimally()
    print("All reactive-rescheduling tests passed.")
