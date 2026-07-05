#!/usr/bin/env python3
"""
scoper.py — minimal context-scoping helper for vibe-coding agents.

Purpose:
    Given a short instruction ("ubah button di header"), return a small
    list of candidate file paths from the project, WITHOUT the calling
    agent having to read/scan the whole codebase.

Design goals (v1 - minimal):
    1. .gitignore-aware file listing via `git ls-files` (falls back to a
       basic os.walk + ignore-list if the project isn't a git repo).
    2. Cache the file list to disk so repeated prompts in the same
       session don't re-walk the filesystem every time.
    3. Cache invalidation: git-aware first (HEAD commit hash + dirty
       status), mtime-based fallback for non-git projects.
    4. Simple fuzzy keyword matching (stdlib difflib) to shortlist
       candidate files from the cached file list based on the prompt.

Explicitly NOT in v1 (future iterations):
    - import/dependency graph matching
    - symbol/ctags indexing
    - session/prompt history reuse
    - token-budget estimation warnings

Usage:
    python3 scripts/scoper.py --scope "ubah button di header" [--root .] [--max 5]
    python3 scripts/scoper.py --build-index [--root .]     # force rebuild cache
    python3 scripts/scoper.py --check [--root .]           # just report cache freshness

Output (stdout, JSON):
    {
      "cache_status": "hit" | "rebuilt",
      "candidates": ["src/components/Header.jsx", ...],
      "total_files_indexed": 132
    }
"""

import argparse
import difflib
import json
import os
import re
import subprocess
import sys
import time

CACHE_DIRNAME = ".scoper_cache"
CACHE_FILENAME = "tree_index.json"

# Fallback ignore list, only used when project has no .git (git ls-files
# already respects .gitignore automatically, so this list is intentionally
# small — it's a safety net, not the primary filter).
FALLBACK_IGNORE_DIRS = {
    ".git", "node_modules", "dist", "build", ".next", ".cache",
    "venv", ".venv", "__pycache__", ".scoper_cache",
}


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
    return sorted(f for f in files if f)


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
            results.append(rel)
    return sorted(results)


def cache_paths(root):
    cache_dir = os.path.join(root, CACHE_DIRNAME)
    return cache_dir, os.path.join(cache_dir, CACHE_FILENAME)


def load_cache(root):
    _, cache_file = cache_paths(root)
    if not os.path.exists(cache_file):
        return None
    try:
        with open(cache_file, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def save_cache(root, data):
    cache_dir, cache_file = cache_paths(root)
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

    # Fallback: mtime-based. Consider stale after 5 minutes OR if root
    # dir's own mtime changed (crude signal that entries were added/removed).
    cached_at = cached.get("built_at", 0)
    max_age_seconds = 5 * 60
    return (time.time() - cached_at) < max_age_seconds


def build_index(root):
    if is_git_repo(root):
        files = list_files_git(root)
        fingerprint = git_fingerprint(root)
    else:
        files = list_files_fallback(root)
        fingerprint = None

    data = {
        "built_at": time.time(),
        "git_fingerprint": fingerprint,
        "files": files,
    }
    save_cache(root, data)
    return data


def get_index(root, force_rebuild=False):
    cached = None if force_rebuild else load_cache(root)
    if cached is not None and is_cache_fresh(root, cached):
        return cached, "hit"
    return build_index(root), "rebuilt"


# --- Matching -----------------------------------------------------------

STOPWORDS = {
    "ubah", "ganti", "samain", "sama", "seperti", "kayak", "dengan",
    "di", "ke", "yang", "dan", "atau", "the", "a", "an", "for", "with",
    "like", "same", "as", "on", "in", "make", "change", "update",
}


def extract_keywords(prompt):
    words = re.findall(r"[a-zA-Z0-9]+", prompt.lower())
    return [w for w in words if w not in STOPWORDS and len(w) > 2]


def score_file(path, keywords):
    """
    Score a file path against keywords using:
      - substring match on path components (strong signal)
      - difflib fuzzy ratio on filename (catches typos/variants)
    Returns best score in [0, 1].
    """
    path_lower = path.lower()
    filename = os.path.basename(path_lower)
    stem = os.path.splitext(filename)[0]

    best = 0.0
    for kw in keywords:
        if kw in path_lower:
            best = max(best, 0.9)
            continue
        ratio = difflib.SequenceMatcher(None, kw, stem).ratio()
        best = max(best, ratio)
    return best


def match_candidates(files, prompt, max_results=5, min_score=0.45):
    keywords = extract_keywords(prompt)
    if not keywords:
        return []

    scored = [(f, score_file(f, keywords)) for f in files]
    scored = [pair for pair in scored if pair[1] >= min_score]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return [f for f, _ in scored[:max_results]]


# --- CLI ------------------------------------------------------------------

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
    args = parser.parse_args()

    root = os.path.abspath(args.root)

    if args.check:
        cached = load_cache(root)
        fresh = is_cache_fresh(root, cached) if cached else False
        print(json.dumps({
            "cache_exists": cached is not None,
            "cache_fresh": fresh,
            "total_files_indexed": len(cached["files"]) if cached else 0,
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
            index["files"], args.scope, max_results=args.max
        )

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
