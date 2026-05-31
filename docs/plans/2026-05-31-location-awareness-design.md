# Location Awareness — "Where to build, where my stuff is" — Design

**Date:** 2026-05-31
**Status:** Approved, ready for planning
**Author:** Neeraj (with Claude)

## Problem

The build-list / action-plan flow is location-aware in the *engine* but barely in the *UI*:

- **"Where to build" is invisible and uncontrollable.** `_compute_plan` silently picks
  `build_station = stations[0]` (your most-used facility from job history). It's never shown
  and there's no way to change it.
- **"Where my stuff is" is computed then dropped.** The buy list already calculates `at_station`,
  `elsewhere` (`{station_id: qty}`), and `to_buy` per material, but the plan only shows
  At-station + To-buy — the `elsewhere` (haul-from) map is discarded (`plan.html` even notes
  "We don't surface `elsewhere` in this view"). Station IDs are never resolved to names.
- **The Materials flat view is location-blind**, netting "owned anywhere" via a flat index, so
  its "to buy" can disagree with the plan's at-station "to buy".

All the building blocks already exist (`hauling.calculate_deficit`, `_get_station_list` which
returns `{id, name}`, `esi.get_cached_location_name`, the `/shopping` page's proven
station-dropdown + elsewhere-by-name pattern). This feature is mostly *wiring them into the new
flow*.

## Decisions (from brainstorming)

- **Station control:** a dropdown on Plan + Materials, defaulting to most-used; the choice
  **persists** in `build_list.json` (global `build_station`).
- **Haul detail:** a **per-station breakdown** — each material shows at-build-station / haul from
  named other stations (qty each) / buy.
- **Scope:** **Plan + Materials, consistent** — both get the picker, and the Materials flat
  "to buy" becomes location-aware via the same `calculate_deficit` path, so the two screens agree.
- **Approach A:** wire location into the screens already in use (not a separate Logistics page),
  reusing the existing hauling/station/name code.

## Section 1 — The station picker (persisted)

`build_list.json` gains a third top-level field:
```json
{ "targets": [...], "buy_set": [...], "build_station": 1035620655209 }
```
`build_list.py`: add `set_build_station(id)`; `load()` defaults `build_station` to `null`
(backward-compatible with existing files).

Both Plan and Materials show a header **"Building at: [Sotiyo ▾]"** — a `<select>` populated
from `_get_station_list` (facilities ranked by use, names already resolved). Choosing one POSTs
to `/build-station` (save + redirect back to the originating view). Default when unset = most-used.
A saved station that's dropped out of recent jobs is still listed and kept selected.

## Section 2 — Haul-from in the buy list (Plan)

Resolve the existing `elsewhere {station_id: qty}` to names and render a per-station breakdown:

```
🛒 BUY LIST                          Building at: Sotiyo ▾
Material      At Sotiyo   Haul from                      Buy
Tritanium     1.0M        500k @ Athanor, 200k @ T5-A     2.3M
Nitrogen FB   40          —                               0
Morphite      0           1,200 @ Sotiyo (Corp)           800
```

Per material: how much is **at the build station**, how much is **owned elsewhere and haulable**
(and from where), and how much to genuinely **buy**. The "to buy" number is unchanged (it already
nets out haulable stock) — we only make the haul portion visible.

## Section 3 — Materials flat view: location-aware & consistent

Switch the flat view from the flat all-locations index onto the **same `calculate_deficit` path**
the plan uses, against the selected station:

```
Materials · Flat        Building at: Sotiyo ▾
Item        Total req  At Sotiyo  Haul from        To buy  Volume
Tritanium   3.8M       1.0M       700k @ Athanor   2.1M    38k m³
```

Same picker, same at-station/haul/buy semantics → Plan and Materials now agree on what to buy.
"Total required" (full BOM) stays. Logged out degrades to Total required + Volume + login note.

`plan.attach_supply_columns` (the flat all-stock helper added last feature) is **removed** in
favor of the shared `calculate_deficit` path — one source of truth for the buy split.

## Section 4 — Architecture & engine reuse

```
build_list.json {build_station}  ─┐
_get_station_list (id+name)       ├─► station <select> on Plan + Materials
POST /build-station (save+back)  ─┘

selected station ─► calculate_deficit(needed, loc_index, station, volumes)
                      → at_station / elsewhere{id:qty} / to_buy        [hauling.py, exists]
elsewhere ids ─► get_cached_location_name → names                       [esi.py, exists]
```

**New code, small and mostly pure (tested like `hauling`):**
- `build_list.set_build_station` + `load()` default (persistence).
- `plan.resolve_build_station(saved, stations)` — pure: saved if set, else most-used, else None.
- `plan.attach_haul_breakdown(rows, names)` — pure: each row's `elsewhere {id:qty}` → sorted
  named `[{name, qty}]`; ESI name fetch stays in the route.
- Routes (`_compute_plan`, `materials`) resolve the station, fetch the location index + names,
  pass the station list + named rows to the templates.

Reuses `get_cached_location_asset_index` (already used by the plan buy list), `calculate_deficit`,
`_get_station_list`, and `get_cached_location_name` (proven on `/shopping`).

## Section 5 — CLI, edge cases, testing

**CLI** (`eve_inventory.py plan`): honor the persisted `build_station` (instead of auto most-used);
print haul-from in its buy list (at-station / haul from <names> / buy). No station change from the
CLI (set on the web); one-line HELP note.

**Edge cases:**
- Logged out → no picker, no haul columns; degrades to blocked/buy + total-required.
- New char / no job history → no stations to offer; fall back to "owned anywhere", note no station set.
- Saved station absent from recent jobs → kept in the dropdown and selected.
- Corp vs personal assets → unchanged corp-or-personal toggle feeds the location index.
- Unresolvable station (lost docking access) → show raw ID; name resolver already falls back.

**Testing:**
- Pure: `resolve_build_station` (saved / fallback / none); `attach_haul_breakdown`
  (map → named sorted list, missing-name fallback, empty).
- `build_list.py`: `set_build_station` round-trip + `load()` default + backward-compat.
- Routes (smoke, SDE-gated): authed plan/materials render the `<select>` with the selected
  station; `POST /build-station` persists + redirects; buy list shows a "Haul from" cell; unauthed
  hides picker/haul.
- Regression: plan and materials "to buy" match for the same list; existing 86 tests stay green.

## Scope / non-goals

- No manual station entry (only detected facilities).
- No per-target stations (one global build station).
- No cross-station hauling optimization or jump-freight volume math — just *show* where things
  are and let you choose where to build.
