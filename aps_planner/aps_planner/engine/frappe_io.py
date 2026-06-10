"""Frappe-facing side of the engine: DocTypes -> payload -> solve -> DocTypes.

This is the only engine module that imports ``frappe``. It reduces the live
site to the plain payload defined in ``adapter.py``, runs the solver, and
persists the result as an APS Schedule Run + APS Scheduled Operation rows.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import frappe
from frappe.utils import get_datetime, now_datetime

from aps_solver import solve

from .adapter import build_problem, schedule_to_rows

# Work Orders in these states are planned; everything else is ignored.
_PLANNABLE_STATES = ("Not Started", "In Process")


# --------------------------------------------------------------------------- #
# Payload collection
# --------------------------------------------------------------------------- #
def collect_payload(horizon_start: datetime, horizon_days: int) -> dict:
    horizon_minutes = horizon_days * 24 * 60
    return {
        "horizon_start": horizon_start.isoformat(),
        "horizon_minutes": horizon_minutes,
        "machines": _collect_machines(),
        "operators": _collect_operators(),
        "tools": _collect_tools(),
        "jobs": _collect_jobs(horizon_start, horizon_minutes),
    }


def _collect_machines() -> list[dict]:
    machines = []
    for profile in frappe.get_all(
        "APS Machine Profile",
        filters={"enabled": 1},
        fields=["name", "workstation", "performance_factor",
                "availability_derate", "quality_yield"],
    ):
        windows = frappe.get_all(
            "APS Time Window",
            filters={"parent": profile.name,
                     "parenttype": "APS Machine Profile"},
            fields=["from_time", "to_time"],
        )
        machines.append(
            {
                "id": profile.workstation,
                "name": profile.workstation,
                "performance_factor": profile.performance_factor,
                "availability_derate": profile.availability_derate,
                "maintenance_windows": [
                    {"from": w.from_time.isoformat(), "to": w.to_time.isoformat()}
                    for w in windows
                ],
            }
        )
    return machines


def _collect_operators() -> list[dict]:
    operators = []
    for op in frappe.get_all(
        "APS Operator",
        filters={"enabled": 1},
        fields=["name", "operator_name"],
    ):
        skills = frappe.get_all(
            "APS Skill Item",
            filters={"parent": op.name, "parenttype": "APS Operator"},
            pluck="skill",
        )
        shifts = frappe.get_all(
            "APS Time Window",
            filters={"parent": op.name, "parenttype": "APS Operator"},
            fields=["from_time", "to_time"],
        )
        operators.append(
            {
                "id": op.name,
                "name": op.operator_name or op.name,
                "skills": skills,
                "shifts": [
                    {"from": w.from_time.isoformat(), "to": w.to_time.isoformat()}
                    for w in shifts
                ],
            }
        )
    return operators


def _collect_tools() -> list[dict]:
    return [
        {"id": t.name, "name": t.tool_name, "quantity": t.quantity}
        for t in frappe.get_all(
            "APS Tool", fields=["name", "tool_name", "quantity"]
        )
    ]


def _collect_jobs(horizon_start: datetime, horizon_minutes: int) -> list[dict]:
    jobs = []
    work_orders = frappe.get_all(
        "Work Order",
        filters={"status": ["in", _PLANNABLE_STATES], "docstatus": 1},
        fields=["name", "item_name", "qty", "planned_end_date",
                "expected_delivery_date", "bom_no", "priority" ],
    )
    # Per-operation requirements, keyed by ERPNext Operation.
    profiles = {
        p.operation: p
        for p in frappe.get_all(
            "APS Operation Profile",
            fields=["name", "operation", "required_skill", "required_tool"],
        )
    }
    eligible_cache: dict[str, list[str]] = {}

    for wo in work_orders:
        wo_ops = frappe.get_all(
            "Work Order Operation",
            filters={"parent": wo.name},
            fields=["operation", "workstation", "time_in_mins", "idx"],
            order_by="idx asc",
        )
        if not wo_ops:
            continue

        operations = []
        for row in wo_ops:
            profile = profiles.get(row.operation)
            eligible = _eligible_workstations(profile, row, eligible_cache)
            operations.append(
                {
                    # "{work_order}::{idx}" round-trips through the solver so
                    # write-back needs no extra lookup (see schedule_to_rows).
                    "id": f"{wo.name}::{row.idx}",
                    "index": row.idx,
                    "operation": row.operation,
                    "eligible_machines": eligible,
                    "run_minutes": int(row.time_in_mins or 60),
                    # ERPNext keeps setup on the BOM Operation; default until
                    # sequence-dependent setups land in Phase 4.
                    "setup_minutes": _setup_minutes(wo.bom_no, row.operation),
                    "skill": profile.required_skill if profile else None,
                    "tool": profile.required_tool if profile else None,
                }
            )

        due = wo.expected_delivery_date or wo.planned_end_date
        jobs.append(
            {
                "id": wo.name,
                "name": f"{wo.name} ({wo.item_name})",
                "due_date": (get_datetime(due) if due
                             else horizon_start
                             + timedelta(minutes=horizon_minutes)).isoformat(),
                "release_time": _material_release_time(wo.name,
                                                       horizon_start).isoformat(),
                "weight": _priority_weight(wo.get("priority")),
                "operations": operations,
            }
        )
    return jobs


def _eligible_workstations(profile, wo_op_row, cache: dict) -> list[str]:
    """Flexible routing from the Operation Profile; fall back to the WO's own
    workstation so an unprofiled operation still schedules (inflexibly)."""
    if profile is None:
        return [wo_op_row.workstation] if wo_op_row.workstation else []
    if profile.name not in cache:
        listed = frappe.get_all(
            "APS Eligible Workstation",
            filters={"parent": profile.name,
                     "parenttype": "APS Operation Profile"},
            pluck="workstation",
        )
        cache[profile.name] = listed
    return cache[profile.name] or (
        [wo_op_row.workstation] if wo_op_row.workstation else []
    )


def _setup_minutes(bom_no: str | None, operation: str | None) -> int:
    if not (bom_no and operation):
        return 0
    setup = frappe.db.get_value(
        "BOM Operation", {"parent": bom_no, "operation": operation},
        "fixed_time" )
    return int(setup or 0)


def _material_release_time(work_order: str, horizon_start: datetime) -> datetime:
    """Material readiness gate.

    A Work Order may start once every required item is on hand. If something
    is short, the job is gated to the latest expected receipt among open
    Purchase Orders covering the shortage; with no covering PO it is pushed to
    the horizon end (effectively unschedulable, which is the honest answer).
    """
    required = frappe.get_all(
        "Work Order Item",
        filters={"parent": work_order},
        fields=["item_code", "required_qty", "transferred_qty", "source_warehouse"],
    )
    release = horizon_start
    for item in required:
        shortage = (item.required_qty or 0) - (item.transferred_qty or 0)
        if shortage <= 0:
            continue
        on_hand = frappe.db.get_value(
            "Bin",
            {"item_code": item.item_code,
             **({"warehouse": item.source_warehouse}
                if item.source_warehouse else {})},
            "sum(actual_qty)",
        ) or 0
        if on_hand >= shortage:
            continue
        expected = frappe.db.sql(
            """
            select max(poi.schedule_date)
            from `tabPurchase Order Item` poi
            join `tabPurchase Order` po on po.name = poi.parent
            where poi.item_code = %s and po.docstatus = 1
              and po.status not in ('Closed', 'Completed', 'Cancelled')
              and poi.received_qty < poi.qty
            """,
            (item.item_code,),
        )
        expected_date = expected and expected[0][0]
        candidate = (
            get_datetime(expected_date)
            if expected_date
            else horizon_start + timedelta(days=3650)  # no supply in sight
        )
        release = max(release, candidate)
    return release


def _priority_weight(priority) -> int:
    # Map ERPNext priority-ish values onto tardiness weights.
    return {"High": 5, "Medium": 3}.get(priority, 1)


# --------------------------------------------------------------------------- #
# Run + persist
# --------------------------------------------------------------------------- #
def execute_run(run_name: str) -> None:
    """Body of the background job: solve and persist for one APS Schedule Run."""
    run = frappe.get_doc("APS Schedule Run", run_name)
    run.db_set("status", "Running")
    try:
        horizon_start = get_datetime(run.horizon_start)
        payload = collect_payload(horizon_start, run.horizon_days or 7)
        if not payload["jobs"]:
            run.db_set("status", "Completed")
            run.db_set("solver_status", "NO_JOBS")
            return

        problem = build_problem(payload)
        schedule = solve(problem, max_time_s=float(run.max_solve_seconds or 60))

        run.db_set("solver_status", schedule.status)
        run.db_set("solve_time_seconds", schedule.solve_time_s)
        if schedule.status not in ("OPTIMAL", "FEASIBLE"):
            run.db_set("status", "Failed")
            return

        for row in schedule_to_rows(schedule, payload):
            frappe.get_doc(
                {"doctype": "APS Scheduled Operation",
                 "schedule_run": run.name, **row}
            ).insert(ignore_permissions=True)

        on_time = sum(1 for t in schedule.job_tardiness.values() if t <= 0)
        run.db_set("weighted_tardiness", schedule.weighted_tardiness)
        run.db_set("makespan_minutes", schedule.makespan)
        run.db_set("on_time_jobs", on_time)
        run.db_set("total_jobs", len(schedule.job_tardiness))
        run.db_set("status", "Completed")

        _supersede_older_runs(run.name)
        frappe.db.commit()
    except Exception:
        frappe.db.rollback()
        run.db_set("status", "Failed")
        run.db_set("error_log", frappe.get_traceback())
        frappe.db.commit()
        raise


def _supersede_older_runs(current: str) -> None:
    for name in frappe.get_all(
        "APS Schedule Run",
        filters={"status": "Completed", "name": ["!=", current]},
        pluck="name",
    ):
        frappe.db.set_value("APS Schedule Run", name, "status", "Superseded")


def latest_completed_run() -> str | None:
    rows = frappe.get_all(
        "APS Schedule Run",
        filters={"status": "Completed"},
        order_by="modified desc",
        limit=1,
        pluck="name",
    )
    return rows[0] if rows else None


def new_run(trigger_type: str, horizon_days: int = 7,
            max_solve_seconds: int = 60) -> str:
    run = frappe.get_doc(
        {
            "doctype": "APS Schedule Run",
            "status": "Queued",
            "trigger_type": trigger_type,
            "horizon_start": now_datetime(),
            "horizon_days": horizon_days,
            "max_solve_seconds": max_solve_seconds,
        }
    ).insert(ignore_permissions=True)
    return run.name
