from unidiff import PatchSet

from app.tools.code_parser import (
    extract_deleted_function_names,
    extract_modified_functions,
    top_level_function_names,
)

NEW_FILE_CONTENT = """def add(a, b):
    return a + b


def subtract(a, b):
    return a - b
"""

# Unified diff: `add` gained a comment (modified), `subtract` is untouched.
DIFF_ADD_MODIFIED = """--- a/math_utils.py
+++ b/math_utils.py
@@ -1,2 +1,3 @@
 def add(a, b):
+    # sum two numbers
     return a + b
"""

# Unified diff: `subtract` is a brand-new function (added at the end of the file).
DIFF_SUBTRACT_ADDED = """--- a/math_utils.py
+++ b/math_utils.py
@@ -1,2 +1,6 @@
 def add(a, b):
     return a + b
+
+
+def subtract(a, b):
+    return a - b
"""


def _patched_file(diff_text: str):
    return PatchSet(diff_text)[0]


class TestExtractModifiedFunctions:
    def test_finds_only_the_modified_function(self):
        functions = extract_modified_functions(_patched_file(DIFF_ADD_MODIFIED), NEW_FILE_CONTENT)
        names = {f.name for f in functions}
        assert names == {"add"}

    def test_finds_only_the_added_function(self):
        functions = extract_modified_functions(_patched_file(DIFF_SUBTRACT_ADDED), NEW_FILE_CONTENT)
        names = {f.name for f in functions}
        assert names == {"subtract"}

    def test_function_info_carries_full_source(self):
        functions = extract_modified_functions(_patched_file(DIFF_ADD_MODIFIED), NEW_FILE_CONTENT)
        add_fn = next(f for f in functions if f.name == "add")
        assert "def add(a, b):" in add_fn.source
        assert "return a + b" in add_fn.source

    def test_function_info_carries_file_path(self):
        functions = extract_modified_functions(_patched_file(DIFF_ADD_MODIFIED), NEW_FILE_CONTENT)
        assert functions[0].file_path == "math_utils.py"

    def test_no_changes_returns_empty(self):
        empty_diff = """--- a/math_utils.py
+++ b/math_utils.py
"""
        assert extract_modified_functions(_patched_file(empty_diff), NEW_FILE_CONTENT) == []


class TestDeletedFunctions:
    def test_compares_before_and_after_ast(self):
        old = "def add():\n    pass\n\ndef removed():\n    pass\n"
        new = "def add():\n    pass\n"
        assert extract_deleted_function_names(old, new) == ["removed"]

    def test_rename_is_deletion_plus_addition(self):
        old = "def old_name():\n    pass\n"
        new = "def new_name():\n    pass\n"
        assert extract_deleted_function_names(old, new) == ["old_name"]
        assert top_level_function_names(new) == ["new_name"]

    def test_preserves_old_source_order(self):
        old = "def first():\n    pass\n\ndef second():\n    pass\n"
        assert extract_deleted_function_names(old, "") == ["first", "second"]

    def test_top_level_names_ignore_nested_functions(self):
        source = "def outer():\n    def inner():\n        pass\n    return inner\n"
        assert top_level_function_names(source) == ["outer"]
