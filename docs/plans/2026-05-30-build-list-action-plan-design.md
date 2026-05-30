# Build List + Action Plan — Design

**Date:** 2026-05-30
**Status:** Approved, ready for planning
**Author:** Neeraj (with Claude)

## Problem

The tool's data layer (CCP SDE, material-chain resolution, hauling, profit) is solid,
but the *flow* doesn't fit how Neeraj actually works. Two concrete failures, in his words:

1. **It starts from the wrong question.** The entry point is "search and pick a blueprint,"
   but his real question is *"I have a target — plan it for me."*
2. **No sense of what-now / what-next.** It answers one-off lookups but doesn't support the
   ongoing, multi-stage workflow of capital production (reactions → components → hull), which
   plays out over days.

The engine is good; we are replacing the **flow**, not the data layer.

## Vision (one sentence)

> A build list of targets → one aggregated, **action-first** plan that reads live ESI assets
> & jobs and tells you what you can start building in EVE **right now**, what's blocked (and on
> what), and what to buy.

## Design decisions captured during brainstorming

- **Right question:** target-driven — "I have a target, plan it for me."
- **State model:** *live state, recomputed.* No manually-saved progress. Execution state is
  derived from real ESI assets + jobs on every load. The only persisted thing is the build
  list itself (intent).
- **Layout:** *action-first* — lead with "what I can start now," then blocked, then buy.
  The user confirmed a per-stage/sectioned layout reads well.
- **Target input:** a **build list** of multiple targets, producing one combined plan
  (shared intermediates aggregated, not duplicated).
- **Direction:** Approach A — new primary flow on top of the existing engine; old pages
  become drill-downs; lowest risk, full vision.

## User-facing design

### Entry: the Build List (replaces the search homepage)

A small, persistent table of targets. Each target carries `{item, qty, ME, runs,
structure_bonus}` plus per-intermediate build-or-buy choices. Adding a target reuses the
existing search. The list persists server-side; progress does not (derived live from ESI).

```
  BUILD LIST                                          [+ add target]
  ┌─────────────────┬─────┬─────┬──────┬───────────┬──────────────┐
  │ Target          │ Qty │ ME  │ Runs │ Structure │              │
  ├─────────────────┼─────┼─────┼──────┼───────────┼──────────────┤
  │ Revelation      │  5  │ 10  │  5   │ Sotiyo    │  [edit] [x]  │
  │ Phoenix         │  3  │ 10  │  3   │ Sotiyo    │  [edit] [x]  │
  │ Fuel Block (He) │ 200 │ 10  │  —   │ Athanor   │  [edit] [x]  │
  └─────────────────┴─────┴─────┴──────┴───────────┴──────────────┘
                                            [ Build the plan → ]
```

### The Plan: four action-first stages

"Build the plan" aggregates all targets into one dependency graph, cross-references live ESI
assets + jobs, and sorts by what's actionable now:

- **▶ Ready to start now** — nodes whose direct inputs are owned at the build station.
  Row: product, qty/runs, est. time, station, drill-in `→`.
- **⏳ In progress** — matched to active ESI industry jobs (by product type_id), with
  completion countdown.
- **🔒 Blocked** — buildable in principle but inputs not on hand; names what's missing.
- **🛒 Buy list** — aggregated terminal/raw materials not owned, with haul/buy split and cost.
  (See **Deferred** below — the haul-from-other-stations breakdown is netted out of "to buy"
  but not yet displayed by name.)
- **💰 Profit rollup** — total material cost vs. output value across the whole build list.
  (See **Deferred** below — not shipped in the first cut.)

Reopening the tool after jobs complete makes rows move up the page on their own — no manual
check-off.

### The readiness rule (the core algorithm)

For each node in the merged graph that has a blueprint and is not marked "buy":

```
owned   = assets owned at build station (fallback: owned anywhere)
in_job  = quantity of this product currently in active ESI jobs
shortfall = needed - owned - in_job

if shortfall <= 0:                          satisfied (not shown / counts as done)
elif a matching active job exists:          IN PROGRESS
elif all direct inputs have shortfall <= 0: READY TO START NOW
else:                                       BLOCKED (list missing inputs)
```

Terminal/raw materials with a shortfall fall to the **Buy list**.

## Architecture

No changes to engine modules. New work = one orchestration layer + build-list UI.

```
  Build list (persisted)  ──┐
                            ▼
  plan.py  (NEW, pure logic, no Flask/ESI imports)
    ├─ for each target: resolve_material_chain()           [sde.py]
    ├─ merge trees → one requirement graph keyed by type_id (sum qtys)
    ├─ classify each node against:
    │     • location asset index   [esi.build_location_asset_index]
    │     • active jobs by product [esi.fetch_industry_jobs]
    │     • build station          [esi.extract_manufacturing_stations]
    │     • build/buy choices      [from build list]
    └─ → { ready[], in_progress[], blocked[], buy[] }  +  profit rollup

  app.py  (routes only)
    ├─ /            → build list page
    ├─ /plan        → renders plan.py output
    ├─ /api/plan    → JSON for live refresh
    └─ existing routes survive as drill-downs
```

- The **classifier is a pure function** — `(graph, asset_index, jobs, buy_set) → four buckets`.
  Testable with no DB/network, mirroring `hauling.calculate_deficit`.
- Buy-list rows reuse `hauling.calculate_deficit` for the haul/buy split.
- Profit rollup reuses `_compute_profit`.
- Build/buy toggles feed the existing `flatten_material_tree(buy_set=…)` mechanism.

## Persistence

- `build_list.json` — flat JSON file, gitignored (like `tokens.json`).
  List of `{type_id, qty, me, runs, structure_bonus, buy_set}`.
- Single-user tool, so a file is sufficient — no DB.

## What stays / what changes

- **Changes:** `/` becomes the build list; `/plan` is the new primary view.
- **Survives untouched as drill-downs:** `blueprint`, `chain`, `market`, `profit`, `shopping`
  — reached from a row's `→`. Nothing discarded.
- **CLI:** gains a parallel `plan` command reading the same `build_list.json`, so the terminal
  flow matches the web one.

## Edge cases

- **No ESI auth / stale token:** plan still renders from SDE; everything shows Blocked/Buy
  (nothing owned) with a login banner. Degrades, doesn't break.
- **Build station ambiguity:** default to most-used (`extract_manufacturing_stations`) with a
  selector; "own at station" falls back to "own anywhere" when none chosen.
- **Cycles / depth:** already guarded by `resolve_material_chain`'s `max_depth` + cache.

## Testing

New `tests/test_plan.py` — pure-function classifier tests, mirroring `test_hauling.py`:

- all-ready, all-blocked, partial shortfall
- in-progress match (active job consumes a node)
- multi-target shared-intermediate aggregation (Rev + Phoenix share cap parts)
- build-vs-buy toggle moves a node from buildable to buy list

No new network/DB test dependencies.

## Scope decisions

- Build list stored as a flat JSON file (not a DB) — fine for a single-user tool.
- CLI gets a parallel `plan` command — included because both surfaces are used; could be
  deferred to keep the first cut web-only.

## Deferred (not in first cut)

- **Profit rollup — DEFERRED.** The design envisioned a material-cost-vs-output-value rollup;
  the first implementation ships the four operational stages (ready/in-progress/blocked/buy)
  only. Profit remains available on the existing per-blueprint `/profit` drill-down page.
- **Buy list haul column — PARTIAL.** The buy list nets out stock held at your other stations
  (so "to buy" is accurate), but currently displays only At-station and To-buy quantities.
  Surfacing the per-location "haul from" breakdown with human-readable station names is a
  follow-up (station-name resolution already noted as a next step).
