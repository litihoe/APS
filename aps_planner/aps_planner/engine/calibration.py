"""OEE calibration: turn shop-floor actuals into updated machine factors.

This is the feedback loop that makes the planner *learn*. Each machine profile
carries three OEE-derived factors used by the solver:

* ``performance_factor`` (OEE-P) — effective speed vs. book rate,
* ``availability_derate`` (OEE-A) — slack for unplanned downtime,
* ``quality_yield``       (OEE-Q) — expected good-parts ratio.

Calibration compares planned vs. actual minutes (Job Card time logs), good vs.
scrapped quantity, and unplanned downtime vs. scheduled time (Downtime Entry),
then nudges each factor toward the observed value with an EWMA. Nudging — not
replacing — matters: one bad week on one fixture must not halve a machine's
planned capacity, but a persistent drift should steadily show up in the plan.

Pure Python, no ``frappe`` import: the Frappe side (``oee_io.py``) reduces
DocTypes to ``ObservedOp`` / ``DowntimeObservation`` records and applies the
returned factors back to APS Machine Profile.
"""

from __future__ import annotations

from dataclasses import dataclass

# Factors are nudged, clamped, and only updated on enough evidence.
DEFAULT_ALPHA = 0.3          # EWMA weight of the new observation
DEFAULT_MIN_SAMPLES = 5      # minimum finished operations to trust P/Q
CLAMP_LO, CLAMP_HI = 0.3, 1.2  # sane bounds; matches Machine Profile validate


@dataclass(frozen=True)
class ObservedOp:
    """One finished operation on one workstation (from a submitted Job Card)."""

    workstation: str
    planned_minutes: float   # nominal run+setup from the routing
    actual_minutes: float    # summed Job Card time logs
    qty_good: float = 0.0
    qty_scrap: float = 0.0


@dataclass(frozen=True)
class DowntimeObservation:
    """Aggregate unplanned downtime for one workstation over the window."""

    workstation: str
    downtime_minutes: float
    scheduled_minutes: float  # time the machine was supposed to be available


@dataclass(frozen=True)
class Factors:
    performance_factor: float = 1.0
    availability_derate: float = 1.0
    quality_yield: float = 1.0


@dataclass(frozen=True)
class CalibrationResult:
    workstation: str
    old: Factors
    new: Factors
    n_operations: int
    note: str


def _clamp(x: float) -> float:
    return max(CLAMP_LO, min(CLAMP_HI, x))


def _ewma(old: float, observed: float, alpha: float) -> float:
    return _clamp((1 - alpha) * old + alpha * observed)


def calibrate(
    current: dict[str, Factors],
    operations: list[ObservedOp],
    downtimes: list[DowntimeObservation] | None = None,
    *,
    alpha: float = DEFAULT_ALPHA,
    min_samples: int = DEFAULT_MIN_SAMPLES,
) -> dict[str, CalibrationResult]:
    """Compute updated factors per workstation.

    Only workstations present in ``current`` are calibrated (unknown ones in
    the actuals are ignored — they have no profile to update). A factor is
    left untouched when its evidence is missing or too thin, so partial data
    never degrades a profile.
    """
    by_ws: dict[str, list[ObservedOp]] = {}
    for op in operations:
        if op.workstation in current:
            by_ws.setdefault(op.workstation, []).append(op)

    down_by_ws = {
        d.workstation: d
        for d in (downtimes or [])
        if d.workstation in current and d.scheduled_minutes > 0
    }

    results: dict[str, CalibrationResult] = {}
    for ws, old in current.items():
        ops = by_ws.get(ws, [])
        notes: list[str] = []

        # --- Performance (planned vs. actual minutes) ---------------------- #
        perf = old.performance_factor
        timed = [o for o in ops if o.actual_minutes > 0 and o.planned_minutes > 0]
        if len(timed) >= min_samples:
            observed = sum(o.planned_minutes for o in timed) / sum(
                o.actual_minutes for o in timed
            )
            perf = _ewma(old.performance_factor, observed, alpha)
            notes.append(
                f"P: observed {observed:.2f} over {len(timed)} ops, "
                f"{old.performance_factor:.2f} -> {perf:.2f}"
            )
        else:
            notes.append(f"P: kept ({len(timed)} samples < {min_samples})")

        # --- Quality (good vs. scrap) -------------------------------------- #
        qual = old.quality_yield
        counted = [o for o in ops if (o.qty_good + o.qty_scrap) > 0]
        if len(counted) >= min_samples:
            good = sum(o.qty_good for o in counted)
            total = good + sum(o.qty_scrap for o in counted)
            observed = good / total
            qual = _ewma(old.quality_yield, observed, alpha)
            notes.append(
                f"Q: observed {observed:.2f} over {len(counted)} ops, "
                f"{old.quality_yield:.2f} -> {qual:.2f}"
            )
        else:
            notes.append(f"Q: kept ({len(counted)} samples < {min_samples})")

        # --- Availability (unplanned downtime share) ------------------------ #
        avail = old.availability_derate
        down = down_by_ws.get(ws)
        if down is not None:
            observed = 1.0 - min(
                down.downtime_minutes / down.scheduled_minutes, 1.0
            )
            avail = _ewma(old.availability_derate, observed, alpha)
            notes.append(
                f"A: observed {observed:.2f} "
                f"({down.downtime_minutes:.0f}/{down.scheduled_minutes:.0f} min down), "
                f"{old.availability_derate:.2f} -> {avail:.2f}"
            )
        else:
            notes.append("A: kept (no downtime data)")

        results[ws] = CalibrationResult(
            workstation=ws,
            old=old,
            new=Factors(
                performance_factor=round(perf, 4),
                availability_derate=round(avail, 4),
                quality_yield=round(qual, 4),
            ),
            n_operations=len(ops),
            note="; ".join(notes),
        )
    return results
