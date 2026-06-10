app_name = "aps_planner"
app_title = "APS Planner"
app_publisher = "APS"
app_description = (
    "Dynamic Advanced Planning & Scheduling for HMLV precision manufacturing, "
    "with Google OR-Tools CP-SAT as the optimisation backbone."
)
app_email = "tiamhoe@gmail.com"
app_license = "MIT"

# Required ERPNext doctypes (Work Order, Workstation, Operation, Employee).
required_apps = ["erpnext"]

# ---------------------------------------------------------------------------
# Scheduled solver runs.
# A nightly full re-plan; intraday reactive re-solves are triggered by events
# (doc_events below) or on demand via the REST endpoint.
# ---------------------------------------------------------------------------
scheduler_events = {
    "cron": {
        # Full re-plan every night at 02:00, before the morning shift.
        "0 2 * * *": ["aps_planner.tasks.nightly_full_replan"],
    },
}

# ---------------------------------------------------------------------------
# Dynamic rescheduling triggers (Phase 4 wiring lands here).
# Submitting/cancelling a Work Order, or completing a Job Card (actuals),
# marks the current schedule stale so the next reactive solve picks it up.
# ---------------------------------------------------------------------------
doc_events = {
    "Work Order": {
        "on_submit": "aps_planner.tasks.mark_schedule_stale",
        "on_cancel": "aps_planner.tasks.mark_schedule_stale",
    },
    "Job Card": {
        "on_submit": "aps_planner.tasks.mark_schedule_stale",
    },
}
