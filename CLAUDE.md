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
Filename convention: `<week-id>-<meal-id>.jpg` (e.g. `may11-15-tue.jpg`)

```bash
curl -s -X POST "<SUPABASE_URL>/storage/v1/object/recipe-images/<filename>" \
  -H "apikey: <ANON_KEY>" \
  -H "Authorization: Bearer <ANON_KEY>" \
  -H "Content-Type: image/jpeg" \
  -H "x-upsert: true" \
  --data-binary @/tmp/<filename>
```

Public URL: `<SUPABASE_URL>/storage/v1/object/public/recipe-images/<filename>`

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
