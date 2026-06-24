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

Model: `@cf/leonardo/phoenix-1.0`  
Dimensions: `1024 × 512` (2:1, matches CSS `aspect-ratio: 2/1` exactly — no cropping)  
Response: raw JPEG binary (save directly, no base64 extraction needed)

```bash
curl -s -X POST \
  "https://api.cloudflare.com/client/v4/accounts/5790e6bb8b193165d6ce7707fc5e8b39/ai/run/@cf/leonardo/phoenix-1.0" \
  -H "Authorization: Bearer <CLOUDFLARE_API_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "<visual_prompt>", "width": 1024, "height": 512}' \
  -o /tmp/<week-id>-<meal-id>.jpg
```

### Writing the visual prompt
Do NOT use just the dish title. Read `title`, `ingredients` and `steps`, then describe
how the **finished plated dish looks**: colours, textures, garnishes, vessel, style.
Append `, close-up food photography, warm natural light, appetizing` to every prompt.

**Bad:** `"Beef Quesadillas"`  
**Good:** `"Two golden crispy quesadilla wedges on a wooden board, melted cheddar and seasoned beef visible at the cut edge, chunky guacamole and sour cream on the side, close-up food photography, warm natural light, appetizing"`

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

- `avail` (array of ingredient names, optional) — which of this recipe's ingredients
  the user already has on hand *this week*. Must exactly match an ingredient `name`
  in the recipe's `ingredients` array (case-sensitive). Never put `avail` data on the
  shared recipe itself — the same dish can be fully stocked one week and not the next.
- `meal_type` (`'breakfast'|'lunch'|'dinner'|'special'`, optional) — defaults to being
  treated as a regular dinner-style slot if omitted.
- `remaining` (array of free-text strings, optional) — human-readable leftover notes
  for the shopping list, not tied to a specific ingredient name.

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
