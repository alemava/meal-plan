# Mesa — Handover / Roadmap Notes

Items to surface in the recipe-chat roadmap. These are known gaps or decisions that the AI chat workflow needs to handle explicitly.

---

## 1. Recipe duplication across weeks (NEEDS ROADMAP ITEM)

**What happens now:** Each `meal_weeks` row stores full recipe JSON inline in the `meals` array. There is no shared recipes table. If the same recipe is used in two different weeks (e.g. moved from last Friday to this Monday), it is stored **twice** — as two independent full copies.

**Observed problem:** When asking Claude to "use this Friday's recipe for Monday of next week", Claude adds it to the new week but does NOT remove it from the old week. Result: same recipe visible in two weeks, appears twice in "All Recipes" library.

**What the recipe chat should do:**
- When moving a recipe from week A to week B: add to B **and** remove from A in the same operation.
- When copying intentionally (same recipe repeated): leave both, but that should be an explicit user choice.

**Open question / roadmap decision:** Should we introduce a canonical `recipes` table (one row per unique recipe, referenced by ID from `meal_weeks.meals`)? This would eliminate duplication naturally. Not yet decided — add to roadmap discussion.

---

## 2. Library deduplication (LINKED TO ITEM 1)

**What happens now:** `renderLib` in `index.html` aggregates meals from all `meal_weeks` rows without deduplicating by title. Same recipe appearing in multiple weeks shows up multiple times in the library list.

**Quick fix (no schema change):** Deduplicate by `title` in `renderLib` client-side — show once, use the most-recent week's data.

**Proper fix:** Canonical recipes table (see item 1).

---

## 3. Tour — demo fallback when no meals exist (NEEDS ROADMAP ITEM)

**What happens now:** Tour steps for "Rate as you go", "Hands-free cook mode", timers, and "Exit cook mode" are only added when `cookMeals.length > 0` (i.e. current week has real recipes). If a week has no recipes, those steps are silently skipped.

**What should happen:** Tour should always show all steps. When no real meals are available, use hardcoded demo/placeholder recipe data (with steps and a timer) so the user sees the full tour regardless of DB state.

**Implementation note:** Demo data should be stored in Supabase (both dev + prod) in a shared table (e.g. `demo_recipes`) so all users see the same demo content with proper images — not hardcoded strings. This was flagged as "Phase 2d" in earlier planning.

---

## 4. `_prod` flag temporarily forced to `true` (REVERT BEFORE COMMIT)

`index.html` line ~412 currently reads:
```js
var _prod = true; // TEMP: force prod data locally — revert before commit
```
Must be changed back to:
```js
var _prod = location.hostname === 'alemava.github.io';
```
before any commit/push to `dev` or `main`.
