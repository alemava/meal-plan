# mesa — project instructions for Claude

## SW cache version — ALWAYS bump on changes

Every time `index.html` (or any other shell file) is modified, the Service Worker
cache version in `sw.js` **must** be incremented before pushing, otherwise installed
PWA users will not receive the update.

The version string is at the top of `sw.js`:
```
var CACHE = 'mesa-vN';
```

Increment N by 1 on every deploy. Current version is tracked in that file — just
read it and add 1. **Always bump sw.js even if only index.html changed** — the
SW cache controls what the installed PWA serves.

## File to edit
All app code lives in a single file: `index.html`

## Push policy
Make changes locally first, then **ask the user for confirmation** before pushing
to GitHub. Never commit + push in the same step without asking.

## Repo
https://github.com/alemava/meal-plan  (GitHub Pages at https://alemava.github.io/meal-plan)

## Supabase environments
Runtime hostname detection in `index.html`:
- `alemava.github.io` → **prod** project `vcluruaueetktctdyplh`
- any other host → **dev** project `jtgyttbobxtxgtnmuyas`

Never hardcode one set of credentials — the ternary in `index.html` handles both.

## Week IDs must include the year

`meal_weeks.id` (the "week-id" used everywhere below) is a human-chosen slug like
`may11-15`. **Always prefix it with the year**, e.g. `2027-may11-15` — not `may11-15`.

Without the year, the same slug recurs every year for the same user (e.g. "May 11-15"
happens again in 2027, 2028, ...). Since `meal_weeks`'s primary key is
`(user_id, id)`, a repeated slug for the same user is a real primary-key collision,
not a cosmetic issue — the insert fails. `recipes.id` does not have this problem; it's
a generated UUID with no human-chosen component.

## Recipe image generation

Model: `@cf/black-forest-labs/flux-2-klein-9b` (bumped 2026-07-20 from
`flux-1-schnell` after a same-day model shootout — see `backend/benchmarks/` and the
model-audit artifact — found it visibly more realistic at comparable latency; DeepInfra's
`black-forest-labs/FLUX-2-klein-9b` was switched the same day, so both links in the
image provider chain now use the same model). Native size `1024×1024` on Cloudflare.  
Dimensions: `1024 × 512` (2:1, matches CSS `aspect-ratio: 2/1` exactly) on DeepInfra;
Cloudflare's endpoint has no width/height parameter, so the backend
(`app/services/cloudflare.py`) centre-crops the decoded image to 2:1 itself, measuring
the actual returned dimensions at runtime rather than assuming a fixed native size.  
Response: **JSON with base64** (`{"result": {"image": "<base64>"}}`), not raw bytes.  
Request: **`multipart/form-data`**, not plain JSON — a bare JSON POST returns
`"required properties at '/' are 'multipart'"` (confirmed via Cloudflare's own
`ai/models/schema` endpoint). flux-1-schnell (the old model) accepted plain JSON;
this one doesn't.

```bash
curl -s -X POST \
  "https://api.cloudflare.com/client/v4/accounts/5790e6bb8b193165d6ce7707fc5e8b39/ai/run/@cf/black-forest-labs/flux-2-klein-9b" \
  -H "Authorization: Bearer <CLOUDFLARE_API_TOKEN>" \
  -F "prompt=<visual_prompt>" \
  -F "steps=4" \
  | python3 -c "import sys,json,base64; open('/tmp/<week-id>-<meal-id>.jpg','wb').write(base64.b64decode(json.load(sys.stdin)['result']['image']))"
```

If generating manually (outside the backend's own crop step), open the saved file and
centre-crop to 2:1 before uploading — don't skip this, the frontend CSS no longer
crops on its own.

### Writing the visual prompt
Do NOT use just the dish title. Read `title`, `ingredients` and `steps`, then describe
how the **finished plated dish looks**: colours, textures, garnishes, vessel, style.
Append `, close-up food photography, warm natural light, appetizing` to every prompt.

**Bad:** `"Beef Quesadillas"`  
**Good:** `"Two golden crispy quesadilla wedges on a wooden board, melted cheddar and seasoned beef visible at the cut edge, chunky guacamole and sour cream on the side, close-up food photography, warm natural light, appetizing"`

**"False friend" dish names** — if the title commonly means a *different* food in
English/international usage (e.g. Spanish tortilla = an egg-and-potato omelette, not
a Mexican flour tortilla wrap/flatbread), explicitly rule out the wrong reading inside
the prompt itself. Real bug found live (2026-07-18): "Tortilla de Patatas con
Aceitunas"'s image rendered as a wrap, because the prompt described the dish
accurately but never said the word "tortilla" doesn't mean wrap here — the image
model has no other way to know.  
**Bad:** `"A golden-brown, thick tortilla cut into wedges, revealing layers of soft potatoes, onions, and black olives..."`  
**Good:** `"A thick Spanish potato omelette (an egg dish, NOT a flatbread or wrap), sliced into wedges, revealing layers of soft potatoes, onions, and black olives..."`

**Send the title alongside the prompt, always** — the backend's own
`image_chain.generate_and_upload_image` does this automatically now (prepends
`title + ". " + image_prompt` before calling the image provider, the one call site
every image request goes through), as a second, deterministic layer of disambiguation
that doesn't depend on the model remembering to write it into the prompt text itself.
Do the same by hand if generating manually — prepend the recipe title to
`<visual_prompt>` in the curl call below.

### Uploading to Supabase Storage
Bucket: `recipe-images`  
Filename convention: `<week-id>-<meal-id>.jpg` (e.g. `2027-may11-15-tue.jpg` — week-id includes the year, see above)

As of Phase 3, **dev** (`jtgyttbobxtxgtnmuyas`) requires an authenticated admin to write
to this bucket — the anon key alone is rejected there (only SELECT via the public bucket
URL stays open to everyone). **Prod** (`vcluruaueetktctdyplh`) has not been touched yet —
the anon key still works there for now, until prod gets the same fix in a later, explicit
step.

Use the **service_role key** for this upload step on dev, and recommended on prod too even
though not yet required there (one habit, works on both, and prod will need it eventually).
Copy it fresh from the Supabase Dashboard → Project Settings → API → `service_role` secret
for whichever project you're uploading to. Never paste it into this file, never commit it,
never put it in `index.html` — it bypasses RLS entirely.

```bash
curl -s -X POST "<SUPABASE_URL>/storage/v1/object/recipe-images/<filename>" \
  -H "apikey: <SERVICE_ROLE_KEY>" \
  -H "Authorization: Bearer <SERVICE_ROLE_KEY>" \
  -H "Content-Type: image/jpeg" \
  -H "x-upsert: true" \
  --data-binary @/tmp/<filename>
```

Public URL: `<SUPABASE_URL>/storage/v1/object/public/recipe-images/<filename>`

### Per-week meal reference fields

Each entry in `meal_weeks.meals[]` is a slim reference, not a full recipe (the recipe
itself lives in `public.recipes`, looked up by `recipe_id`). Fields on the reference
itself are **week-instance data** — things that are true for this specific week for
this specific user, never true of the recipe in general:

- `avail` (array of ingredient names, optional) — **deprecated**, superseded by
  `week_pantry` below. Always written as `[]` by `select_recipe.py` and never
  populated by any other code path (front or back end) — confirmed dead in
  practice during Phase 4's pantry-quantity work. Still present in the schema
  for backward compatibility; not the mechanism to build on.
- `meal_type` (`'breakfast'|'lunch'|'dinner'|'special'`, optional) — defaults to being
  treated as a regular dinner-style slot if omitted.
- `remaining` (array of free-text strings, optional) — human-readable leftover notes
  for the shopping list, not tied to a specific ingredient name.
- `people` (int) — servings for this specific meal instance. Changeable after
  assignment via `PATCH /api/meal-servings` (`app/api/meal_servings.py`) — the
  frontend's `hydrateMeals` re-derives scaled ingredient quantities and kcal
  from canonical `recipes` data against this value on every load, so changing
  it is the only write this endpoint needs to do.

### `week_pantry` — quantity-aware pantry, per user per week

```sql
CREATE TABLE week_pantry (
  user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  week_id text NOT NULL,
  pantry jsonb NOT NULL DEFAULT '[]',  -- [{name, qty, unit}, ...]
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, week_id)
);
```

A **dedicated table, deliberately not a `meal_weeks.pantry` column** —
`meal_weeks` has a live `meal_weeks_insert_notify` trigger that fires the
"your week is ready" email on **any** INSERT, unconditionally, with no
WHERE/WHEN clause. Pantry is saved at *generation* time, before any real
selection exists (the normal, first-insert state for a new week) — piggybacking
on `meal_weeks` would send a false "ready" email with zero meals. `week_pantry`
mirrors `shopping_state`'s role (auxiliary per-week data, its own table, written
directly from the frontend via `sbFetch` + RLS, no backend endpoint needed).

Threaded into generation via `generation_jobs.pantry` (sanitized once, in the
`POST /api/generate-recipes` route handler, via
`guardrails.sanitize_pantry_ingredients`) — the Cloud Tasks worker
(`app/api/internal.py`) reads it back out of the job row and passes it to
`_run_generation`, since `_run_generation` is only ever invoked from the
worker, never directly from the route. `fresh_generation.py`'s system prompt
includes it as DATA ("the user has these on hand..."), explicitly subordinate
to the dish-authenticity rule — a hint that narrows which real dish to pick,
never license to invent one or drop a defining ingredient.

Shopping-list coverage math (`buildShopBuckets` in `index.html`) matches a
pantry entry to recipe ingredient need by the same `ingKey()` normalization,
computed only when both share one exact unit (no unit-conversion system
exists) — e.g. 500g on hand against two recipes needing 300g each shows 500g
covered, 100g to buy. A fully-covered ingredient moves into "Already have"
entirely, same as the (now-deprecated) `avail` mechanism was meant to.

### Frontend writes needing `user_id` explicitly

`shopping_state` and `favourites` are written directly from `index.html` via
`sbFetch` (RLS-protected, no backend endpoint). Both **require `user_id` in
the POST body** — there is no column default or trigger that populates it.
A real bug found live (2026-07-16): both write paths omitted it, so
`shopping_state` inserts failed the NOT NULL constraint silently (the calling
code's `catch` swallowed the error — the table had **zero rows**, ever,
for any user) and `favourites` inserts succeeded with `user_id = NULL`
(nullable there), permanently invisible under RLS. Fixed by capturing
`session.user.id` into a `currentUserId` global (alongside the existing
`currentAccessToken`) in `onAuthStateChange`, and including it explicitly in
both write bodies. Any *new* direct-from-frontend write to an RLS-protected
table must include `user_id` explicitly — never assume it's populated for you.

## Backend jsonb columns — never pre-serialize

`backend/app/core/db.py` registers an asyncpg codec that already does
`json.dumps`/`json.loads` on every `json`/`jsonb` column. Any Python code going
through `db.pool()` (app code or an ad-hoc script) must pass a native
`list`/`dict` for a jsonb parameter — **never** `json.dumps(...)` it first.
Doing so double-encodes: the column ends up holding a jsonb *string* whose
content is JSON text, instead of a jsonb *array*/*object* — `jsonb_typeof()`
shows `'string'` instead of `'array'`/`'object'`. This happened for real
(2026-07-14): ad-hoc repair scripts touching `recipes.ingredients`/`steps`
directly caused it on 108 rows, later fixed with `(col #>> '{}')::jsonb`
and closed off with a CHECK constraint (`recipes_ingredients_is_array`,
`recipes_steps_is_array`) so it fails loudly instead of corrupting silently.

### Updating the meal in Supabase (use MCP execute_sql)
Project IDs: dev `jtgyttbobxtxgtnmuyas` · prod `vcluruaueetktctdyplh`

```sql
UPDATE meal_weeks
SET meals = (
  SELECT jsonb_agg(
    CASE WHEN m->>'id' = '<meal_id>'
    THEN m || jsonb_build_object(
      'image_url',    '<public_url>',
      'image_prompt', '<visual_prompt>'
    )
    ELSE m END
  )
  FROM jsonb_array_elements(meals) m
)
WHERE id = '<week_id>';
```

## Next chapter (MVP is closed — this is what comes after)

The AI backend (Phase 4) is complete and verified against **dev** only. Prod
(`alemava.github.io` / `vcluruaueetktctdyplh`) still serves the old, pre-AI,
single-tenant app. In priority order:

1. **Prod cutover — the big one.**
   - Replay all `backend/` migrations against prod (currently only 3 tables:
     `favourites`, `meal_weeks`, `shopping_state` — dev has 48+ migrations of
     schema on top of that). **Caution**: prod's `meal_weeks` rows predate
     the `recipe_id`-reference schema (recipes used to be embedded inline as
     full JSON, per `HANDOVER.md` item 1) — any data migration written for
     dev's already-migrated shape must be dry-run against prod's real rows
     first, not assumed compatible.
   - Deploy the backend as a prod Cloud Run service using the `*_PROD`
     credentials already present in `.env` (`DATABASE_URL_PROD`,
     `SUPABASE_URL_PROD`, `SUPABASE_SERVICE_KEY_PROD`,
     `SUPABASE_JWT_SECRET_PROD`) — currently unused, dev-only ones are live.
   - Flip the `BACKEND` constant in `index.html` (~line 558) so the prod
     host stops resolving to `null` and points at the new prod service.
   - Storage RLS parity: prod's `recipe-images` bucket still accepts the
     anon key for writes (dev already locked this down to admin-only — see
     "Uploading to Supabase Storage" above). Apply the same fix to prod
     during cutover, not before (no images are generated against prod yet).
   - Rewrite the "Updating the meal in Supabase" section above once prod is
     on the new schema — it currently patches embedded JSON, which won't
     exist anymore (see `HANDOVER.md` item 5 for the exact rewrite needed).
2. **Design pass on the AI generation screens** (generate form + suggestion
   cards) via Claude Design's `/design`/`/design-sync` — deliberately done
   *after* the pantry UI shipped, not before, so the form is designed once
   against its final control set, not twice. See the closeout plan for the
   step-by-step handoff (extract standalone preview files first — mesa has
   no component-file library today, `DesignSync` expects one).
3. Smaller, deferred items: `recipe_discards`'s RLS-disabled state was found
   and fixed 2026-07-17 (owner-scoped SELECT/INSERT policies); the
   `notify_meal_ready()` trigger function's public EXECUTE grant (via the
   `PUBLIC` pseudo-role, not `anon`/`authenticated` directly) was revoked the
   same day. Still open: `pg_net` extension living in the `public` schema
   (cosmetic, Supabase advisor WARN); a "the amount needed for X increased
   since you marked it bought" nudge for the shopping list (pre-existing
   ambiguity, made more visible by real pantry quantities, never a blocker);
   unit conversion in shopping-list math (today: exact-unit match only);
   tier filtering (recommended/optional ingredients) on rehydrate.
4. Business verification (Google OAuth consent screen, Facebook app review)
   before any real public launch beyond a handful of test users — see
   `HANDOVER.md` item 8. Not urgent while testing solo/with a few people.
