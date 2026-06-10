"""Payload <-> solver adapter. Deliberately free of any ``frappe`` import.

The Frappe side (``frappe_io.py``) reduces DocTypes to a plain JSON-friendly
payload; this module converts that payload into the solver's ``ShopProblem``
(datetimes -> integer minutes from horizon start) and converts the resulting
``Schedule`` back into rows ready to be inserted as APS Scheduled Operation
documents. Keeping this boundary pure means the whole engine is testable
without a bench, and the solver service could later move out-of-process with
the same payload as its wire format.

Payload schema (all datetimes ISO-8601 strings, naive, site timezone):

    {
      "horizon_start": "2026-06-10T08:00:00",
      "horizon_minutes": 10080,
      "machines":  [{"id", "performance_factor", "availability_derate",
                     "maintenance_windows": [{"from", "to"}]}],
      "operators": [{"id", "name", "skills": [...],
                     "shifts": [{"from", "to"}]}],
      "tools":     [{"id", "name", "quantity"}],
      "jobs":      [{"id", "name", "due_date", "release_time", "weight",
                     "operations": [{"id", "index", "operation",
                                     "eligible_machines": [...],
                                     "run_minutes", "setup_minutes",
                                     "skill", "tool"}]}]
    }
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from aps_solver import (
    Job,
    Machine,
    Operation,
    Operator,
    Schedule,
    ShopProblem,
    Tool,
)


def _parse(dt: str | datetime) -> datetime:
    return dt if isinstance(dt, datetime) else datetime.fromisoformat(dt)


def _to_minutes(dt: str | datetime, horizon_start: datetime, horizon: int) -> int:
    """Clamp a wall-clock time into [0, horizon] integer minutes."""
    delta = (_parse(dt) - horizon_start).total_seconds() / 60
    return max(0, min(horizon, int(delta)))


def _windows(
    raw: list[dict], horizon_start: datetime, horizon: int
) -> list[tuple[int, int]]:
    out = []
    for w in raw or []:
        s = _to_minutes(w["from"], horizon_start, horizon)
        e = _to_minutes(w["to"], horizon_start, horizon)
        if e > s:  # windows entirely outside the horizon collapse and drop out
            out.append((s, e))
    return out


def build_problem(payload: dict[str, Any]) -> ShopProblem:
    horizon_start = _parse(payload["horizon_start"])
    horizon = int(payload["horizon_minutes"])

    machines: dict[str, Machine] = {}
    for m in payload["machines"]:
        perf = float(m.get("performance_factor") or 1.0)
        # OEE Availability derate: shrink effective speed further so the plan
        # carries slack for historical unplanned downtime.
        derate = float(m.get("availability_derate") or 1.0)
        machines[m["id"]] = Machine(
            id=m["id"],
            name=m.get("name", m["id"]),
            performance_factor=perf * derate,
            maintenance_windows=_windows(
                m.get("maintenance_windows"), horizon_start, horizon
            ),
        )

    operators: dict[str, Operator] = {}
    for p in payload.get("operators", []):
        operators[p["id"]] = Operator(
            id=p["id"],
            name=p.get("name", p["id"]),
            skills=frozenset(p.get("skills", [])),
            shifts=_windows(p.get("shifts"), horizon_start, horizon),
        )

    tools = {
        t["id"]: Tool(id=t["id"], name=t.get("name", t["id"]),
                      quantity=int(t.get("quantity") or 1))
        for t in payload.get("tools", [])
    }

    jobs: dict[str, Job] = {}
    for j in payload["jobs"]:
        release = _to_minutes(j.get("release_time") or horizon_start,
                              horizon_start, horizon)
        ops = []
        for o in sorted(j["operations"], key=lambda x: x["index"]):
            eligible = tuple(m for m in o["eligible_machines"] if m in machines)
            if not eligible:
                raise ValueError(
                    f"Operation {o['id']}: no eligible machine is enabled "
                    f"(requested {o['eligible_machines']})"
                )
            # OEE Quality: inflate run time so output quantity survives scrap.
            yield_q = float(o.get("quality_yield") or 1.0)
            run = math.ceil(int(o["run_minutes"]) / max(yield_q, 0.01))
            ops.append(
                Operation(
                    id=o["id"],
                    job_id=j["id"],
                    index=int(o["index"]),
                    eligible_machines=eligible,
                    run_time=run,
                    setup_time=int(o.get("setup_minutes") or 0),
                    required_skill=o.get("skill") or None,
                    required_tool=o.get("tool") or None,
                )
            )
        jobs[j["id"]] = Job(
            id=j["id"],
            name=j.get("name", j["id"]),
            operations=tuple(ops),
            due_date=_to_minutes(j["due_date"], horizon_start, horizon),
            release_time=release,
            weight=int(j.get("weight") or 1),
        )

    return ShopProblem(
        machines=machines, operators=operators, tools=tools, jobs=jobs,
        horizon=horizon,
    )


def schedule_to_rows(
    schedule: Schedule, payload: dict[str, Any]
) -> list[dict[str, Any]]:
    """Map solver output back to wall-clock rows for APS Scheduled Operation.

    Operation ids produced by the Frappe side are ``f"{work_order}::{index}"``
    so they round-trip without an extra lookup table; ``op_meta`` carries the
    display fields (ERPNext Operation name) keyed the same way.
    """
    horizon_start = _parse(payload["horizon_start"])
    op_meta = {
        o["id"]: o for j in payload["jobs"] for o in j["operations"]
    }

    rows = []
    for so in schedule.operations:
        meta = op_meta[so.op_id]
        rows.append(
            {
                "work_order": so.job_id,
                "operation_index": int(meta["index"]),
                "operation": meta.get("operation"),
                "workstation": so.machine_id,
                "operator": so.operator_id,
                "tool": so.tool_id,
                "planned_start": horizon_start + timedelta(minutes=so.start),
                "setup_end": horizon_start + timedelta(minutes=so.setup_end),
                "planned_end": horizon_start + timedelta(minutes=so.end),
            }
        )
    return rows
