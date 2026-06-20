import pytest
from app.agent.graph import build_agent_graph
from app.agent.state import create_initial_state
from app.agent.nodes import (
    generate_test_node,
    run_test_node,
    fix_test_node,
    finalize_node,
    should_retry,
)
from app.tools.sandbox import SandboxResult
from tests.fixtures.sample_functions import SIMPLE_ADD, VALIDATE_AGE


# ── Helpers ──────────────────────────────────────────────────────────────────

def _state(**overrides):
    """Base state with sensible defaults; override any field via kwargs."""
    base = create_initial_state(SIMPLE_ADD, module_name="simple_add", max_attempts=3)
    return {**base, **overrides}


# ── graph ────────────────────────────────────────────────────────────────────

def test_graph_compiles():
    graph = build_agent_graph()
    nodes = set(graph.get_graph().nodes.keys())
    assert {"generate_test", "run_test", "fix_test", "finalize"}.issubset(nodes)


# ── generate_test_node ────────────────────────────────────────────────────────

class TestGenerateTestNode:
    def test_increments_attempt_count(self, mocker):
        mocker.patch("app.agent.nodes._call_llm", return_value="def test_add(): assert add(1,2)==3")
        result = generate_test_node(_state())
        assert result["attempt_count"] == 1

    def test_returns_test_code(self, mocker):
        expected = "def test_add():\n    assert add(1, 2) == 3"
        mocker.patch("app.agent.nodes._call_llm", return_value=expected)
        result = generate_test_node(_state())
        assert result["test_code"] == expected

    def test_strips_markdown_fences(self, mocker):
        mocker.patch(
            "app.agent.nodes._call_llm",
            return_value="```python\ndef test_add(): pass\n```",
        )
        result = generate_test_node(_state())
        assert "```" not in result["test_code"]
        assert "def test_add" in result["test_code"]

    def test_strips_plain_fences(self, mocker):
        mocker.patch(
            "app.agent.nodes._call_llm",
            return_value="```\ndef test_add(): pass\n```",
        )
        result = generate_test_node(_state())
        assert "```" not in result["test_code"]


# ── run_test_node ─────────────────────────────────────────────────────────────

class TestRunTestNode:
    def test_sets_passed_true_on_success(self, mocker):
        mocker.patch(
            "app.agent.nodes.run_test_in_sandbox",
            return_value=SandboxResult(passed=True, stdout="1 passed", stderr="", exit_code=0),
        )
        result = run_test_node(_state(test_code="def test_x(): pass", attempt_count=1))
        assert result["test_passed"] is True
        assert result["test_stdout"] == "1 passed"
        assert result["test_stderr"] == ""

    def test_sets_passed_false_on_failure(self, mocker):
        mocker.patch(
            "app.agent.nodes.run_test_in_sandbox",
            return_value=SandboxResult(passed=False, stdout="", stderr="AssertionError", exit_code=1),
        )
        result = run_test_node(_state(test_code="def test_x(): assert False", attempt_count=1))
        assert result["test_passed"] is False
        assert result["test_stderr"] == "AssertionError"


# ── fix_test_node ─────────────────────────────────────────────────────────────

class TestFixTestNode:
    def _failing_state(self, **overrides):
        defaults = dict(
            test_code="def test_add():\n    assert add(1, 2) == 99",
            test_stderr="AssertionError: assert 3 == 99",
            test_stdout="",
            test_passed=False,
            attempt_count=1,
        )
        defaults.update(overrides)
        return _state(**defaults)

    def test_increments_attempt_count(self, mocker):
        mocker.patch("app.agent.nodes._call_llm", return_value="def test_fixed(): pass")
        result = fix_test_node(self._failing_state())
        assert result["attempt_count"] == 2

    def test_strips_fences_from_fixed_test(self, mocker):
        mocker.patch(
            "app.agent.nodes._call_llm",
            return_value="```python\ndef test_fixed(): pass\n```",
        )
        result = fix_test_node(self._failing_state())
        assert "```" not in result["test_code"]

    def test_prefers_stderr_over_stdout_as_error_output(self, mocker):
        llm_call = mocker.patch("app.agent.nodes._call_llm", return_value="def test_fixed(): pass")
        fix_test_node(self._failing_state(test_stderr="stderr error", test_stdout="stdout content"))
        _, user_prompt = llm_call.call_args[0]
        assert "stderr error" in user_prompt
        assert "stdout content" not in user_prompt

    def test_falls_back_to_stdout_when_stderr_empty(self, mocker):
        llm_call = mocker.patch("app.agent.nodes._call_llm", return_value="def test_fixed(): pass")
        fix_test_node(self._failing_state(test_stderr="", test_stdout="stdout only error"))
        _, user_prompt = llm_call.call_args[0]
        assert "stdout only error" in user_prompt

    def test_handles_both_none(self, mocker):
        llm_call = mocker.patch("app.agent.nodes._call_llm", return_value="def test_fixed(): pass")
        fix_test_node(self._failing_state(test_stderr=None, test_stdout=None))
        _, user_prompt = llm_call.call_args[0]
        # Should not raise; error_output defaults to ""
        assert user_prompt is not None


# ── finalize_node ─────────────────────────────────────────────────────────────

class TestFinalizeNode:
    def test_success_when_test_passed(self):
        result = finalize_node(_state(test_passed=True, attempt_count=1))
        assert result["final_status"] == "success"

    def test_failed_max_attempts_when_not_passed(self):
        result = finalize_node(_state(test_passed=False, attempt_count=3))
        assert result["final_status"] == "failed_max_attempts"


# ── should_retry ──────────────────────────────────────────────────────────────

class TestShouldRetry:
    def test_returns_finalize_when_passed(self):
        assert should_retry(_state(test_passed=True, attempt_count=1)) == "finalize"

    def test_returns_fix_test_when_failed_and_attempts_remain(self):
        assert should_retry(_state(test_passed=False, attempt_count=1, max_attempts=3)) == "fix_test"

    def test_returns_finalize_at_max_attempts(self):
        assert should_retry(_state(test_passed=False, attempt_count=3, max_attempts=3)) == "finalize"

    def test_returns_finalize_when_attempt_count_exceeds_max(self):
        # Defensive: should not happen in normal flow but must not infinite-loop
        assert should_retry(_state(test_passed=False, attempt_count=5, max_attempts=3)) == "finalize"
