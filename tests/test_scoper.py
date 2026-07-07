"""Tests for scoper.py — pure functions only, no filesystem or git I/O."""

import json
import os
import tempfile
import unittest
from unittest.mock import patch


# Shim to make tests importable without the real scoper hitting git.
# We patch sys.modules before importing scoper so the module-level
# subprocess calls in the real scoper don't fire; since scoper is a
# single script with no __init__.py tricks, importing directly works.
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import scoper


class TestExtractKeywords(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(
            scoper.extract_keywords("fix the header button"),
            ["fix", "header", "button"],
        )

    def test_stopwords_filtered(self):
        self.assertNotIn("the", scoper.extract_keywords("fix the header"))

    def test_short_words_filtered(self):
        self.assertNotIn("on", scoper.extract_keywords("button on header"))

    def test_empty_prompt(self):
        self.assertEqual(scoper.extract_keywords(""), [])

    def test_only_stopwords(self):
        self.assertEqual(scoper.extract_keywords("the a an in di"), [])


class TestSplitIdentifier(unittest.TestCase):
    def test_camelcase(self):
        self.assertEqual(scoper.split_identifier("TopHeader"), ["top", "header"])

    def test_kebabcase(self):
        self.assertEqual(
            scoper.split_identifier("nav-bar-container"),
            ["nav", "bar", "container"],
        )

    def test_path(self):
        self.assertEqual(
            scoper.split_identifier("src/components/Header.jsx"),
            ["src", "components", "header", "jsx"],
        )

    def test_underscore(self):
        self.assertEqual(
            scoper.split_identifier("my_component_test"),
            ["my", "component", "test"],
        )


class TestTextMatchScore(unittest.TestCase):
    def test_exact_token_match(self):
        self.assertEqual(scoper.text_match_score("header", ["header"]), 0.95)

    def test_substring_in_token(self):
        score = scoper.text_match_score("topheader", ["top"])
        self.assertAlmostEqual(score, 0.8)

    def test_substring_in_raw_text(self):
        score = scoper.text_match_score("somethingheader", ["header"])
        self.assertGreaterEqual(score, 0.7)

    def test_no_match(self):
        self.assertEqual(scoper.text_match_score("footer", ["header"]), 0.0)

    def test_fuzzy_high_similarity(self):
        score = scoper.text_match_score("hedaer", ["header"])
        self.assertGreater(score, 0.0)

    def test_fuzzy_low_similarity(self):
        self.assertEqual(scoper.text_match_score("xyz", ["header"]), 0.0)


class TestScoreFile(unittest.TestCase):
    def test_filename_match(self):
        score = scoper.score_file("src/Header.jsx", ["header"], [], 0.0, 0.0)
        self.assertGreater(score, 0.0)

    def test_symbol_match(self):
        score = scoper.score_file(
            "src/Nav.jsx", ["topheader"], ["TopHeader"], 0.0, 0.0
        )
        self.assertGreater(score, 0.0)

    def test_git_boost(self):
        base = scoper.score_file("src/Header.jsx", ["header"], [], 0.0, 0.0)
        boosted = scoper.score_file("src/Header.jsx", ["header"], [], 1.0, 0.0)
        self.assertGreater(boosted, base)

    def test_session_boost(self):
        base = scoper.score_file("src/Header.jsx", ["header"], [], 0.0, 0.0)
        boosted = scoper.score_file("src/Header.jsx", ["header"], [], 0.0, 0.2)
        self.assertGreater(boosted, base)

    def test_no_boost_on_zero_base(self):
        score = scoper.score_file("src/Foo.jsx", ["bar"], [], 1.0, 0.2)
        self.assertEqual(score, 0.0)


class TestDetectPackageRoots(unittest.TestCase):
    def test_single_root(self):
        files = ["package.json", "src/index.js"]
        roots = scoper.detect_package_roots(files)
        self.assertIn(".", roots)
        self.assertEqual(len(roots), 1)

    def test_multiple_roots(self):
        files = [
            "package.json",
            "packages/a/package.json",
            "packages/b/package.json",
            "src/index.js",
        ]
        roots = scoper.detect_package_roots(files)
        self.assertIn(".", roots)
        self.assertIn("packages/a", roots)
        self.assertIn("packages/b", roots)
        # "." should be last (shortest path)
        self.assertEqual(roots[-1], ".")

    def test_pyproject_toml(self):
        files = ["pyproject.toml", "src/main.py"]
        roots = scoper.detect_package_roots(files)
        self.assertIn(".", roots)

    def test_marker_files(self):
        files = [
            "package.json",
            "backend/pyproject.toml",
            "backend/app/main.py",
            "frontend/package.json",
        ]
        roots = scoper.detect_package_roots(files)
        for r in ("backend", "frontend"):
            self.assertIn(r, roots)


class TestPackageForFile(unittest.TestCase):
    def setUp(self):
        self.roots = scoper.detect_package_roots(
            [
                "package.json",
                "packages/a/package.json",
                "packages/b/package.json",
            ]
        )

    def test_root_package(self):
        self.assertEqual(scoper.package_for_file("src/index.js", self.roots), ".")

    def test_subpackage(self):
        self.assertEqual(
            scoper.package_for_file("packages/a/src/foo.js", self.roots), "packages/a"
        )


class TestResolveJsImport(unittest.TestCase):
    def setUp(self):
        self.files = {"src/components/Button.jsx", "src/utils/helpers.js"}

    def test_relative_import(self):
        result = scoper.resolve_js_import(
            "src/components/Header.jsx", "./Button", self.files
        )
        self.assertEqual(result, "src/components/Button.jsx")

    def test_relative_with_extension(self):
        result = scoper.resolve_js_import(
            "src/pages/Login.jsx", "./Button.jsx",
            {"src/pages/Button.jsx", "src/pages/Login.jsx"},
        )
        self.assertEqual(result, "src/pages/Button.jsx")

    def test_bare_import_skipped(self):
        result = scoper.resolve_js_import(
            "src/index.js", "react", self.files
        )
        self.assertIsNone(result)

    def test_nonexistent_import(self):
        result = scoper.resolve_js_import(
            "src/index.js", "./nonexistent", self.files
        )
        self.assertIsNone(result)

    def test_index_resolution(self):
        files = {"src/pages/components/index.jsx"}
        result = scoper.resolve_js_import(
            "src/pages/Page.jsx", "./components", files
        )
        self.assertEqual(result, "src/pages/components/index.jsx")


class TestResolvePythonImport(unittest.TestCase):
    def setUp(self):
        self.files = {"src/utils/helpers.py", "src/utils/__init__.py"}

    def test_absolute_import(self):
        files = {"package/module.py", "src/main.py"}
        result = scoper.resolve_python_import(
            "src/main.py", "package.module", files
        )
        self.assertEqual(result, "package/module.py")

    def test_relative_import(self):
        result = scoper.resolve_python_import(
            "src/utils/formatter.py", ".helpers", self.files
        )
        self.assertEqual(result, "src/utils/helpers.py")

    def test_init_resolution(self):
        files = {"src/pkg/__init__.py"}
        result = scoper.resolve_python_import(
            "src/main.py", "src.pkg", files
        )
        self.assertEqual(result, "src/pkg/__init__.py")

    def test_nonexistent(self):
        result = scoper.resolve_python_import(
            "src/main.py", "nonexistent", self.files
        )
        self.assertIsNone(result)


class TestIsScoperInternal(unittest.TestCase):
    def test_cache_dir_equal(self):
        self.assertTrue(scoper._is_scoper_internal(".scoper_cache"))

    def test_cache_dir_prefix(self):
        self.assertTrue(
            scoper._is_scoper_internal(".scoper_cache/tree_index.json")
        )

    def test_other_file(self):
        self.assertFalse(scoper._is_scoper_internal("src/index.js"))

    def test_nested_not_internal(self):
        self.assertFalse(
            scoper._is_scoper_internal("stuff/.scoper_cache/notreal")
        )

    def test_scoperrc_is_internal(self):
        self.assertTrue(scoper._is_scoper_internal(".scoperrc"))


class TestApplyMonorepoPenalty(unittest.TestCase):
    def test_single_root_no_change(self):
        index = {"package_roots": ["."]}
        scored = [("src/a.js", 0.8), ("src/b.js", 0.6)]
        result = scoper.apply_monorepo_penalty(scored, index)
        self.assertEqual(result, scored)

    def test_cross_package_penalty(self):
        files = [
            "package.json",
            "packages/a/package.json",
            "packages/b/package.json",
            "packages/a/src/foo.js",
            "packages/b/src/bar.js",
        ]
        roots = scoper.detect_package_roots(files)
        index = {"package_roots": roots}
        scored = [("packages/a/src/foo.js", 0.9), ("packages/b/src/bar.js", 0.7)]
        result = scoper.apply_monorepo_penalty(scored, index)
        self.assertEqual(result[0][0], "packages/a/src/foo.js")
        result_b_score = dict(result).get("packages/b/src/bar.js", 0)
        self.assertAlmostEqual(result_b_score, 0.7 * 0.6)

    def test_low_confidence_skips_penalty(self):
        files = ["package.json", "packages/a/package.json"]
        roots = scoper.detect_package_roots(files)
        index = {"package_roots": roots}
        scored = [("packages/a/x.js", 0.3), ("src/y.js", 0.2)]
        result = scoper.apply_monorepo_penalty(scored, index)
        self.assertEqual(result, scored)


class TestExpandRelatedFiles(unittest.TestCase):
    def test_imports_added(self):
        index = {
            "imports": {"src/Page.jsx": ["src/Button.jsx"]},
            "importers": {},
        }
        related = scoper.expand_related_files(["src/Page.jsx"], index)
        self.assertIn("src/Button.jsx", related)

    def test_importers_added(self):
        index = {
            "imports": {},
            "importers": {"src/Button.jsx": ["src/Page.jsx"]},
        }
        related = scoper.expand_related_files(["src/Button.jsx"], index)
        self.assertIn("src/Page.jsx", related)

    def test_candidate_excluded_from_related(self):
        index = {
            "imports": {"src/Page.jsx": ["src/Button.jsx"]},
            "importers": {},
        }
        related = scoper.expand_related_files(
            ["src/Page.jsx", "src/Button.jsx"], index
        )
        self.assertNotIn("src/Page.jsx", related)
        self.assertNotIn("src/Button.jsx", related)

    def test_max_results_respected(self):
        index = {
            "imports": {
                "a.jsx": ["b.jsx", "c.jsx", "d.jsx", "e.jsx", "f.jsx"]
            },
            "importers": {},
        }
        related = scoper.expand_related_files(
            ["a.jsx"], index, max_related=2, per_file_neighbors=5
        )
        self.assertLessEqual(len(related), 2)


class TestSessionMemoryBoost(unittest.TestCase):
    def test_similar_prompt_boosts(self):
        log = [{"prompt": "fix header button", "candidates": ["src/Header.jsx"]}]
        boosts = scoper.session_memory_boost("fix the header", log)
        self.assertIn("src/Header.jsx", boosts)

    def test_dissimilar_prompt_no_boost(self):
        log = [{"prompt": "fix header button", "candidates": ["src/Header.jsx"]}]
        boosts = scoper.session_memory_boost("database migration", log)
        self.assertEqual(boosts, {})

    def test_empty_log(self):
        boosts = scoper.session_memory_boost("anything", [])
        self.assertEqual(boosts, {})


class TestBuildTokenReport(unittest.TestCase):
    @patch("scoper.os.path.getsize")
    def test_small_file_no_warning(self, mock_getsize):
        mock_getsize.return_value = 1000
        tokens, warns = scoper.build_token_report("/root", ["small.js"])
        self.assertEqual(warns, [])

    @patch("scoper.os.path.getsize")
    def test_large_file_warning(self, mock_getsize):
        mock_getsize.return_value = 20000
        tokens, warns = scoper.build_token_report("/root", ["big.js"])
        self.assertGreaterEqual(len(warns), 1)

    @patch("scoper.os.path.getsize")
    def test_total_warning(self, mock_getsize):
        mock_getsize.return_value = 6001 * 4
        tokens, warns = scoper.build_token_report(
            "/root", ["a.js", "b.js"]
        )
        total_warn = [w for w in warns if "Total" in w]
        self.assertGreaterEqual(len(total_warn), 1)

    @patch("scoper.os.path.getsize")
    def test_token_estimate_correct(self, mock_getsize):
        mock_getsize.return_value = 400
        tokens, _ = scoper.build_token_report("/root", ["f.js"])
        self.assertEqual(tokens["f.js"], 100)


class TestExtractKeywordsEdgeCases(unittest.TestCase):
    def test_mixed_language(self):
        keywords = scoper.extract_keywords("ubah button di header")
        self.assertIn("button", keywords)
        self.assertIn("header", keywords)
        self.assertNotIn("ubah", keywords)
        self.assertNotIn("di", keywords)

    def test_numbers(self):
        keywords = scoper.extract_keywords("page 404 error")
        self.assertIn("404", keywords)
        self.assertIn("page", keywords)
        self.assertIn("error", keywords)

    def test_special_chars_removed(self):
        keywords = scoper.extract_keywords("hello! @world #test")
        self.assertIn("hello", keywords)
        self.assertIn("world", keywords)
        self.assertIn("test", keywords)


class TestLoadConfig(unittest.TestCase):
    def test_no_config_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = scoper.load_config(tmp)
            self.assertEqual(config["max_results"], 5)
            self.assertEqual(config["min_score"], 0.45)

    def test_project_config_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ".scoperrc"), "w") as f:
                json.dump({"min_score": 0.6, "max_results": 3}, f)
            config = scoper.load_config(tmp)
            self.assertEqual(config["min_score"], 0.6)
            self.assertEqual(config["max_results"], 3)

    def test_bad_json_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ".scoperrc"), "w") as f:
                f.write("not json")
            config = scoper.load_config(tmp)
            self.assertEqual(config["max_results"], 5)


class TestParseGitignorePatterns(unittest.TestCase):
    def test_reads_patterns(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ".gitignore"), "w") as f:
                f.write("*.log\nbuild/\n# comment\n")
            patterns = scoper.parse_gitignore_patterns(tmp)
            self.assertIn("*.log", patterns)
            self.assertIn("build/", patterns)
            self.assertNotIn("# comment", patterns)

    def test_no_gitignore(self):
        with tempfile.TemporaryDirectory() as tmp:
            patterns = scoper.parse_gitignore_patterns(tmp)
            self.assertEqual(patterns, [])

    def test_empty_gitignore(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ".gitignore"), "w") as f:
                f.write("")
            patterns = scoper.parse_gitignore_patterns(tmp)
            self.assertEqual(patterns, [])


class TestIsBinaryFile(unittest.TestCase):
    def test_text_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.txt")
            with open(path, "w") as f:
                f.write("hello world\n")
            self.assertFalse(scoper.is_binary_file(tmp, "test.txt"))

    def test_binary_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.bin")
            with open(path, "wb") as f:
                f.write(b"\x00\x01\x02")
            self.assertTrue(scoper.is_binary_file(tmp, "test.bin"))

    def test_nonexistent_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(
                scoper.is_binary_file(tmp, "nonexistent.txt")
            )


class TestResolveGoImport(unittest.TestCase):
    def test_basic_import(self):
        files = {"foo/bar.go"}
        result = scoper.resolve_go_import("main.go", "foo/bar", files)
        self.assertEqual(result, "foo/bar.go")

    def test_stdlib_skipped(self):
        result = scoper.resolve_go_import("main.go", "fmt", set())
        self.assertIsNone(result)

    def test_nested_path(self):
        files = {"pkg/util/helper.go"}
        result = scoper.resolve_go_import(
            "main.go", "pkg/util/helper", files
        )
        self.assertEqual(result, "pkg/util/helper.go")


class TestResolveRustImport(unittest.TestCase):
    def test_crate_path(self):
        files = {"src/foo.rs"}
        result = scoper.resolve_rust_import(
            "src/main.rs", "crate::foo", files
        )
        self.assertEqual(result, "src/foo.rs")

    def test_relative_mod(self):
        files = {"src/bar.rs"}
        result = scoper.resolve_rust_import(
            "src/main.rs", "super::bar",
            files | {"src/lib.rs"},
        )
        self.assertEqual(result, "src/bar.rs")

    def test_stdlib_skipped(self):
        result = scoper.resolve_rust_import(
            "src/main.rs", "std::collections::HashMap", set()
        )
        self.assertIsNone(result)

    def test_nested_path(self):
        files = {"src/utils/helpers.rs"}
        result = scoper.resolve_rust_import(
            "src/main.rs", "crate::utils::helpers", files
        )
        self.assertEqual(result, "src/utils/helpers.rs")


class TestResolveRubyImport(unittest.TestCase):
    def test_require_relative(self):
        files = {"lib/util.rb"}
        result = scoper.resolve_ruby_import(
            "lib/main.rb", "./util", files
        )
        self.assertEqual(result, "lib/util.rb")

    def test_direct_require(self):
        files = {"config.rb"}
        result = scoper.resolve_ruby_import(
            "lib/main.rb", "config", files
        )
        self.assertEqual(result, "config.rb")

    def test_nonexistent(self):
        result = scoper.resolve_ruby_import(
            "lib/main.rb", "missing", set()
        )
        self.assertIsNone(result)


class TestResolvePhpImport(unittest.TestCase):
    def test_namespace_to_path(self):
        files = {"src/App/Controller/Foo.php"}
        result = scoper.resolve_php_import(
            "src/index.php", "App\\Controller\\Foo", files
        )
        self.assertEqual(result, "src/App/Controller/Foo.php")

    def test_require_once(self):
        files = {"src/config.php"}
        result = scoper.resolve_php_import(
            "src/index.php", "./config", files
        )
        self.assertEqual(result, "src/config.php")

    def test_nonexistent(self):
        result = scoper.resolve_php_import(
            "src/index.php", "MissingClass", set()
        )
        self.assertIsNone(result)


class TestResolveJavaImport(unittest.TestCase):
    def test_import_to_path(self):
        files = {"com/example/Foo.java"}
        result = scoper.resolve_java_import(
            "com/example/Main.java", "com.example.Foo", files
        )
        self.assertEqual(result, "com/example/Foo.java")

    def test_wildcard_skipped(self):
        result = scoper.resolve_java_import(
            "com/example/Main.java", "com.example.*", set()
        )
        self.assertIsNone(result)

    def test_nonexistent(self):
        result = scoper.resolve_java_import(
            "com/example/Main.java", "com.example.Foo", set()
        )
        self.assertIsNone(result)


class TestStopwords(unittest.TestCase):
    def test_indonesian_stopwords(self):
        for w in ("ubah", "ganti", "samain", "di", "ke", "yang"):
            self.assertIn(w, scoper.STOPWORDS)

    def test_english_stopwords(self):
        for w in ("the", "a", "an", "for", "in", "make", "change"):
            self.assertIn(w, scoper.STOPWORDS)


class TestCachePaths(unittest.TestCase):
    def test_cache_paths_returns_tuple(self):
        result = scoper.cache_paths("/tmp")
        self.assertEqual(len(result), 3)
        self.assertTrue(result[0].endswith(".scoper_cache"))
        self.assertTrue(result[1].endswith("tree_index.json"))
        self.assertTrue(result[2].endswith("session_log.json"))


if __name__ == "__main__":
    unittest.main()
