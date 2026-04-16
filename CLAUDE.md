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
