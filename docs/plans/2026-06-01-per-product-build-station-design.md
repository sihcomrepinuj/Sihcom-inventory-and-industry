# Per-Product Build Station — Design

**Date:** 2026-06-01
**Status:** Approved, ready for planning
**Author:** Neeraj (with Claude)

## Problem

The action plan assumes a **single global build station** for the whole build list. Neeraj
builds different products at different structures and wants to choose a **station per product**,
with each product's readiness and shopping computed against its own station.

## Decisions (from brainstorming)

- **Plan layout:** grouped by product. Each target gets its own block: its station picker and
  its own Ready/In-progress/Blocked/Buy/Haul, computed against THAT station. No cross-product
  merge (a shared component is counted under each product).
- **Station default:** pure per-product, **no global default**. Each target carries its own
  station; unset = `None` = "owned anywhere" netting + a "— choose —" picker.
- **Materials page:** Flat view becomes an **aggregated bill of materials** — total required +
  volume + **to-buy (own anywhere)** (netted against everything you own across all locations, no
  station). The Tree view (build/buy toggles) is unchanged. No station picker on Materials.
- **Approach A:** per-product loop over the existing engine — `classify`/`enrich_buy`/
  `attach_haul_breakdown` are unchanged, just called once per product instead of once for the
  merged whole.

## Section 1 — Data model: station per target

`build_list.json` drops the top-level `build_station`; each target gains one:
```json
{ "targets": [
    { "type_id": 23773, "name": "Ragnarok", ..., "build_station": 1035238621934 },
    { "type_id": 671,   "name": "Revelation", ..., "build_station": null }
  ],
  "buy_set": [ ... ] }
```
`build_list.py`:
- `load()` stops injecting a global `build_station`; an old file's top-level value is ignored;
  a target with no `build_station` key loads as `None`. `add_target` stores `build_station: None`.
- `set_build_station(id)` (global) → **`set_target_station(type_id, station_id)`** (sets/clears
  one target's station).

## Section 2 — The Plan: one block per product

`_compute_plan` stops merging. It fetches assets/jobs and the blueprint-station options once,
then loops per target:
```
for each target:
    graph   = merge_trees([target], buy_set)            # that product's own tree
    station = target.build_station                       # used directly; None if unset
    buckets = classify(graph, loc_index, jobs, station, buy_set)
    buckets.buy = attach_haul_breakdown(enrich_buy(buckets.buy, …, station), names)
    → block { name, type_id, station, station_options, buckets }
```
Returns a **list of blocks**. `plan.html` renders one section per product, each with its own
station picker. `/build-station` takes the product's `type_id` (+ `station_id`) and calls
`set_target_station`. `/api/plan` returns the list of blocks; the ⟳ refresh re-renders them
(full-reload fallback acceptable; stays progressive-enhancement). `classify`/`enrich_buy`/
`attach_haul_breakdown` are unchanged — called per product.

## Section 3 — Materials flat view: aggregated BOM

Tree view unchanged. Flat view:
```
Item            Total required   To buy (own anywhere)   Volume
Tritanium       8.4M             6.1M                    84,000 m³
```
- `flatten_material_tree(all targets' children, buy_set)` → one summed row per material.
- A small **pure helper** computes `total / owned / to_buy` from a **flat owned index**
  (`get_cached_asset_index`, corp-or-personal as today) + `total_volume`. (Re-adds the retired
  `attach_supply_columns` logic for this explicitly station-less purpose.)
- Logged out → owned empty → `to_buy == total`; show total + volume + "log in for to-buy".
- No station picker, no at-station/haul columns.

## Section 4 — CLI + architecture

- **CLI `plan`** loops per-product: classify each target against its `build_station`, print a
  per-product block with its haul-from. Honors each target's saved station. (Can't set stations;
  that's the web.)
- **Engine reuse:** `merge_trees`, `classify`, `enrich_buy`, `attach_haul_breakdown` unchanged
  (called per target). Only new pure code: the Materials owned-anywhere totals helper.
- **Retired:** `plan.resolve_build_station` (saved-or-most-used) is unused — per-product reads
  `target.build_station` directly, no default. `_get_station_list` stays (populates every
  product's picker with the same blueprint-station options).

## Section 5 — Edge cases & testing

**Edge cases:**
- Product with no station → block nets owned-anywhere, picker "— choose —"; usable, less precise.
- Shared owned stock counted under each product (documented aggregate-availability simplification,
  now per-product).
- Logged out → no pickers/haul; blocks show Blocked/Buy; Materials shows total + volume only.
- Old `build_list.json` with top-level `build_station` → ignored; targets start at `None`.
- Empty list / non-manufacturable target → graceful as today.

**Testing:**
- `build_list.py`: `set_target_station` sets/clears the right target; new targets default `None`;
  old global-station file loads with targets at `None`.
- Plan (stubbed ESI): `_compute_plan` → one block per target; two targets at different stations
  net independently (material owned only at A → on-hand for A's block, to-buy for B's).
- Materials: owned-anywhere totals helper (pure) — total/owned/to_buy/volume, none/partial/missing.
- Routes (smoke): `/build-station` with `type_id` updates only that target; `/plan` renders
  multiple blocks; Materials flat shows aggregated columns, no station picker.
- Remove the now-obsolete plan↔materials "same to-buy" consistency test (intentionally different
  bases now).

**Scope / non-goals:** no cross-product owned-stock reservation; build/buy (`buy_set`) stays
global; structure-name resolution is a separate ESI-scope issue, not part of this.
