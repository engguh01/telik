# spotter — agent notes

## Project type
OpenCode skill, not a user app. `SKILL.md` is the skill definition consumed by OpenCode. `scoper.py` is the single script it invokes.

## Key files
- `scripts/scoper.py` — single entrypoint, stdlib-only (Python 3.7+). No deps, no `pip install`.
- `SKILL.md` — OpenCode skill definition consumed by the agent runtime.
- `README.md` — full docs and install instructions.
- `tests/test_scoper.py` — unittest suite (stdlib, no deps).

## Commands
```bash
python3 scripts/scoper.py --scope "fix the header button"
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
python3 scripts/scoper.py --scope-dir src/components  # restrict to subdirectory
python3 -m unittest discover tests/               # run tests
```

## Caching
- Cache dir: `.scoper_cache/tree_index.json` (gitignored)
- Invalidation: git HEAD hash + dirty file count, or 5-min mtime fallback

## Architecture notes
- `.gitignore`-aware via `git ls-files` (tracked + untracked-but-not-ignored)
- Non-git fallback uses `os.walk` + parses `.gitignore` patterns via fnmatch-like matching
- Fuzzy matching via stdlib `difflib` on filenames/paths and symbols
- Symbol matching: regex-based extraction of function/class/component names inside files (JS/TS/Python/Go/Rust/Kotlin/C#/Swift/Dart)
- Git-hot recency boost: staged/unstaged changes + last 5 commits get ranking nudge
- Session memory: logs prompt→candidates in session_log.json for multi-turn continuity
- Import graph: resolves JS/TS/Python/Go/Rust/Ruby/PHP/Java relative imports + TS path aliases (@/, ~/); 1-hop expansion surfaces related_files
- Monorepo detection: scans for package.json/pyproject.toml/go.mod etc.; cross-package candidates penalized
- Token warnings: rough size-based estimate (~4 chars/token), warns if per-file >2K or total >6K
- Config file: ~/.scoperrc (global) and ./.scoperrc (project) JSON overrides
- Binary detection: skips symbol/import extraction on files with null bytes in first 1KB
- Score tie-breaking: secondary sort by keyword hit count, then path depth (shorter paths preferred)
- Token frequency penalty: common path tokens (>30% of files) reduce filename match weight
- Symbol density boost: files with multiple matching symbols get weighted higher
- Output: JSON with `{cache_status, candidates, related_files, token_estimate, warnings, scope_dir, total_files_indexed}`

## Distinctions
- SKILL.md goes into `.opencode/skills/spotter/` or `~/.config/opencode/skills/spotter/`
- scoper.py goes into a `scripts/` subdir alongside SKILL.md
