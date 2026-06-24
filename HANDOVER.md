# Mesa — Handover / Roadmap Notes

Items to surface in the recipe-chat roadmap. These are known gaps or decisions that the AI chat workflow needs to handle explicitly.

---

## 1. Recipe duplication across weeks (RESOLVED AT SCHEMA LEVEL — DEV, Phase 3)

**What happened before:** Each `meal_weeks` row stored full recipe JSON inline in the `meals` array. There was no shared recipes table. If the same recipe was used in two different weeks, it was stored **twice** — as two independent full copies.

**What changed (Phase 3, dev project `jtgyttbobxtxgtnmuyas`):** A canonical `public.recipes` table now exists. `meal_weeks.meals` entries are slim objects referencing a recipe by `recipe_id`, not full embedded JSON. Confirmed working in practice during the Phase 3 data migration: "White Bean, Chorizo and Tomato Stew" appeared in both `apr27-may1` and `may11-15` — it was consolidated into one canonical recipe row (`usage_count: 2`) instead of two duplicate rows.

**Still open:** This fix is schema-level only. `index.html` itself (`renderLib`, week rendering, etc.) still reads the *old* embedded-JSON shape and hasn't been updated to look up recipes by `recipe_id` — that's expected to happen when the login UI / session work (Prompt 2) lands, since the app needs the new schema either way. Until then, this fix only exists in the dev database, not in the running app.

---

## 2. Library deduplication (LINKED TO ITEM 1)

**What happens now:** `renderLib` in `index.html` aggregates meals from all `meal_weeks` rows without deduplicating by title. Same recipe appearing in multiple weeks shows up multiple times in the library list.

**Quick fix (no schema change):** Deduplicate by `title` in `renderLib` client-side — show once, use the most-recent week's data.

**Proper fix:** Canonical recipes table (see item 1).

---

## 3. Tour — demo fallback when no meals exist (DONE)

Implemented: when `cookMeals.length === 0`, `startTour()` fetches a shared `tour-demo` week (Spaghetti Carbonara) from Supabase and injects it temporarily so all tour steps render regardless of DB state. Demo data exists in both dev and prod Supabase projects.

---

## 4. Local dev server — always use port 8000

Standard convention going forward: any local server started for mesa (e.g. `python3 -m http.server 8000`) should bind to **port 8000**. This matches the Google OAuth dev client's authorized JavaScript origins (`http://localhost:8000` and `http://127.0.0.1:8000`), configured in Phase 3. Using a different port will break the Google login flow locally until the origin list is updated in Google Cloud Console.

---

## 5. Recipe-chat workflow against PROD breaks once prod adopts the new schema (NEEDS ROADMAP ITEM)

**Current state:** The recipe-image generation workflow described in `CLAUDE.md` ("Updating the meal in Supabase") writes directly into `meal_weeks.meals` JSONB via `jsonb_agg`/`CASE WHEN m->>'id' = ...`, assuming each meal is a full embedded JSON object inside `meal_weeks`. This still works today because **prod still has the old single-tenant schema** — Phase 3 changes so far only exist in dev (`jtgyttbobxtxgtnmuyas`).

**What breaks, and when:** The moment prod is migrated to the Phase 3 schema (recipes as a separate global table, `meal_weeks.meals` holding `recipe_id` references instead of full JSON), that SQL snippet in `CLAUDE.md` stops making sense — there's no embedded recipe JSON left to patch inside `meal_weeks`.

**What needs to happen then:** Rewrite the "Updating the meal in Supabase" section of `CLAUDE.md` to target `public.recipes` directly: `UPDATE recipes SET image_url = '<url>', image_prompt = '<prompt>' WHERE id = '<recipe_id>'` instead of patching JSON inside `meal_weeks`. Not done yet — do this as part of (or immediately before) the prod cutover, not before.

---

## 6. Phase 3 schema migration — dev only, not yet on prod

Dev project `jtgyttbobxtxgtnmuyas` now has: Google OAuth, `profiles`, `recipes` (shared pool, 9 real rows migrated from the 4 existing `meal_weeks`), `user_recipe_history`, `user_preferences`, `email_events`, `prompt_audit_log`, `recipe_translations`, and `user_id` + tightened composite primary keys on `meal_weeks`/`favourites`/`shopping_state`. Full per-user RLS is live in dev. **None of this exists in prod (`vcluruaueetktctdyplh`) yet** — prod is untouched and still serves the old single-tenant schema to the live app. Cutting prod over is a separate, deliberate future step (see items above for what needs rewriting first).

---

## 7. Personal learning reminder — understand RLS properly (NOT URGENT, PERSONAL TODO)

Alejandro asked to flag this for himself: at some point, sit down and actually learn how Postgres Row Level Security works under the hood (policies, `auth.uid()`, `to authenticated`/`to anon` roles, `using` vs `with check`) rather than just trusting Claude's explanations. Not blocking anything — a personal-knowledge item, not a code gap.

---

## 8. Business verification needed before scaling past a handful of users (ROADMAP ITEM)

Both Google and Facebook cap how many real users can log in **before** the app goes through their respective verification processes:

- **Google**: the OAuth consent screen is currently in "Testing" mode — capped at 100 test users (each manually added in Cloud Console), and each test user's authorization expires after 7 days. To remove the cap, the app must be **Published** and go through Google's verification (for our basic scopes — email/profile/openid — this is the lighter tier: domain verification + privacy policy, no security assessment, but still a real step with review time).
- **Facebook**: verification is not needed while every logged-in user has a role (Admin/Developer/Tester) on the app — fine for dev/testing now. It becomes mandatory the moment real users without an assigned role need to log in.

**Action needed before any real public launch (not just dev testing):** verify the "business" on both platforms — register/confirm whatever entity backs mesa (a sole proprietorship / personal business name, since this isn't an incorporated company), then submit both apps for verification ahead of time, since review can take days. Add this explicitly to the roadmap so it doesn't get discovered the week of a real launch.

---

## 9a. Custom domain + Resend SMTP — bundled together, deferred (ROADMAP ITEM)

Decided 2026-06-23: buying/setting up a custom domain for mesa is deferred to a later phase, not done now. **Important finding that affects sequencing:** Resend (already the intended provider for marketing/lifecycle emails per the `email_events.resend_event_id` column and `user_preferences.email_*` columns) requires a verified custom domain before it can send *any* email — there is no sandbox/test sender that works without one (confirmed against Resend's own docs; this differs from how some other providers work, e.g. SendGrid's old sandbox mode). So **domain purchase/setup and Resend SMTP configuration are one combined step, not two** — don't try to configure Resend before the domain exists, it won't work.

**Interim consequence:** until then, dev stays on Supabase's default built-in mailer for the magic-link email login, which has a low hourly send limit (hit it during Phase 3 testing after ~3 sends). Not a blocker — Google and Facebook login aren't email-rate-limited at all, and the limit resets hourly. Just don't expect to send-test magic links rapid-fire.

**UI requirement for Prompt 2 (login UI work), not built yet:** when `signInWithOtp()` returns the rate-limit error, the login screen should catch it specifically and show something like "El magic link está temporalmente fuera de servicio — usa Google o Facebook, o inténtalo de nuevo más tarde," instead of a raw/generic error. Decided 2026-06-23, while testing magic link in dev — capture this now so it's not lost before Prompt 2 starts.

---

## 9. Recipe scaling model — decided (Phase 3 data), engine not built yet (Phase 4)

Based on a deep-research pass (no mainstream recipe app — Paprika, NYT Cooking, AllRecipes, Mela — does category-aware scaling; it's all naive linear multiplication, and users complain about over-salted/over-leavened results from it). **Decided model, ready for whoever builds the Fase 4 scaling engine:**

**Ingredient categories** (`recipes.ingredients[].scaling`, already applied to all 9 recipes):
| Category | Meaning | Approx. exponent | Used today? |
|---|---|---|---|
| `linear` | scales 1:1 with servings (proteins, bulk veg, grains, liquids, sauce bodies) | 1.0 | yes — 49 ingredients |
| `seasoning` | mild aromatics/spices/acids — garlic, onion, cumin, oregano, wine, lemon juice | ~0.8 (use ~75% of linear at 2x, adjust to taste) | yes — 26 ingredients |
| `heat` | dominant/hot — chilli, chipotle, sriracha | ~0.6–0.7 (scale even less aggressively, flag strongly) | yes — 5 ingredients |
| `fixed` | doesn't scale at all — bay leaf, salt/pepper "to taste" | n/a | yes — 10 ingredients |
| `leavening` | baking powder/soda/yeast — hard chemistry rule, not a taste preference (excess collapses structure) | ~0.75–0.8 | not yet — no baking recipes in the catalog |
| `thickener` | flour/cornstarch/roux — gelatinizes more efficiently at volume | ~0.8–0.9 | not yet — no recipe needs one currently |

**Time scaling** — not modeled per-step yet (still just free-text `time_scaling_notes` per recipe, written with the right intuition already, e.g. "fry chicken in batches above 4 servings"). The research's recommended model for later: tag each step `PER_QUANTITY` (prep, ~scale^0.8), `FIXED` (boil/simmer/bake-same-depth), or `PER_BATCH` (browning/searing — has a pan-capacity ceiling, more servings = more batches at the *same* per-batch time, not longer batches).

**Soft cap on scaling** — the research recommends ~4x as a practical ceiling, above which recipes should be cooked in repeated batches rather than scaled further. `recipes.serves_max` already encodes exactly this per-recipe (we set it independently per dish, e.g. 4 for pan-fried things, 6 for a stew) — validates that decision from Phase 3, nothing to change.

**Explicitly NOT built**: the actual computation engine (turning category + exponent into a real scaled quantity), the per-step time model, and the batch-ceiling warning logic. The research itself says the exponents are "tunable defaults, not ground truth" — needs real testing against actual recipes before trusting the numbers, not just implementing them blind.

---

## 10. Tour fix — "Pick a day" step now shows matching demo content (DONE, unrelated to Phase 3)

**Bug:** in `startTour()`, the "Pick a day" step injected a synthetic nav into `#wnav` showing a "Tue" pill with a badge of "2", but the actual content below in `#main` still showed whatever real day was active (usually 1 recipe) — the badge and the content disagreed.

**Fix:** the same step now also injects synthetic day-overview content into `#main` (a Breakfast + a Dinner meal card) matching the "Tue 2" badge, via shared `injectDemoDayState()`/`restoreDemoDayState()` helpers. Added a new tour step right after it (`#tour-day-demo`), highlighting that injected content, explaining a day can hold more than one recipe — placed before the existing "Shopping list" step.

**Follow-up bug, found by actually automating the tour with Playwright instead of relying on manual retests:** the first version of this fix called `resyncHighlight()` (a `moveTo(sameIndex)` re-resolve helper used elsewhere in the tour for elements that don't exist yet) on the new `#tour-day-demo` step. Root cause traced by reading driver.js's own source: `moveTo` always replays the full deselect→reselect lifecycle, and driver.js decides whether to fire the outgoing step's `onDeselected` by comparing **DOM node identity**, not selector/id. Since `injectDemoDayState()`/`restoreDemoDayState()` recreate `#tour-day-demo` via `innerHTML` reassignment, every pass produced a new node — so `moveTo(sameIndex)` always saw "changed", re-fired `onDeselected`, which destroyed the node, which made the next pass recreate it again, looping, and briefly measuring the highlight against a just-detached zero-size node (the "box disappears"). Moving the fix to "Pick a day" instead just relocated the same bug, since the bug was inherent to `moveTo`-to-self plus DOM-recreating handlers, not tied to either specific step.

**Real fix:** stop destroying/recreating `#tour-day-demo` *between* the two paired steps in either direction. Neither step's `onDeselected` tears anything down anymore. Teardown happens explicitly in the next real step that needs the actual nav back ("Shopping list", via `restoreDemoDayState()` at the top of its `onHighlightStarted`) — mirroring how cooking-mode is already restored elsewhere in this same tour (a *later* step cleans up earlier synthetic state, rather than the owning step tearing itself down). A safety-net `restoreDemoDayState()` call was also added to the tour's global `onDestroyed` in case the tour is closed mid-pair. The `#tour-day-demo` step's own `onHighlightStarted` only recreates the content if it's missing (mirrors the existing `.rxn`/`start-cooking`/`floating-timer` check-then-resync idiom already used elsewhere in `startTour()`), so it self-heals when entered backward (e.g. from "Shopping list") without looping.

**Verified, not just assumed:** ran the actual tour end-to-end in headless Chromium (Playwright) — both directions, all four affected steps, with screenshots and DOM measurements at each transition, including settle-time checks to rule out flicker/loops. No JS errors, no zero-sized/disappearing highlight at any step, both forward and backward navigation confirmed visually correct. One harmless cosmetic artifact found and accepted: a leftover invisible (0×0, opacity 0, non-interactive) driver.js "dummy" placeholder element occasionally keeps a stale CSS class after a backward-navigation resync — confirmed it never affects what's actually drawn on screen (driver.js positions the visible highlight/popover from a single latest-element rect, not by class query), so left as-is rather than reaching into driver.js internals to chase a non-visible artifact.

Purely synthetic/injected HTML for the tour overlay — doesn't read from `G.week.meals` or touch the schema, so it was safe to drop in regardless of the Phase 3/3b schema work happening in parallel. SW bumped to v88.

**Follow-up, same day:** both demo cards used the emoji `meal-card-ph` fallback instead of a real photo. "Spaghetti Carbonara" already had a real generated image (`tour-demo-tour-demo-meal.jpg`, used elsewhere as the real shared `tour-demo` recipe) but the synthetic HTML never referenced it. "Greek Yogurt with Honey & Walnuts" isn't a real recipe anywhere — it was invented purely as filler text for this card — so a new image was generated for it via the same Cloudflare Leonardo Phoenix workflow documented above and uploaded to dev's `recipe-images` bucket as `tour-demo-tour-demo-breakfast.jpg`. Both cards now render real `<img>` tags, built from the environment-aware `SB` constant (same dev/prod branching the rest of the app already uses) rather than a hardcoded absolute URL — so no code change will be needed when prod gets these images too.

**Still open:** the new breakfast image was only uploaded to **dev's** bucket — prod's Supabase project doesn't have a `recipes` table yet (confirmed: querying it errors with `relation "recipes" does not exist`), so this whole tour fix, like the rest of the Phase 3/B work, can't be deployed to prod in isolation. When the prod schema cutover (item 6) finally happens, upload `tour-demo-tour-demo-breakfast.jpg` to prod's `recipe-images` bucket too (same file, same name) — the code already points at the right place via `SB`, it just needs the asset to exist there. SW bumped to v89.
