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
python3 scripts/scoper.py --build-index           # force cache rebuild
python3 scripts/scoper.py --check                 # report cache freshness
python3 scripts/scoper.py --root /path            # default: .
python3 scripts/scoper.py --max 10                # default: 5
python3 scripts/scoper.py --no-symbols            # disable symbol matching
python3 scripts/scoper.py --no-git-boost          # disable git-hot boost
python3 scripts/scoper.py --no-session-memory     # disable session memory
python3 scripts/scoper.py --no-import-graph       # disable import-graph expansion
python3 scripts/scoper.py --no-monorepo           # disable cross-package penalty
python3 scripts/scoper.py --no-token-warnings     # disable token-budget warnings
```

## Caching
- Cache dir: `.scoper_cache/tree_index.json` (gitignored)
- Invalidation: git HEAD hash + dirty file count, or 5-min mtime fallback

## Architecture notes
- `.gitignore`-aware via `git ls-files` (tracked + untracked-but-not-ignored)
- Non-git fallback uses `os.walk` with hardcoded ignore list (node_modules, dist, venv, etc.)
- Fuzzy matching via stdlib `difflib` on filenames/paths — no import graph or dependency traversal
- Symbol matching: regex-based extraction of function/class/component names inside files (JS/TS/Python/Go etc.)
- Git-hot recency boost: staged/unstaged changes + last 5 commits get ranking nudge
- Session memory: logs prompt→candidates in session_log.json for multi-turn continuity
- Import graph: resolves JS/TS/Python relative imports; 1-hop expansion surfaces related_files
- Monorepo detection: scans for package.json/pyproject.toml/go.mod etc.; cross-package candidates penalized
- Token warnings: rough size-based estimate (~4 chars/token), warns if per-file >2K or total >6K
- Output: JSON with `{cache_status, candidates, related_files, token_estimate, warnings, total_files_indexed}`

## Distinctions
- SKILL.md goes into `.opencode/skills/spotter/` or `~/.config/opencode/skills/spotter/`
- scoper.py goes into a `scripts/` subdir alongside SKILL.md
