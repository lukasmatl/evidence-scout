from __future__ import annotations

from pathlib import Path

import pytest

from code_scout.core.errors import ConfigError
from code_scout.verification import (
    GateOutcome,
    GateResult,
    MemberRole,
    ResultInputs,
    VerificationOutcome,
    WorkspaceMember,
    parse_verification_policy,
    reduce_result,
    validate_profile_members,
)


def test_reducer_ignores_misleading_output_and_optional_failure() -> None:
    result = reduce_result(
        ResultInputs(
            gates=(
                GateResult(id="unit", outcome=GateOutcome.PASS),
                GateResult(
                    id="advisory",
                    required=False,
                    outcome=GateOutcome.FAIL,
                ),
            )
        )
    )

    assert result.outcome is VerificationOutcome.VERIFIED
    assert result.required_gate_pass_count == 1


@pytest.mark.parametrize(
    ("inputs", "expected"),
    [
        (
            ResultInputs(gates=(GateResult(id="unit", outcome=GateOutcome.TIMEOUT),)),
            VerificationOutcome.FAILED,
        ),
        (
            ResultInputs(gates=(GateResult(id="unit", evidence_complete=False),)),
            VerificationOutcome.ERROR,
        ),
        (
            ResultInputs(
                gates=(
                    GateResult(id="unit", outcome=GateOutcome.FAIL),
                    GateResult(id="interrupted", outcome=GateOutcome.CANCELLED),
                )
            ),
            VerificationOutcome.ERROR,
        ),
        (
            ResultInputs(
                gates=(GateResult(id="unit", outcome=GateOutcome.FAIL),),
                candidate_drift=True,
            ),
            VerificationOutcome.INVALIDATED,
        ),
        (
            ResultInputs(gates=(GateResult(id="unit", outcome=GateOutcome.NOT_RUN),)),
            VerificationOutcome.INCOMPLETE,
        ),
        (ResultInputs(), VerificationOutcome.INCOMPLETE),
    ],
)
def test_reducer_precedence(
    inputs: ResultInputs, expected: VerificationOutcome
) -> None:
    assert reduce_result(inputs).outcome is expected


def test_parse_policy_and_bind_members(tmp_path: Path) -> None:
    (tmp_path / "api").mkdir()
    policy = parse_verification_policy(
        """
schema_version = 1
[profiles.default.repositories.api]
protected_paths = ["pyproject.toml"]
input_paths = ["uv.lock"]
[[profiles.default.steps]]
id = "api-tests"
scope = "repository"
repository = "api"
argv = ["uv", "run", "pytest"]
"""
    )

    validate_profile_members(
        policy.profiles["default"],
        (
            WorkspaceMember(
                id="api", root=str(tmp_path / "api"), role=MemberRole.EDITABLE
            ),
        ),
        workspace_root=tmp_path,
    )


@pytest.mark.parametrize(
    "toml",
    [
        "schema_version = 1\n[profiles.default.repositories.api]\n",
        """schema_version = 1
[profiles.default]
unknown = true
[profiles.default.repositories.api]
[[profiles.default.steps]]
id='x'
scope='repository'
repository='api'
argv=['echo']
required=false
""",
        """schema_version = 1
[profiles.default.repositories.api]
[[profiles.default.steps]]
id='bad id'
scope='repository'
repository='api'
argv=[]
""",
        """schema_version = 1
[profiles.default.repositories.api]
[[profiles.default.steps]]
id='workspace'
scope='workspace'
repositories=[]
argv=['echo']
""",
        """schema_version = 1
[profiles.default.repositories.api]
[[profiles.default.steps]]
id='escape'
scope='repository'
repository='api'
argv=['../tool']
""",
    ],
)
def test_policy_rejects_invalid_profiles(toml: str) -> None:
    with pytest.raises(ConfigError):
        parse_verification_policy(toml)


def test_policy_rejects_workspace_input_owned_by_member(tmp_path: Path) -> None:
    (tmp_path / "api").mkdir()
    policy = parse_verification_policy(
        """
schema_version = 1
[profiles.default]
workspace_input_paths = ["api/config.toml"]
[profiles.default.repositories.api]
[[profiles.default.steps]]
id = "api-tests"
scope = "repository"
repository = "api"
argv = ["pytest"]
"""
    )
    with pytest.raises(ConfigError):
        validate_profile_members(
            policy.profiles["default"],
            (WorkspaceMember(id="api", root=str(tmp_path / "api")),),
            workspace_root=tmp_path,
        )
