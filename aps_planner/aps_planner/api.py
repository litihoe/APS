"""REST API for the planner UI.

    POST /api/method/aps_planner.api.run_schedule
    GET  /api/method/aps_planner.api.get_schedule
"""

from __future__ import annotations

import frappe

from .engine import frappe_io


@frappe.whitelist()
def run_schedule(horizon_days: int = 7, max_solve_seconds: int = 60) -> dict:
    """Queue a new scheduling run; returns its name for polling."""
    frappe.only_for(("System Manager", "Manufacturing Manager"))
    run_name = frappe_io.new_run(
        trigger_type="Manual",
        horizon_days=int(horizon_days),
        max_solve_seconds=int(max_solve_seconds),
    )
    frappe.enqueue(
        "aps_planner.engine.frappe_io.execute_run",
        queue="long",
        timeout=3600,
        run_name=run_name,
    )
    return {"schedule_run": run_name}


@frappe.whitelist()
def get_schedule(schedule_run: str | None = None) -> dict:
    """The latest (or a specific) schedule, shaped for a Gantt board."""
    run_name = schedule_run or frappe_io.latest_completed_run()
    if not run_name:
        return {"schedule_run": None, "operations": []}
    run = frappe.get_doc("APS Schedule Run", run_name)
    ops = frappe.get_all(
        "APS Scheduled Operation",
        filters={"schedule_run": run_name},
        fields=["name", "work_order", "operation_index", "operation",
                "workstation", "operator", "tool",
                "planned_start", "setup_end", "planned_end", "pinned"],
        order_by="planned_start asc",
    )
    return {
        "schedule_run": run_name,
        "status": run.status,
        "solver_status": run.solver_status,
        "kpis": {
            "weighted_tardiness": run.weighted_tardiness,
            "makespan_minutes": run.makespan_minutes,
            "on_time_jobs": run.on_time_jobs,
            "total_jobs": run.total_jobs,
            "deviation_score": run.deviation_score,
            "reassigned_ops": run.reassigned_ops,
        },
        "operations": ops,
    }


@frappe.whitelist()
def get_run_status(schedule_run: str) -> dict:
    """Lightweight poll target for the board's Re-plan button."""
    run = frappe.db.get_value(
        "APS Schedule Run",
        schedule_run,
        ["status", "solver_status"],
        as_dict=True,
    )
    return run or {"status": "Unknown", "solver_status": None}


@frappe.whitelist()
def pin_operation(name: str, pinned: int = 1) -> dict:
    """Toggle the planner's manual pin on one scheduled operation.

    A pinned operation is frozen in subsequent reactive re-solves (the solver
    treats it exactly like an in-progress job), letting a planner lock a
    decision the optimiser would otherwise be free to revise.
    """
    frappe.only_for(("System Manager", "Manufacturing Manager"))
    frappe.db.set_value(
        "APS Scheduled Operation", name, "pinned", 1 if int(pinned) else 0
    )
    return {"name": name, "pinned": 1 if int(pinned) else 0}
