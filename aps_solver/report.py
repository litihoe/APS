"""Human-readable rendering of a Schedule: KPIs + a per-machine text Gantt."""

from __future__ import annotations

from .model import ShopProblem
from .solver import Schedule


def _fmt(t: int) -> str:
    """Minutes from t=0 -> Dd HH:MM, treating the horizon as wall-clock days."""
    day, rem = divmod(t, 1440)
    h, m = divmod(rem, 60)
    return f"D{day} {h:02d}:{m:02d}"


def render(problem: ShopProblem, sched: Schedule, scale_min_per_cell: int = 30) -> str:
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("  APS SCHEDULE  (FJSSP / CP-SAT)")
    lines.append("=" * 72)
    lines.append(f"  Solver status      : {sched.status}")
    lines.append(f"  Solve time         : {sched.solve_time_s:.2f}s")
    lines.append(f"  Weighted tardiness : {sched.weighted_tardiness}")
    lines.append(f"  Makespan           : {sched.makespan} min ({_fmt(sched.makespan)})")

    if not sched.operations:
        lines.append("  (no feasible schedule found)")
        return "\n".join(lines)

    # --- On-time delivery KPIs -------------------------------------------- #
    late = {j: t for j, t in sched.job_tardiness.items() if t > 0}
    total = len(problem.jobs)
    on_time = total - len(late)
    lines.append("-" * 72)
    lines.append(f"  On-time jobs       : {on_time}/{total} "
                 f"({100 * on_time / total:.0f}%)")
    if late:
        lines.append("  Late jobs          :")
        for j, t in sorted(late.items(), key=lambda kv: -kv[1]):
            w = problem.jobs[j].weight
            lines.append(f"      {j}  late by {t:>4} min  (weight {w})")

    # --- Per-machine Gantt ------------------------------------------------ #
    lines.append("-" * 72)
    lines.append(f"  GANTT  (1 cell = {scale_min_per_cell} min)")
    by_machine: dict[str, list] = {m: [] for m in problem.machines}
    for so in sched.operations:
        by_machine[so.machine_id].append(so)

    span_cells = max(1, sched.makespan // scale_min_per_cell + 1)
    span_cells = min(span_cells, 100)  # keep terminal-friendly
    for m_id in sorted(by_machine):
        row = ["."] * span_cells
        for so in by_machine[m_id]:
            s_cell = so.start // scale_min_per_cell
            e_cell = max(s_cell + 1, so.end // scale_min_per_cell)
            for c in range(s_cell, min(e_cell, span_cells)):
                row[c] = "#"
            # mark setup portion with 's'
            su_cell = so.setup_end // scale_min_per_cell
            for c in range(s_cell, min(su_cell, span_cells)):
                if row[c] == "#":
                    row[c] = "s"
        lines.append(f"  {m_id:<8}|{''.join(row)}|")

    # --- Operation detail ------------------------------------------------- #
    lines.append("-" * 72)
    lines.append("  OPERATIONS")
    lines.append(f"  {'op':<12}{'machine':<9}{'operator':<9}{'tool':<10}"
                 f"{'start':<10}{'end':<10}")
    for so in sched.operations:
        lines.append(
            f"  {so.op_id:<12}{so.machine_id:<9}"
            f"{(so.operator_id or '-'):<9}{(so.tool_id or '-'):<10}"
            f"{_fmt(so.start):<10}{_fmt(so.end):<10}"
        )
    lines.append("=" * 72)
    return "\n".join(lines)
