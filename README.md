<h1 align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://img.shields.io/badge/telik-scope%20before%20read-4f46e5?style=for-the-badge&labelColor=1e1e2e">
    <img src="https://img.shields.io/badge/telik-scope%20before%20read-4f46e5?style=for-the-badge&labelColor=ffffff">
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

**Without telik**, the agent wanders:

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

**With telik**, the agent locates first:

```
scoper.py --scope "..."  →  5 candidates
read(candidate-1)        →  2,829 bytes
  ... 3 more reads ...
read(candidate-5)        →    535 bytes
───────────────────────────────────
Total: 5 files, ~6,121 tokens
```

On a Laravel POS app (727 tracked files), telik averages **86% fewer tokens** across 5 test prompts — and **98%** when vendor-bundle candidates are excluded (which the latest fix now handles).

## Benchmarks

Lakasir v1.1.11 (Laravel 11 + Filament + Livewire, 727 files). Baseline: grep 1–2 dominant keywords, read all matches in full.

| Prompt | Files (telik) | Files (baseline) | Token savings |
|---|---|---|---|
| fix save button di halaman add product | 5 | 180 | **98.96%** |
| tambahin validasi stock pas checkout di cashier page | 5 | 112 | **96.32%** |
| fix bug discount ga kehitung di cashier report | 5 | 52 | **94.63%** |
| samain style widget best selling product sama expired product | 5 | 170 | 76.68% |
| samain card member style sama product resource | 5 | 207 | 75.03% |
| **Average** | **5** | **144** | **86.26%** |

After excluding large vendor/bundle files from candidates (latest fix), the average rises to **98.46%**. Savings depend on prompt language — code-switching (Indonesian + English technical terms) performs best; pure Indonesian without technical terms can miss.

Baseline without telik: agent greps for relevant keywords, reads each match in full. With telik: top 5 candidates. File reduction averages 95.6% (5 vs ~144 files).

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
5. **Import graph** : resolves relative imports and TS path aliases (`@/`) across 10 languages, PHP PSR-4 via `composer.json`, surfaces 1-hop neighbors

Scoring extras: frequency penalty for common path tokens, tie-breaking by keyword density, symbol multi-hit boost, vendor/bundle file penalty (large bundled files scored down so generic keywords don't dominate).

## Features

| Feature | Details |
|---|---|
| `.gitignore`-aware listing | `git ls-files` for git repos, `os.walk` + `.gitignore` parsing for others |
| Cached index | `.scoper_cache/` stores file list, symbols, imports : reused across prompts |
| Smart invalidation | Git fingerprint (HEAD hash + dirty count), 5-min mtime fallback |
| 10-language import graph | JS, TS, Python, Go, Rust, Ruby, PHP, Java. TS path aliases (`@/`) resolved. PHP PSR-4 via `composer.json` |
| 10-language symbols | Regex extraction for declarations in 10 languages |
| Monorepo penalty | Cross-package candidates deprioritized, never excluded |
| Vendor bundle penalty | `public/vendor/`, `*.min.js`, `*.min.css`, `*.esm.js` scored down so generic keywords don't dominate |
| Token warnings | Flags files >2K tokens or totals >6K |
| Session memory | `.scoper_cache/session_log.json` for multi-turn continuity |
| Config file | `~/.scoperrc` (global) or `./.scoperrc` (project) : JSON overrides |
| Binary safety | Null-byte detection skips binary files |
| Default ignore dirs | `vendor/`, `node_modules/`, `Pods/`, `target/`, `.bundle/` excluded from fallback listing |

## Install

### OpenCode

```bash
# Project-local
mkdir -p .opencode/skills/telik
cp SKILL.md .opencode/skills/telik/
cp -r scripts .opencode/skills/telik/

# Global
mkdir -p ~/.config/opencode/skills/telik
cp SKILL.md ~/.config/opencode/skills/telik/
cp -r scripts ~/.config/opencode/skills/telik/
```

### Claude Code

Place under `.claude/skills/telik/` (project) or `~/.claude/skills/telik/` (global).

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

## Limitations

- Ranking is heuristic (filename/symbol/import matching), not semantic — it won't understand intent beyond keyword overlap.
- Works best when files are named after what they do. Projects with heavy use of generic filenames (`index.ts`, `page.tsx` in every route folder) can see lower precision.
- Prompt language matters: code-switching (Indonesian + English technical terms) works best. Pure Indonesian prompts without English technical terms can miss relevant files because the scorer has no synonym layer.
- Git is strongly recommended. Without `.gitignore`, `vendor/` and other dependency dirs are excluded by default but project-specific ignore patterns won't be applied.
- PHP PSR-4 import resolution reads `composer.json` `autoload.psr-4` — works for standard Laravel/Symfony setups but won't cover custom autoload configurations.
- Tie-breaking falls back to alphabetical order when scores are identical — always sanity-check `candidates` before editing blind, especially when results look generic rather than component-specific.

## Requirements

Python 3.7+, stdlib only. Git optional but recommended for best accuracy (enables `.gitignore`-aware listing, git recency boost, and proper index caching).

## License

MIT
