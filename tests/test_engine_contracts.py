from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from code_scout.engines import (
    Citation,
    CitationValidation,
    CodeScoutRequest,
    CodeScoutResult,
    EngineMetadata,
    EngineRegistrationError,
    EngineRegistry,
    EngineWarning,
    EvidenceRequiredError,
    OpenAIModelTarget,
    RuntimeOptions,
    SourceAssessment,
    TimingMetadata,
    UnknownEngineError,
    bind_citations_to_index,
    create_engine_registry,
    validate_citations,
    validate_result_citations,
)
from code_scout.engines.citations import ObservedSource
from code_scout.storage import (
    RawEvidenceUnit,
    open_storage,
    source_content_sha256,
    store_source_revision,
    upsert_raw_evidence_units,
)


class DummyEngine:
    metadata = EngineMetadata(name="dummy", provider="test-suite")

    def __init__(self) -> None:
        self.requests: list[CodeScoutRequest] = []

    def execute(self, request: CodeScoutRequest) -> CodeScoutResult:
        self.requests.append(request)
        return CodeScoutResult(
            answer=f"Found: {request.query}",
            citations=(Citation("src/example.py", 2, 4),),
            warnings=(EngineWarning("dummy-warning", "Dummy warning."),),
            engine=self.metadata,
            timing=TimingMetadata(
                duration_seconds=0.25,
                started_at=datetime(2026, 7, 16, tzinfo=UTC),
            ),
        )


def test_request_resolves_workspace_and_enforces_runtime_bounds(
    tmp_path: Path,
) -> None:
    request = CodeScoutRequest(
        query="Where is the router?",
        workspace=tmp_path / ".." / tmp_path.name,
        engine="dummy",
        runtime=RuntimeOptions(timeout_seconds=30, max_turns=2, max_tokens=1000),
    )

    assert request.workspace == tmp_path.resolve()
    with pytest.raises(ValueError, match="max_turns"):
        RuntimeOptions(max_turns=0)
    with pytest.raises(ValueError, match="max_turns_override"):
        RuntimeOptions(max_turns_override=101)


def test_citation_preserves_untrusted_provider_values_for_validation() -> None:
    citation = Citation("src\\example.py", 3)

    assert citation == Citation("src/example.py", 3, 3)
    assert Citation("../outside.py", 0).path == "../outside.py"
    assert Citation("src/example.py", 3, 2).end_line == 2
    assert Citation("src/example.py", 3).source_assessment == SourceAssessment()


def test_openai_model_target_carries_compatible_request_options() -> None:
    target = OpenAIModelTarget(
        base_url="http://localhost:8000/v1",
        model="local-model",
        timeout_seconds=30,
        max_tokens=2048,
        parameters={"temperature": 0.1},
    )

    assert target.parameters == {"temperature": 0.1}


def test_registry_registers_canonical_name_and_aliases_case_insensitively() -> None:
    engine = DummyEngine()
    registry = EngineRegistry()

    registry.register(engine, aliases=("fast", "QUICK"))

    assert registry.available_engines == ("dummy",)
    assert registry.get("DUMMY") is engine
    assert registry.get(" fast ") is engine
    assert registry.get("quick") is engine


def test_registry_rejects_duplicate_canonical_names_and_aliases() -> None:
    registry = EngineRegistry()
    registry.register(DummyEngine(), aliases=("fast",))

    with pytest.raises(EngineRegistrationError, match="already registered: dummy"):
        registry.register(DummyEngine())

    other_engine = DummyEngine()
    other_engine.metadata = EngineMetadata(name="other", provider="test-suite")
    with pytest.raises(EngineRegistrationError, match="already registered: fast"):
        registry.register(other_engine, aliases=("fast",))


def test_unknown_engine_lists_available_canonical_names() -> None:
    registry = EngineRegistry()
    registry.register(DummyEngine(), aliases=("fast",))

    with pytest.raises(UnknownEngineError) as caught:
        registry.get("missing")

    assert caught.value.message == "Unknown engine: missing."
    assert caught.value.hint == "Available engines: dummy."


def test_registry_executes_dummy_engine(tmp_path: Path) -> None:
    engine = DummyEngine()
    registry = EngineRegistry()
    registry.register(engine, aliases=("fast",))
    source = tmp_path / "src" / "example.py"
    source.parent.mkdir()
    source.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    request = CodeScoutRequest(
        query="Find the implementation",
        workspace=tmp_path,
        engine="fast",
    )

    result = registry.execute(request)

    assert engine.requests == [request]
    assert result.answer == "Found: Find the implementation"
    assert result.citations == (Citation("src/example.py", 2, 4),)
    assert result.verified is True
    assert result.engine.provider == "test-suite"
    assert result.metadata["requested_engine"] == "fast"
    assert result.metadata["resolved_engine"] == "dummy"
    assert result.metadata["resolved_provider"] == "test-suite"
    assert result.metadata["fallback_used"] is False
    assert result.timing.duration_seconds == 0.25


def test_registry_uses_only_explicit_fallback_order_and_reports_selection(
    tmp_path: Path,
) -> None:
    class FailingEngine(DummyEngine):
        metadata = EngineMetadata(name="primary", provider="primary-provider")

        def execute(self, request: CodeScoutRequest) -> CodeScoutResult:
            raise RuntimeError("provider response must not be exposed")

    fallback = DummyEngine()
    fallback.metadata = EngineMetadata(name="fallback", provider="fallback-provider")
    registry = EngineRegistry(fallbacks={"primary": ("fallback",)})
    registry.register(FailingEngine(), aliases=("fast",))
    registry.register(fallback)
    source = tmp_path / "src" / "example.py"
    source.parent.mkdir()
    source.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")

    result = registry.execute(
        CodeScoutRequest(query="Find it", workspace=tmp_path, engine="fast")
    )

    assert result.metadata["requested_engine"] == "fast"
    assert result.metadata["resolved_engine"] == "fallback"
    assert result.metadata["resolved_provider"] == "fallback-provider"
    assert result.metadata["fallback_used"] is True
    assert result.warnings[0] == EngineWarning(
        "engine-fallback",
        "Provider 'primary' failed; selected fallback 'fallback'.",
    )
    assert "provider response" not in result.warnings[0].message


def test_registry_never_falls_back_without_opt_in(tmp_path: Path) -> None:
    class FailingEngine(DummyEngine):
        metadata = EngineMetadata(name="primary", provider="primary-provider")

        def execute(self, request: CodeScoutRequest) -> CodeScoutResult:
            raise RuntimeError("primary stopped")

    fallback = DummyEngine()
    fallback.metadata = EngineMetadata(name="fallback", provider="fallback-provider")
    registry = EngineRegistry()
    registry.register(FailingEngine(), aliases=("fast",))
    registry.register(fallback)

    with pytest.raises(RuntimeError, match="primary stopped"):
        registry.execute(
            CodeScoutRequest(query="Find it", workspace=tmp_path, engine="fast")
        )
    assert fallback.requests == []


def test_citations_normalize_valid_relative_and_absolute_paths(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src" / "example.py"
    source.parent.mkdir()
    source.write_text("one\ntwo\nthree\n", encoding="utf-8")

    validation = validate_citations(
        tmp_path,
        (
            Citation("src/example.py", 1, 2),
            Citation(str(source), 3),
        ),
    )

    assert validation == CitationValidation(
        citations=(
            Citation("src/example.py", 1, 2),
            Citation("src/example.py", 3, 3),
        ),
        warnings=(),
    )


def test_citations_reject_unsafe_paths_and_invalid_ranges(tmp_path: Path) -> None:
    source = tmp_path / "source.py"
    source.write_text("one\ntwo\n", encoding="utf-8")
    directory = tmp_path / "directory"
    directory.mkdir()
    outside = tmp_path.parent / "outside.py"
    outside.write_text("outside\n", encoding="utf-8")

    validation = validate_citations(
        tmp_path,
        (
            Citation("../outside.py", 1),
            Citation(str(outside), 1),
            Citation("missing.py", 1),
            Citation("directory", 1),
            Citation("source.py", 0),
            Citation("source.py", 2, 3),
            Citation("source.py", 2, 1),
        ),
    )

    assert validation.citations == ()
    assert tuple(warning.code for warning in validation.warnings) == (
        "citation-path-traversal",
        "citation-outside-workspace",
        "citation-missing-file",
        "citation-not-file",
        "citation-invalid-range",
        "citation-range-out-of-bounds",
        "citation-invalid-range",
    )


def test_citations_reject_symlinks_escaping_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-target.py"
    outside.write_text("outside\n", encoding="utf-8")
    link = tmp_path / "linked.py"
    try:
        link.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"symlinks are unavailable: {error}")

    validation = validate_citations(tmp_path, (Citation("linked.py", 1),))

    assert validation.citations == ()
    assert validation.warnings[0].code == "citation-symlink-escape"


def test_citations_deduplicate_in_provider_order_and_warn_for_mixed_invalid(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("one\ntwo\n", encoding="utf-8")
    second.write_text("one\n", encoding="utf-8")

    validation = validate_citations(
        tmp_path,
        (
            Citation("first.py", 2),
            Citation("missing.py", 1),
            Citation(str(first), 2),
            Citation("second.py", 1),
            Citation("first.py", 4),
        ),
    )

    assert validation.citations == (
        Citation("first.py", 2),
        Citation("second.py", 1),
    )
    assert tuple(warning.code for warning in validation.warnings) == (
        "citation-missing-file",
        "citation-range-out-of-bounds",
    )


def test_retained_citation_validates_historical_bytes_and_live_freshness(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src" / "example.py"
    source.parent.mkdir()
    retained = "caf\u00e9\r\nsecond\r\n".encode()
    source.write_bytes(retained)
    with open_storage(tmp_path) as connection:
        revision = store_source_revision(
            connection,
            repository_id="repo:api",
            path="src/example.py",
            source_bytes=retained,
        )

    citation = Citation(
        "src/example.py",
        2,
        repository_id=revision.repository_id,
        source_revision_id=revision.id,
        content_sha256=revision.content_sha256,
    )
    fresh = validate_citations(tmp_path, (citation,))

    assert fresh.warnings == ()
    assert fresh.citations[0].source_assessment == SourceAssessment(
        "verified", "fresh", ("source-current",)
    )

    # Same number of lines is still stale when the exact bytes differ.
    source.write_bytes(b"cafe\r\nsecond\r\n")
    stale = validate_citations(tmp_path, (citation,))

    assert stale.warnings == ()
    assert stale.citations[0].source_assessment == SourceAssessment(
        "verified", "stale", ("source-changed",)
    )


def test_retained_citation_survives_deleted_live_file_and_checks_old_bounds(
    tmp_path: Path,
) -> None:
    source = tmp_path / "retained.py"
    source.write_text("one\ntwo\n", encoding="utf-8")
    with open_storage(tmp_path) as connection:
        revision = store_source_revision(
            connection,
            repository_id="repo:api",
            path="retained.py",
            source_bytes=source.read_bytes(),
        )
    source.unlink()
    citation = Citation(
        "retained.py",
        2,
        repository_id=revision.repository_id,
        source_revision_id=revision.id,
        content_sha256=revision.content_sha256,
    )

    validation = validate_citations(tmp_path, (citation,))

    assert validation.warnings == ()
    assert validation.citations[0].source_assessment == SourceAssessment(
        "verified", "stale", ("source-missing",)
    )
    out_of_bounds = validate_citations(
        tmp_path, (Citation(**{**citation.__dict__, "start_line": 3, "end_line": 3}),)
    )
    assert out_of_bounds.citations == ()
    assert out_of_bounds.warnings[0].code == "citation-range-out-of-bounds"


def test_unbound_legacy_citation_stays_revision_unknown(tmp_path: Path) -> None:
    source = tmp_path / "legacy.py"
    source.write_text("one\n", encoding="utf-8")

    validation = validate_citations(tmp_path, (Citation("legacy.py", 1),))

    assert validation.warnings == ()
    assert validation.citations[0].source_assessment == SourceAssessment(
        "unknown", "unknown", ("source-unbound",)
    )

    partial = validate_citations(
        tmp_path, (Citation("legacy.py", 1, repository_id="repo:api"),)
    )
    assert partial.citations[0].source_assessment == SourceAssessment(
        "unknown", "unknown", ("source-unbound",)
    )


def test_index_range_without_request_observation_stays_revision_unknown(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.py"
    source.write_text("old\nvalue\n", encoding="utf-8")
    with open_storage(tmp_path) as connection:
        revision = store_source_revision(
            connection,
            repository_id="repo:api",
            path="source.py",
            source_bytes=source.read_bytes(),
        )
        upsert_raw_evidence_units(
            connection,
            (
                RawEvidenceUnit(
                    id="evidence:source",
                    owner_entity_type="symbol",
                    owner_entity_id="symbol:source",
                    source_file="source.py",
                    start_line=1,
                    end_line=2,
                    text="source",
                    provenance="test",
                    confidence=1.0,
                    metadata={
                        "repository_id": revision.repository_id,
                        "source_revision_id": revision.id,
                        "content_sha256": revision.content_sha256,
                    },
                ),
            ),
        )
        connection.commit()

    bound = bind_citations_to_index(tmp_path, (Citation("source.py", 1, 2),))
    source.write_text("new\nvalue\n", encoding="utf-8")
    validation = validate_citations(tmp_path, bound)

    assert validation.citations[0].source_revision_id is None
    assert validation.citations[0].source_assessment == SourceAssessment(
        "unknown", "unknown", ("source-unbound",)
    )


def test_index_binding_uses_the_request_observed_source_revision(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.py"
    observed = b"old\nvalue\n"
    source.write_bytes(observed)
    with open_storage(tmp_path) as connection:
        revision = store_source_revision(
            connection,
            repository_id="repo:api",
            path="source.py",
            source_bytes=observed,
        )
        # Simulate a concurrent rescan after the terminal saw A but before the
        # fast engine emits its final localization.
        source.write_text("new\nvalue\n", encoding="utf-8")
        store_source_revision(
            connection,
            repository_id="repo:api",
            path="source.py",
            source_bytes=source.read_bytes(),
        )

    bound = bind_citations_to_index(
        tmp_path,
        (Citation("source.py", 1, 2),),
        observed_sources=(ObservedSource("source.py", observed.decode()),),
    )
    assert bound[0].source_revision_id == revision.id
    assert validate_citations(tmp_path, bound).citations[0].source_assessment == (
        SourceAssessment("verified", "stale", ("source-changed",))
    )


def test_retained_citation_marks_unreadable_live_source_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.py"
    source.write_text("one\n", encoding="utf-8")
    with open_storage(tmp_path) as connection:
        revision = store_source_revision(
            connection,
            repository_id="repo:api",
            path="source.py",
            source_bytes=source.read_bytes(),
        )
    original_read_bytes = Path.read_bytes

    def unreadable(path: Path) -> bytes:
        if path == source:
            raise OSError("permission denied")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    validation = validate_citations(
        tmp_path,
        (
            Citation(
                "source.py",
                1,
                repository_id=revision.repository_id,
                source_revision_id=revision.id,
                content_sha256=revision.content_sha256,
            ),
        ),
    )

    assert validation.citations[0].source_assessment == SourceAssessment(
        "verified", "unknown", ("source-unreadable",)
    )


def test_retained_citation_rejects_hash_and_binding_mismatches(tmp_path: Path) -> None:
    source = tmp_path / "source.py"
    source.write_text("one\n", encoding="utf-8")
    with open_storage(tmp_path) as connection:
        revision = store_source_revision(
            connection,
            repository_id="repo:api",
            path="source.py",
            source_bytes=source.read_bytes(),
        )
    validation = validate_citations(
        tmp_path,
        (
            Citation(
                "source.py",
                1,
                repository_id="repo:other",
                source_revision_id=revision.id,
                content_sha256=source_content_sha256(b"different"),
            ),
        ),
    )

    assert validation.citations == ()
    assert validation.warnings[0].code == "citation-source-binding-mismatch"


def test_retained_citation_rejects_corrupt_retained_bytes(tmp_path: Path) -> None:
    source = tmp_path / "source.py"
    source.write_text("one\n", encoding="utf-8")
    with open_storage(tmp_path) as connection:
        revision = store_source_revision(
            connection,
            repository_id="repo:api",
            path="source.py",
            source_bytes=source.read_bytes(),
        )
        connection.execute("DROP TRIGGER prevent_source_blob_mutation")
        connection.execute(
            "UPDATE source_blobs SET bytes = ? WHERE content_sha256 = ?",
            (b"bad\n", revision.content_sha256),
        )

    validation = validate_citations(
        tmp_path,
        (
            Citation(
                "source.py",
                1,
                repository_id=revision.repository_id,
                source_revision_id=revision.id,
                content_sha256=revision.content_sha256,
            ),
        ),
    )

    assert validation.citations == ()
    assert validation.warnings[0].code == "citation-retained-source-corrupt"


def test_unverified_result_can_be_rejected_when_evidence_is_required(
    tmp_path: Path,
) -> None:
    result = CodeScoutResult(
        answer="Unsubstantiated answer",
        citations=(Citation("invented.py", 1),),
        warnings=(EngineWarning("provider-note", "Provider note."),),
        engine=EngineMetadata(name="strict", provider="test-suite"),
        timing=TimingMetadata(duration_seconds=0),
    )

    unverified = validate_result_citations(result, tmp_path)

    assert unverified.verified is False
    assert tuple(warning.code for warning in unverified.warnings) == (
        "provider-note",
        "citation-missing-file",
    )
    with pytest.raises(EvidenceRequiredError, match="no valid citations") as caught:
        validate_result_citations(result, tmp_path, require_evidence=True)
    assert tuple(warning.code for warning in caught.value.result.warnings) == (
        "provider-note",
        "citation-missing-file",
    )


def test_registry_rejects_unverified_result_for_evidence_requiring_engine(
    tmp_path: Path,
) -> None:
    engine = DummyEngine()
    engine.metadata = EngineMetadata(
        name="dummy",
        provider="test-suite",
        requires_evidence=True,
    )
    registry = EngineRegistry()
    registry.register(engine)

    with pytest.raises(EvidenceRequiredError, match="no valid citations"):
        registry.execute(
            CodeScoutRequest(query="Find it", workspace=tmp_path, engine="dummy")
        )


def test_default_registry_exposes_builtins_and_configurable_fast_alias(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("CODE_SCOUT_HOME", str(tmp_path / "empty-code-scout-home"))
    default_registry = create_engine_registry()

    assert default_registry.available_engines == ("fast", "static", "verify")
    assert default_registry.get("fast").metadata.provider == "not-configured"


def test_default_registry_rejects_invalid_fast_alias_target() -> None:
    with pytest.raises(EngineRegistrationError, match="Invalid fast alias target"):
        create_engine_registry(fast_alias="missing")
