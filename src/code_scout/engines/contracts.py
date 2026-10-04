from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol

from code_scout.evidence.assessment import (
    EvidenceAssessment,
    assess_legacy_evidence,
)

if TYPE_CHECKING:
    from code_scout.engines.availability import ProviderAvailability


@dataclass(frozen=True)
class RuntimeOptions:
    """Provider-independent limits for one exploration request.

    `timeout_seconds` is `None` unless the caller explicitly overrides it
    (e.g. via `--timeout-seconds`); `None` means "defer to configured model
    target timeouts" rather than "no timeout".
    """

    timeout_seconds: float | None = None
    max_turns: int = 4
    max_turns_override: int | None = None
    max_tokens: int = 4096

    def __post_init__(self) -> None:
        if self.timeout_seconds is not None and not 0 < self.timeout_seconds <= 3600:
            raise ValueError("timeout_seconds must be between 0 and 3600.")
        if not 1 <= self.max_turns <= 100:
            raise ValueError("max_turns must be between 1 and 100.")
        if (
            self.max_turns_override is not None
            and not 1 <= self.max_turns_override <= 100
        ):
            raise ValueError("max_turns_override must be between 1 and 100.")
        if not 1 <= self.max_tokens <= 1_000_000:
            raise ValueError("max_tokens must be between 1 and 1000000.")


@dataclass(frozen=True)
class CodeScoutRequest:
    """A workspace exploration request independent of any provider."""

    query: str
    workspace: Path
    engine: str
    runtime: RuntimeOptions = field(default_factory=RuntimeOptions)

    def __post_init__(self) -> None:
        if not self.query.strip():
            raise ValueError("query must not be empty.")
        if not self.engine.strip():
            raise ValueError("engine must not be empty.")

        object.__setattr__(self, "workspace", Path(self.workspace).resolve())


@dataclass(frozen=True)
class SupportingEvidence:
    """Locally identified evidence; never populated from model-authored labels."""

    evidence_id: str | None = None
    assessment: EvidenceAssessment = field(
        default_factory=lambda: assess_legacy_evidence({})
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "assessment": self.assessment.to_dict(),
        }


@dataclass(frozen=True)
class SourceAssessment:
    """Local status of a citation's retained source and live workspace bytes.

    ``historical`` is deliberately separate from ``freshness``: a retained
    source can remain verifiable after its live path has changed or disappeared.
    The values are strings so this additive result stays straightforward for
    existing JSON consumers.
    """

    historical: str = "unknown"
    freshness: str = "unknown"
    reasons: tuple[str, ...] = ("source-unbound",)

    def to_dict(self) -> dict[str, object]:
        return {
            "historical": self.historical,
            "freshness": self.freshness,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class Citation:
    """A source location with optional, locally bound evidence assessments.

    Location validation is independent of these per-evidence assessments.
    Provider adapters must ignore model-authored supporting evidence.
    """

    path: str
    start_line: int
    end_line: int | None = None
    supporting_evidence: tuple[SupportingEvidence, ...] = (SupportingEvidence(),)
    repository_id: str | None = None
    source_revision_id: str | None = None
    content_sha256: str | None = None
    source_assessment: SourceAssessment = field(default_factory=SourceAssessment)

    def __post_init__(self) -> None:
        end_line = self.start_line if self.end_line is None else self.end_line
        object.__setattr__(self, "path", self.path.replace("\\", "/"))
        object.__setattr__(self, "end_line", end_line)

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "supporting_evidence": [
                item.to_dict() for item in self.supporting_evidence
            ],
            "repository_id": self.repository_id,
            "source_revision_id": self.source_revision_id,
            "content_sha256": self.content_sha256,
            "source_assessment": self.source_assessment.to_dict(),
        }


@dataclass(frozen=True)
class EngineWarning:
    """A structured, printable non-fatal engine diagnostic."""

    code: str
    message: str

    def __post_init__(self) -> None:
        if not self.code.strip():
            raise ValueError("warning code must not be empty.")
        if not self.message.strip():
            raise ValueError("warning message must not be empty.")


@dataclass(frozen=True)
class EngineMetadata:
    """Stable engine and provider identity returned with each result."""

    name: str
    provider: str
    provider_version: str | None = None
    model: str | None = None
    requires_evidence: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("engine name must not be empty.")
        if not self.provider.strip():
            raise ValueError("provider must not be empty.")


@dataclass(frozen=True)
class TimingMetadata:
    """Wall-clock timing recorded by an exploration engine."""

    duration_seconds: float
    started_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.duration_seconds < 0:
            raise ValueError("duration_seconds must not be negative.")


@dataclass(frozen=True)
class OpenAIModelTarget:
    """Configuration shared by OpenAI Chat Completions-compatible engines."""

    base_url: str
    model: str
    api_key_env: str | None = None
    timeout_seconds: float = 300.0
    max_tokens: int = 4096
    parameters: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.base_url.strip():
            raise ValueError("base_url must not be empty.")
        if not self.model.strip():
            raise ValueError("model must not be empty.")
        if self.api_key_env is not None and not self.api_key_env.strip():
            raise ValueError("api_key_env must not be empty when provided.")
        if not 0 < self.timeout_seconds <= 3600:
            raise ValueError("timeout_seconds must be between 0 and 3600.")
        if not 1 <= self.max_tokens <= 1_000_000:
            raise ValueError("max_tokens must be between 1 and 1000000.")
        reserved = {"model", "messages", "max_tokens"}.intersection(self.parameters)
        if reserved:
            names = ", ".join(sorted(reserved))
            raise ValueError(
                f"parameters must not override managed request fields: {names}."
            )
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))


def resolve_timeout_seconds(
    runtime_override: float | None, *configured: float
) -> float:
    """Resolve the effective request timeout.

    `configured` values come from global/local model target config and act
    as a ceiling. An explicit `runtime_override` (e.g. `--timeout-seconds`)
    can only tighten that ceiling, never raise it; leaving it unset (`None`)
    defers entirely to the smallest configured timeout.
    """

    values = list(configured)
    if runtime_override is not None:
        values.append(runtime_override)
    return min(values)


@dataclass(frozen=True)
class CodeScoutResult:
    """Provider-neutral answer with evidence and execution metadata."""

    answer: str
    citations: tuple[Citation, ...]
    warnings: tuple[EngineWarning, ...]
    engine: EngineMetadata
    timing: TimingMetadata
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.answer.strip():
            raise ValueError("answer must not be empty.")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def verified(self) -> bool:
        """Legacy citation-presence flag, not corroboration of the answer."""
        return bool(self.citations)


class Engine(Protocol):
    """Typed boundary implemented by every exploration provider."""

    @property
    def metadata(self) -> EngineMetadata: ...

    def availability(self, *, probe_endpoint: bool = False) -> ProviderAvailability: ...

    def execute(self, request: CodeScoutRequest) -> CodeScoutResult: ...
