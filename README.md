# spotter

An [Agent Skill](https://opencode.ai/docs/skills/) (SKILL.md format) that
enforces a **scope-before-read** workflow for vibe-coding agents.

> In sniping, the shooter never works alone. Next to them is a spotter —
> the one with the scope, calling out coordinates so the shooter never
> wastes a shot searching for the target. This skill plays that role for
> your coding agent: it finds the coordinates (file paths) before the
> agent (the shooter) reads or touches anything.

## The problem

Vibe-coding instructions are short — `"ubah button di header"`,
`"samain style sama halaman login"` — but a naive agent resolves them by
reading the entire file tree, or opening many files just to find the one
that matters. That's a lot of wasted input tokens for a trivial "where is
this file" lookup.

## What this skill does

It forces the agent to **locate candidate files first, and read file
contents second** — instead of scanning or reading the whole project on
every prompt.

```
User prompt
   │
   ▼
scripts/scoper.py --scope "<prompt>"
   │  (gitignore-aware file list, cached, scored by filename/symbol/
   │   git-recency/session-history, expanded via the import graph)
   ▼
candidates (primary)  +  related_files (import-graph context)
   │
   ▼
Agent reads candidates (and related_files only if actually needed),
then executes the edit
```

## Features

- **`.gitignore`-aware file listing** via `git ls-files` — `node_modules`,
  build output, etc. are excluded automatically, no manual ignore-list
  needed. Falls back to a basic `os.walk` + ignore-list for non-git
  projects.
- **Caching** — the file list, symbol index, and import graph are cached
  in `.scoper_cache/` per project so repeated prompts in the same session
  are cheap after the first call.
- **Git-aware cache invalidation** — uses the HEAD commit hash + dirty
  file count as a fingerprint when the project is a git repo; falls back
  to a time-based (mtime) check otherwise.
- **Filename/path matching** — extracts keywords from the prompt and
  scores candidate files by tokenized (camelCase/kebab-case-aware)
  substring matching + fuzzy similarity (stdlib `difflib`, no extra
  dependencies).
- **Symbol matching** — regex-based extraction of function/class/component
  names declared inside each file, so a prompt mentioning `TopHeader`
  still finds it even if it's declared inside a file named `Nav.jsx`.
- **Git-hot recency boost** — files with uncommitted changes or touched
  in the last 5 commits get a small ranking boost, since vibe-coding
  prompts are often continuations of whatever was just being worked on.
- **Session memory** — logs recent prompts and their resulting
  candidates (`.scoper_cache/session_log.json`); a new prompt similar to
  a recent one gets a small boost toward those same files, helping
  multi-turn sessions ("lanjutin yang tadi, tambahin border juga").
- **Import/dependency graph matching** — resolves relative `import`/
  `require` statements (JS/TS/Vue/Svelte/Astro and Python) to real project
  files, then surfaces direct imports/importers of primary candidates as
  `related_files` — e.g. a shared `Button.jsx`/theme file, even if it
  shares no keywords with the prompt.
- **Monorepo/package-boundary awareness** — detects package roots via
  marker files (`package.json`, `pyproject.toml`, `go.mod`, `Cargo.toml`,
  `composer.json`) and ranks candidates outside the top match's package
  lower (never hard-excluded, just deprioritized).
- **Token-budget warnings** — a rough size-based token estimate
  (`token_estimate`) for every candidate/related file, with `warnings`
  when a single file or the total would be wasteful to read in full.

All of the above (except the core file listing + cache) can be toggled
off via CLI flags for debugging/comparison — see Usage below.

## Installation

### OpenCode

Project-local (applies to one repo):

```bash
mkdir -p .opencode/skills/spotter
cp SKILL.md .opencode/skills/spotter/
cp -r scripts .opencode/skills/spotter/
```

Global (applies to every project):

```bash
mkdir -p ~/.config/opencode/skills/spotter
cp SKILL.md ~/.config/opencode/skills/spotter/
cp -r scripts ~/.config/opencode/skills/spotter/
```

### Claude Code

Same SKILL.md format is compatible — place the folder under
`.claude/skills/spotter/` (project) or `~/.claude/skills/spotter/`
(global).

## Usage

The skill triggers automatically when your instruction references
existing UI elements/components/pages in vague terms. You can also invoke
it manually:

```
@codebase Use the spotter skill to find files relevant to "fix the navbar spacing"
```

You can also run the underlying script directly, outside of an agent:

```bash
python3 scripts/scoper.py --root . --scope "ubah button di header samain tema login"
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
  "warnings": []
}
```

Other flags:

```bash
python3 scripts/scoper.py --root . --build-index          # force cache rebuild
python3 scripts/scoper.py --root . --check                # report cache freshness only
python3 scripts/scoper.py --root . --scope "..." --no-symbols
python3 scripts/scoper.py --root . --scope "..." --no-git-boost
python3 scripts/scoper.py --root . --scope "..." --no-session-memory
python3 scripts/scoper.py --root . --scope "..." --no-import-graph
python3 scripts/scoper.py --root . --scope "..." --no-monorepo
python3 scripts/scoper.py --root . --scope "..." --no-token-warnings
```

## Requirements

- Python 3.7+ (stdlib only, no dependencies)
- `git` (optional but recommended — enables `.gitignore`-aware listing and
  fingerprint-based cache invalidation; the script still works without it)

## License

MIT
