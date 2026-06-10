# Dynamic APS for HMLV Precision Manufacturing

An **Advanced Planning & Scheduling** system for high-mix / low-volume precision
shops (CNC, EDM, grinding, aerospace/medical parts). Target architecture:

- **Frappe / ERPNext** as system-of-record + UI (BOM, inventory, work orders,
  job cards, plus new DocTypes for Skills / OEE / Schedule runs).
- **Google OR-Tools (CP-SAT)** as the optimisation backbone, running as a
  separate stateless solver service.

The repository contains all five phases: the **Phase 1 solver core**
(framework-agnostic FJSSP engine, de-risked first), the **Phase 2 Frappe app**
(`aps_planner`) that drives it from ERPNext data, **Phase 3 reactive
rescheduling** (the minimal-perturbation re-solve that makes the planning
*dynamic*), the **Phase 4 OEE feedback loop** (the plan learns the shop from
Job Card actuals), and the **Phase 5 planner board** (a Gantt UI with KPIs,
one-click re-plan, and click-to-pin).

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
      calibration.py         # pure OEE EWMA recalibration (no frappe import)
      oee_io.py              # Job Cards + Downtime Entry -> calibration -> profiles
    aps_planner/doctype/     # 10 DocTypes (see below)
    aps_planner/page/aps_planning_board/   # Phase 5 Gantt board (Desk page)
    aps_planner/workspace/                 # desk workspace + shortcuts

tests/
  test_schedule_valid.py   # independently re-checks every constraint on the output
  test_adapter.py          # payload->solve->rows round trip + pinning (no bench)
  test_reschedule.py       # reactive minimal-perturbation re-solve
  test_calibration.py      # OEE feedback-loop math
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

### OEE feedback loop (the plan learns the shop)

Book rates lie; HMLV shops drift. Every Sunday 03:00 (`weekly_oee_calibration`)
the last 30 days of actuals recalibrate each APS Machine Profile, then a fresh
plan is queued so Monday runs on measured reality:

| Factor | Evidence | Source |
|---|---|---|
| `performance_factor` (OEE-P) | planned vs. actual minutes | submitted Job Cards (time logs) |
| `quality_yield` (OEE-Q) | good vs. scrapped quantity | Job Card completed / process-loss qty |
| `availability_derate` (OEE-A) | unplanned downtime share | ERPNext Downtime Entry |

The math (`engine/calibration.py`, pure Python) *nudges* factors with an EWMA
(default α=0.3) rather than replacing them — one bad week on one fixture can't
halve a machine's planned capacity, but persistent drift steadily shows up in
the plan. Factors are clamped to sane bounds, thin evidence (< 5 ops) leaves a
factor untouched, and every update writes an audit note
(`P: observed 0.80 over 6 ops, 1.00 -> 0.94; …`) onto the profile.

### Planner board (Phase 5)

A Frappe Desk page (**APS Planner → Planning Board**, route `aps-planning-board`)
that turns the schedule into a planner-facing view:

- **KPI strip** — on-time %, weighted tardiness, makespan, and ops reassigned
  by the last re-solve.
- **Machine-timeline Gantt** — rows are workstations, x is time; each bar is an
  operation coloured by job, with the attended-setup portion hatched. Built as
  a lightweight self-contained renderer (no external Gantt dependency) so the
  resource-row view matches how a shop actually reads a board.
- **Re-plan** button runs `run_schedule` and polls `get_run_status` until the
  background solve finishes, then reloads.
- **Click-to-pin** — clicking a bar calls `pin_operation`, which sets `pinned`
  on the APS Scheduled Operation. Pinned ops are frozen in the next reactive
  re-solve (the solver treats them like in-progress work, via the same
  `ReschedulePolicy.pinned_op_ids` path), letting a planner lock a decision the
  optimiser would otherwise revise.

> **A note on the front end.** The brief mentioned a Django front end; this
> build uses **Frappe's native Desk UI** instead. Frappe is already a full
> Python web framework sharing the ORM, auth, permissions, and background-job
> queue the planner relies on — bolting a separate Django app alongside it would
> duplicate all of that and fight over the same database. The optimisation core
> stays framework-agnostic regardless, so a standalone Django (or React) client
> could later consume the same whitelisted `get_schedule` / `run_schedule`
> REST endpoints if an external UI is ever needed.

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
python tests/test_calibration.py      # OEE feedback loop math
```

The 12-job instance solves to **OPTIMAL**; larger instances return the best
**FEASIBLE** solution found within the time budget — exactly the trade-off a
production scheduler manages with a time-boxed, warm-started rolling solve.

## Roadmap

- **Phase 1 — Static solver.** FJSSP + all four constraints + on-time objective. ✅
- **Phase 2 — Frappe integration.** DocTypes, adapter, background solve, REST API. ✅ *(needs a live bench for end-to-end verification)*
- **Phase 3 — Reactive rescheduling.** Freeze window + warm start + minimal-perturbation objective; event triggers enqueue debounced re-solves. ✅
- **Phase 4 — OEE feedback loop.** Weekly EWMA recalibration of performance/availability/yield from Job Card actuals + Downtime Entries. ✅
- **Phase 5 — Planner board.** Desk page: machine-timeline Gantt, KPI strip, one-click re-plan, click-to-pin. ✅ *(needs a live bench to view)*

### Modelling notes / next refinements

- **Sequence-dependent setups** (changeover by part family/material) — high value
  in HMLV; add via per-machine `AddCircuit` transition times.
- **Actuals-aware freezing** — the freeze currently pins by *planned* start; with
  Job Card actuals it should drop completed ops and clamp in-progress ones to
  their true remaining time rather than re-running the full duration.
- **Operator attended-run option** — currently setup-only; some ops need an
  operator for the full cycle (configurable per operation).
