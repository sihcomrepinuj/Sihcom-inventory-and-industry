# Search-to-Add + Materials View (build/buy + flattened supplies) — Design

**Date:** 2026-05-30
**Status:** Approved, ready for planning
**Author:** Neeraj (with Claude)

## Problem

Two gaps in the build-list / action-plan flow shipped earlier today:

1. **Adding a target requires a type_id.** You have to search on a separate page, then
   hand-copy the numeric type_id into the add form. You want to search by ship name and add
   it directly.
2. **No build/buy control and no flattened supply view.** The `buy_set` (build-vs-buy per
   component) exists in the data model and the plan honors it, but there's no UI to set it,
   and there's no way to see the flattened bill of materials. You want to choose which items
   to build vs buy at each point in the material chain, and see the resulting supply list.

## Design decisions (from brainstorming)

- **Search-to-add:** inline results + Add buttons on the build-list page (server-rendered, no
  required JS).
- **Build/buy scope:** one **global** decision per component, applied across the whole build
  list (matches how the plan already aggregates).
- **Where it lives:** a dedicated **Materials view** (`/materials`) — the whole list's chain
  as a tree with build/buy toggles, plus a tree⇄flat switch. Keeps the action-first plan clean.
- **Flat quantities:** show **both** total required (full BOM, respecting build/buy) and
  to-buy after inventory (when logged in).
- **Approach:** server-rendered, progressive enhancement (POST-to-toggle), reusing existing
  engine functions. Optional JS live-refresh can be added later.

## Feature 1 — Search-to-add

The build-list "Add a target" area drops the manual Type ID field:

```
  Add a target
  Search:  [ revel____ ]  [Search]
  Results for "revel":
    Revelation             Qty[1] ME[10]  [Add]
    Revelation Navy Issue  Qty[1] ME[10]  [Add]
```

- Searching posts back to the build-list page with `?q=`, running `sde.search_types(q)`
  **filtered to manufacturable products only** (items that have a blueprint — so you can't add
  an un-buildable item like a mineral).
- Each result row carries small Qty/ME inputs and an **Add** button posting to the existing
  `build_list_add` route (type_id hidden in the row). One search, one click; no type_id shown.

## Feature 2 — Materials view

### Data model: a global build/buy set

`build_list.json` evolves from a bare list to:
```json
{ "targets": [ {...} ], "buy_set": [ 2867, 16671 ] }
```
`buy_set` = global list of component type_ids chosen to **buy** instead of build.

`build_list.py` changes:
- `load()` returns `{"targets": [...], "buy_set": [...]}`, **backward-compatible**: a legacy
  bare list loads as `{targets: list, buy_set: []}`.
- `save(data)` writes the dict.
- `add_target(target)` / `remove_target(type_id)` operate on `targets` (upsert/remove as today).
- `toggle_buy(type_id)` flips membership in `buy_set`.

Callers that aggregated a per-target buy_set (`{tid for t in targets ...}`) now read the single
global `set(data["buy_set"])`.

### The `/materials` route

`GET /materials?view=tree|flat` (default `tree`). Resolves every target's chain into a forest
(one subtree per target), cached like the existing chain route.

**Tree view** — forest indented by depth. Every *buildable* node (has a blueprint) shows
name · qty · activity and a build/buy toggle; raw materials are plain leaves. A node in
`buy_set` is treated as a leaf (bought; its subtree disappears from the build).

```
  ▾ Revelation                       ×5     [build → "buy instead"]
    ▾ Capital Construction Parts     ×210   [build → "buy instead"]
        Tritanium                    ×4.2M
    ▸ Capital Power Generator        ×90    [BUY] ← chosen to buy
```

A toggle is a tiny `POST /materials/toggle/<type_id>` that flips the component in the global
`buy_set`, saves, and redirects back to `?view=tree`.

**Flat view** — `flatten_material_tree(forest, buy_set)` aggregated across all targets:
```
  Item            Total required   Volume      To buy (after stock)
  Tritanium       8.4M             84,000 m³   6.1M
  Capital Power…  90               9,000 m³    90
```
- "Total required" = full BOM respecting build/buy choices.
- "To buy" = net of owned inventory; shown **only when logged in**.
- A **Tree ⇄ Flat** switch at the top. Reachable from the build-list page and the plan.

### Plan integration

The global `buy_set` is the single source of truth shared by Materials and Plan. In
`_compute_plan` (web) and `cmd_plan` (CLI), the per-target aggregation is replaced by
`buy_set = set(load()["buy_set"])`. A component marked "buy" on Materials immediately moves
from a *build* node to a *buy* line in the action plan. No change to `classify`/`enrich_buy`
(they already take a buy_set).

## Architecture / engine reuse

No new pure-logic module needed:
- **Tree:** `resolve_material_chain` (exists) → forest.
- **Flat "total required":** `flatten_material_tree(nodes, buy_set)` (exists) → aggregated rows.
- **"To buy" column:** one small **pure** helper (testable like `hauling`) taking the flat rows
  + a flat owned-asset index, returning `to_buy = max(0, total − owned)` per item with volumes.
  This is the only genuinely new logic.

## Edge cases

- Empty build list → friendly empty state.
- Unauthed → flat view hides the "To buy" column (total-required still works with no ESI);
  tree view fully works offline.
- `toggle_buy` is idempotent (flip on/off).
- Depth/cycles already guarded by `resolve_material_chain`'s `max_depth` + cache.
- Search with no/blank query → no results section; search of a non-manufacturable term → empty
  results with a hint.

## Testing

- **Pure logic** (no DB/network, like `test_hauling.py`): the new `to_buy` helper
  (all-owned, none-owned, partial, missing-from-index, volumes); confirm
  `flatten_material_tree` respects `buy_set` (stops at a bought node).
- **`build_list.py` migration:** backward-compat `load()` (legacy list → dict),
  `toggle_buy` idempotency, round-trip of the dict shape, `add_target`/`remove_target`
  upsert/remove.
- **Routes (smoke, Flask test client, SDE-gated):** `/materials?view=tree` and `?view=flat`
  → 200; `POST /materials/toggle/<id>` flips and redirects; build-list search renders inline
  results + Add, and adding works; unauthed flat view omits the To-buy column.
- **Regression / integration:** existing plan tests stay green; a component toggled "buy" on
  `/materials` appears in the plan's buy bucket.

## Scope / non-goals

- No per-target build/buy (global only).
- No JS live-refresh in the first cut (POST-to-toggle round-trip); can be layered on later
  like the plan's ⟳ button.
- No haul-from-named-locations or profit rollup (still deferred from the prior design).
