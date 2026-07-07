#!/usr/bin/env python3
"""
scoper.py — context-scoping helper for vibe-coding agents. Part of the
"spotter" skill: locates candidate files BEFORE the calling agent reads
any file contents, so the agent never has to scan the whole codebase for
a short instruction like "ubah button di header".

Core pipeline:
    1. .gitignore-aware file listing via `git ls-files` (falls back to a
       basic os.walk + ignore-list if the project isn't a git repo).
    2. Cache the file list + lightweight symbol index to disk so repeated
       prompts in the same session don't re-walk/re-scan every time.
    3. Cache invalidation: git-aware first (HEAD commit hash + dirty
       status), mtime-based fallback for non-git projects.
    4. Candidate scoring combines three signals:
         a. filename/path fuzzy match  (does the prompt mention the
                                         file/folder name?)
         b. symbol match               (does the prompt mention a
                                         function/component/class name
                                         declared INSIDE the file?)
         c. git-hot boost              (was this file touched recently —
                                         staged, unstaged, or in the last
                                         few commits?)
    5. Session memory: logs prompt -> candidates per session. If the new
       prompt is similar to a recent one, files from that prior result
       get a small relevance boost (helps multi-turn "lanjutin yang tadi"
       vibe-coding sessions). This only nudges ranking — it never adds a
       file that has zero independent relevance to the current prompt.

Usage:
    python3 scoper.py --scope "ubah button di header" [--root .] [--max 5]
    python3 scoper.py --build-index [--root .]     # force rebuild cache
    python3 scoper.py --check [--root .]           # report cache freshness

    Flags to disable individual signals (useful for debugging/comparison):
        --no-symbols        disable symbol-index matching
        --no-git-boost      disable git-hot recency boost
        --no-session-memory disable session-memory boost & logging

Output (stdout, JSON):
    {
      "cache_status": "hit" | "rebuilt",
      "total_files_indexed": 132,
      "candidates": ["src/components/Header.jsx", ...]
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

# Extensions worth scanning for symbols. Kept small and cheap on purpose —
# this is regex-based, not a real parser.
SYMBOL_EXTENSIONS = {
    ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte",
    ".py", ".go", ".rb", ".php", ".java", ".astro",
}

# Cap how much of a file we read for symbol extraction. Most declarations
# worth matching live near the top; this keeps indexing fast on huge files.
SYMBOL_SCAN_BYTE_LIMIT = 20_000

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
    for line in dirty.strip().splitlines():
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


def extract_symbols(root, rel_path):
    """
    Regex-based, best-effort extraction of top-level-ish declaration names
    from a file. Not a real parser — just enough to catch cases where the
    prompt mentions a component/function name that doesn't match the
    filename (e.g. `TopHeader` declared inside `Nav.jsx`).
    """
    ext = os.path.splitext(rel_path)[1].lower()
    if ext not in SYMBOL_EXTENSIONS:
        return []

    full_path = os.path.join(root, rel_path)
    try:
        with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read(SYMBOL_SCAN_BYTE_LIMIT)
    except OSError:
        return []

    symbols = set()
    for pattern in SYMBOL_PATTERNS:
        for match in pattern.finditer(content):
            symbols.add(match.group(1))
    return sorted(symbols)


def build_index(root):
    if is_git_repo(root):
        files = list_files_git(root)
        fingerprint = git_fingerprint(root)
    else:
        files = list_files_fallback(root)
        fingerprint = None

    symbols = {}
    for rel_path in files:
        syms = extract_symbols(root, rel_path)
        if syms:
            symbols[rel_path] = syms

    data = {
        "built_at": time.time(),
        "git_fingerprint": fingerprint,
        "files": files,
        "symbols": symbols,
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
                # Scale by similarity so a near-identical follow-up counts
                # more than a loosely-related one.
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
         character overlap between otherwise-unrelated short strings
         (e.g. "topheader" vs "footer" should NOT match).
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

    # Match against the filename stem AND the full path (keywords
    # sometimes refer to a folder name, e.g. "header" as a directory
    # containing index.jsx).
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
    # recency alone.
    if base_score > 0:
        base_score += hot_weight * 0.15
        base_score += session_boost

    return min(base_score, 1.0)


def match_candidates(
    index,
    prompt,
    root,
    max_results=5,
    min_score=0.45,
    use_symbols=True,
    use_git_boost=True,
    use_session_memory=True,
):
    keywords = extract_keywords(prompt)
    if not keywords:
        return []

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

    scored = [pair for pair in scored if pair[1] >= min_score]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    candidates = [f for f, _ in scored[:max_results]]

    if use_session_memory:
        session_log.append({
            "prompt": prompt,
            "candidates": candidates,
            "timestamp": time.time(),
        })
        save_session_log(root, session_log)

    return candidates


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
        }, indent=2))
        return

    index, cache_status = get_index(root, force_rebuild=args.build_index)

    result = {
        "cache_status": cache_status,
        "total_files_indexed": len(index["files"]),
        "candidates": [],
    }

    if args.scope:
        result["candidates"] = match_candidates(
            index,
            args.scope,
            root,
            max_results=args.max,
            use_symbols=not args.no_symbols,
            use_git_boost=not args.no_git_boost,
            use_session_memory=not args.no_session_memory,
        )

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
