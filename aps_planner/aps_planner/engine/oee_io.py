"""Frappe side of the OEE feedback loop.

Reduces submitted Job Cards (time logs, completed/scrapped qty) and ERPNext
Downtime Entries to the pure observation records in ``calibration.py``, then
writes the recalibrated factors back to APS Machine Profile. Scheduled weekly
from hooks.py; safe to run ad hoc via ``run_calibration``.
"""

from __future__ import annotations

from datetime import timedelta

import frappe
from frappe.utils import now_datetime

from .calibration import (
    CalibrationResult,
    DowntimeObservation,
    Factors,
    ObservedOp,
    calibrate,
)

# Scheduled availability assumed when deriving the downtime share. Two shifts
# a day is a common HMLV pattern; refine later from Workstation working hours.
SCHEDULED_MINUTES_PER_DAY = 2 * 8 * 60


def collect_observed_ops(lookback_days: int) -> list[ObservedOp]:
    """One ObservedOp per submitted Job Card finished inside the window."""
    cutoff = now_datetime() - timedelta(days=lookback_days)
    cards = frappe.get_all(
        "Job Card",
        filters={"docstatus": 1, "modified": [">=", cutoff]},
        fields=["name", "workstation", "work_order", "operation",
                "total_completed_qty", "total_time_in_mins"],
    )
    observed: list[ObservedOp] = []
    for card in cards:
        if not card.workstation:
            continue
        planned = frappe.db.get_value(
            "Work Order Operation",
            {"parent": card.work_order, "operation": card.operation},
            "time_in_mins",
        )
        # Newer ERPNext tracks per-card process loss; default 0 when absent.
        scrap = frappe.db.get_value(
            "Job Card", card.name, "process_loss_qty", ignore=True
        ) or 0
        observed.append(
            ObservedOp(
                workstation=card.workstation,
                planned_minutes=float(planned or 0),
                actual_minutes=float(card.total_time_in_mins or 0),
                qty_good=float(card.total_completed_qty or 0),
                qty_scrap=float(scrap),
            )
        )
    return observed


def collect_downtime(lookback_days: int) -> list[DowntimeObservation]:
    """Aggregate ERPNext Downtime Entry per workstation over the window."""
    cutoff = now_datetime() - timedelta(days=lookback_days)
    rows = frappe.get_all(
        "Downtime Entry",
        filters={"from_time": [">=", cutoff]},
        fields=["workstation", "from_time", "to_time"],
    )
    minutes: dict[str, float] = {}
    for r in rows:
        if r.workstation and r.to_time and r.from_time:
            minutes[r.workstation] = minutes.get(r.workstation, 0.0) + (
                (r.to_time - r.from_time).total_seconds() / 60
            )
    scheduled = lookback_days * SCHEDULED_MINUTES_PER_DAY
    return [
        DowntimeObservation(
            workstation=ws, downtime_minutes=m, scheduled_minutes=scheduled
        )
        for ws, m in minutes.items()
    ]


def run_calibration(lookback_days: int = 30) -> dict[str, CalibrationResult]:
    """Recalibrate every enabled APS Machine Profile from recent actuals."""
    profiles = frappe.get_all(
        "APS Machine Profile",
        filters={"enabled": 1},
        fields=["name", "workstation", "performance_factor",
                "availability_derate", "quality_yield"],
    )
    current = {
        p.workstation: Factors(
            performance_factor=p.performance_factor or 1.0,
            availability_derate=p.availability_derate or 1.0,
            quality_yield=p.quality_yield or 1.0,
        )
        for p in profiles
    }
    if not current:
        return {}

    results = calibrate(
        current,
        collect_observed_ops(lookback_days),
        collect_downtime(lookback_days),
    )

    name_by_ws = {p.workstation: p.name for p in profiles}
    stamp = now_datetime()
    for ws, res in results.items():
        frappe.db.set_value(
            "APS Machine Profile",
            name_by_ws[ws],
            {
                "performance_factor": res.new.performance_factor,
                "availability_derate": res.new.availability_derate,
                "quality_yield": res.new.quality_yield,
                "last_calibrated": stamp,
                "calibration_note": res.note,
            },
        )
    frappe.db.commit()
    return results
