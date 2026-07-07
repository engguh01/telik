# spotter

An [Agent Skill](https://opencode.ai/docs/skills/) (SKILL.md format) that enforces a **scope-before-read** workflow for vibe-coding agents.

> In sniping, the shooter never works alone. Next to them is a spotter, the one with the scope calling out coordinates so the shooter never wastes a shot searching for the target. This skill plays that role for your coding agent: it finds the coordinates (file paths) before the agent (the shooter) reads or touches anything.

## The problem

Vibe-coding instructions are short: `"fix the button in the header"`, `"match the login page style"`. A naive agent resolves them by reading the entire file tree or opening many files to find the one that matters. That burns input tokens on a trivial "where is this file" lookup.

## What this skill does

It forces the agent to **locate candidate files first, read file contents second**. No more scanning the whole project on every prompt.

```
User prompt
   │
   ▼
scripts/scoper.py --scope "<prompt>"
   │  (gitignore-aware file list, cached, scored by filename/symbol/
   │   git-recency/session-history, expanded via import graph)
   ▼
candidates (primary)  +  related_files (import-graph context)
   │
   ▼
Agent reads candidates (and related_files only if needed),
then executes the edit
```

## Features

- **`.gitignore`-aware file listing** via `git ls-files`. `node_modules`, build output, and other ignored paths excluded automatically. Falls back to `os.walk` with a hardcoded ignore list for non-git projects.
- **Caching**. The file list, symbol index, and import graph live in `.scoper_cache/` per project. Repeated prompts in the same session are cheap after the first call.
- **Git-aware cache invalidation**. Uses the HEAD commit hash plus dirty file count as a fingerprint. Falls back to mtime for non-git projects.
- **Filename/path matching**. Extracts keywords from the prompt and scores candidates by tokenized (camelCase/kebab-case-aware) substring matching and fuzzy similarity (stdlib `difflib`, no extra dependencies).
- **Symbol matching**. Regex-based extraction of function, class, and component names declared inside each file. A prompt mentioning `TopHeader` still finds it even if declared inside `Nav.jsx`.
- **Git-hot recency boost**. Files with uncommitted changes or touched in the last 5 commits get a small ranking boost. Vibe-coding prompts continue whatever you were working on.
- **Session memory**. Logs recent prompts and their candidates (`.scoper_cache/session_log.json`). A new prompt similar to a recent one gets a small boost toward those same files. Helps multi-turn sessions like "continue from before, add a border too".
- **Import/dependency graph matching**. Resolves relative `import` and `require` statements (JS, TS, Vue, Svelte, Astro, Python) to real project files. Surfaces direct imports and importers of primary candidates as `related_files`. Catches shared `Button.jsx` or theme files even with zero keyword overlap.
- **Monorepo/package-boundary awareness**. Detects package roots via marker files (`package.json`, `pyproject.toml`, `go.mod`, `Cargo.toml`, `composer.json`). Ranks candidates outside the top match's package lower. Never hard-excludes.
- **Token-budget warnings**. A rough size-based token estimate (`token_estimate`) for every candidate and related file. Emits `warnings` when a single file or the total would be wasteful to read in full.

All features except the core file listing and cache can be toggled off via CLI flags for debugging or comparison. See Usage below.

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

Same SKILL.md format is compatible. Place the folder under `.claude/skills/spotter/` (project) or `~/.claude/skills/spotter/` (global).

## Usage

The skill triggers automatically when your instruction references existing UI elements, components, or pages in vague terms. You can invoke it manually:

```
@codebase Use the spotter skill to find files relevant to "fix the navbar spacing"
```

Run the underlying script directly, outside of an agent:

```bash
python3 scripts/scoper.py --root . --scope "fix the header button and match the login theme"
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
- `git` (optional but recommended. Enables `.gitignore`-aware listing and fingerprint-based cache invalidation. The script works without it.)

## License

MIT
