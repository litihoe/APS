"""Background jobs and document-event handlers (wired in hooks.py)."""

from __future__ import annotations

import frappe

from .engine import frappe_io


def nightly_full_replan() -> None:
    """Cron 02:00: a fresh full-horizon plan before the morning shift."""
    run_name = frappe_io.new_run(trigger_type="Nightly")
    frappe.enqueue(
        "aps_planner.engine.frappe_io.execute_run",
        queue="long",
        timeout=3600,
        run_name=run_name,
    )


def mark_schedule_stale(doc, method=None) -> None:
    """Doc event for Work Order / Job Card changes.

    The live schedule no longer reflects reality, so we flag it Stale (planners
    see it on the dashboard) and enqueue a reactive, minimal-perturbation
    re-solve. The enqueue is debounced: if a run is already queued or running,
    it will pick up this change when it collects the payload, so we don't pile
    up solves during a bulk Work Order import.
    """
    current = frappe_io.latest_completed_run()
    if current:
        frappe.db.set_value("APS Schedule Run", current, "status", "Stale")

    if frappe_io.has_pending_run():
        return

    run_name = frappe_io.new_run(trigger_type="Reactive")
    frappe.enqueue(
        "aps_planner.engine.frappe_io.execute_run",
        queue="long",
        timeout=3600,
        enqueue_after_commit=True,
        run_name=run_name,
    )
