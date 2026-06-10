"""Domain model for the APS scheduling prototype.

These dataclasses are the *engine-facing* representation of the shop. In the
target system they are populated from Frappe/ERPNext DocTypes (Work Order, Job
Card, Workstation, Operation, plus the new Skill / OEE Log DocTypes), but the
solver itself stays framework-agnostic so it can be developed and tested in
isolation.

Time is modelled in integer "time units" (minutes by default). CP-SAT works on
integers, so all durations and calendar windows are integers on a common
horizon that starts at t=0.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# --------------------------------------------------------------------------- #
# Resources
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Machine:
    """A workstation / CNC / EDM cell. A unary resource (one op at a time)."""

    id: str
    name: str
    # OEE -> Performance. Effective run time = nominal / performance_factor.
    # 0.9 means the machine runs at 90% of book rate, so jobs take ~11% longer.
    performance_factor: float = 1.0
    # OEE -> Availability. Planned maintenance / known downtime windows that
    # block the machine. List of (start, end) in time units.
    maintenance_windows: list[tuple[int, int]] = field(default_factory=list)


@dataclass(frozen=True)
class Operator:
    """A person. A renewable unary resource, gated by skill and shift."""

    id: str
    name: str
    skills: frozenset[str] = field(default_factory=frozenset)
    # Working windows (shift calendar) as (start, end) time-unit pairs. An
    # operator can only be assigned to setups that fall entirely inside a shift.
    shifts: list[tuple[int, int]] = field(default_factory=list)


@dataclass(frozen=True)
class Tool:
    """A fixture / tool type with limited quantity (a cumulative resource)."""

    id: str
    name: str
    quantity: int = 1


# --------------------------------------------------------------------------- #
# Work
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Operation:
    """One step of a job's routing.

    An operation is *flexible*: it can run on any machine in
    ``eligible_machines`` and is performed by any operator holding
    ``required_skill``. Setup needs an operator (attended); the run portion can
    proceed unattended (lights-out), which is what lets one operator cover
    several machines.
    """

    id: str
    job_id: str
    index: int  # position in the routing (0-based); enforces precedence
    eligible_machines: tuple[str, ...]
    # Nominal processing time per piece is folded into ``run_time`` already
    # (quantity * cycle time / expected yield). Setup is sequence-independent
    # in this prototype and incurred once per operation.
    run_time: int
    setup_time: int = 0
    required_skill: str | None = None
    required_tool: str | None = None


@dataclass(frozen=True)
class Job:
    """A work order: an ordered routing of operations for one part/lot."""

    id: str
    name: str
    operations: tuple[Operation, ...]
    due_date: int
    # Material readiness gate: the job cannot start before this time.
    release_time: int = 0
    # Tardiness weight (customer priority / contractual penalty).
    weight: int = 1


# --------------------------------------------------------------------------- #
# Problem container
# --------------------------------------------------------------------------- #
@dataclass
class ShopProblem:
    machines: dict[str, Machine]
    operators: dict[str, Operator]
    tools: dict[str, Tool]
    jobs: dict[str, Job]
    horizon: int  # upper bound on any end time (time units)

    def all_operations(self) -> list[Operation]:
        return [op for job in self.jobs.values() for op in job.operations]
