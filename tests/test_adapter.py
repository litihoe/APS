"""Round-trip test for the Frappe<->solver adapter (no bench required).

Builds the wire payload exactly as ``frappe_io.collect_payload`` would emit it,
converts to a ShopProblem, solves, and maps back to wall-clock rows — then
checks the conversions (datetime->minutes->datetime, material gating, OEE
quality inflation, maintenance windows) survived the trip.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "aps_planner"))

from aps_planner.engine.adapter import build_problem, schedule_to_rows  # noqa: E402
from aps_solver import solve  # noqa: E402

H_START = datetime(2026, 6, 10, 8, 0)


def _payload() -> dict:
    return {
        "horizon_start": H_START.isoformat(),
        "horizon_minutes": 2 * 24 * 60,
        "machines": [
            {"id": "MILL-1", "performance_factor": 1.0,
             "maintenance_windows": [
                 # 2h maintenance starting 1h into the horizon
                 {"from": "2026-06-10T09:00:00", "to": "2026-06-10T11:00:00"}]},
            {"id": "MILL-2", "performance_factor": 0.8,
             "availability_derate": 1.0, "maintenance_windows": []},
        ],
        "operators": [
            {"id": "OP-1", "name": "Ana", "skills": ["milling"],
             "shifts": [{"from": "2026-06-10T08:00:00",
                         "to": "2026-06-11T08:00:00"},
                        {"from": "2026-06-11T08:00:00",
                         "to": "2026-06-12T08:00:00"}]},
        ],
        "tools": [{"id": "FIX-A", "name": "Fixture A", "quantity": 1}],
        "jobs": [
            {
                "id": "WO-1", "name": "WO-1 (bracket)",
                "due_date": "2026-06-10T20:00:00",
                "release_time": "2026-06-10T08:00:00",
                "weight": 3,
                "operations": [
                    {"id": "WO-1::1", "index": 1, "operation": "Milling",
                     "eligible_machines": ["MILL-1", "MILL-2"],
                     "run_minutes": 100, "setup_minutes": 20,
                     "skill": "milling", "tool": "FIX-A",
                     "quality_yield": 0.8},
                ],
            },
            {
                "id": "WO-2", "name": "WO-2 (housing)",
                "due_date": "2026-06-11T20:00:00",
                # Material arrives 4h after horizon start -> gate at minute 240
                "release_time": "2026-06-10T12:00:00",
                "weight": 1,
                "operations": [
                    {"id": "WO-2::1", "index": 1, "operation": "Milling",
                     "eligible_machines": ["MILL-1", "MILL-2"],
                     "run_minutes": 60, "setup_minutes": 10,
                     "skill": "milling", "tool": "FIX-A"},
                ],
            },
        ],
    }


def test_problem_conversion():
    problem = build_problem(_payload())
    # Maintenance window: 09:00-11:00 -> minutes 60..180.
    assert problem.machines["MILL-1"].maintenance_windows == [(60, 180)]
    # Material gate: 12:00 -> minute 240.
    assert problem.jobs["WO-2"].release_time == 240
    # OEE Quality 0.8 inflates 100 min -> 125 min.
    assert problem.jobs["WO-1"].operations[0].run_time == 125
    # Operator shift windows converted and merged into the horizon.
    assert problem.operators["OP-1"].shifts[0] == (0, 1440)


def test_round_trip_rows():
    payload = _payload()
    problem = build_problem(payload)
    schedule = solve(problem, max_time_s=10.0)
    assert schedule.status in ("OPTIMAL", "FEASIBLE")

    rows = schedule_to_rows(schedule, payload)
    assert len(rows) == 2
    by_wo = {r["work_order"]: r for r in rows}

    # Material gate honoured in wall-clock terms.
    assert by_wo["WO-2"]["planned_start"] >= datetime(2026, 6, 10, 12, 0)
    # Rows carry everything the DocType needs.
    for r in rows:
        assert r["workstation"] in ("MILL-1", "MILL-2")
        assert r["operator"] == "OP-1"
        assert r["tool"] == "FIX-A"
        assert r["planned_start"] < r["planned_end"]
        assert r["operation"] == "Milling"

    # Single fixture (qty 1) -> the two ops cannot overlap in wall-clock time.
    a, b = sorted(rows, key=lambda r: r["planned_start"])
    assert a["planned_end"] <= b["planned_start"]


def test_unschedulable_machine_rejected():
    payload = _payload()
    payload["jobs"][0]["operations"][0]["eligible_machines"] = ["GHOST-1"]
    try:
        build_problem(payload)
    except ValueError as e:
        assert "no eligible machine" in str(e)
    else:
        raise AssertionError("expected ValueError for unknown machine")


if __name__ == "__main__":
    test_problem_conversion()
    test_round_trip_rows()
    test_unschedulable_machine_rejected()
    print("All adapter tests passed.")
