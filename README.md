<h1 align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://img.shields.io/badge/spotter-scope%20before%20read-4f46e5?style=for-the-badge&labelColor=1e1e2e">
    <img src="https://img.shields.io/badge/spotter-scope%20before%20read-4f46e5?style=for-the-badge&labelColor=ffffff">
  </picture>
</h1>

<p align="center">
  <em>You don't read 70 files to find one. Neither should your agent.</em>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.7+-blue?style=flat-square" alt="Python 3.7+">
  <img src="https://img.shields.io/badge/tests-109%20passed-brightgreen?style=flat-square" alt="109 tests passed">
  <img src="https://img.shields.io/badge/deps-stdlib%20only-10b981?style=flat-square" alt="Zero dependencies">
  <img src="https://img.shields.io/badge/license-MIT-8b5cf6?style=flat-square" alt="MIT license">
</p>

---

## Before / After

You tell your agent, *"fix the add product button on the inventory page."*

**Without spotter**, the agent wanders:

```
read(root/)          →  8 entries
read(src/)           →  6 directories
read(src/app/)       →  7 directories
grep("product", src) → 17 matches found
read(match-1)        → 12,000 bytes
read(match-2)        →  8,000 bytes
  ... 15 more reads ...
read(match-17)       →  5,000 bytes
───────────────────────────────────
Total: 17 files, ~51,000 tokens
```

**With spotter**, the agent locates first:

```
scoper.py --scope "..."  →  6 candidates
read(candidate-1)        →  2,500 bytes
  ... 4 more reads ...
read(candidate-6)        →  1,200 bytes
───────────────────────────────────
Total: 6 files, ~1,800 tokens
```

96.5% fewer tokens. Same edit.

## Numbers

Real measurement against a Next.js POS project (111 tracked files, 12 prompts averaged):

| Metric | Without spotter | With spotter | Saved |
|---|---|---|---|
| Files scanned | 17 | 6 | 65% fewer |
| Context tokens | ~51K | ~1.8K | 96.5% |
| Context waste | 28x extra | zero | all of it |

An agent scanning the entire `src/` directory (70 files) burns 51x more context than spotter. Neither reads a byte more relevant code.

## How it works

```
User prompt
   │
   ▼
scripts/scoper.py --scope "<prompt>"
   │  (gitignore-aware listing, cached index, scored across
   │   filename + symbols + imports + git recency + session)
   ▼
candidates (primary)  +  related_files (import-graph neighbors)
   │
   ▼
Agent reads candidates. Edits. Done.
```

The scoper combines five signals:

1. **Filename/path** : tokenized camelCase/kebab-case fuzzy matching
2. **Symbol extraction** : regex-scans JS, TS, Python, Go, Rust, Kotlin, C#, Swift, Dart for function/class/component names
3. **Git recency** : files you touched recently get a ranking nudge
4. **Session memory** : similar prompts boost prior candidates (multi-turn "continue from before")
5. **Import graph** : resolves relative imports and TS path aliases (`@/`) across 9 languages, surfaces 1-hop neighbors

Scoring extras: frequency penalty for common path tokens, tie-breaking by keyword density, symbol multi-hit boost.

## Features

| Feature | Details |
|---|---|
| `.gitignore`-aware listing | `git ls-files` for git repos, `os.walk` + `.gitignore` parsing for others |
| Cached index | `.scoper_cache/` stores file list, symbols, imports : reused across prompts |
| Smart invalidation | Git fingerprint (HEAD hash + dirty count), 5-min mtime fallback |
| 9-language import graph | JS, TS, Python, Go, Rust, Ruby, PHP, Java. TS path aliases (`@/`) resolved |
| 10-language symbols | Regex extraction for declarations in 10 languages |
| Monorepo penalty | Cross-package candidates deprioritized, never excluded |
| Token warnings | Flags files >2K tokens or totals >6K |
| Session memory | `.scoper_cache/session_log.json` for multi-turn continuity |
| Config file | `~/.scoperrc` (global) or `./.scoperrc` (project) : JSON overrides |
| Binary safety | Null-byte detection skips binary files |

## Install

### OpenCode

```bash
# Project-local
mkdir -p .opencode/skills/spotter
cp SKILL.md .opencode/skills/spotter/
cp -r scripts .opencode/skills/spotter/

# Global
mkdir -p ~/.config/opencode/skills/spotter
cp SKILL.md ~/.config/opencode/skills/spotter/
cp -r scripts ~/.config/opencode/skills/spotter/
```

### Claude Code

Place under `.claude/skills/spotter/` (project) or `~/.claude/skills/spotter/` (global).

## Usage

The skill triggers on vague UI/component instructions. Run manually:

```bash
python3 scripts/scoper.py --root . --scope "fix the header button"
```

```json
{
  "cache_status": "hit",
  "total_files_indexed": 132,
  "candidates": [
    "src/components/Header.jsx",
    "src/components/Login.jsx"
  ],
  "related_files": [
    "src/components/Button.jsx"
  ],
  "token_estimate": {
    "src/components/Header.jsx": 812,
    "src/components/Login.jsx": 340,
    "src/components/Button.jsx": 210
  },
  "warnings": [],
  "scope_dir": null
}
```

All flags:

```bash
python3 scripts/scoper.py --root . --build-index
python3 scripts/scoper.py --root . --check
python3 scripts/scoper.py --root . --scope "..." --no-symbols
python3 scripts/scoper.py --root . --scope "..." --no-git-boost
python3 scripts/scoper.py --root . --scope "..." --no-session-memory
python3 scripts/scoper.py --root . --scope "..." --no-import-graph
python3 scripts/scoper.py --root . --scope "..." --no-monorepo
python3 scripts/scoper.py --root . --scope "..." --no-token-warnings
python3 scripts/scoper.py --root . --scope "..." --scope-dir src/components
python3 scripts/scoper.py --root . --scope "..." --min-score 0.6
python3 scripts/scoper.py --root . --scope "..." --max 10
```

Run tests:

```bash
python3 -m unittest discover tests/
```

## Requirements

Python 3.7+, stdlib only. Git optional but recommended.

## License

MIT
