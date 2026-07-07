---
name: spotter
description: Use this skill whenever the user gives a short or vague vibe-coding instruction that references existing UI elements, components, pages, or behavior inside an existing codebase (e.g. "ubah button di header", "samain style sama halaman login", "fix the navbar spacing"). This skill enforces a scoping-first workflow — run scripts/scoper.py to get a short list of relevant file paths BEFORE reading any file contents, instead of scanning or reading the whole project. Do not use this for brand-new files/features with no existing reference, or for tasks that already specify exact file paths.
---

# Prompt Context Scoper

## Why this exists

Vibe-coding instructions are short ("ubah button di header") but resolving
them naively costs a lot of tokens: reading the whole file tree, or opening
many files just to find the one that matters. This skill enforces a
disciplined order of operations: **locate candidates first, read second.**

## Workflow

**Step 1 — Run the scoper, do not browse the filesystem manually first.**

```bash
python3 <skill_dir>/scripts/scoper.py --root <project_root> --scope "<user's instruction, verbatim>"
```

Replace `<skill_dir>` with this skill's own directory and `<project_root>`
with the project's root (usually the current working directory or git
worktree root). Pass the user's instruction as close to verbatim as
possible — do not pre-summarize it, since the matcher extracts its own
keywords.

The script returns JSON:

```json
{
  "cache_status": "hit" | "rebuilt",
  "total_files_indexed": 132,
  "candidates": ["src/components/Header.jsx"],
  "related_files": ["src/components/Button.jsx"],
  "token_estimate": {"src/components/Header.jsx": 812, "src/components/Button.jsx": 210},
  "warnings": []
}
```

- `candidates` — the primary files to read and edit.
- `related_files` — files structurally connected to a candidate via the
  import graph (something a candidate imports, or something that imports
  a candidate) that didn't independently match the prompt's keywords.
  Treat these as reference/context only (e.g. a shared theme/Button file
  a matched component imports) — don't edit them unless the task actually
  requires it.
- `token_estimate` — rough token-size estimate per file (candidates +
  related_files). Useful for judging whether a file is worth reading in
  full vs. grep-ing just the relevant part.
- `warnings` — populated when a candidate file is unusually large, or the
  combined estimated size of all candidates is large. Take these
  seriously: prefer reading only the relevant section (e.g. via `grep -n`
  or a targeted `view` range) over reading the whole file when a warning
  is present.

- The file list is already `.gitignore`-aware (via `git ls-files`), so
  `node_modules`, build output, etc. are already excluded — don't add
  your own exclusion logic on top.
- The cache lives in `<project_root>/.scoper_cache/` and is reused across
  calls within the same working session, so repeated prompts in one
  session are cheap after the first call.
- In monorepos (multiple `package.json`/`pyproject.toml`/etc. roots
  detected), candidates outside the package of the top match are ranked
  lower automatically — you don't need to reason about this yourself,
  just trust the ordering `candidates` already comes in.

**Step 2 — Judge the candidates before reading anything.**

- If `candidates` is empty, or clearly doesn't match what the user meant
  (e.g. score was too low), do NOT fall back to a broad manual scan.
  Instead, ask the user one short clarifying question naming a
  folder/component, or ask them to point at the relevant file directly.
- If `candidates` has 1-2 clearly relevant files: proceed to Step 3.
- If `candidates` has several plausible files and it's ambiguous which
  one the user means (e.g. multiple `Header.jsx` in different
  subfolders/packages), ask a single clarifying question before reading
  any of them.

**Step 3 — Read candidates first, `related_files` only if actually needed.**

- Read/open the paths in `candidates`. Do not proactively read sibling
  files, entire directories, or the project tree "just in case."
- `related_files` are surfaced via the import graph as likely *context*
  (e.g. a shared component/theme file a candidate imports). Only open one
  if the edit genuinely requires understanding or touching it — don't
  read all of them by default just because they're listed.
- If you discover mid-edit that you need a file not in either list, it's
  fine to read that one additional file — but don't use this as an
  excuse to widen the scope broadly. Read only what the edit actually
  requires.
- If `warnings` flagged a file as large, prefer a targeted read (grep for
  the relevant function/section, or a bounded `view` range) over reading
  the entire file.
- Proceed with the code change using only this narrowed context.

## Explicit guardrails

- Do NOT run a recursive directory listing (`view` on the whole project
  root, `find .`, `ls -R`, etc.) before calling the scoper. The scoper's
  job is precisely to avoid that.
- Do NOT read more than a small handful of files based on a vague prompt.
  If the scoper's candidate list feels insufficient, re-run it with a
  more specific `--scope` string (e.g. add a folder hint the user
  mentioned) rather than manually exploring.
- Do NOT skip the scoper because "the project is probably small enough."
  Running it is cheap; skipping it is the exact anti-pattern this skill
  exists to prevent.

## When NOT to use this skill

- The user already gave an exact file path — just read that file.
- The task is a brand-new feature/file with no existing reference to
  locate (there's nothing to scope).
- The task is project-wide by nature (e.g. "rename this variable
  everywhere", "add TypeScript to the whole project") — scoping to a
  handful of files would be counterproductive; a full-project approach
  is correct here.

## How matching works (for context, not required reading to use the skill)

The scoper combines four signals when ranking primary candidates:

1. **Filename/path matching** — does the prompt mention the file or
   folder name (including tokenized camelCase/kebab-case, e.g. "header"
   matches `TopHeader.jsx`)?
2. **Symbol matching** — does the prompt mention a function/component/
   class name declared *inside* a file, even if the filename itself
   doesn't match (e.g. `TopHeader` declared inside `Nav.jsx`)?
3. **Git-hot boost** — files with uncommitted changes or touched in the
   last few commits get a small ranking boost, since vibe-coding prompts
   are often continuations of whatever was just being worked on.
4. **Session memory** — a small log of recent prompts and their resulting
   candidates (`.scoper_cache/session_log.json`); a new prompt similar to
   a recent one gets a boost toward those same files (helps "lanjutin
   yang tadi, tambahin border juga" follow-ups).

On top of that:

- **Monorepo awareness** — if the project has multiple detected
  package/workspace roots, candidates outside the package of the
  top-scoring match are ranked lower (not excluded).
- **Import-graph expansion** — direct imports/importers of primary
  candidates are surfaced separately as `related_files`, so a shared
  file (e.g. a Button/theme component) shows up even without keyword
  overlap.
- **Token-budget estimate** — every candidate + related file gets a rough
  size estimate, with `warnings` when a file or the total is large enough
  that reading it whole would be wasteful.

None of these fully replace judgment: if matching still misses a file
due to very unusual naming or project structure, fall back to asking the
user rather than reading broadly.
