#!/usr/bin/env python3
"""
scoper.py — context-scoping helper for vibe-coding agents. Part of the
"spotter" skill: locates candidate files BEFORE the calling agent reads
any file contents, so the agent never has to scan the whole codebase for
a short instruction like "fix the header button".

Core pipeline:
    1. .gitignore-aware file listing via `git ls-files` (falls back to
       os.walk + .gitignore parsing if the project isn't a git repo).
    2. Cache the file list + symbol index + import graph + package
       boundaries to disk so repeated prompts in the same session don't
       re-walk/re-scan every time.
    3. Cache invalidation: git-aware first (HEAD commit hash + dirty
       status), mtime-based fallback for non-git projects.
    4. Primary candidate scoring combines four signals:
         a. filename/path match   (tokenized camelCase/kebab-case aware)
         b. symbol match          (regex-extracted function/class/component
                                   names inside JS, TS, Python, Go, Rust,
                                   Kotlin, C#, Swift, Dart, and more)
         c. git-hot boost         (staged, unstaged, or last 5 commits)
         d. session memory boost  (similar recent prompts boost previous
                                   candidates)
       Monorepo awareness applies a penalty to candidates that live in a
       different package/workspace than the top match.
    5. Import-graph expansion: resolves relative imports (JS, TS, Python,
       Go, Rust, Ruby, PHP, Java) and surfaces direct imports/importers
       as `related_files`.
    6. Token-budget estimate with warnings for large files.
    7. Config file support via ~/.scoperrc and project .scoperrc (JSON).
    8. Binary file detection — binary files are indexed but skipped for
       symbol/import extraction.

Usage:
    python3 scoper.py --scope "fix the header button" [--root .] [--max 5]
    python3 scoper.py --build-index [--root .]
    python3 scoper.py --check [--root .]

    Optional flags:
        --no-symbols           disable symbol-index matching
        --no-git-boost         disable git-hot recency boost
        --no-session-memory    disable session-memory boost & logging
        --no-import-graph      disable related_files expansion
        --no-monorepo          disable cross-package penalty
        --no-token-warnings    disable token-budget estimate/warnings
        --scope-dir PATH       restrict search to a subdirectory

Output (stdout, JSON):
    {
      "cache_status": "hit" | "rebuilt",
      "total_files_indexed": 132,
      "candidates": ["src/components/Header.jsx", ...],
      "related_files": ["src/components/Button.jsx", ...],
      "token_estimate": {"src/components/Header.jsx": 812, ...},
      "warnings": ["..."],
      "scope_dir": null
    }
"""

import argparse
import difflib
import json
import os
import re
import subprocess
import time
from typing import Any, Dict, List, Optional, Set, Tuple

# --- Constants ---------------------------------------------------------------

CACHE_DIRNAME = ".scoper_cache"
CACHE_FILENAME = "tree_index.json"
SESSION_LOG_FILENAME = "session_log.json"
SESSION_LOG_MAX_ENTRIES = 20
SESSION_SIMILARITY_THRESHOLD = 0.55
SESSION_BOOST = 0.12

FALLBACK_IGNORE_DIRS: Set[str] = {
    ".git", "node_modules", "dist", "build", ".next", ".cache",
    "venv", ".venv", "__pycache__", ".scoper_cache",
}

CODE_EXTENSIONS: Set[str] = {
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".vue", ".svelte", ".astro",
    ".py", ".go", ".rb", ".php", ".java",
    ".rs", ".kt", ".cs", ".swift", ".dart",
}

JS_LIKE_EXTENSIONS: Set[str] = {
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".vue", ".svelte", ".astro",
}

# Cap how much of a file we read for symbol/import extraction.
CODE_SCAN_BYTE_LIMIT = 20_000

# Number of bytes to check for binary detection.
BINARY_CHECK_BYTES = 1024

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
    # Rust
    re.compile(r"\b(?:pub\s+)?fn\s+([A-Za-z_][\w]*)"),
    re.compile(r"\b(?:pub\s+)?struct\s+([A-Za-z_][\w]*)"),
    re.compile(r"\b(?:pub\s+)?(?:trait|enum|union)\s+([A-Za-z_][\w]*)"),
    re.compile(r"\b(?:pub\s+)?impl\s+(?:<[^>]*>\s+)?([A-Za-z_][\w]*)"),
    # Kotlin
    re.compile(r"\bfun\s+([A-Za-z_][\w]*)"),
    re.compile(r"\bclass\s+([A-Za-z_][\w]*)"),
    re.compile(r"\b(?:data|sealed|open|abstract)\s+class\s+([A-Za-z_][\w]*)"),
    re.compile(r"\bobject\s+([A-Za-z_][\w]*)"),
    # C#
    re.compile(r"\bclass\s+([A-Za-z_][\w]*)\s*(?::|{)"),
    re.compile(r"\b(?:void|int|string|bool|Task|Task<[^>]*>|IActionResult)\s+([A-Za-z_][\w]*)\s*\("),
    re.compile(r"\benum\s+([A-Za-z_][\w]*)"),
    # Swift
    re.compile(r"\bfunc\s+([A-Za-z_][\w]*)"),
    re.compile(r"\bclass\s+([A-Za-z_][\w]*)"),
    re.compile(r"\bstruct\s+([A-Za-z_][\w]*)"),
    re.compile(r"\bprotocol\s+([A-Za-z_][\w]*)"),
    re.compile(r"\benum\s+([A-Za-z_][\w]*)"),
    # Dart
    re.compile(r"\bclass\s+([A-Za-z_][\w]*)"),
    re.compile(r"\b(?:void|int|String|bool|double|Future|Stream)\s+([A-Za-z_][\w]*)\s*\("),
    re.compile(r"\bWidget\s+([A-Za-z_][\w]*)\s*\("),
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
IMPORT_PATTERNS_GO = [
    re.compile(r'\bimport\s+(?:\w+\s+)?\x22([^\x22]+)\x22'),
    re.compile(r'^\s+\x22([^\x22]+)\x22', re.MULTILINE),
]
IMPORT_PATTERNS_RUST = [
    re.compile(r'\buse\s+([^;]+)'),
    re.compile(r'\bmod\s+([a-zA-Z_][\w]*)'),
]
IMPORT_PATTERNS_RUBY = [
    re.compile(r"require\s+['\"]([^'\"]+)['\"]"),
    re.compile(r"require_relative\s+['\"]([^'\"]+)['\"]"),
]
IMPORT_PATTERNS_PHP = [
    re.compile(r'\buse\s+([^;]+)'),
    re.compile(r"require_once\s+['\"]([^'\"]+)['\"]"),
    re.compile(r"include(?:_once)?\s+['\"]([^'\"]+)['\"]"),
]
IMPORT_PATTERNS_JAVA = [
    re.compile(r'^\s*import\s+([^;]+)', re.MULTILINE),
]

# Resolution suffixes for JS-like imports.
JS_RESOLUTION_SUFFIXES = [
    "", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte",
    "/index.js", "/index.jsx", "/index.ts", "/index.tsx",
]

# Marker files used to detect package/workspace boundaries.
PACKAGE_MARKER_FILES: Set[str] = {
    "package.json", "pyproject.toml", "go.mod", "Cargo.toml", "composer.json",
}

# Token-budget thresholds (~4 chars per token heuristic).
CHARS_PER_TOKEN_ESTIMATE = 4
TOKEN_WARN_FILE_THRESHOLD = 2000
TOKEN_WARN_TOTAL_THRESHOLD = 6000

# Default config values.
DEFAULT_CONFIG: Dict[str, Any] = {
    "max_results": 5,
    "min_score": 0.45,
    "ignore_patterns": [],
    "extra_code_extensions": [],
}

RUST_STDLIB_PREFIXES = {"std::", "core::", "alloc::"}


# --- Git helpers -------------------------------------------------------------

def run_git(args: List[str], cwd: str) -> Optional[str]:
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


def is_git_repo(root: str) -> bool:
    return run_git(["rev-parse", "--is-inside-work-tree"], root) is not None


def git_fingerprint(root: str) -> Optional[str]:
    head = run_git(["rev-parse", "HEAD"], root)
    status = run_git(["status", "--porcelain"], root)
    if head is None:
        return None
    dirty_count = len(status.strip().splitlines()) if status else 0
    return f"{head.strip()}:{dirty_count}"


def _is_scoper_internal(rel_path: str) -> bool:
    normalized = rel_path.replace(os.sep, "/")
    if normalized == CACHE_DIRNAME or normalized.startswith(CACHE_DIRNAME + "/"):
        return True
    if normalized == ".scoperrc":
        return True
    return False


def list_files_git(root: str) -> List[str]:
    tracked = run_git(["ls-files"], root) or ""
    untracked = run_git(
        ["ls-files", "--others", "--exclude-standard"], root
    ) or ""
    files = set(tracked.splitlines()) | set(untracked.splitlines())
    return sorted(f for f in files if f and not _is_scoper_internal(f))


def parse_gitignore_patterns(root: str) -> List[str]:
    gitignore_path = os.path.join(root, ".gitignore")
    if not os.path.exists(gitignore_path):
        return []
    patterns: List[str] = []
    try:
        with open(gitignore_path, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(line)
    except OSError:
        pass
    return patterns


def list_files_fallback(root: str) -> List[str]:
    gitignore_patterns = parse_gitignore_patterns(root)
    results: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in FALLBACK_IGNORE_DIRS
        ]
        for fname in filenames:
            full = os.path.join(dirpath, fname)
            rel = os.path.relpath(full, root)
            if _is_scoper_internal(rel):
                continue
            if gitignore_patterns:
                matched = False
                for pat in gitignore_patterns:
                    if pat.endswith("/"):
                        if rel.startswith(pat) or rel.startswith(pat[:-1]):
                            matched = True
                            break
                    else:
                        if fnmatch_glob(rel, pat):
                            matched = True
                            break
                if matched:
                    continue
            results.append(rel)
    return sorted(results)


def fnmatch_glob(path: str, pattern: str) -> bool:
    """Simple glob-like matching for .gitignore patterns."""
    if pattern.startswith("/"):
        pattern = pattern[1:]
    if "*" in pattern:
        parts = pattern.split("*")
        if len(parts) == 2:
            if parts[0] and parts[1]:
                return path.startswith(parts[0]) and path.endswith(parts[1])
            elif parts[0]:
                return path.startswith(parts[0])
            elif parts[1]:
                return path.endswith(parts[1])
            return True
    return path == pattern


def is_binary_file(root: str, rel_path: str) -> bool:
    full_path = os.path.join(root, rel_path)
    try:
        with open(full_path, "rb") as f:
            chunk = f.read(BINARY_CHECK_BYTES)
    except OSError:
        return False
    return b"\0" in chunk


def git_hot_files(root: str) -> Dict[str, float]:
    if not is_git_repo(root):
        return {}

    weights: Dict[str, float] = {}

    dirty = run_git(["status", "--porcelain"], root) or ""
    for line in dirty.splitlines():
        if not line:
            continue
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


# --- Cache operations --------------------------------------------------------

def cache_paths(root: str) -> Tuple[str, str, str]:
    cache_dir = os.path.join(root, CACHE_DIRNAME)
    return (
        cache_dir,
        os.path.join(cache_dir, CACHE_FILENAME),
        os.path.join(cache_dir, SESSION_LOG_FILENAME),
    )


def load_cache(root: str) -> Optional[Dict[str, Any]]:
    _, cache_file, _ = cache_paths(root)
    if not os.path.exists(cache_file):
        return None
    try:
        with open(cache_file, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def save_cache(root: str, data: Dict[str, Any]) -> None:
    cache_dir, cache_file, _ = cache_paths(root)
    os.makedirs(cache_dir, exist_ok=True)
    with open(cache_file, "w") as f:
        json.dump(data, f, indent=2)


def is_cache_fresh(root: str, cached: Optional[Dict[str, Any]]) -> bool:
    if cached is None:
        return False

    if is_git_repo(root):
        current_fp = git_fingerprint(root)
        if current_fp is not None:
            return cached.get("git_fingerprint") == current_fp

    cached_at = cached.get("built_at", 0)
    max_age_seconds = 5 * 60
    return (time.time() - cached_at) < max_age_seconds


# --- Config file support -----------------------------------------------------

def load_config(root: str) -> Dict[str, Any]:
    config = dict(DEFAULT_CONFIG)

    global_path = os.path.expanduser("~/.scoperrc")
    if os.path.exists(global_path):
        try:
            with open(global_path, "r") as f:
                global_cfg = json.load(f)
                if isinstance(global_cfg, dict):
                    config.update(global_cfg)
        except (json.JSONDecodeError, OSError):
            pass

    project_path = os.path.join(root, ".scoperrc")
    if os.path.exists(project_path):
        try:
            with open(project_path, "r") as f:
                project_cfg = json.load(f)
                if isinstance(project_cfg, dict):
                    config.update(project_cfg)
        except (json.JSONDecodeError, OSError):
            pass

    return config


# --- Symbol + import extraction -----------------------------------------------

def analyze_file(root: str, rel_path: str) -> Tuple[List[str], List[str], Optional[str]]:
    ext = os.path.splitext(rel_path)[1].lower()

    if ext not in CODE_EXTENSIONS:
        return [], [], None

    if is_binary_file(root, rel_path):
        return [], [], None

    full_path = os.path.join(root, rel_path)
    try:
        with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read(CODE_SCAN_BYTE_LIMIT)
    except OSError:
        return [], [], None

    symbols: Set[str] = set()
    for pattern in SYMBOL_PATTERNS:
        for match in pattern.finditer(content):
            symbols.add(match.group(1))

    raw_imports: List[str] = []
    import_kind: Optional[str] = None

    if ext in JS_LIKE_EXTENSIONS:
        import_kind = "js"
        for pattern in IMPORT_PATTERNS_JS:
            raw_imports.extend(m.group(1) for m in pattern.finditer(content))
    elif ext == ".py":
        import_kind = "py"
        for pattern in IMPORT_PATTERNS_PY:
            raw_imports.extend(m.group(1) for m in pattern.finditer(content))
    elif ext == ".go":
        import_kind = "go"
        for pattern in IMPORT_PATTERNS_GO:
            raw_imports.extend(m.group(1) for m in pattern.finditer(content))
    elif ext == ".rs":
        import_kind = "rust"
        for pattern in IMPORT_PATTERNS_RUST:
            raw_imports.extend(m.group(1) for m in pattern.finditer(content))
    elif ext == ".rb":
        import_kind = "ruby"
        for pattern in IMPORT_PATTERNS_RUBY:
            raw_imports.extend(m.group(1) for m in pattern.finditer(content))
    elif ext == ".php":
        import_kind = "php"
        for pattern in IMPORT_PATTERNS_PHP:
            raw_imports.extend(m.group(1) for m in pattern.finditer(content))
    elif ext == ".java":
        import_kind = "java"
        for pattern in IMPORT_PATTERNS_JAVA:
            raw_imports.extend(m.group(1) for m in pattern.finditer(content))

    return sorted(symbols), raw_imports, import_kind


# --- Import resolvers --------------------------------------------------------

def resolve_js_import(
    importer_rel_path: str, raw_import: str, files_set: Set[str]
) -> Optional[str]:
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


def resolve_python_import(
    importer_rel_path: str, raw_import: str, files_set: Set[str]
) -> Optional[str]:
    importer_dir = os.path.dirname(importer_rel_path)

    if raw_import.startswith("."):
        parts = [p for p in raw_import.lstrip(".").split(".") if p]
        base = os.path.normpath(
            os.path.join(importer_dir, *parts)
        ) if parts else importer_dir
    else:
        parts = raw_import.split(".")
        base = os.path.normpath(os.path.join(*parts))

    base = base.replace(os.sep, "/")
    for suffix in [".py", "/__init__.py"]:
        candidate = base + suffix
        if candidate in files_set:
            return candidate
    return None


def resolve_go_import(
    importer_rel_path: str, raw_import: str, files_set: Set[str]
) -> Optional[str]:
    if "/" not in raw_import:
        return None
    candidate = raw_import + ".go"
    if candidate in files_set:
        return candidate
    candidate2 = raw_import + "/index.go"
    if candidate2 in files_set:
        return candidate2
    return None


RUST_SOURCE_DIRS = ["", "src/", "lib/"]


def resolve_rust_import(
    importer_rel_path: str, raw_import: str, files_set: Set[str]
) -> Optional[str]:
    # Skip stdlib-looking prefixes (std, core, alloc).
    if any(raw_import.startswith(p) for p in RUST_STDLIB_PREFIXES):
        return None

    parts = raw_import.split("::")
    if not parts:
        return None

    if parts[0] in ("crate", "self"):
        parts = parts[1:]
    elif parts[0] == "super":
        importer_dir = os.path.dirname(importer_rel_path)
        parts = parts[1:]
        if not parts:
            return None
        path = os.path.normpath(
            os.path.join(importer_dir, *parts)
        ).replace(os.sep, "/")
        for suffix in [".rs", "/mod.rs"]:
            c = path + suffix
            if c in files_set:
                return c
        return None

    if not parts:
        return None

    path = os.path.join(*parts).replace(os.sep, "/")
    for suffix in [".rs", "/mod.rs"]:
        for source_dir in RUST_SOURCE_DIRS:
            c = source_dir + path + suffix
            if c in files_set:
                return c
    return None


def resolve_ruby_import(
    importer_rel_path: str, raw_import: str, files_set: Set[str]
) -> Optional[str]:
    if raw_import.startswith("."):
        importer_dir = os.path.dirname(importer_rel_path)
        base = os.path.normpath(
            os.path.join(importer_dir, raw_import)
        ).replace(os.sep, "/")
        c = base + ".rb"
        if c in files_set:
            return c
        c2 = base + "/index.rb"
        if c2 in files_set:
            return c2
        return None

    c = raw_import + ".rb"
    if c in files_set:
        return c
    return None


PHP_SOURCE_DIRS = ["", "src/", "lib/", "app/"]


def resolve_php_import(
    importer_rel_path: str, raw_import: str, files_set: Set[str]
) -> Optional[str]:
    if raw_import.startswith(".") or "/" in raw_import:
        importer_dir = os.path.dirname(importer_rel_path)
        base = os.path.normpath(
            os.path.join(importer_dir, raw_import)
        ).replace(os.sep, "/")
        for suffix in ["", ".php"]:
            c = base + suffix
            if c in files_set:
                return c
        return None

    path = raw_import.replace("\\", "/") + ".php"
    for source_dir in PHP_SOURCE_DIRS:
        c = source_dir + path
        if c in files_set:
            return c
    return None


def resolve_java_import(
    importer_rel_path: str, raw_import: str, files_set: Set[str]
) -> Optional[str]:
    if raw_import.endswith(".*"):
        return None
    path = raw_import.replace(".", "/") + ".java"
    if path in files_set:
        return path
    return None


# --- Graph building ----------------------------------------------------------

def build_import_graph(
    root: str, files: List[str]
) -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
    files_set = set(files)
    imports_forward: Dict[str, List[str]] = {}

    for rel_path in files:
        _symbols, raw_imports, kind = analyze_file(root, rel_path)
        if not raw_imports:
            continue
        resolved: Set[str] = set()
        for raw in raw_imports:
            target: Optional[str] = None
            if kind == "js":
                target = resolve_js_import(rel_path, raw, files_set)
            elif kind == "py":
                target = resolve_python_import(rel_path, raw, files_set)
            elif kind == "go":
                target = resolve_go_import(rel_path, raw, files_set)
            elif kind == "rust":
                target = resolve_rust_import(rel_path, raw, files_set)
            elif kind == "ruby":
                target = resolve_ruby_import(rel_path, raw, files_set)
            elif kind == "php":
                target = resolve_php_import(rel_path, raw, files_set)
            elif kind == "java":
                target = resolve_java_import(rel_path, raw, files_set)
            if target and target != rel_path:
                resolved.add(target)
        if resolved:
            imports_forward[rel_path] = sorted(resolved)

    importers_reverse: Dict[str, List[str]] = {}
    for src, targets in imports_forward.items():
        for tgt in targets:
            importers_reverse.setdefault(tgt, []).append(src)

    return imports_forward, importers_reverse


def build_symbol_index(root: str, files: List[str]) -> Dict[str, List[str]]:
    symbols: Dict[str, List[str]] = {}
    for rel_path in files:
        syms, _raw_imports, _kind = analyze_file(root, rel_path)
        if syms:
            symbols[rel_path] = syms
    return symbols


# --- Monorepo / package boundary detection -------------------------------------

def detect_package_roots(files: List[str]) -> List[str]:
    roots: Set[str] = {"."}
    for f in files:
        base = os.path.basename(f)
        if base in PACKAGE_MARKER_FILES:
            d = os.path.dirname(f)
            roots.add(d if d else ".")
    return sorted(roots, key=lambda r: -len(r))


def package_for_file(rel_path: str, sorted_roots: List[str]) -> str:
    dirname = os.path.dirname(rel_path).replace(os.sep, "/")
    for root in sorted_roots:
        if root == ".":
            continue
        if dirname == root or dirname.startswith(root + "/"):
            return root
    return "."


# --- Index build/load ---------------------------------------------------------

def build_index(
    root: str,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if is_git_repo(root):
        files = list_files_git(root)
        fingerprint = git_fingerprint(root)
    else:
        files = list_files_fallback(root)
        fingerprint = None

    symbols = build_symbol_index(root, files)
    imports_forward, importers_reverse = build_import_graph(root, files)
    package_roots = detect_package_roots(files)

    data: Dict[str, Any] = {
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


def get_index(
    root: str,
    force_rebuild: bool = False,
    config: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], str]:
    cached = None if force_rebuild else load_cache(root)
    if cached is not None and is_cache_fresh(root, cached):
        return cached, "hit"
    return build_index(root, config), "rebuilt"


# --- Session memory -----------------------------------------------------------

def load_session_log(root: str) -> List[Dict[str, Any]]:
    _, _, log_file = cache_paths(root)
    if not os.path.exists(log_file):
        return []
    try:
        with open(log_file, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def save_session_log(root: str, log: List[Dict[str, Any]]) -> None:
    cache_dir, _, log_file = cache_paths(root)
    os.makedirs(cache_dir, exist_ok=True)
    with open(log_file, "w") as f:
        json.dump(log[-SESSION_LOG_MAX_ENTRIES:], f, indent=2)


def session_memory_boost(
    prompt: str, session_log: List[Dict[str, Any]]
) -> Dict[str, float]:
    boosts: Dict[str, float] = {}
    prompt_lower = prompt.lower()
    for entry in session_log:
        similarity = difflib.SequenceMatcher(
            None, prompt_lower, entry.get("prompt", "").lower()
        ).ratio()
        if similarity >= SESSION_SIMILARITY_THRESHOLD:
            for path in entry.get("candidates", []):
                boosts[path] = max(
                    boosts.get(path, 0.0), SESSION_BOOST * similarity
                )
    return boosts


# --- Matching -----------------------------------------------------------------
STOPWORDS: Set[str] = {
    "ubah", "ganti", "samain", "sama", "seperti", "kayak", "dengan",
    "di", "ke", "yang", "dan", "atau", "the", "a", "an", "for", "with",
    "like", "same", "as", "on", "in", "make", "change", "update",
}


def extract_keywords(prompt: str) -> List[str]:
    words = re.findall(r"[a-zA-Z0-9]+", prompt.lower())
    return [w for w in words if w not in STOPWORDS and len(w) > 2]


def split_identifier(text: str) -> List[str]:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    text = re.sub(r"[_\-./]", " ", text)
    return [t for t in text.lower().split() if t]


def text_match_score(text_lower: str, keywords: List[str]) -> float:
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


def score_file(
    path: str,
    keywords: List[str],
    symbols_for_file: List[str],
    hot_weight: float,
    session_boost_val: float,
) -> float:
    path_lower = path.lower()
    stem = os.path.splitext(os.path.basename(path_lower))[0]

    filename_score = max(
        text_match_score(stem, keywords),
        text_match_score(path_lower, keywords),
    )

    symbol_score = 0.0
    for symbol in symbols_for_file:
        symbol_score = max(
            symbol_score, text_match_score(symbol.lower(), keywords)
        )

    base_score = max(filename_score, symbol_score)

    if base_score > 0:
        base_score += hot_weight * 0.15
        base_score += session_boost_val

    return base_score


def apply_monorepo_penalty(
    scored: List[Tuple[str, float]], index: Dict[str, Any]
) -> List[Tuple[str, float]]:
    package_roots = index.get("package_roots", ["."])
    if len(package_roots) <= 1 or not scored:
        return scored

    top_file, top_score = max(scored, key=lambda pair: pair[1])
    if top_score < 0.6:
        return scored

    top_package = package_for_file(top_file, package_roots)
    adjusted: List[Tuple[str, float]] = []
    for f, score in scored:
        if score > 0 and package_for_file(f, package_roots) != top_package:
            score *= 0.6
        adjusted.append((f, score))
    return adjusted


def expand_related_files(
    primary_files: List[str],
    index: Dict[str, Any],
    max_related: int = 3,
    per_file_neighbors: int = 4,
) -> List[str]:
    imports_map = index.get("imports", {})
    importers_map = index.get("importers", {})

    seen = set(primary_files)
    related: List[str] = []
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


def estimate_tokens(root: str, rel_path: str) -> int:
    full_path = os.path.join(root, rel_path)
    try:
        size_bytes = os.path.getsize(full_path)
    except OSError:
        return 0
    return size_bytes // CHARS_PER_TOKEN_ESTIMATE


def build_token_report(
    root: str, files: List[str]
) -> Tuple[Dict[str, int], List[str]]:
    token_estimate: Dict[str, int] = {
        f: estimate_tokens(root, f) for f in files
    }
    warnings: List[str] = []
    for f, tokens in token_estimate.items():
        if tokens >= TOKEN_WARN_FILE_THRESHOLD:
            warnings.append(
                f"{f} is ~{tokens} tokens \u2014 consider reading only "
                f"the relevant section instead of the whole file."
            )
    total = sum(token_estimate.values())
    if total >= TOKEN_WARN_TOTAL_THRESHOLD:
        warnings.append(
            f"Total estimated context across candidates is ~{total} "
            f"tokens \u2014 consider narrowing the prompt or working "
            f"in smaller steps."
        )
    return token_estimate, warnings


def match_candidates(
    index: Dict[str, Any],
    prompt: str,
    root: str,
    max_results: int = 5,
    min_score: float = 0.45,
    use_symbols: bool = True,
    use_git_boost: bool = True,
    use_session_memory: bool = True,
    use_import_graph: bool = True,
    use_monorepo: bool = True,
    use_token_warnings: bool = True,
    scope_dir: Optional[str] = None,
) -> Dict[str, Any]:
    keywords = extract_keywords(prompt)
    if not keywords:
        return {
            "candidates": [],
            "related_files": [],
            "token_estimate": {},
            "warnings": [],
        }

    files = index["files"]

    if scope_dir:
        scope_dir_normalized = scope_dir.replace(os.sep, "/").rstrip("/") + "/"
        files = [f for f in files if f.startswith(scope_dir_normalized)]

    symbols_map = index.get("symbols", {}) if use_symbols else {}
    hot_files = git_hot_files(root) if use_git_boost else {}

    session_log = load_session_log(root) if use_session_memory else []
    session_boosts = (
        session_memory_boost(prompt, session_log) if use_session_memory else {}
    )

    scored: List[Tuple[str, float]] = []
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

    related: List[str] = []
    if use_import_graph and primary:
        related = expand_related_files(primary, index)

    token_estimate, warnings = {}, []
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


# --- CLI -----------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="Project root directory")
    parser.add_argument("--scope", help="Prompt/instruction to scope files for")
    parser.add_argument(
        "--build-index", action="store_true",
        help="Force rebuild the cache",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Just report cache freshness, no scoping",
    )
    parser.add_argument(
        "--max", type=int, default=None,
        help="Max candidate files to return (default 5)",
    )
    parser.add_argument(
        "--min-score", type=float, default=None,
        help="Minimum similarity score threshold (default 0.45)",
    )
    parser.add_argument(
        "--no-symbols", action="store_true",
        help="Disable symbol-index matching",
    )
    parser.add_argument(
        "--no-git-boost", action="store_true",
        help="Disable git-hot recency boost",
    )
    parser.add_argument(
        "--no-session-memory", action="store_true",
        help="Disable session-memory boost & logging",
    )
    parser.add_argument(
        "--no-import-graph", action="store_true",
        help="Disable related_files expansion via import graph",
    )
    parser.add_argument(
        "--no-monorepo", action="store_true",
        help="Disable cross-package ranking penalty",
    )
    parser.add_argument(
        "--no-token-warnings", action="store_true",
        help="Disable token-budget estimate/warnings",
    )
    parser.add_argument(
        "--scope-dir",
        help="Restrict search to a subdirectory (relative to --root)",
    )
    args = parser.parse_args()

    root = os.path.abspath(args.root)

    if args.check:
        cached = load_cache(root)
        fresh = is_cache_fresh(root, cached) if cached else False
        print(json.dumps({
            "cache_exists": cached is not None,
            "cache_fresh": fresh,
            "total_files_indexed": len(cached["files"]) if cached else 0,
            "total_files_with_symbols": (
                len(cached.get("symbols", {})) if cached else 0
            ),
            "total_files_with_imports": (
                len(cached.get("imports", {})) if cached else 0
            ),
            "package_roots": (
                cached.get("package_roots", ["."]) if cached else ["."]
            ),
        }, indent=2))
        return

    config = load_config(root)
    index, cache_status = get_index(
        root, force_rebuild=args.build_index, config=config
    )

    max_results = args.max if args.max is not None else config.get("max_results", 5)
    min_score = (args.min_score if args.min_score is not None
                 else float(config.get("min_score", 0.45)))

    result: Dict[str, Any] = {
        "cache_status": cache_status,
        "total_files_indexed": len(index["files"]),
        "candidates": [],
        "related_files": [],
        "token_estimate": {},
        "warnings": [],
        "scope_dir": args.scope_dir,
    }

    if args.scope:
        result.update(match_candidates(
            index,
            args.scope,
            root,
            max_results=max_results,
            min_score=min_score,
            use_symbols=not args.no_symbols,
            use_git_boost=not args.no_git_boost,
            use_session_memory=not args.no_session_memory,
            use_import_graph=not args.no_import_graph,
            use_monorepo=not args.no_monorepo,
            use_token_warnings=not args.no_token_warnings,
            scope_dir=args.scope_dir,
        ))

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
