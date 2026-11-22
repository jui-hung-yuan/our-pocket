# data/

| Path | Tracked in git? | What |
|---|---|---|
| `synthetic/` | yes | Generated exports (invented people, instruments and amounts). Regenerate with `make synthetic`; never hand-edit. |
| `raw/` (or anything else) | **no** | Real household exports. Ignored by `.gitignore`; CI fails if anything outside `synthetic/` is tracked. |

Real files are read only by the local CLI into the local database. No numbers from them appear
in the repo, not even counts or totals.
