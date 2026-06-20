import pytest
from app.tools.sandbox import run_test_in_sandbox

pytestmark = pytest.mark.docker

# ── Fixtures ──────────────────────────────────────────────────────────────────

PASSING_SOURCE = """\
def add(a, b):
    return a + b
"""

PASSING_TEST = """\
from target_module import add

def test_add():
    assert add(1, 2) == 3
"""

FAILING_TEST = """\
from target_module import add

def test_add_wrong():
    assert add(1, 2) == 99
"""

INFINITE_LOOP_SOURCE = """\
def loop():
    while True:
        pass
"""

INFINITE_LOOP_TEST = """\
from target_module import loop

def test_loop():
    loop()
"""

NETWORK_SOURCE = """\
import socket

def check_network():
    s = socket.socket()
    s.connect(("8.8.8.8", 80))
    return True
"""

NETWORK_TEST = """\
from target_module import check_network

def test_network():
    assert check_network()
"""

# ── Tests ─────────────────────────────────────────────────────────────────────

def test_passing_test_returns_passed_true():
    result = run_test_in_sandbox(PASSING_SOURCE, PASSING_TEST, module_name="target_module")
    assert result.passed is True
    assert result.exit_code == 0


def test_failing_assertion_returns_passed_false():
    result = run_test_in_sandbox(PASSING_SOURCE, FAILING_TEST, module_name="target_module")
    assert result.passed is False
    assert result.exit_code != 0


def test_timeout_kills_container():
    result = run_test_in_sandbox(
        INFINITE_LOOP_SOURCE,
        INFINITE_LOOP_TEST,
        module_name="target_module",
        timeout_seconds=4,
    )
    assert result.timed_out is True
    assert result.passed is False


def test_network_access_is_blocked():
    result = run_test_in_sandbox(NETWORK_SOURCE, NETWORK_TEST, module_name="target_module")
    assert result.passed is False
