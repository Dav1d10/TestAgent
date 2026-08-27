"""
Surgical AST-based merge of a single function's generated tests into a module's
existing test file.

The agent produces tests one function at a time, but all tests for a module live
in one file: `tests/test_{module}.py`. When a function changes, we must update
*only its* tests and leave every other function's tests byte-for-byte intact —
that is what keeps the resulting PR diff tight and what makes it safe to run the
agent repeatedly over the same module.

Ownership convention: a test function belongs to source function `F` when its
name is exactly `test_{F}` or starts with `test_{F}_`. Because one function name
can be a prefix of another (`add` vs `add_all`), ambiguity is resolved by
*longest match wins* against the set of all function names defined in the source
module — so `test_add_all_basic` is owned by `add_all`, never by `add`.
"""

import ast
from typing import Dict, List, Optional, Set, Tuple

from app.tools.code_parser import top_level_function_names


def has_bug_annotation(code: str) -> bool:
    """True if the test code carries a `# BUG:` marker (CASE 2 in the fix prompt)."""
    return "# BUG" in code


def extract_bug_lines(code: str) -> List[str]:
    """Return the stripped `# BUG:` annotation lines the LLM added, in order."""
    return [line.strip() for line in code.splitlines() if "# BUG" in line]


def _test_matches_function(test_name: str, fn: str) -> bool:
    return test_name == f"test_{fn}" or test_name.startswith(f"test_{fn}_")


def _test_owner(test_name: str, all_function_names: List[str]) -> Optional[str]:
    """Return the longest function-name match for a test, or None."""
    candidates = [fn for fn in all_function_names if _test_matches_function(test_name, fn)]
    return max(candidates, key=len) if candidates else None


def _owns_test(test_name: str, function_name: str, all_function_names: List[str]) -> bool:
    """
    Decide whether `test_name` belongs to `function_name`, resolving prefix
    ambiguity (`add` vs `add_all`) by longest-matching source function name.
    """
    owner = _test_owner(test_name, all_function_names)
    if owner is None:
        return _test_matches_function(test_name, function_name)
    return owner == function_name


def _node_span(node: ast.AST) -> tuple:
    """1-indexed inclusive (start, end) line span of a def, including its decorators."""
    start = node.lineno
    if getattr(node, "decorator_list", None):
        start = min(start, node.decorator_list[0].lineno)
    end = getattr(node, "end_lineno", node.lineno)
    return start, end


def _import_segments(source_code: str) -> List[str]:
    """Return the stripped source text of each top-level import statement."""
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return []
    segments = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            seg = ast.get_source_segment(source_code, node)
            if seg:
                segments.append(seg.strip())
    return segments


def _strip_imports(source_code: str) -> str:
    """Return `source_code` with its top-level import statements removed."""
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return source_code.strip()
    import_lines: set = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            end = getattr(node, "end_lineno", node.lineno)
            import_lines.update(range(node.lineno, end + 1))
    lines = source_code.splitlines()
    kept = [line for i, line in enumerate(lines, start=1) if i not in import_lines]
    return "\n".join(kept).strip()


def _insert_imports(base: str, new_imports: List[str]) -> str:
    """Insert `new_imports` right after the last existing import in `base`."""
    if not new_imports:
        return base
    try:
        tree = ast.parse(base)
    except SyntaxError:
        return "\n".join(new_imports) + "\n" + base
    last_import_end = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            last_import_end = max(last_import_end, getattr(node, "end_lineno", node.lineno))
    lines = base.splitlines()
    if last_import_end == 0:
        return "\n".join(new_imports) + "\n\n" + base
    head, tail = lines[:last_import_end], lines[last_import_end:]
    return "\n".join(head + new_imports + tail)


def _format_import_from(node: ast.ImportFrom, aliases: List[ast.alias]) -> str:
    """Render an ImportFrom after selected aliases have been removed."""
    prefix = "." * node.level
    module = node.module or ""
    names = ", ".join(
        f"{alias.name} as {alias.asname}" if alias.asname else alias.name
        for alias in aliases
    )
    return f"from {prefix}{module} import {names}"


def _apply_line_edits(source: str, edits: Dict[int, Tuple[int, Optional[str]]]) -> str:
    """Apply non-overlapping 1-indexed whole-line edits while preserving other text."""
    lines = source.splitlines()
    output: List[str] = []
    line_no = 1
    while line_no <= len(lines):
        edit = edits.get(line_no)
        if edit is None:
            output.append(lines[line_no - 1])
            line_no += 1
            continue
        end, replacement = edit
        if replacement is not None:
            output.append(replacement)
        line_no = end + 1
    return "\n".join(output).rstrip() + "\n"


def find_orphaned_test_functions(
    existing_content: str,
    module_name: str,
    current_function_names: List[str],
) -> List[str]:
    """
    Infer stale function-owned test blocks from exact-module imports.

    This is the self-healing path for a missed deletion webhook: a symbol
    imported from the sandbox module is considered orphaned only when it no
    longer exists in the current source *and* at least one convention-named test
    is owned by it. Requiring both signals avoids deleting unrelated imports.
    """
    try:
        tree = ast.parse(existing_content)
    except SyntaxError:
        return []

    imported_names: List[str] = []
    test_names: List[str] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == module_name:
            imported_names.extend(alias.name for alias in node.names if alias.name != "*")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            test_names.append(node.name)

    current = set(current_function_names)
    missing = [name for name in imported_names if name not in current]
    all_candidates = list(dict.fromkeys(current_function_names + imported_names))
    return [
        name
        for name in missing
        if any(_test_owner(test_name, all_candidates) == name for test_name in test_names)
    ]


def remove_owned_test_blocks(
    existing_content: str,
    function_names: List[str],
    module_name: str,
    all_function_names: Optional[List[str]] = None,
) -> str:
    """
    Remove convention-owned tests and their exact-module import aliases.

    Other test functions and arbitrary human-authored content are preserved
    byte-for-byte. For a shared import such as ``from utils import add, sub``,
    only aliases belonging to removed functions are pruned.
    """
    if not existing_content.strip() or not function_names:
        return existing_content
    try:
        tree = ast.parse(existing_content)
    except SyntaxError:
        return existing_content

    targets: Set[str] = set(function_names)
    all_fns = all_function_names or function_names
    edits: Dict[int, Tuple[int, Optional[str]]] = {}

    # Only claim a deleted function when at least one convention-owned test
    # proves this file contains an agent-owned block for it. Differently named
    # human tests remain outside the agent's ownership boundary.
    owned_targets: Set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            owner = _test_owner(node.name, all_fns)
            if owner in targets:
                owned_targets.add(owner)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            owner = _test_owner(node.name, all_fns)
            if owner in owned_targets:
                start, end = _node_span(node)
                edits[start] = (end, None)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == module_name:
            remaining = [alias for alias in node.names if alias.name not in owned_targets]
            if len(remaining) != len(node.names):
                start, end = _node_span(node)
                replacement = _format_import_from(node, remaining) if remaining else None
                edits[start] = (end, replacement)

    return _apply_line_edits(existing_content, edits) if edits else existing_content


def contains_test_cases(source_code: str) -> bool:
    """True when a test module still contains pytest-style functions or classes."""
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return False
    return any(
        (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        )
        or (isinstance(node, ast.ClassDef) and node.name.startswith("Test"))
        for node in tree.body
    )


def merge_test_block(
    existing_content: str,
    function_name: str,
    new_block: str,
    all_function_names: Optional[List[str]] = None,
) -> str:
    """
    Merge `new_block` (the tests for `function_name`) into `existing_content`.

    - If the module has no test file yet, the block is treated as a complete,
      runnable file and returned as-is.
    - Otherwise the target function's existing tests are removed, any imports the
      block introduces that aren't already present are reconciled, and the block's
      test functions are appended — every other function's tests are untouched.

    Tradeoff: merged tests are appended at the end of the file rather than kept in
    source order. This keeps the merge robust and deterministic; the diff still
    only touches the target function's tests plus the append point.
    """
    all_fns = all_function_names or [function_name]

    if not existing_content.strip():
        return new_block if new_block.endswith("\n") else new_block + "\n"

    try:
        existing_tree = ast.parse(existing_content)
    except SyntaxError:
        # Unparseable existing file — do not risk corrupting it; append verbatim.
        return existing_content.rstrip("\n") + "\n\n\n" + new_block.strip() + "\n"

    existing_lines = existing_content.splitlines()
    removed: set = set()
    for node in existing_tree.body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
            and _owns_test(node.name, function_name, all_fns)
        ):
            start, end = _node_span(node)
            removed.update(range(start, end + 1))

    base = "\n".join(line for i, line in enumerate(existing_lines, start=1) if i not in removed)

    existing_imports = set(_import_segments(base))
    new_imports = [seg for seg in _import_segments(new_block) if seg not in existing_imports]
    base = _insert_imports(base, new_imports)

    block_body = _strip_imports(new_block)
    return base.rstrip("\n") + "\n\n\n" + block_body + "\n"
