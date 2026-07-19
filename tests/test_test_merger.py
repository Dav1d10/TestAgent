import ast

from app.tools.test_merger import (
    merge_test_block,
    top_level_function_names,
    has_bug_annotation,
    extract_bug_lines,
    _owns_test,
)

SOURCE = """def add(a, b):
    return a + b


def add_all(items):
    return sum(items)
"""


class TestOwnership:
    def test_exact_match(self):
        assert _owns_test("test_add", "add", ["add", "add_all"])

    def test_suffix_match(self):
        assert _owns_test("test_add_negatives", "add", ["add", "add_all"])

    def test_longest_prefix_wins(self):
        # test_add_all_basic must belong to add_all, never to add.
        assert _owns_test("test_add_all_basic", "add_all", ["add", "add_all"])
        assert not _owns_test("test_add_all_basic", "add", ["add", "add_all"])

    def test_unrelated_test_not_owned(self):
        assert not _owns_test("test_subtract", "add", ["add", "add_all"])


class TestTopLevelFunctionNames:
    def test_lists_module_level_functions(self):
        assert top_level_function_names(SOURCE) == ["add", "add_all"]

    def test_empty_on_syntax_error(self):
        assert top_level_function_names("def (") == []


class TestMerge:
    def test_empty_baseline_returns_block(self):
        block = "from utils import add\n\ndef test_add_ok():\n    assert add(1, 2) == 3\n"
        result = merge_test_block("", "add", block, ["add"])
        assert "def test_add_ok" in result
        ast.parse(result)  # must be valid Python

    def test_replaces_only_target_function_tests(self):
        existing = (
            "from utils import add, add_all\n\n"
            "def test_add_old():\n    assert add(1, 1) == 2\n\n"
            "def test_add_all_basic():\n    assert add_all([1, 2]) == 3\n"
        )
        new_block = "def test_add_ok():\n    assert add(2, 2) == 4\n"
        result = merge_test_block(existing, "add", new_block, ["add", "add_all"])

        # add's old test is gone, its new test is in, add_all's test is untouched.
        assert "test_add_old" not in result
        assert "test_add_ok" in result
        assert "test_add_all_basic" in result
        ast.parse(result)

    def test_does_not_clobber_prefix_sibling(self):
        existing = (
            "from utils import add, add_all\n\n"
            "def test_add_all_basic():\n    assert add_all([1]) == 1\n"
        )
        new_block = "def test_add_ok():\n    assert add(1, 1) == 2\n"
        result = merge_test_block(existing, "add", new_block, ["add", "add_all"])
        # Regenerating `add` must not remove add_all's tests.
        assert "test_add_all_basic" in result

    def test_does_not_duplicate_existing_imports(self):
        existing = "from utils import add\n\ndef test_add_old():\n    assert add(1, 1) == 2\n"
        new_block = "from utils import add\n\ndef test_add_ok():\n    assert add(2, 2) == 4\n"
        result = merge_test_block(existing, "add", new_block, ["add"])
        assert result.count("from utils import add") == 1


class TestBugHelpers:
    def test_has_bug_annotation_true(self):
        assert has_bug_annotation("assert x == 1  # BUG: returned 0")

    def test_has_bug_annotation_false(self):
        assert not has_bug_annotation("assert x == 1  # normal comment")

    def test_extract_bug_lines(self):
        code = "def t():\n    assert f() == 1  # BUG: f() returned 0\n    assert g() == 2\n"
        assert extract_bug_lines(code) == ["assert f() == 1  # BUG: f() returned 0"]
