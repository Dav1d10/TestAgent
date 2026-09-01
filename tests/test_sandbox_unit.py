import subprocess

from app.tools.sandbox import (
    SANDBOX_STARTED_MARKER,
    SandboxOutcome,
    run_test_in_sandbox,
)


SOURCE = "def value():\n    return 1\n"
TEST = "from target_module import value\n\ndef test_value():\n    assert value() == 1\n"


def _completed(returncode, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=["docker", "run"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def test_started_container_with_pytest_success_is_passed(mocker):
    mocker.patch(
        "app.tools.sandbox.subprocess.run",
        return_value=_completed(0, f"{SANDBOX_STARTED_MARKER}\n1 passed\n"),
    )

    result = run_test_in_sandbox(SOURCE, TEST)

    assert result.outcome == SandboxOutcome.PASSED
    assert result.passed is True
    assert SANDBOX_STARTED_MARKER not in result.stdout
    assert result.stdout == "1 passed\n"


def test_started_container_with_pytest_failure_is_test_failed(mocker):
    mocker.patch(
        "app.tools.sandbox.subprocess.run",
        return_value=_completed(1, f"{SANDBOX_STARTED_MARKER}\n1 failed\n"),
    )

    result = run_test_in_sandbox(SOURCE, TEST)

    assert result.outcome == SandboxOutcome.TEST_FAILED
    assert result.passed is False


def test_docker_failure_before_container_start_is_infrastructure_error(mocker):
    mocker.patch(
        "app.tools.sandbox.subprocess.run",
        return_value=_completed(1, stderr="Cannot connect to the Docker daemon"),
    )

    result = run_test_in_sandbox(SOURCE, TEST)

    assert result.outcome == SandboxOutcome.INFRASTRUCTURE_ERROR
    assert result.stderr == "Cannot connect to the Docker daemon"


def test_missing_docker_executable_is_infrastructure_error(mocker):
    mocker.patch(
        "app.tools.sandbox.subprocess.run",
        side_effect=FileNotFoundError("docker executable not found"),
    )

    result = run_test_in_sandbox(SOURCE, TEST)

    assert result.outcome == SandboxOutcome.INFRASTRUCTURE_ERROR
    assert "docker executable not found" in result.stderr


def test_non_pytest_exit_after_start_is_infrastructure_error(mocker):
    mocker.patch(
        "app.tools.sandbox.subprocess.run",
        return_value=_completed(
            127,
            f"{SANDBOX_STARTED_MARKER}\n",
            "pytest: not found",
        ),
    )

    result = run_test_in_sandbox(SOURCE, TEST)

    assert result.outcome == SandboxOutcome.INFRASTRUCTURE_ERROR


def test_timeout_remains_distinct_from_infrastructure_error(mocker):
    run = mocker.patch(
        "app.tools.sandbox.subprocess.run",
        side_effect=[subprocess.TimeoutExpired(cmd=["docker", "run"], timeout=1), _completed(0)],
    )

    result = run_test_in_sandbox(SOURCE, TEST, timeout_seconds=1)

    assert result.outcome == SandboxOutcome.TIMED_OUT
    assert result.timed_out is True
    assert run.call_count == 2
