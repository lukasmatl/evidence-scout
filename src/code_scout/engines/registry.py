from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace

from code_scout.core.errors import CodeScoutError
from code_scout.engines.citations import validate_result_citations
from code_scout.engines.contracts import (
    CodeScoutRequest,
    CodeScoutResult,
    Engine,
    EngineWarning,
)


class EngineRegistrationError(CodeScoutError):
    """An engine name or alias conflicts with an existing registration."""


class UnknownEngineError(CodeScoutError):
    """The requested engine is not registered."""


class EngineRegistry:
    """Resolves canonical engine names and aliases to engine instances."""

    def __init__(
        self,
        *,
        fallbacks: Mapping[str, Iterable[str]] | None = None,
    ) -> None:
        self._engines: dict[str, Engine] = {}
        self._canonical_names: set[str] = set()
        self._fallbacks = {
            _normalize_name(name): tuple(_normalize_name(item) for item in order)
            for name, order in (fallbacks or {}).items()
        }

    @property
    def available_engines(self) -> tuple[str, ...]:
        return tuple(sorted(self._canonical_names))

    def register(
        self,
        engine: Engine,
        *,
        aliases: Iterable[str] = (),
    ) -> None:
        canonical_name = _normalize_name(engine.metadata.name)
        registration_names = (canonical_name, *map(_normalize_name, aliases))

        duplicate_in_registration = _first_duplicate(registration_names)
        if duplicate_in_registration is not None:
            raise EngineRegistrationError(
                f"Engine name or alias is repeated: {duplicate_in_registration}.",
            )

        conflict = next(
            (name for name in registration_names if name in self._engines),
            None,
        )
        if conflict is not None:
            raise EngineRegistrationError(
                f"Engine name or alias is already registered: {conflict}.",
            )

        for name in registration_names:
            self._engines[name] = engine
        self._canonical_names.add(canonical_name)

    def get(self, name: str) -> Engine:
        normalized_name = _normalize_name(name)
        try:
            return self._engines[normalized_name]
        except KeyError as error:
            available = ", ".join(self.available_engines) or "none"
            raise UnknownEngineError(
                f"Unknown engine: {name}.",
                f"Available engines: {available}.",
            ) from error

    def resolve_name(self, name: str) -> str:
        """Return the canonical engine name selected by a name or alias."""

        return _normalize_name(self.get(name).metadata.name)

    def execute(self, request: CodeScoutRequest) -> CodeScoutResult:
        primary_name = self.resolve_name(request.engine)
        candidates = (primary_name, *self._fallbacks.get(primary_name, ()))
        fallback_warnings = []
        for index, candidate in enumerate(candidates):
            engine = self.get(candidate)
            try:
                result = engine.execute(request)
                validated = validate_result_citations(
                    result,
                    request.workspace,
                    require_evidence=engine.metadata.requires_evidence,
                )
            except Exception:
                if index + 1 == len(candidates):
                    raise
                selected_fallback = candidates[index + 1]
                fallback_warnings.append(
                    EngineWarning(
                        "engine-fallback",
                        f"Provider '{candidate}' failed; selected fallback "
                        f"'{selected_fallback}'.",
                    )
                )
                continue

            metadata = dict(validated.metadata)
            metadata.update(
                {
                    "requested_engine": request.engine.strip().casefold(),
                    "resolved_engine": candidate,
                    "resolved_provider": validated.engine.provider,
                    "fallback_used": candidate != primary_name,
                }
            )
            return replace(
                validated,
                warnings=(*fallback_warnings, *validated.warnings),
                metadata=metadata,
            )
        raise AssertionError("engine candidate loop did not return or raise")


def _normalize_name(name: str) -> str:
    normalized_name = name.strip().casefold()
    if not normalized_name:
        raise EngineRegistrationError("Engine names and aliases must not be empty.")
    return normalized_name


def _first_duplicate(names: tuple[str, ...]) -> str | None:
    seen: set[str] = set()
    for name in names:
        if name in seen:
            return name
        seen.add(name)
    return None
