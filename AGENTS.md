# spotter — agent notes

## Project type
OpenCode skill, not a user app. `SKILL.md` is the skill definition consumed by OpenCode. `scoper.py` is the single script it invokes.

## Key files
- `scripts/scoper.py` — single entrypoint, stdlib-only (Python 3.7+). No deps, no `pip install`.
- `SKILL.md` — OpenCode skill definition consumed by the agent runtime.
- `README.md` — full docs and install instructions.

## Commands
```bash
python3 scripts/scoper.py --scope "ubah button di header"
python3 scripts/scoper.py --build-index       # force cache rebuild
python3 scripts/scoper.py --check             # report cache freshness
python3 scripts/scoper.py --root /path        # default: .
python3 scripts/scoper.py --max 10            # default: 5
```

## Caching
- Cache dir: `.scoper_cache/tree_index.json` (gitignored)
- Invalidation: git HEAD hash + dirty file count, or 5-min mtime fallback

## Architecture notes
- `.gitignore`-aware via `git ls-files` (tracked + untracked-but-not-ignored)
- Non-git fallback uses `os.walk` with hardcoded ignore list (node_modules, dist, venv, etc.)
- Fuzzy matching via stdlib `difflib` on filenames/paths — no import graph or symbol indexing
- Output: JSON with `{cache_status, candidates, total_files_indexed}`

## Distinctions
- SKILL.md goes into `.opencode/skills/spotter/` or `~/.config/opencode/skills/spotter/`
- scoper.py goes into a `scripts/` subdir alongside SKILL.md
