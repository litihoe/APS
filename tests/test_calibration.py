"""Tests for the OEE feedback loop (pure calibration math, no bench).

The contract under test: factors are *nudged* toward observed reality (EWMA),
never replaced; thin evidence leaves a factor untouched; results stay inside
sane clamps; and the recalibrated factors actually change what the solver
plans (slower machine -> longer planned duration).
"""

from __future__ import annotations

import math
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "aps_planner"))

from aps_planner.engine.calibration import (  # noqa: E402
    CLAMP_LO,
    DowntimeObservation,
    Factors,
    ObservedOp,
    calibrate,
)


def _ops(ws: str, n: int, planned: float, actual: float,
         good: float = 10, scrap: float = 0) -> list[ObservedOp]:
    return [
        ObservedOp(workstation=ws, planned_minutes=planned,
                   actual_minutes=actual, qty_good=good, qty_scrap=scrap)
        for _ in range(n)
    ]


def test_slow_machine_drags_performance_down():
    """Ops consistently taking 25% longer than planned -> P factor drops,
    but only by the EWMA step, not all the way to the observation."""
    current = {"MILL-1": Factors()}
    # planned 60, actual 75 -> observed P = 0.8
    res = calibrate(current, _ops("MILL-1", 6, 60, 75), alpha=0.3)["MILL-1"]
    assert math.isclose(res.new.performance_factor, 0.94, abs_tol=0.001)
    assert res.new.quality_yield == 1.0  # no scrap observed, Q nudges to 1.0
    assert "0.80" in res.note


def test_thin_evidence_changes_nothing():
    """Fewer than min_samples ops -> every factor kept as-is."""
    current = {"MILL-1": Factors(performance_factor=0.9, quality_yield=0.95)}
    res = calibrate(current, _ops("MILL-1", 3, 60, 90), min_samples=5)["MILL-1"]
    assert res.new == Factors(performance_factor=0.9, quality_yield=0.95)
    assert "kept" in res.note


def test_scrap_lowers_quality_yield():
    current = {"EDM-1": Factors()}
    # 90 good / 10 scrap -> observed Q = 0.9; EWMA 0.3 -> 0.97
    res = calibrate(
        current, _ops("EDM-1", 5, 60, 60, good=18, scrap=2), alpha=0.3
    )["EDM-1"]
    assert math.isclose(res.new.quality_yield, 0.97, abs_tol=0.001)


def test_downtime_lowers_availability():
    current = {"MILL-1": Factors()}
    down = [DowntimeObservation("MILL-1", downtime_minutes=960,
                                scheduled_minutes=9600)]  # 10% down
    res = calibrate(current, [], down, alpha=0.5)["MILL-1"]
    assert math.isclose(res.new.availability_derate, 0.95, abs_tol=0.001)


def test_clamp_survives_garbage_actuals():
    """A data-entry disaster (actuals 100x planned) cannot crater the factor
    below the clamp floor."""
    current = {"MILL-1": Factors()}
    res = calibrate(current, _ops("MILL-1", 10, 60, 6000), alpha=1.0)["MILL-1"]
    assert res.new.performance_factor == CLAMP_LO


def test_unknown_workstation_ignored():
    current = {"MILL-1": Factors()}
    results = calibrate(current, _ops("GHOST-9", 10, 60, 90))
    assert set(results) == {"MILL-1"}
    assert results["MILL-1"].new == Factors()


def test_calibrated_factor_changes_the_plan():
    """End-to-end: a recalibrated performance factor lengthens the planned
    duration in the next solve (the loop actually closes)."""
    from aps_solver import Job, Machine, Operation, ShopProblem, solve

    def plan_with(perf: float) -> int:
        op = Operation(id="J1::1", job_id="J1", index=1,
                       eligible_machines=("M1",), run_time=100, setup_time=0)
        problem = ShopProblem(
            machines={"M1": Machine(id="M1", name="M1",
                                    performance_factor=perf)},
            operators={}, tools={},
            jobs={"J1": Job(id="J1", name="J1", operations=(op,),
                            due_date=500)},
            horizon=1000,
        )
        sched = solve(problem, max_time_s=5.0)
        assert sched.status in ("OPTIMAL", "FEASIBLE")
        return sched.operations[0].end - sched.operations[0].start

    res = calibrate({"M1": Factors()}, _ops("M1", 6, 60, 75), alpha=0.3)["M1"]
    assert plan_with(res.new.performance_factor) > plan_with(1.0)


if __name__ == "__main__":
    test_slow_machine_drags_performance_down()
    test_thin_evidence_changes_nothing()
    test_scrap_lowers_quality_yield()
    test_downtime_lowers_availability()
    test_clamp_survives_garbage_actuals()
    test_unknown_workstation_ignored()
    test_calibrated_factor_changes_the_plan()
    print("All OEE calibration tests passed.")
