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

    The current schedule no longer reflects reality. Phase 4 will trigger a
    reactive minimal-perturbation re-solve here; for now we flag the run so
    planners see the staleness on the dashboard and can re-run on demand.
    """
    current = frappe_io.latest_completed_run()
    if current:
        frappe.db.set_value("APS Schedule Run", current, "status", "Stale")
