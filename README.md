# Dynamic APS for HMLV Precision Manufacturing

An **Advanced Planning & Scheduling** system for high-mix / low-volume precision
shops (CNC, EDM, grinding, aerospace/medical parts). Target architecture:

- **Frappe / ERPNext** as system-of-record + UI (BOM, inventory, work orders,
  job cards, plus new DocTypes for Skills / OEE / Schedule runs).
- **Google OR-Tools (CP-SAT)** as the optimisation backbone, running as a
  separate stateless solver service.

The repository contains the **Phase 1 solver core** (framework-agnostic
FJSSP engine, de-risked first), the **Phase 2 Frappe app** (`aps_planner`)
that drives it from ERPNext data, and **Phase 3 reactive rescheduling** (the
minimal-perturbation re-solve that makes the planning *dynamic*).

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
aps_solver/                  # Phase 1: pure optimisation core (no Frappe)
  model.py    # framework-agnostic domain dataclasses
  solver.py   # CP-SAT FJSSP model + solve()
  data.py     # synthetic HMLV shop generator (deterministic)
  report.py   # KPIs + text Gantt
run.py        # CLI entry point

aps_planner/                 # Phase 2: Frappe app (install with bench)
  aps_planner/
    hooks.py                 # nightly cron solve + Work Order/Job Card events
    tasks.py                 # background jobs, stale-schedule marking
    api.py                   # REST: run_schedule / get_schedule (for Gantt UI)
    engine/
      adapter.py             # pure payload <-> ShopProblem/rows (no frappe import)
      frappe_io.py           # DocTypes -> payload -> solve -> DocTypes
    aps_planner/doctype/     # 10 DocTypes (see below)

tests/
  test_schedule_valid.py   # independently re-checks every constraint on the output
  test_adapter.py          # payload->solve->rows round trip (no bench needed)
```

## Frappe app (`aps_planner`)

New DocTypes layered over ERPNext Manufacturing (Work Order, Workstation,
Operation, Employee stay the system of record):

| DocType | Purpose |
|---|---|
| **APS Machine Profile** | Per-Workstation OEE factors (performance, availability derate, quality yield) + planned maintenance windows. |
| **APS Operator** | Links an Employee to skills and shift windows. |
| **APS Skill** / **APS Tool** | Skill master; fixture/tool master with available quantity. |
| **APS Operation Profile** | Per-Operation requirements: required skill, required tool, eligible workstations (flexible routing). |
| **APS Schedule Run** | One solver execution: parameters, status, KPIs. |
| **APS Scheduled Operation** | One planned operation: assignment + timing, the Gantt's data source. |
| (children) | APS Time Window, APS Skill Item, APS Eligible Workstation. |

Data flow: `frappe_io.collect_payload()` reduces the site to a plain JSON
payload (material readiness derived from Work Order Item shortages vs. Bin
stock and open Purchase Order receipt dates) → `adapter.build_problem()`
converts wall-clock datetimes to solver minutes → `solve()` →
`adapter.schedule_to_rows()` converts back → persisted as APS Scheduled
Operations. The adapter has **no frappe import**, so the whole engine tests
without a bench and can later move to a separate solver service unchanged.

Triggers: nightly cron full re-plan (02:00), manual via
`POST /api/method/aps_planner.api.run_schedule`, and Work Order / Job Card
events that mark the live schedule **Stale** and enqueue a debounced
**reactive re-solve** (see below).

### Reactive rescheduling (the "dynamic" part)

A static optimum is worthless ten minutes later when a machine trips or a rush
order lands. Re-optimising from scratch is correct but *nervous* — it can
reshuffle the whole floor for a trivial gain, which operators distrust. The
`ReschedulePolicy` (in `aps_solver/solver.py`) turns a re-solve into a
**minimal-perturbation** one:

- **Freeze.** Operations already underway, or starting within a `freeze_minutes`
  window of *now* in the live plan, are pinned to their machine/operator/start —
  you cannot un-start a job.
- **Warm start.** Every other operation hints CP-SAT toward its previous
  assignment, so the solver explores near the incumbent first (fast convergence).
- **Stability objective.** Changing a machine or shifting a start is penalised,
  sitting *below* tardiness (on-time delivery still wins) but *above* makespan,
  so the plan only churns when it actually buys due-date performance. The static
  objective is recovered exactly when no previous plan is supplied.

Each reactive run records its `deviation_score` and `reassigned_ops`, so you can
see how much the floor was disturbed. `mark_schedule_stale` enqueues these
reactively and debounces them (no solve storm during a bulk Work Order import).

### Install on a bench

```bash
# repo root provides the aps_solver engine package
pip install -e /path/to/APS
bench get-app /path/to/APS/aps_planner
bench --site yoursite install-app aps_planner
bench --site yoursite migrate
```

## Run it

```bash
pip install -r requirements.txt

python run.py                    # default 12-job shop, finds OPTIMAL in ~10s
python run.py --jobs 20 --time 30   # heavier load (FJSSP is NP-hard; expect FEASIBLE)
python run.py --jobs 16 --seed 7

python tests/test_schedule_valid.py   # validate constraints hold
python tests/test_adapter.py          # Frappe adapter round trip (no bench)
python tests/test_reschedule.py       # reactive minimal-perturbation re-solve
```

The 12-job instance solves to **OPTIMAL**; larger instances return the best
**FEASIBLE** solution found within the time budget — exactly the trade-off a
production scheduler manages with a time-boxed, warm-started rolling solve.

## Roadmap

- **Phase 1 — Static solver.** FJSSP + all four constraints + on-time objective. ✅
- **Phase 2 — Frappe integration.** DocTypes, adapter, background solve, REST API. ✅ *(needs a live bench for end-to-end verification)*
- **Phase 3 — Reactive rescheduling.** Freeze window + warm start + minimal-perturbation objective; event triggers enqueue debounced re-solves. ✅
- **Phase 4 — OEE feedback loop.** Recalibrate performance/availability/yield factors from Job Card actuals so the plan learns the shop.
- **Phase 5 — Planner UX.** Gantt board, what-if simulation, manual pinning, KPI dashboards.

### Modelling notes / next refinements

- **Sequence-dependent setups** (changeover by part family/material) — high value
  in HMLV; add via per-machine `AddCircuit` transition times.
- **Actuals-aware freezing** — the freeze currently pins by *planned* start; with
  Job Card actuals it should drop completed ops and clamp in-progress ones to
  their true remaining time rather than re-running the full duration.
- **Operator attended-run option** — currently setup-only; some ops need an
  operator for the full cycle (configurable per operation).
