#!/usr/bin/env python3
"""
scoper.py — context-scoping helper for vibe-coding agents. Part of the
"spotter" skill: locates candidate files BEFORE the calling agent reads
any file contents, so the agent never has to scan the whole codebase for
a short instruction like "ubah button di header".

Core pipeline:
    1. .gitignore-aware file listing via `git ls-files` (falls back to a
       basic os.walk + ignore-list if the project isn't a git repo).
    2. Cache the file list + symbol index + import graph + package
       boundaries to disk so repeated prompts in the same session don't
       re-walk/re-scan every time.
    3. Cache invalidation: git-aware first (HEAD commit hash + dirty
       status), mtime-based fallback for non-git projects.
    4. Primary candidate scoring combines four signals:
         a. filename/path match   (does the prompt mention the file or
                                    folder name — tokenized camelCase/
                                    kebab-case aware?)
         b. symbol match          (does the prompt mention a function/
                                    component/class name declared INSIDE
                                    a file, even if the filename itself
                                    doesn't match?)
         c. git-hot boost         (was this file touched recently —
                                    staged, unstaged, or in the last few
                                    commits?)
         d. session memory boost  (did a similar recent prompt already
                                    point at this file?)
       Monorepo awareness applies a penalty to candidates that live in a
       different package/workspace than the top match, so cross-package
       false positives rank lower (never hard-excluded).
    5. Import-graph expansion: once primary candidates are decided, their
       direct imports and direct importers (1-hop) are surfaced
       separately as `related_files` — e.g. a shared Button/theme file
       that a matched component imports, even though it shares no
       keywords with the prompt. These are reference/context files, not
       primary edit targets.
    6. Token-budget estimate: a rough size-based token estimate is
       computed for every primary + related file, with warnings emitted
       if any single file or the total is large enough that reading it
       in full would be wasteful.

Usage:
    python3 scoper.py --scope "ubah button di header" [--root .] [--max 5]
    python3 scoper.py --build-index [--root .]     # force rebuild cache
    python3 scoper.py --check [--root .]           # report cache freshness

    Flags to disable individual signals (useful for debugging/comparison):
        --no-symbols          disable symbol-index matching
        --no-git-boost        disable git-hot recency boost
        --no-session-memory   disable session-memory boost & logging
        --no-import-graph     disable related_files expansion
        --no-monorepo         disable cross-package penalty
        --no-token-warnings   disable token-budget estimate/warnings

Output (stdout, JSON):
    {
      "cache_status": "hit" | "rebuilt",
      "total_files_indexed": 132,
      "candidates": ["src/components/Header.jsx", ...],
      "related_files": ["src/components/Button.jsx", ...],
      "token_estimate": {"src/components/Header.jsx": 812, ...},
      "warnings": ["..."]
    }
"""

import argparse
import difflib
import json
import os
import re
import subprocess
import time

CACHE_DIRNAME = ".scoper_cache"
CACHE_FILENAME = "tree_index.json"
SESSION_LOG_FILENAME = "session_log.json"
SESSION_LOG_MAX_ENTRIES = 20
SESSION_SIMILARITY_THRESHOLD = 0.55
SESSION_BOOST = 0.12

# Fallback ignore list, only used when project has no .git (git ls-files
# already respects .gitignore automatically, so this list is intentionally
# small — it's a safety net, not the primary filter).
FALLBACK_IGNORE_DIRS = {
    ".git", "node_modules", "dist", "build", ".next", ".cache",
    "venv", ".venv", "__pycache__", ".scoper_cache",
}

# Extensions worth scanning for symbols/imports. Kept small and cheap on
# purpose — this is regex-based, not a real parser.
CODE_EXTENSIONS = {
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte",
    ".py", ".go", ".rb", ".php", ".java", ".astro",
}

JS_LIKE_EXTENSIONS = {
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte", ".astro",
}

# Cap how much of a file we read for symbol/import extraction. Most
# declarations worth matching live near the top; this keeps indexing fast
# on huge files.
CODE_SCAN_BYTE_LIMIT = 20_000

SYMBOL_PATTERNS = [
    # JS/TS/JSX/TSX/Vue/Svelte/Astro-ish declarations
    re.compile(r"\bexport\s+default\s+function\s+([A-Za-z_$][\w$]*)"),
    re.compile(r"\bexport\s+default\s+class\s+([A-Za-z_$][\w$]*)"),
    re.compile(r"\bexport\s+(?:const|function|class|let|var)\s+([A-Za-z_$][\w$]*)"),
    re.compile(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\("),
    re.compile(r"\bclass\s+([A-Za-z_$][\w$]*)"),
    re.compile(r"\bconst\s+([A-Za-z_$][\w$]*)\s*=\s*\(?.*=>"),
    # Python
    re.compile(r"\bdef\s+([A-Za-z_][\w]*)\s*\("),
    re.compile(r"\bclass\s+([A-Za-z_][\w]*)\s*[:\(]"),
    # Go
    re.compile(r"\bfunc\s+(?:\([^)]*\)\s*)?([A-Za-z_][\w]*)\s*\("),
]

IMPORT_PATTERNS_JS = [
    re.compile(r"import\s+(?:[^'\";]+\sfrom\s+)?['\"]([^'\"]+)['\"]"),
    re.compile(r"require\(\s*['\"]([^'\"]+)['\"]\s*\)"),
    re.compile(r"export\s+(?:\*|\{[^}]*\})\s+from\s+['\"]([^'\"]+)['\"]"),
]
IMPORT_PATTERNS_PY = [
    re.compile(r"^\s*from\s+([\.\w]+)\s+import", re.MULTILINE),
    re.compile(r"^\s*import\s+([\.\w]+)", re.MULTILINE),
]

# Resolution suffixes tried when turning a relative JS import ("./Button")
# into an actual project file path.
JS_RESOLUTION_SUFFIXES = [
    "", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte",
    "/index.js", "/index.jsx", "/index.ts", "/index.tsx",
]

# Marker files used to detect package/workspace boundaries in a monorepo.
PACKAGE_MARKER_FILES = {
    "package.json", "pyproject.toml", "go.mod", "Cargo.toml", "composer.json",
}

# Token-budget thresholds (rough heuristic: ~4 chars per token).
CHARS_PER_TOKEN_ESTIMATE = 4
TOKEN_WARN_FILE_THRESHOLD = 2000
TOKEN_WARN_TOTAL_THRESHOLD = 6000


def run_git(args, cwd):
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        return result.stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return None


def is_git_repo(root):
    return run_git(["rev-parse", "--is-inside-work-tree"], root) is not None


def git_fingerprint(root):
    """
    Cheap fingerprint of repo state: HEAD commit hash + dirty file count.
    Good enough to detect 'something changed' without hashing file contents.
    """
    head = run_git(["rev-parse", "HEAD"], root)
    status = run_git(["status", "--porcelain"], root)
    if head is None:
        return None
    dirty_count = len(status.strip().splitlines()) if status else 0
    return f"{head.strip()}:{dirty_count}"


def _is_scoper_internal(rel_path):
    """
    The scoper's own cache/log directory should never appear as a
    candidate, regardless of whether the project's .gitignore happens to
    exclude it (a fresh project may not have that entry yet).
    """
    normalized = rel_path.replace(os.sep, "/")
    return normalized == CACHE_DIRNAME or normalized.startswith(CACHE_DIRNAME + "/")


def list_files_git(root):
    """
    Uses `git ls-files` which is inherently .gitignore-aware.
    Includes tracked files + untracked-but-not-ignored files, so new
    files you just created still show up before the first commit.
    """
    tracked = run_git(["ls-files"], root) or ""
    untracked = run_git(
        ["ls-files", "--others", "--exclude-standard"], root
    ) or ""
    files = set(tracked.splitlines()) | set(untracked.splitlines())
    return sorted(f for f in files if f and not _is_scoper_internal(f))


def list_files_fallback(root):
    """
    Plain os.walk fallback for non-git projects. No .gitignore to read,
    so we rely on FALLBACK_IGNORE_DIRS as a basic noise filter.
    """
    results = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in FALLBACK_IGNORE_DIRS]
        for fname in filenames:
            full = os.path.join(dirpath, fname)
            rel = os.path.relpath(full, root)
            if not _is_scoper_internal(rel):
                results.append(rel)
    return sorted(results)


def git_hot_files(root):
    """
    Returns {path: weight} for recently-touched files:
      - staged/unstaged changes right now  -> weight 1.0
      - touched in the last 5 commits      -> weight decaying 0.6 -> 0.2

    This is a *ranking nudge*, not a filter — it only ever applies to
    files that already cleared the base relevance threshold elsewhere.
    """
    if not is_git_repo(root):
        return {}

    weights = {}

    dirty = run_git(["status", "--porcelain"], root) or ""
    for line in dirty.splitlines():
        if not line:
            continue
        # porcelain format: "XY path" (rename shows "old -> new")
        path = line[3:].split(" -> ")[-1].strip()
        if path:
            weights[path] = 1.0

    log_output = run_git(
        ["log", "-5", "--name-only", "--pretty=format:__COMMIT__"], root
    ) or ""
    commits = [c for c in log_output.split("__COMMIT__") if c.strip()]
    for i, commit_files in enumerate(commits):
        decay = max(0.2, 0.6 - i * 0.1)
        for path in commit_files.strip().splitlines():
            path = path.strip()
            if path and path not in weights:
                weights[path] = decay

    return weights


def cache_paths(root):
    cache_dir = os.path.join(root, CACHE_DIRNAME)
    return (
        cache_dir,
        os.path.join(cache_dir, CACHE_FILENAME),
        os.path.join(cache_dir, SESSION_LOG_FILENAME),
    )


def load_cache(root):
    _, cache_file, _ = cache_paths(root)
    if not os.path.exists(cache_file):
        return None
    try:
        with open(cache_file, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def save_cache(root, data):
    cache_dir, cache_file, _ = cache_paths(root)
    os.makedirs(cache_dir, exist_ok=True)
    with open(cache_file, "w") as f:
        json.dump(data, f, indent=2)


def is_cache_fresh(root, cached):
    if cached is None:
        return False

    if is_git_repo(root):
        current_fp = git_fingerprint(root)
        # If git fingerprint is available, it's the source of truth.
        if current_fp is not None:
            return cached.get("git_fingerprint") == current_fp

    # Fallback: mtime-based. Consider stale after 5 minutes.
    cached_at = cached.get("built_at", 0)
    max_age_seconds = 5 * 60
    return (time.time() - cached_at) < max_age_seconds


# --- Symbol + import extraction (single read pass per file) ---------------

def analyze_file(root, rel_path):
    """
    Single-read analysis of a file: extracts both declared symbols
    (functions/classes/components) and raw import/require targets.
    Combined into one function so we don't read the same file twice.
    """
    ext = os.path.splitext(rel_path)[1].lower()
    if ext not in CODE_EXTENSIONS:
        return [], [], None

    full_path = os.path.join(root, rel_path)
    try:
        with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read(CODE_SCAN_BYTE_LIMIT)
    except OSError:
        return [], [], None

    symbols = set()
    for pattern in SYMBOL_PATTERNS:
        for match in pattern.finditer(content):
            symbols.add(match.group(1))

    raw_imports = []
    import_kind = None
    if ext in JS_LIKE_EXTENSIONS:
        import_kind = "js"
        for pattern in IMPORT_PATTERNS_JS:
            raw_imports.extend(m.group(1) for m in pattern.finditer(content))
    elif ext == ".py":
        import_kind = "py"
        for pattern in IMPORT_PATTERNS_PY:
            raw_imports.extend(m.group(1) for m in pattern.finditer(content))

    return sorted(symbols), raw_imports, import_kind


def resolve_js_import(importer_rel_path, raw_import, files_set):
    """Resolves a relative JS/TS import ('./Button') to a real project
    file path. Skips bare package imports (e.g. 'react') since those
    aren't part of the project and can't be scoped to."""
    if not raw_import.startswith("."):
        return None

    importer_dir = os.path.dirname(importer_rel_path)
    base = os.path.normpath(os.path.join(importer_dir, raw_import))
    base = base.replace(os.sep, "/")

    for suffix in JS_RESOLUTION_SUFFIXES:
        candidate = (base + suffix) if suffix else base
        if candidate in files_set:
            return candidate
    return None


def resolve_python_import(importer_rel_path, raw_import, files_set):
    """Best-effort resolution of a Python import to a project file path.
    Handles simple relative ('.module') and absolute ('package.module')
    forms; does not fully replicate Python's import machinery."""
    importer_dir = os.path.dirname(importer_rel_path)

    if raw_import.startswith("."):
        parts = [p for p in raw_import.lstrip(".").split(".") if p]
        base = os.path.normpath(os.path.join(importer_dir, *parts)) if parts else importer_dir
    else:
        parts = raw_import.split(".")
        base = os.path.normpath(os.path.join(*parts))

    base = base.replace(os.sep, "/")
    for suffix in [".py", "/__init__.py"]:
        candidate = base + suffix
        if candidate in files_set:
            return candidate
    return None


def build_import_graph(root, files):
    """
    Returns (imports_forward, importers_reverse):
      imports_forward[file]  = list of files it imports (resolved, exist in project)
      importers_reverse[file] = list of files that import it
    Only includes edges we could actually resolve to a real project file —
    external package imports (react, lodash, etc.) are not part of the graph.
    """
    files_set = set(files)
    imports_forward = {}

    for rel_path in files:
        _symbols, raw_imports, kind = analyze_file(root, rel_path)
        if not raw_imports:
            continue
        resolved = set()
        for raw in raw_imports:
            target = None
            if kind == "js":
                target = resolve_js_import(rel_path, raw, files_set)
            elif kind == "py":
                target = resolve_python_import(rel_path, raw, files_set)
            if target and target != rel_path:
                resolved.add(target)
        if resolved:
            imports_forward[rel_path] = sorted(resolved)

    importers_reverse = {}
    for src, targets in imports_forward.items():
        for tgt in targets:
            importers_reverse.setdefault(tgt, []).append(src)

    return imports_forward, importers_reverse


def build_symbol_index(root, files):
    symbols = {}
    for rel_path in files:
        syms, _raw_imports, _kind = analyze_file(root, rel_path)
        if syms:
            symbols[rel_path] = syms
    return symbols


# --- Monorepo / package boundary detection ---------------------------------

def detect_package_roots(files):
    """
    Returns a list of directories (relative to project root) that contain
    a package marker file (package.json, pyproject.toml, etc.), sorted
    longest-path-first so prefix matching finds the most specific root.
    Always includes "." as the fallback root.
    """
    roots = {"."}
    for f in files:
        base = os.path.basename(f)
        if base in PACKAGE_MARKER_FILES:
            d = os.path.dirname(f)
            roots.add(d if d else ".")
    return sorted(roots, key=lambda r: -len(r))


def package_for_file(rel_path, sorted_roots):
    dirname = os.path.dirname(rel_path).replace(os.sep, "/")
    for root in sorted_roots:
        if root == ".":
            continue
        if dirname == root or dirname.startswith(root + "/"):
            return root
    return "."


# --- Index build/load -------------------------------------------------------

def build_index(root):
    if is_git_repo(root):
        files = list_files_git(root)
        fingerprint = git_fingerprint(root)
    else:
        files = list_files_fallback(root)
        fingerprint = None

    symbols = build_symbol_index(root, files)
    imports_forward, importers_reverse = build_import_graph(root, files)
    package_roots = detect_package_roots(files)

    data = {
        "built_at": time.time(),
        "git_fingerprint": fingerprint,
        "files": files,
        "symbols": symbols,
        "imports": imports_forward,
        "importers": importers_reverse,
        "package_roots": package_roots,
    }
    save_cache(root, data)
    return data


def get_index(root, force_rebuild=False):
    cached = None if force_rebuild else load_cache(root)
    if cached is not None and is_cache_fresh(root, cached):
        return cached, "hit"
    return build_index(root), "rebuilt"


# --- Session memory -------------------------------------------------------

def load_session_log(root):
    _, _, log_file = cache_paths(root)
    if not os.path.exists(log_file):
        return []
    try:
        with open(log_file, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def save_session_log(root, log):
    cache_dir, _, log_file = cache_paths(root)
    os.makedirs(cache_dir, exist_ok=True)
    with open(log_file, "w") as f:
        json.dump(log[-SESSION_LOG_MAX_ENTRIES:], f, indent=2)


def session_memory_boost(prompt, session_log):
    """
    Returns {path: boost} for files that appeared in prior prompts similar
    to the current one. Boost is additive and capped small on purpose —
    this nudges ranking for "lanjutin yang tadi" style follow-ups, it
    never single-handedly qualifies a file as a candidate.
    """
    boosts = {}
    prompt_lower = prompt.lower()
    for entry in session_log:
        similarity = difflib.SequenceMatcher(
            None, prompt_lower, entry.get("prompt", "").lower()
        ).ratio()
        if similarity >= SESSION_SIMILARITY_THRESHOLD:
            for path in entry.get("candidates", []):
                boosts[path] = max(boosts.get(path, 0.0), SESSION_BOOST * similarity)
    return boosts


# --- Matching ---------------------------------------------------------------

STOPWORDS = {
    "ubah", "ganti", "samain", "sama", "seperti", "kayak", "dengan",
    "di", "ke", "yang", "dan", "atau", "the", "a", "an", "for", "with",
    "like", "same", "as", "on", "in", "make", "change", "update",
}


def extract_keywords(prompt):
    words = re.findall(r"[a-zA-Z0-9]+", prompt.lower())
    return [w for w in words if w not in STOPWORDS and len(w) > 2]


def split_identifier(text):
    """
    Splits a filename, path, or symbol name into lowercase word tokens,
    breaking on camelCase/PascalCase boundaries, underscores, dashes, and
    path separators. e.g. "TopHeader" -> ["top", "header"],
    "src/components/nav-bar.jsx" -> ["src", "components", "nav", "bar", "jsx"]
    """
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    text = re.sub(r"[_\-./]", " ", text)
    return [t for t in text.lower().split() if t]


def text_match_score(text_lower, keywords):
    """
    Matches a piece of text (filename stem/path, or a symbol name) against
    keywords using, in order of strength:
      1. exact/substring match against a WORD TOKEN (e.g. keyword "header"
         against tokens ["top", "header"] from "TopHeader") -> strong
      2. substring match against the raw, unsplit text -> medium
      3. difflib fuzzy ratio, but only above a high cutoff, to catch
         near-miss typos/variants without matching on coincidental
         character overlap between otherwise-unrelated short strings.
    """
    tokens = split_identifier(text_lower)
    best = 0.0

    for kw in keywords:
        if kw in tokens:
            best = max(best, 0.95)
            continue
        if any(kw in tok or tok in kw for tok in tokens if len(tok) > 2):
            best = max(best, 0.8)
            continue
        if kw in text_lower:
            best = max(best, 0.7)
            continue
        ratio = difflib.SequenceMatcher(None, kw, text_lower).ratio()
        if ratio >= 0.75:
            best = max(best, ratio)

    return best


def score_file(path, keywords, symbols_for_file, hot_weight, session_boost):
    path_lower = path.lower()
    stem = os.path.splitext(os.path.basename(path_lower))[0]

    filename_score = max(
        text_match_score(stem, keywords),
        text_match_score(path_lower, keywords),
    )

    symbol_score = 0.0
    for symbol in symbols_for_file:
        symbol_score = max(symbol_score, text_match_score(symbol.lower(), keywords))

    base_score = max(filename_score, symbol_score)

    # Git-hot boost and session boost only ever apply on top of a base
    # score that already shows some independent relevance — this prevents
    # an unrelated-but-recently-edited file from being surfaced by
    # recency alone. Deliberately NOT clamped to 1.0: these scores are
    # only used internally for ranking (never exposed in the output), so
    # letting boosts push a score above 1.0 preserves useful tie-breaks
    # between files with an identical base filename/symbol match (e.g.
    # two same-named components in different packages).
    if base_score > 0:
        base_score += hot_weight * 0.15
        base_score += session_boost

    return base_score


def apply_monorepo_penalty(scored, index):
    """
    If the project has more than one detected package root, candidates
    living in a different package than the top-scoring candidate get a
    ranking penalty (not exclusion) — reduces cross-package false
    positives (e.g. a same-named component in a different app/package)
    without discarding genuinely shared/cross-package files outright.
    Only activates when the top score is confident enough to trust as an
    anchor.
    """
    package_roots = index.get("package_roots", ["."])
    if len(package_roots) <= 1 or not scored:
        return scored

    top_file, top_score = max(scored, key=lambda pair: pair[1])
    if top_score < 0.6:
        return scored

    top_package = package_for_file(top_file, package_roots)
    adjusted = []
    for f, score in scored:
        if score > 0 and package_for_file(f, package_roots) != top_package:
            score *= 0.6
        adjusted.append((f, score))
    return adjusted


def expand_related_files(primary_files, index, max_related=3, per_file_neighbors=4):
    """
    1-hop import-graph expansion: direct imports and direct importers of
    the primary candidates, surfaced as reference/context files rather
    than primary edit targets (e.g. a shared Button/theme file that a
    matched component imports).
    """
    imports_map = index.get("imports", {})
    importers_map = index.get("importers", {})

    seen = set(primary_files)
    related = []
    for f in primary_files:
        neighbors = (
            imports_map.get(f, [])[:per_file_neighbors]
            + importers_map.get(f, [])[:per_file_neighbors]
        )
        for n in neighbors:
            if n in seen:
                continue
            seen.add(n)
            related.append(n)
            if len(related) >= max_related:
                return related
    return related


def estimate_tokens(root, rel_path):
    full_path = os.path.join(root, rel_path)
    try:
        size_bytes = os.path.getsize(full_path)
    except OSError:
        return 0
    return size_bytes // CHARS_PER_TOKEN_ESTIMATE


def build_token_report(root, files):
    token_estimate = {f: estimate_tokens(root, f) for f in files}
    warnings = []
    for f, tokens in token_estimate.items():
        if tokens >= TOKEN_WARN_FILE_THRESHOLD:
            warnings.append(
                f"{f} is ~{tokens} tokens — consider reading only the "
                f"relevant section instead of the whole file."
            )
    total = sum(token_estimate.values())
    if total >= TOKEN_WARN_TOTAL_THRESHOLD:
        warnings.append(
            f"Total estimated context across candidates is ~{total} "
            f"tokens — consider narrowing the prompt or working in "
            f"smaller steps."
        )
    return token_estimate, warnings


def match_candidates(
    index,
    prompt,
    root,
    max_results=5,
    min_score=0.45,
    use_symbols=True,
    use_git_boost=True,
    use_session_memory=True,
    use_import_graph=True,
    use_monorepo=True,
    use_token_warnings=True,
):
    keywords = extract_keywords(prompt)
    if not keywords:
        return {
            "candidates": [], "related_files": [],
            "token_estimate": {}, "warnings": [],
        }

    files = index["files"]
    symbols_map = index.get("symbols", {}) if use_symbols else {}
    hot_files = git_hot_files(root) if use_git_boost else {}

    session_log = load_session_log(root) if use_session_memory else []
    session_boosts = (
        session_memory_boost(prompt, session_log) if use_session_memory else {}
    )

    scored = []
    for f in files:
        score = score_file(
            f,
            keywords,
            symbols_map.get(f, []),
            hot_files.get(f, 0.0),
            session_boosts.get(f, 0.0),
        )
        scored.append((f, score))

    if use_monorepo:
        scored = apply_monorepo_penalty(scored, index)

    scored = [pair for pair in scored if pair[1] >= min_score]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    primary = [f for f, _ in scored[:max_results]]

    related = []
    if use_import_graph and primary:
        related = expand_related_files(primary, index)

    token_estimate, warnings = ({}, [])
    if use_token_warnings:
        token_estimate, warnings = build_token_report(root, primary + related)

    if use_session_memory:
        session_log.append({
            "prompt": prompt,
            "candidates": primary,
            "timestamp": time.time(),
        })
        save_session_log(root, session_log)

    return {
        "candidates": primary,
        "related_files": related,
        "token_estimate": token_estimate,
        "warnings": warnings,
    }


# --- CLI ---------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="Project root directory")
    parser.add_argument("--scope", help="Prompt/instruction to scope files for")
    parser.add_argument("--build-index", action="store_true",
                         help="Force rebuild the cache")
    parser.add_argument("--check", action="store_true",
                         help="Just report cache freshness, no scoping")
    parser.add_argument("--max", type=int, default=5,
                         help="Max candidate files to return (default 5)")
    parser.add_argument("--no-symbols", action="store_true",
                         help="Disable symbol-index matching")
    parser.add_argument("--no-git-boost", action="store_true",
                         help="Disable git-hot recency boost")
    parser.add_argument("--no-session-memory", action="store_true",
                         help="Disable session-memory boost & logging")
    parser.add_argument("--no-import-graph", action="store_true",
                         help="Disable related_files expansion via import graph")
    parser.add_argument("--no-monorepo", action="store_true",
                         help="Disable cross-package ranking penalty")
    parser.add_argument("--no-token-warnings", action="store_true",
                         help="Disable token-budget estimate/warnings")
    args = parser.parse_args()

    root = os.path.abspath(args.root)

    if args.check:
        cached = load_cache(root)
        fresh = is_cache_fresh(root, cached) if cached else False
        print(json.dumps({
            "cache_exists": cached is not None,
            "cache_fresh": fresh,
            "total_files_indexed": len(cached["files"]) if cached else 0,
            "total_files_with_symbols": len(cached.get("symbols", {})) if cached else 0,
            "total_files_with_imports": len(cached.get("imports", {})) if cached else 0,
            "package_roots": cached.get("package_roots", ["."]) if cached else ["."],
        }, indent=2))
        return

    index, cache_status = get_index(root, force_rebuild=args.build_index)

    result = {
        "cache_status": cache_status,
        "total_files_indexed": len(index["files"]),
        "candidates": [],
        "related_files": [],
        "token_estimate": {},
        "warnings": [],
    }

    if args.scope:
        result.update(match_candidates(
            index,
            args.scope,
            root,
            max_results=args.max,
            use_symbols=not args.no_symbols,
            use_git_boost=not args.no_git_boost,
            use_session_memory=not args.no_session_memory,
            use_import_graph=not args.no_import_graph,
            use_monorepo=not args.no_monorepo,
            use_token_warnings=not args.no_token_warnings,
        ))

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
