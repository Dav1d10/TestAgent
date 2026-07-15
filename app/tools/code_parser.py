"""
Maps a unified diff for a single file back to the set of top-level functions
whose bodies were added or modified, using the file's full new content.

A diff hunk only carries a handful of context lines, not whole function
bodies — so extracting a function's complete source requires walking the
`ast` of the new file content and checking which definitions overlap the
hunk's changed line numbers. This module has no network dependency: callers
fetch the diff and the file content separately (see app/tools/github_client.py)
and pass both in here.
"""

import ast
from dataclasses import dataclass
from typing import List

from unidiff import PatchedFile


@dataclass
class FunctionInfo:
    name: str
    source: str
    file_path: str
    line_start: int
    line_end: int


def _changed_line_numbers(file_diff: PatchedFile) -> set:
    """Returns the set of line numbers (in the new file) touched by added lines."""
    changed = set()
    for hunk in file_diff:
        for line in hunk:
            if line.is_added and line.target_line_no is not None:
                changed.add(line.target_line_no)
    return changed


def extract_modified_functions(file_diff: PatchedFile, new_file_content: str) -> List[FunctionInfo]:
    """
    Returns a FunctionInfo for every function or async function definition (module-level
    or a class method) in new_file_content whose line range overlaps a line added/modified
    by file_diff.
    """
    changed_lines = _changed_line_numbers(file_diff)
    if not changed_lines:
        return []

    tree = ast.parse(new_file_content)
    source_lines = new_file_content.splitlines()
    results = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        line_start = node.lineno
        line_end = getattr(node, "end_lineno", None) or line_start
        if any(line_start <= ln <= line_end for ln in changed_lines):
            source = "\n".join(source_lines[line_start - 1:line_end])
            results.append(
                FunctionInfo(
                    name=node.name,
                    source=source,
                    file_path=file_diff.path,
                    line_start=line_start,
                    line_end=line_end,
                )
            )

    return results
