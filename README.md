# Dynamic APS for HMLV Precision Manufacturing

An **Advanced Planning & Scheduling** system for high-mix / low-volume precision
shops (CNC, EDM, grinding, aerospace/medical parts). Target architecture:

- **Frappe / ERPNext** as system-of-record + UI (BOM, inventory, work orders,
  job cards, plus new DocTypes for Skills / OEE / Schedule runs).
- **Google OR-Tools (CP-SAT)** as the optimisation backbone, running as a
  separate stateless solver service.

This repository currently contains the **Phase 1 solver prototype** — the
scheduling core, built and tested in isolation from Frappe so the hard part
(the optimisation model) is de-risked first.

## What the prototype does

Models the shop as a **Flexible Job-Shop Scheduling Problem (FJSSP)** with the
four constraint families that matter for HMLV precision work, and optimises for
**on-time delivery** (weighted tardiness), with makespan as a tie-breaker.

| Constraint | How it's modelled in CP-SAT |
|---|---|
| **Machines + precedence** | Optional intervals per eligible machine (flexible routing), `NoOverlap` per machine, routing precedence within a job. |
| **Machine OEE** | *Availability* → maintenance windows block the machine. *Performance* → run time inflated by per-machine factor. *Quality/scrap* → folded into required run time. |
| **Operators + skills** | Operators are skill-gated unary resources, attended **during setup only** (run goes lights-out, so one operator covers several machines). Shift calendars enforced as off-shift blocking. |
| **Material readiness** | Each job has a `release_time`; no operation starts before material is available. |
| **Tooling / fixtures** | Limited fixtures modelled as a cumulative resource (`AddCumulative` with capacity = quantity). |

## Layout

```
aps_solver/
  model.py    # framework-agnostic domain dataclasses (the Frappe-facing schema)
  solver.py   # CP-SAT FJSSP model + solve()
  data.py     # synthetic HMLV shop generator (deterministic)
  report.py   # KPIs + text Gantt
run.py        # CLI entry point
tests/
  test_schedule_valid.py   # independently re-checks every constraint on the output
```

## Run it

```bash
pip install -r requirements.txt

python run.py                    # default 12-job shop, finds OPTIMAL in ~10s
python run.py --jobs 20 --time 30   # heavier load (FJSSP is NP-hard; expect FEASIBLE)
python run.py --jobs 16 --seed 7

python tests/test_schedule_valid.py   # validate constraints hold
```

The 12-job instance solves to **OPTIMAL**; larger instances return the best
**FEASIBLE** solution found within the time budget — exactly the trade-off a
production scheduler manages with a time-boxed, warm-started rolling solve.

## Roadmap

- **Phase 1 — Static solver (this repo).** FJSSP + all four constraints + on-time objective. ✅
- **Phase 2 — Frappe integration.** Adapter to load DocTypes → `ShopProblem`, write `Schedule` back as Scheduled Operations; run the solver as a background job.
- **Phase 3 — OEE feedback loop.** Recalibrate performance/yield factors from Job Card actuals.
- **Phase 4 — Dynamic rescheduling.** Event triggers (breakdown, rush order, material arrival), rolling horizon, minimal-perturbation re-solve (warm start + pin near-term ops).
- **Phase 5 — Planner UX.** Gantt board, what-if simulation, manual pinning, KPI dashboards.

### Modelling notes / next refinements

- **Sequence-dependent setups** (changeover by part family/material) — high value
  in HMLV; add via per-machine `AddCircuit` transition times.
- **Minimal-perturbation rescheduling** — penalise deviation from the previous
  schedule so the floor isn't thrashed on every re-solve.
- **Operator attended-run option** — currently setup-only; some ops need an
  operator for the full cycle (configurable per operation).
