"""Recursive verification over one frozen evidence pack.

The optional RLM provider is kept behind :class:`RLMBoundary`.  The native
rawwerks/rlm adapter can use its local REPL or an explicitly configured provider
environment; Docker is optional.
"""

from __future__ import annotations

import importlib
import inspect
import json
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from time import monotonic
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, Protocol
from uuid import uuid4

from code_scout.core.errors import CodeScoutError
from code_scout.engines.availability import (
    AvailabilityState,
    ProviderAvailability,
    check_provider_availability,
)
from code_scout.engines.citations import validate_citations
from code_scout.engines.contracts import (
    Citation,
    CodeScoutRequest,
    CodeScoutResult,
    EngineMetadata,
    EngineWarning,
    OpenAIModelTarget,
    SupportingEvidence,
    TimingMetadata,
    resolve_timeout_seconds,
)
from code_scout.engines.openai_compat import (
    OpenAIChatMessage,
    OpenAIChatRequest,
    complete_chat,
)
from code_scout.evidence.assessment import (
    EvidenceAssessment,
    assess_legacy_evidence,
    combine_path_assessments,
)
from code_scout.evidence.pack import (
    EvidencePack,
    PackedSourceProvenance,
    compile_evidence_pack,
)
from code_scout.evidence.tools import EvidenceTools
from code_scout.workspace.paths import (
    VERIFY_TRAJECTORY_DIRECTORY,
    workspace_state_path,
)

if TYPE_CHECKING:
    from code_scout.runtime.config import VerifyModelConfig

RLM_PROVIDER_PACKAGE = "rlm"
_UNAUTHENTICATED_API_KEY = "not-required"
APPROVED_EVIDENCE_TOOLS = (
    "search_evidence",
    "read_source_range",
    "inspect_symbol",
    "traverse_neighbors",
    "list_callers",
    "list_callees",
    "find_definitions",
    "find_implementations",
    "find_references",
    "symbol_overview",
    "inspect_review_items",
    "inspect_impact_paths",
)
_SENSITIVE_PARTS = ("api_key", "authorization", "password", "secret", "token")
_STRONG_PROVIDERS = {"lsp", "graphify"}
_RLM_STRUCTURED_SYSTEM_PROMPT = """
You are a recursive verification controller. The frozen evidence pack is available
as the Python variable `context` in a persistent REPL. Inspect it with short
```repl``` blocks. You may call `llm_query(prompt)` or
`llm_query_batched(prompts)` only when the context exceeds 50,000 characters;
analyze smaller evidence packs directly to avoid unnecessary endpoint calls.

Answer only from evidence identifiers and source locations present in `context`.
The root prompt defines the required JSON object. Before finishing:
1. Create that object as a Python dict named `final_response` in a REPL block.
2. Run `import json` and assign `final_json = json.dumps(final_response)`.
3. In your next response output only `FINAL_VAR(final_json)`.

Do not return reasoning as the final answer. Do not pass a dict directly to FINAL
or FINAL_VAR. If evidence is insufficient, return the same JSON shape with no
claims and a precise unresolved question.
""".strip()
_EVERY_CALLER_TERMS = (
    "every caller",
    "all caller",
    "all call site",
    "every call site",
    "who calls",
)
RLM_RESPONSE_INSTRUCTIONS = (
    "Return RLMResponse claims only. Each RLMClaim must include claim text, "
    "supporting evidence_ids, claim-bound citations, confidence from 0 to 1, "
    "any contradicting_evidence_ids, and unresolved_questions. Provider prose "
    "is diagnostic and will not be rendered as verified output."
)


class VerifyError(CodeScoutError):
    """The recursive verifier could not safely complete its investigation."""


class RLMEndpointError(Exception):
    """A redaction-safe marker for root or sub-model endpoint failures."""


@dataclass(frozen=True)
class VerifyBudgets:
    max_iterations: int = 40
    max_sub_calls: int = 16
    max_output_characters: int = 20_000
    timeout_seconds: float = 300.0
    max_total_tokens: int = 64_000

    def __post_init__(self) -> None:
        values = asdict(self)
        for name, value in values.items():
            if value <= 0:
                raise ValueError(f"{name} must be greater than zero")
        if self.max_iterations > 100:
            raise ValueError("max_iterations must not exceed 100")
        if self.max_sub_calls > 1_000:
            raise ValueError("max_sub_calls must not exceed 1000")
        if self.max_output_characters > 1_000_000:
            raise ValueError("max_output_characters must not exceed 1000000")
        if self.timeout_seconds > 3_600:
            raise ValueError("timeout_seconds must not exceed 3600")
        if self.max_total_tokens > 10_000_000:
            raise ValueError("max_total_tokens must not exceed 10000000")


@dataclass(frozen=True)
class RLMIsolation:
    kind: str
    image: str | None = None

    def __post_init__(self) -> None:
        normalized = self.kind.strip().casefold()
        if not normalized:
            raise ValueError("isolation kind must not be empty")
        object.__setattr__(self, "kind", normalized)
        if self.image is not None and not self.image.strip():
            raise ValueError("isolation image must not be empty")


@dataclass(frozen=True)
class RLMModelRoute:
    target: OpenAIModelTarget
    api_key: str | None = field(repr=False)


EvidenceTool = Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class RLMRequest:
    """Compatibility request supplied to an official or adapted RLM provider."""

    question: str
    external_context: str
    tools: Mapping[str, EvidenceTool]
    root_model: RLMModelRoute
    sub_model: RLMModelRoute
    isolation: RLMIsolation
    budgets: VerifyBudgets
    response_instructions: str = RLM_RESPONSE_INSTRUCTIONS

    def __post_init__(self) -> None:
        object.__setattr__(self, "tools", MappingProxyType(dict(self.tools)))


@dataclass(frozen=True)
class RLMClaim:
    """One untrusted structured assertion returned by the recursive model."""

    text: str
    evidence_ids: tuple[str, ...]
    citations: tuple[Citation, ...]
    confidence: float
    contradicting_evidence_ids: tuple[str, ...] = ()
    unresolved_questions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("RLM claim text must not be empty")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("RLM claim confidence must be between 0 and 1")
        if any(not value.strip() for value in self.evidence_ids):
            raise ValueError("RLM claim evidence identifiers must not be empty")
        if any(not value.strip() for value in self.contradicting_evidence_ids):
            raise ValueError("RLM contradicting evidence identifiers must not be empty")
        if any(not value.strip() for value in self.unresolved_questions):
            raise ValueError("RLM unresolved questions must not be empty")


@dataclass(frozen=True)
class RLMResponse:
    """Structured provider result; free-form ``answer`` is diagnostic only."""

    answer: str
    citations: tuple[Citation, ...] = ()
    claims: tuple[RLMClaim, ...] = ()
    unresolved_questions: tuple[str, ...] = ()
    iterations: int = 1
    sub_calls: int = 0
    output_characters: int | None = None
    total_tokens: int = 0
    events: tuple[Mapping[str, object], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.answer, str) or not self.answer.strip():
            raise ValueError("RLM answer must not be empty")
        if any(not question.strip() for question in self.unresolved_questions):
            raise ValueError("RLM unresolved questions must not be empty")
        structured_characters = (
            len(self.answer)
            + sum(
                len(claim.text)
                + sum(map(len, claim.evidence_ids))
                + sum(map(len, claim.contradicting_evidence_ids))
                + sum(map(len, claim.unresolved_questions))
                for claim in self.claims
            )
            + sum(map(len, self.unresolved_questions))
        )
        if self.output_characters is None:
            object.__setattr__(self, "output_characters", structured_characters)
        else:
            object.__setattr__(
                self,
                "output_characters",
                max(self.output_characters, structured_characters),
            )
        for name in ("iterations", "sub_calls", "output_characters", "total_tokens"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must not be negative")


class RLMBoundary(Protocol):
    """Compatible recursive-controller boundary used by tests and providers."""

    def run(self, request: RLMRequest) -> RLMResponse: ...


class RLMCLIAdapter:
    """Adapter matching rawwerks/rlm-cli's direct ``rlm.RLM`` integration.

    The pinned CLI constructs an RLM with an OpenAI-compatible backend and calls
    ``completion(prompt=context_payload, root_prompt=question)``.  We use that
    contract directly, add the independent sub-model route, and translate the
    JSON result into code-scout's evidence-bound response types.

    Older provider packages exposing ``create_code_scout_controller`` remain
    supported as a compatibility fallback.
    """

    def run(self, request: RLMRequest) -> RLMResponse:
        module = self._load_module()
        rlm_class = getattr(module, "RLM", None)
        if callable(rlm_class):
            return self._run_native(rlm_class, request)
        return self._run_legacy_controller(request)

    @staticmethod
    def _load_module() -> object:
        try:
            return importlib.import_module(RLM_PROVIDER_PACKAGE)
        except (ImportError, ModuleNotFoundError) as error:
            raise VerifyError(
                "The optional RLM provider is not installed.",
                "Install the rawwerks/rlm provider used by rlm-cli.",
            ) from error

    def _run_native(
        self, rlm_class: Callable[..., object], request: RLMRequest
    ) -> RLMResponse:
        root_kwargs = _rlm_backend_kwargs(request.root_model)
        sub_kwargs = _rlm_backend_kwargs(request.sub_model)
        environment_kwargs: dict[str, object] = {}
        if request.isolation.kind == "docker" and request.isolation.image:
            environment_kwargs["image"] = request.isolation.image

        init_kwargs: dict[str, object] = {
            "backend": "openai",
            "backend_kwargs": root_kwargs,
            "environment": request.isolation.kind,
            "environment_kwargs": environment_kwargs,
            "max_iterations": request.budgets.max_iterations,
            "max_depth": 1,
            "other_backends": ["openai"],
            "other_backend_kwargs": [sub_kwargs],
            "custom_system_prompt": _rlm_system_prompt(),
            # Newer rawwerks/rlm versions accept these limits; the pinned version
            # does not, so signature filtering preserves revision compatibility.
            "max_timeout": request.budgets.timeout_seconds,
            "max_tokens": request.budgets.max_total_tokens,
        }
        try:
            instance = rlm_class(**_filter_callable_kwargs(rlm_class, init_kwargs))
            completion = instance.completion(
                prompt=_rlm_context_payload(request.external_context),
                root_prompt=_rlm_root_prompt(request),
            )
        except Exception as error:
            return self._run_structured_fallback(
                request,
                reason=f"rlm-error:{type(error).__name__}",
                source_error=error,
            )

        raw_answer = getattr(completion, "response", completion)
        if _json_object(str(raw_answer)) is None:
            return self._run_structured_fallback(
                request,
                reason="rlm-invalid-structured-response",
            )
        return _parse_native_response(str(raw_answer), completion, request)

    @staticmethod
    def _run_structured_fallback(
        request: RLMRequest,
        *,
        reason: str,
        source_error: Exception | None = None,
    ) -> RLMResponse:
        messages = (
            OpenAIChatMessage(
                "system",
                "You verify claims against a frozen evidence pack. Return exactly "
                "one JSON object, with no Markdown or prose. Use only evidence IDs "
                "and source locations present in the pack.",
            ),
            OpenAIChatMessage(
                "user",
                "\n\n".join(
                    (
                        _rlm_root_prompt(request),
                        "Frozen evidence pack:",
                        request.external_context,
                    )
                ),
            ),
        )
        chat_request = OpenAIChatRequest.from_target(
            request.root_model.target,
            messages,
        )
        try:
            repaired = complete_chat(
                request.root_model.target,
                chat_request,
                api_key=request.root_model.api_key,
            )
        except Exception as fallback_error:
            cause = source_error or fallback_error
            raise RLMEndpointError(
                "RLM completion and structured fallback failed (details redacted)."
            ) from cause

        response = _parse_native_response(repaired.content, repaired, request)
        return replace(
            response,
            events=(
                *response.events,
                {"kind": "rlm-structured-fallback", "reason": reason},
            ),
        )

    def _run_legacy_controller(self, request: RLMRequest) -> RLMResponse:
        factory = self._load_factory()
        controller = factory(
            isolation={
                "kind": request.isolation.kind,
                "image": request.isolation.image,
            },
            allow_host_execution=request.isolation.kind == "local",
        )
        response = controller.run(request)
        if not isinstance(response, RLMResponse):
            raise VerifyError(
                "The optional RLM adapter returned an incompatible response.",
                "Install rawwerks/rlm or a legacy adapter implementing the "
                "code-scout RLMRequest/RLMResponse boundary.",
            )
        return response

    @staticmethod
    def _load_factory() -> Callable[..., object]:
        module = RLMCLIAdapter._load_module()
        factory = getattr(module, "create_code_scout_controller", None)
        if factory is None:
            try:
                adapters = importlib.import_module("rlm.adapters")
            except (ImportError, ModuleNotFoundError):
                adapters = None
            factory = getattr(adapters, "create_code_scout_controller", None)
        if not callable(factory):
            raise VerifyError(
                "The installed RLM provider has no compatible code-scout adapter.",
                "Install the rawwerks/rlm revision used by rlm-cli.",
            )
        return factory


# Backwards-compatible public name used by earlier code-scout integrations.
OfficialRLMAdapter = RLMCLIAdapter


PackFactory = Callable[..., EvidencePack]
ToolsFactory = Callable[[Path, EvidencePack], EvidenceTools]


class VerifyEngine:
    metadata = EngineMetadata(
        name="verify",
        provider="rawwerks/rlm-cli adapter",
        # An evidence-free unresolved answer is a valid verification outcome.
        # Defensible claims are citation-validated inside this engine.
        requires_evidence=False,
    )

    def __init__(
        self,
        config: VerifyModelConfig,
        *,
        boundary: RLMBoundary | None = None,
        pack_factory: PackFactory = compile_evidence_pack,
        tools_factory: ToolsFactory = EvidenceTools,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.config = config
        self._boundary = boundary or RLMCLIAdapter()
        self._pack_factory = pack_factory
        self._tools_factory = tools_factory
        self._environment = environment

    def availability(self, *, probe_endpoint: bool = False) -> ProviderAvailability:
        isolation = self.config.isolation or "local"
        if isolation not in {"local", "docker"}:
            return ProviderAvailability(
                AvailabilityState.MISSING_ENDPOINT_CONFIGURATION,
                "verify isolation must be 'local' or 'docker'",
            )
        if isolation == "docker" and not self.config.isolation_image:
            return ProviderAvailability(
                AvailabilityState.MISSING_ENDPOINT_CONFIGURATION,
                "verify Docker isolation requires an image",
            )
        provider = check_provider_availability(
            target=self.config.root_model,
            dependency=RLM_PROVIDER_PACKAGE,
            executable="docker" if isolation == "docker" else None,
            probe_endpoint=probe_endpoint,
            environment=self._environment,
        )
        return provider

    def execute(self, request: CodeScoutRequest) -> CodeScoutResult:
        started_at = datetime.now(UTC)
        started = monotonic()
        isolation = self._isolation()
        trajectory = _trajectory_path(request.workspace, started_at)
        budgets = self._budgets(request)
        root = self._route(self.config.root_model, "verify.root_model")
        sub = self._route(self.config.sub_model, "verify.sub_model")
        pack = self._pack_factory(request.workspace, question=request.query)
        tools = self._tools_factory(request.workspace, pack)
        external_tools = {
            name: getattr(tools, name) for name in APPROVED_EVIDENCE_TOOLS
        }
        rlm_request = RLMRequest(
            question=request.query,
            external_context=pack.render(),
            tools=external_tools,
            root_model=root,
            sub_model=sub,
            isolation=isolation,
            budgets=budgets,
        )
        _append_jsonl(
            trajectory,
            {
                "event": "verification_start",
                "question": request.query,
                "external_context": {
                    "source": "frozen_evidence_pack",
                    "characters": len(rlm_request.external_context),
                    "truncated": pack.truncated,
                },
                "tools": list(external_tools),
                "root_model": _route_metadata(root),
                "sub_model": _route_metadata(sub),
                "isolation": asdict(isolation),
                "budgets": asdict(budgets),
            },
        )
        try:
            response = _run_with_timeout(
                self._boundary, rlm_request, budgets.timeout_seconds
            )
            self._enforce_budgets(response, budgets)
        except TimeoutError as error:
            _append_failure(trajectory, "timeout")
            raise VerifyError(
                "RLM verification exceeded its "
                f"{budgets.timeout_seconds:g}-second timeout.",
                f"Inspect the redacted trajectory at {trajectory}.",
            ) from error
        except RLMEndpointError as error:
            _append_failure(trajectory, "endpoint_error")
            raise VerifyError(
                "An RLM model endpoint failed during verification.",
                "Check both root and sub-model endpoint settings and the redacted "
                f"trajectory at {trajectory}.",
            ) from error
        except VerifyError:
            _append_failure(trajectory, "verification_error")
            raise
        except Exception as error:
            _append_failure(trajectory, "provider_error")
            raise VerifyError(
                "RLM verification failed.",
                f"Inspect the redacted trajectory at {trajectory}.",
            ) from error

        synthesis = _synthesize_response(request, response, pack)
        for event in response.events:
            _append_jsonl(trajectory, {"event": "provider_event", "data": event})
        _append_jsonl(
            trajectory,
            {
                "event": "verification_finish",
                "iterations": response.iterations,
                "sub_calls": response.sub_calls,
                "output_characters": response.output_characters,
                "total_tokens": response.total_tokens,
                "claims": len(response.claims),
                "classifications": dict(synthesis.counts),
                "citations": [asdict(item) for item in synthesis.citations],
            },
        )
        return CodeScoutResult(
            answer=synthesis.answer,
            citations=synthesis.citations,
            warnings=synthesis.warnings,
            engine=replace(self.metadata, model=self.config.root_model.model),
            timing=TimingMetadata(monotonic() - started, started_at),
            metadata={
                "trajectory_path": str(trajectory),
                "root_model": self.config.root_model.model,
                "sub_model": self.config.sub_model.model,
                "iterations": response.iterations,
                "sub_calls": response.sub_calls,
                "total_tokens": response.total_tokens,
                "evidence_pack_characters": len(pack.render()),
                "claim_classifications": dict(synthesis.counts),
                "caller_coverage": dict(synthesis.caller_coverage),
                "supporting_evidence": [
                    item.to_dict() for item in synthesis.supporting_evidence
                ],
            },
        )

    def _isolation(self) -> RLMIsolation:
        isolation = RLMIsolation(
            self.config.isolation or "local", self.config.isolation_image
        )
        if isolation.kind not in {"local", "docker"}:
            raise VerifyError(
                f"Unsupported RLM environment: {isolation.kind}.",
                "Configure engines.verify isolation as 'local' or 'docker'.",
            )
        if isolation.kind == "docker" and not isolation.image:
            raise VerifyError(
                "RLM Docker verification has no configured image.",
                "Set engines.verify.isolation_image or use isolation = 'local'.",
            )
        return isolation

    def _budgets(self, request: CodeScoutRequest) -> VerifyBudgets:
        return VerifyBudgets(
            max_iterations=min(self.config.max_iterations, request.runtime.max_turns),
            max_sub_calls=self.config.max_sub_calls,
            max_output_characters=self.config.max_output_characters,
            timeout_seconds=resolve_timeout_seconds(
                request.runtime.timeout_seconds,
                self.config.timeout_seconds,
                self.config.root_model.timeout_seconds,
                self.config.sub_model.timeout_seconds,
            ),
            max_total_tokens=self.config.max_total_tokens,
        )

    def _route(self, target: OpenAIModelTarget, name: str) -> RLMModelRoute:
        from code_scout.runtime.config import resolve_model_target

        resolved = resolve_model_target(
            target, name=name, environment=self._environment
        )
        return RLMModelRoute(resolved.target, resolved.api_key)

    @staticmethod
    def _enforce_budgets(response: RLMResponse, budgets: VerifyBudgets) -> None:
        checks = (
            ("iteration", response.iterations, budgets.max_iterations),
            ("sub-call", response.sub_calls, budgets.max_sub_calls),
            (
                "output-character",
                response.output_characters,
                budgets.max_output_characters,
            ),
            ("total-token", response.total_tokens, budgets.max_total_tokens),
        )
        for label, used, limit in checks:
            if used > limit:
                raise VerifyError(
                    f"RLM verification exceeded its {label} budget ({used} > {limit}).",
                    "Narrow the question or increase the bounded verify setting.",
                )


ClaimClassification = Literal["confirmed", "inferred", "contradicted", "unresolved"]


@dataclass(frozen=True)
class _EvidenceRecord:
    sources: tuple[PackedSourceProvenance, ...]
    strong: bool
    stale: bool = False
    unresolved: bool = False
    assessment: EvidenceAssessment = field(
        default_factory=lambda: assess_legacy_evidence({})
    )


@dataclass(frozen=True)
class _ClassifiedClaim:
    claim: RLMClaim
    classification: ClaimClassification
    citations: tuple[Citation, ...]
    reason: str | None = None
    supporting_evidence: tuple[SupportingEvidence, ...] = ()


@dataclass(frozen=True)
class _Synthesis:
    answer: str
    citations: tuple[Citation, ...]
    warnings: tuple[EngineWarning, ...]
    counts: Mapping[str, int]
    caller_coverage: Mapping[str, str]
    supporting_evidence: tuple[SupportingEvidence, ...]


def _synthesize_response(
    request: CodeScoutRequest,
    response: RLMResponse,
    pack: EvidencePack,
) -> _Synthesis:
    catalog = _evidence_catalog(pack)
    classified: list[_ClassifiedClaim] = []
    warnings: list[EngineWarning] = []
    citations: list[Citation] = []
    seen_citations: set[Citation] = set()

    for index, claim in enumerate(response.claims, start=1):
        result, claim_warnings = _classify_claim(
            request.workspace, claim, catalog, index=index
        )
        result = replace(
            result,
            supporting_evidence=tuple(
                SupportingEvidence(value, catalog[value].assessment)
                if value in catalog
                else SupportingEvidence(value)
                for value in dict.fromkeys(
                    (*claim.evidence_ids, *claim.contradicting_evidence_ids)
                )
            ),
        )
        classified.append(result)
        warnings.extend(claim_warnings)
        for citation in result.citations:
            if citation not in seen_citations:
                seen_citations.add(citation)
                citations.append(citation)

    if response.citations:
        warnings.append(
            EngineWarning(
                "verify-unbound-citations-ignored",
                "Ignored top-level RLM citations because verify citations must be "
                "bound to a structured claim and evidence identifier.",
            )
        )
    if not response.claims:
        warnings.append(
            EngineWarning(
                "verify-no-structured-claims",
                "The RLM returned no structured claims; no provider prose was "
                "treated as a defensible answer.",
            )
        )

    unresolved_questions = list(response.unresolved_questions)
    for item in classified:
        unresolved_questions.extend(item.claim.unresolved_questions)
    coverage = _caller_coverage(request.query, pack)
    answer = _render_synthesis(classified, unresolved_questions, coverage)
    counts = {
        label: sum(item.classification == label for item in classified)
        for label in ("confirmed", "inferred", "contradicted", "unresolved")
    }
    return _Synthesis(
        answer,
        tuple(citations),
        tuple(warnings),
        MappingProxyType(counts),
        MappingProxyType(coverage),
        tuple(
            dict.fromkeys(
                support for item in classified for support in item.supporting_evidence
            )
        ),
    )


def _classify_claim(
    workspace: Path,
    claim: RLMClaim,
    catalog: Mapping[str, _EvidenceRecord],
    *,
    index: int,
) -> tuple[_ClassifiedClaim, tuple[EngineWarning, ...]]:
    warnings: list[EngineWarning] = []
    support = tuple(dict.fromkeys(claim.evidence_ids))
    contradiction = tuple(dict.fromkeys(claim.contradicting_evidence_ids))
    unknown = tuple(
        value for value in (*support, *contradiction) if value not in catalog
    )
    bound_citations = tuple(
        _bind_citation_to_frozen_pack(citation, catalog, (*support, *contradiction))
        or replace(citation, supporting_evidence=(SupportingEvidence(),))
        for citation in claim.citations
    )
    validation = validate_citations(workspace, bound_citations)
    warnings.extend(validation.warnings)

    if unknown:
        reason = "unknown evidence identifiers: " + ", ".join(unknown)
        warnings.append(_claim_warning(index, "unsupported-evidence", reason))
        return _ClassifiedClaim(claim, "unresolved", (), reason), tuple(warnings)
    if not support:
        reason = "no supporting evidence identifiers"
        warnings.append(_claim_warning(index, "unsupported-claim", reason))
        return _ClassifiedClaim(claim, "unresolved", (), reason), tuple(warnings)
    if not claim.citations:
        reason = "no claim-bound citations"
        warnings.append(_claim_warning(index, "missing-citation", reason))
        return _ClassifiedClaim(claim, "unresolved", (), reason), tuple(warnings)
    if len(validation.citations) != len(set(claim.citations)):
        reason = "one or more citations failed workspace validation"
        warnings.append(_claim_warning(index, "invalid-citation", reason))
        return _ClassifiedClaim(claim, "unresolved", (), reason), tuple(warnings)

    referenced = tuple(catalog[value] for value in (*support, *contradiction))
    unbound = tuple(
        citation
        for citation in validation.citations
        if not any(_citation_matches(citation, record) for record in referenced)
    )
    uncited_ids = tuple(
        evidence_id
        for evidence_id in (*support, *contradiction)
        if not any(
            _citation_matches(citation, catalog[evidence_id])
            for citation in validation.citations
        )
    )
    if unbound or uncited_ids:
        details = []
        if unbound:
            details.append("citations are not contained in referenced evidence")
        if uncited_ids:
            details.append("uncited evidence identifiers: " + ", ".join(uncited_ids))
        reason = "; ".join(details)
        warnings.append(_claim_warning(index, "evidence-citation-mismatch", reason))
        return _ClassifiedClaim(claim, "unresolved", (), reason), tuple(warnings)

    validation = replace(
        validation,
        citations=tuple(
            replace(
                citation,
                supporting_evidence=tuple(
                    SupportingEvidence(value, catalog[value].assessment)
                    for value in (*support, *contradiction)
                    if _citation_matches(citation, catalog[value])
                ),
            )
            for citation in validation.citations
        ),
    )

    if contradiction:
        return (
            _ClassifiedClaim(
                claim,
                "contradicted",
                validation.citations,
                "the evidence pack contains cited contradicting evidence",
            ),
            tuple(warnings),
        )

    support_records = tuple(catalog[value] for value in support)
    if any(
        record.unresolved or record.assessment.validation == "unresolved"
        for record in support_records
    ):
        reason = "supporting evidence is explicitly unresolved"
        return _ClassifiedClaim(
            claim, "unresolved", validation.citations, reason
        ), tuple(warnings)
    if claim.confidence >= 0.9 and all(
        record.strong
        and not record.stale
        and not record.assessment.conflicted
        and record.assessment.freshness != "stale"
        for record in support_records
    ):
        classification: ClaimClassification = "confirmed"
        reason = None
    else:
        classification = "inferred"
        reason = "provider confidence or evidence provenance is not confirmatory"
    return _ClassifiedClaim(claim, classification, validation.citations, reason), tuple(
        warnings
    )


def _evidence_catalog(pack: EvidencePack) -> dict[str, _EvidenceRecord]:
    stale_dede = any(
        warning.get("code", "").startswith("dede-stale") for warning in pack.warnings
    )
    catalog: dict[str, _EvidenceRecord] = {}
    for item in pack.evidence:
        source = PackedSourceProvenance(
            item.source_file,
            item.start_line,
            item.end_line,
            item.provenance,
            item.confidence,
            repository_id=item.repository_id,
            source_revision_id=item.source_revision_id,
            content_sha256=item.content_sha256,
        )
        provider = _provider_name(item.provenance)
        catalog[item.id] = _EvidenceRecord(
            (source,),
            provider in _STRONG_PROVIDERS and item.confidence >= 0.9,
            stale=provider == "dede" and stale_dede,
            assessment=assess_legacy_evidence({"assessment": item.assessment}),
        )
    for item in pack.relationships:
        providers = {_provider_name(value) for value in item.provenance}
        certain_dede = (
            "dede" in providers and (item.certainty or "").casefold() == "certain"
        )
        strong = item.confidence >= 0.9 and (
            bool(providers & _STRONG_PROVIDERS) or certain_dede
        )
        catalog[item.id] = _EvidenceRecord(
            item.source_provenance,
            strong,
            stale="dede" in providers and stale_dede,
            unresolved=item.unresolved,
            assessment=assess_legacy_evidence({"assessment": item.assessment}),
        )
    for item in pack.repository_map:
        # Relationship map entries intentionally reuse the packed relationship ID.
        # Preserve the relationship's exact source provenance instead of replacing
        # it with the map-only summary, which may have no source range.
        if item.id in catalog:
            continue
        sources = item.source_provenance
        if (
            item.path
            and item.start_line is not None
            and item.end_line is not None
            and not sources
        ):
            sources = (
                PackedSourceProvenance(
                    item.path, item.start_line, item.end_line, "unknown", None
                ),
            )
        providers = {_provider_name(source.extractor) for source in sources}
        certain_dede = (
            "dede" in providers and (item.certainty or "").casefold() == "certain"
        )
        catalog[item.id] = _EvidenceRecord(
            sources,
            bool(providers & _STRONG_PROVIDERS) or certain_dede,
            stale="dede" in providers and stale_dede,
            unresolved=item.unresolved,
            assessment=assess_legacy_evidence({"assessment": item.assessment}),
        )
    relationships = {item.id: item for item in pack.relationships}
    for item in pack.impact_paths:
        edges = tuple(
            relationships[edge] for edge in item.edge_ids if edge in relationships
        )
        sources = tuple(source for edge in edges for source in edge.source_provenance)
        catalog[item.id] = _EvidenceRecord(
            sources,
            item.certainty.casefold() == "certain" and not item.unresolved,
            stale=stale_dede,
            unresolved=item.unresolved,
            assessment=combine_path_assessments(
                assess_legacy_evidence({"assessment": edge.assessment})
                for edge in edges
            ),
        )
    return catalog


def _citation_matches(citation: Citation, record: _EvidenceRecord) -> bool:
    return any(
        citation.path == source.source_file
        and source.start_line is not None
        and source.end_line is not None
        and citation.start_line >= source.start_line
        and citation.end_line is not None
        and citation.end_line <= source.end_line
        for source in record.sources
    )


def _bind_citation_to_frozen_pack(
    citation: Citation,
    catalog: Mapping[str, _EvidenceRecord],
    evidence_ids: tuple[str, ...],
) -> Citation | None:
    """Resolve provider coordinates to exactly one source captured by this pack."""
    sources = {
        (
            source.repository_id,
            source.source_revision_id,
            source.content_sha256,
        )
        for evidence_id in evidence_ids
        for source in catalog.get(evidence_id, _EvidenceRecord((), False)).sources
        if citation.path == source.source_file
        and source.start_line is not None
        and source.end_line is not None
        and citation.start_line >= source.start_line
        and citation.end_line is not None
        and citation.end_line <= source.end_line
    }
    if len(sources) != 1:
        return None
    repository_id, source_revision_id, content_sha256 = next(iter(sources))
    return replace(
        citation,
        supporting_evidence=(SupportingEvidence(),),
        repository_id=repository_id,
        source_revision_id=source_revision_id,
        content_sha256=content_sha256,
    )


def _provider_name(value: str) -> str:
    return value.casefold().replace("-", "_")


def _claim_warning(index: int, suffix: str, reason: str) -> EngineWarning:
    return EngineWarning(
        f"verify-claim-{suffix}",
        f"Claim {index} was not defensible: {reason}.",
    )


def _caller_coverage(query: str, pack: EvidencePack) -> dict[str, str]:
    if not any(term in query.casefold() for term in _EVERY_CALLER_TERMS):
        return {}
    calls = tuple(
        item for item in pack.relationships if "call" in item.relation_type.casefold()
    )
    statuses: dict[str, str] = {}
    for provider in ("static", "lsp", "dede"):
        if provider == "static":
            covered = tuple(
                item
                for item in calls
                if any(_provider_name(value) != "dede" for value in item.provenance)
            )
        else:
            covered = tuple(
                item
                for item in calls
                if any(_provider_name(value) == provider for value in item.provenance)
            )
        if not covered:
            statuses[provider] = "unavailable"
        elif pack.truncated or any(
            item.unresolved or not item.source_provenance for item in covered
        ):
            statuses[provider] = "incomplete"
        elif provider == "dede" and any(
            warning.get("code", "").startswith("dede-") for warning in pack.warnings
        ):
            statuses[provider] = "incomplete"
        else:
            statuses[provider] = "complete"
    return statuses


def _render_synthesis(
    claims: list[_ClassifiedClaim],
    unresolved_questions: list[str],
    coverage: Mapping[str, str],
) -> str:
    lines: list[str] = []
    if coverage:
        lines.append("Caller coverage")
        lines.extend(f"- {provider}: {status}" for provider, status in coverage.items())
        lines.append("")
    for classification, title in (
        ("confirmed", "Confirmed"),
        ("inferred", "Inferred"),
        ("contradicted", "Contradicted"),
        ("unresolved", "Unresolved"),
    ):
        lines.append(title)
        selected = [item for item in claims if item.classification == classification]
        if not selected and not (
            classification == "unresolved" and (unresolved_questions or not claims)
        ):
            lines.append("- None.")
        for item in selected:
            evidence = ", ".join(item.claim.evidence_ids) or "none"
            suffix = f" Evidence: {evidence}; confidence: {item.claim.confidence:.0%}."
            for support in item.supporting_evidence:
                labels = ", ".join(support.assessment.labels)
                suffix += f" [{support.evidence_id}: {labels}]"
            if item.reason:
                suffix += f" {item.reason.capitalize()}."
            lines.append(f"- {item.claim.text}{suffix}")
        if classification == "unresolved":
            questions = tuple(dict.fromkeys(unresolved_questions))
            lines.extend(f"- Question: {question}" for question in questions)
            if not claims:
                lines.append("- No defensible claims were returned.")
        lines.append("")
    return "\n".join(lines).rstrip()


def _run_with_timeout(
    boundary: RLMBoundary, request: RLMRequest, timeout_seconds: float
) -> RLMResponse:
    results: Queue[tuple[bool, object]] = Queue(maxsize=1)

    def invoke() -> None:
        try:
            results.put((True, boundary.run(request)))
        except BaseException as error:
            results.put((False, error))

    Thread(target=invoke, name="code-scout-rlm", daemon=True).start()
    try:
        succeeded, value = results.get(timeout=timeout_seconds)
    except Empty as error:
        cancel = getattr(boundary, "cancel", None)
        if callable(cancel):
            cancel()
        raise TimeoutError from error
    if not succeeded:
        raise value  # type: ignore[misc]
    if not isinstance(value, RLMResponse):
        raise VerifyError(
            "The RLM boundary returned an invalid response.",
            "Use a compatible boundary returning RLMResponse.",
        )
    return value


def _rlm_backend_kwargs(route: RLMModelRoute) -> dict[str, object]:
    kwargs = dict(route.target.parameters)
    kwargs.update(
        {
            "base_url": route.target.base_url,
            "model_name": route.target.model,
        }
    )
    # The OpenAI Python client requires a non-empty credential even when an
    # OpenAI-compatible local endpoint such as Ollama ignores authentication.
    kwargs["api_key"] = route.api_key or _UNAUTHENTICATED_API_KEY
    return kwargs


def _filter_callable_kwargs(
    cls: Callable[..., object], kwargs: Mapping[str, object]
) -> dict[str, object]:
    try:
        signature = inspect.signature(cls.__init__)  # type: ignore[attr-defined]
    except (AttributeError, TypeError, ValueError):
        return dict(kwargs)
    if any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    ):
        return dict(kwargs)
    allowed = {name for name in signature.parameters if name != "self"}
    return {name: value for name, value in kwargs.items() if name in allowed}


def _rlm_context_payload(external_context: str) -> object:
    try:
        return json.loads(external_context)
    except (TypeError, ValueError):
        return external_context


def _rlm_root_prompt(request: RLMRequest) -> str:
    return "\n\n".join(
        (
            request.question,
            request.response_instructions,
            "Return exactly one JSON object (no Markdown fence) with this shape: "
            '{"answer":"diagnostic summary","claims":[{"text":"claim",'
            '"evidence_ids":["evidence:id"],"citations":[{"path":"src/file.py",'
            '"start_line":1,"end_line":2}],"confidence":0.9,'
            '"contradicting_evidence_ids":[],"unresolved_questions":[]}],'
            '"unresolved_questions":[]}. Use only identifiers and source locations '
            "present in the frozen evidence pack. Build this object as a Python "
            "dict, serialize it with json.dumps into final_json, and finish with "
            "FINAL_VAR(final_json) in a separate response.",
        )
    )


def _rlm_system_prompt() -> str:
    return _RLM_STRUCTURED_SYSTEM_PROMPT


def _parse_native_response(
    raw_answer: str, completion: object, request: RLMRequest
) -> RLMResponse:
    payload = _json_object(raw_answer)
    usage = _usage_totals(completion, request)
    events: tuple[Mapping[str, object], ...] = (
        {
            "kind": "rlm-cli-completion",
            "usage": usage,
            "environment": request.isolation.kind,
        },
    )
    if payload is None:
        return RLMResponse(
            answer=raw_answer or "RLM returned an empty response.",
            unresolved_questions=(
                "The RLM did not return the required structured JSON response.",
            ),
            total_tokens=usage["total_tokens"],
            events=events,
        )

    claims: list[RLMClaim] = []
    raw_claims = payload.get("claims", [])
    for item in raw_claims if isinstance(raw_claims, list) else []:
        try:
            if not isinstance(item, Mapping):
                raise TypeError
            citations = tuple(
                Citation(
                    str(citation["path"]),
                    int(citation["start_line"]),
                    int(citation.get("end_line", citation["start_line"])),
                )
                for citation in item.get("citations", [])
                if isinstance(citation, Mapping)
            )
            claims.append(
                RLMClaim(
                    text=str(item["text"]),
                    evidence_ids=_string_tuple(item.get("evidence_ids", ())),
                    citations=citations,
                    confidence=float(item["confidence"]),
                    contradicting_evidence_ids=_string_tuple(
                        item.get("contradicting_evidence_ids", ())
                    ),
                    unresolved_questions=_string_tuple(
                        item.get("unresolved_questions", ())
                    ),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue

    answer = str(payload.get("answer", "Structured RLM verification result."))
    if not answer.strip():
        answer = "Structured RLM verification result."
    iterations = _positive_int_attribute(completion, "iterations", default=1)
    sub_calls = usage["sub_calls"]
    return RLMResponse(
        answer=answer,
        claims=tuple(claims),
        unresolved_questions=_unresolved_questions(payload),
        iterations=iterations,
        sub_calls=sub_calls,
        total_tokens=usage["total_tokens"],
        events=events,
    )


def _json_object(value: str) -> Mapping[str, object] | None:
    text = value.strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]).strip()
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, Mapping):
        return payload

    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            candidate, _end = decoder.raw_decode(text, index)
        except (TypeError, ValueError):
            continue
        if isinstance(candidate, Mapping):
            return candidate
    return None


def _usage_totals(completion: object, request: RLMRequest) -> dict[str, int]:
    summary = getattr(completion, "usage_summary", None)
    models = getattr(summary, "model_usage_summaries", {})
    total_tokens = 0
    sub_calls = 0
    if isinstance(models, Mapping):
        for usage in models.values():
            total_tokens += int(getattr(usage, "total_input_tokens", 0) or 0)
            total_tokens += int(getattr(usage, "total_output_tokens", 0) or 0)
        if request.sub_model.target.model != request.root_model.target.model:
            sub_usage = models.get(request.sub_model.target.model)
            sub_calls = int(getattr(sub_usage, "total_calls", 0) or 0)
    return {"total_tokens": total_tokens, "sub_calls": sub_calls}


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value)


def _unresolved_questions(payload: Mapping[str, object]) -> tuple[str, ...]:
    questions = list(_string_tuple(payload.get("unresolved_questions", ())))
    singular = payload.get("unresolved_question")
    if isinstance(singular, str) and singular.strip():
        questions.append(singular)
    return tuple(questions)


def _positive_int_attribute(value: object, name: str, *, default: int) -> int:
    candidate = getattr(value, name, default)
    return candidate if isinstance(candidate, int) and candidate >= 0 else default


def _route_metadata(route: RLMModelRoute) -> dict[str, object]:
    return {
        "base_url": route.target.base_url,
        "model": route.target.model,
        "timeout_seconds": route.target.timeout_seconds,
        "max_tokens": route.target.max_tokens,
        "parameters": dict(route.target.parameters),
        "authentication": "configured (redacted)"
        if route.api_key
        else "not configured",
    }


def _trajectory_path(workspace: Path, started_at: datetime) -> Path:
    directory = workspace_state_path(workspace, VERIFY_TRAJECTORY_DIRECTORY)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = started_at.strftime("%Y%m%dT%H%M%S.%fZ")
    return directory / f"{stamp}-{uuid4().hex}.jsonl"


def _append_failure(path: Path, code: str) -> None:
    _append_jsonl(path, {"event": code, "message": "RLM execution failed (redacted)."})


def _append_jsonl(path: Path, event: Mapping[str, object]) -> None:
    redacted = _redact(event)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(redacted, sort_keys=True, separators=(",", ":")))
        stream.write("\n")


def _redact(value: object, *, key: str = "") -> object:
    normalized = key.casefold().replace("-", "_")
    if any(part in normalized for part in _SENSITIVE_PARTS):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {
            str(item_key): _redact(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


__all__ = [
    "APPROVED_EVIDENCE_TOOLS",
    "OfficialRLMAdapter",
    "RLMCLIAdapter",
    "RLMBoundary",
    "RLMClaim",
    "RLMEndpointError",
    "RLMIsolation",
    "RLMModelRoute",
    "RLMRequest",
    "RLMResponse",
    "VerifyBudgets",
    "VerifyEngine",
    "VerifyError",
]
