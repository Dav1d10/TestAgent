import ast

from app.tools.test_merger import (
    contains_test_cases,
    find_orphaned_test_functions,
    merge_test_block,
    remove_owned_test_blocks,
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


class TestRemoval:
    def test_removes_owned_tests_and_exact_import(self):
        existing = (
            "from utils import add\n"
            "from utils import subtract\n\n"
            "def test_add_basic():\n    assert add(1, 2) == 3\n\n"
            "def test_subtract_basic():\n    assert subtract(3, 1) == 2\n"
        )
        result = remove_owned_test_blocks(
            existing, ["add"], "utils", ["add", "subtract"]
        )
        assert "from utils import add" not in result
        assert "test_add_basic" not in result
        assert "from utils import subtract" in result
        assert "test_subtract_basic" in result
        ast.parse(result)

    def test_prunes_only_deleted_alias_from_shared_import(self):
        existing = (
            "from utils import add, add_all\n\n"
            "def test_add_basic():\n    assert add(1, 2) == 3\n\n"
            "def test_add_all_basic():\n    assert add_all([1, 2]) == 3\n"
        )
        result = remove_owned_test_blocks(
            existing, ["add"], "utils", ["add", "add_all"]
        )
        assert "from utils import add_all" in result
        assert "test_add_basic" not in result
        assert "test_add_all_basic" in result

    def test_longest_match_preserves_prefix_sibling(self):
        existing = (
            "from utils import add, add_all\n\n"
            "def test_add_all_basic():\n    assert add_all([1]) == 1\n"
        )
        result = remove_owned_test_blocks(
            existing, ["add"], "utils", ["add", "add_all"]
        )
        assert "test_add_all_basic" in result

    def test_preserves_differently_named_human_test(self):
        existing = (
            "from utils import add\n\n"
            "def test_sum_behavior():\n    assert add(1, 2) == 3\n"
        )
        result = remove_owned_test_blocks(existing, ["add"], "utils", ["add"])
        assert "test_sum_behavior" in result
        assert "from utils import add" in result
        # Its import is preserved because removing it would break human content.
        # Reconciliation only claims a symbol when convention-owned tests exist.
        assert find_orphaned_test_functions(existing, "utils", []) == []


class TestOrphanReconciliation:
    def test_finds_imported_missing_function_with_owned_tests(self):
        existing = (
            "from utils import removed\n"
            "from utils import current\n\n"
            "def test_removed_basic():\n    assert removed() == 1\n\n"
            "def test_current_basic():\n    assert current() == 2\n"
        )
        assert find_orphaned_test_functions(existing, "utils", ["current"]) == ["removed"]

    def test_ignores_missing_import_without_convention_owned_test(self):
        existing = "from utils import helper\n\ndef test_custom_behavior():\n    assert helper()\n"
        assert find_orphaned_test_functions(existing, "utils", []) == []

    def test_prefix_sibling_is_not_misattributed(self):
        existing = (
            "from utils import add, add_all\n\n"
            "def test_add_all_basic():\n    assert add_all([]) == 0\n"
        )
        assert find_orphaned_test_functions(existing, "utils", ["add_all"]) == []


class TestContainsTestCases:
    def test_detects_test_function(self):
        assert contains_test_cases("def test_x():\n    pass\n")

    def test_detects_test_class(self):
        assert contains_test_cases("class TestX:\n    def test_one(self):\n        pass\n")

    def test_false_for_imports_and_helpers_only(self):
        assert not contains_test_cases("import pytest\n\ndef helper():\n    pass\n")


class TestBugHelpers:
    def test_has_bug_annotation_true(self):
        assert has_bug_annotation("assert x == 1  # BUG: returned 0")

    def test_has_bug_annotation_false(self):
        assert not has_bug_annotation("assert x == 1  # normal comment")

    def test_extract_bug_lines(self):
        code = "def t():\n    assert f() == 1  # BUG: f() returned 0\n    assert g() == 2\n"
        assert extract_bug_lines(code) == ["assert f() == 1  # BUG: f() returned 0"]
