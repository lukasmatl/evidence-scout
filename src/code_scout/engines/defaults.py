from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from code_scout.core.errors import CodeScoutError
from code_scout.engines.availability import (
    ProviderAvailability,
    check_provider_availability,
)
from code_scout.engines.contracts import (
    CodeScoutRequest,
    CodeScoutResult,
    Engine,
    EngineMetadata,
    OpenAIModelTarget,
)
from code_scout.engines.registry import EngineRegistrationError, EngineRegistry

if TYPE_CHECKING:
    from code_scout.runtime.config import (
        EngineSelectionConfig,
        VerifyModelConfig,
    )

BUILTIN_ENGINE_NAMES = ("fast", "static", "verify")


class EngineUnavailableError(CodeScoutError):
    """A registered engine has no usable provider in this installation."""


@dataclass(frozen=True)
class UnavailableEngine:
    """Registration placeholder replaced by a provider adapter when installed."""

    metadata: EngineMetadata
    target: OpenAIModelTarget | None = None
    dependency: str | None = None
    executable: str | None = None
    configuration_error: CodeScoutError | None = None

    def availability(self, *, probe_endpoint: bool = False) -> ProviderAvailability:
        return check_provider_availability(
            target=self.target,
            dependency=self.dependency,
            executable=self.executable,
            probe_endpoint=probe_endpoint,
        )

    def execute(self, request: CodeScoutRequest) -> CodeScoutResult:
        if self.configuration_error is not None:
            raise self.configuration_error
        raise EngineUnavailableError(
            f"Engine '{self.metadata.name}' is unavailable.",
            "Install and configure its optional provider before selecting it.",
        )


def create_engine_registry(
    *,
    fast_alias: str | None = None,
    fallbacks: Mapping[str, Iterable[str]] | None = None,
    engines: Iterable[Engine] = (),
) -> EngineRegistry:
    """Create the built-in registry, optionally replacing provider placeholders."""

    (
        targets,
        configuration_error,
        verify_config,
        _configured_selection,
        fast_max_turns,
    ) = _global_targets()
    configured: dict[str, Engine] = {
        name: _unavailable_engine(
            name,
            target=targets.get(name),
            configuration_error=configuration_error,
        )
        for name in BUILTIN_ENGINE_NAMES
        if name != "static"
    }
    from code_scout.engines.static import StaticEngine

    configured["static"] = StaticEngine()
    if "fast" in targets:
        from code_scout.engines.codescout import CodeScoutEngine

        configured["fast"] = CodeScoutEngine(
            targets["fast"],
            max_turns=fast_max_turns,
        )
    if verify_config is not None:
        from code_scout.engines.verify import VerifyEngine

        configured["verify"] = VerifyEngine(verify_config)
    for engine in engines:
        name = engine.metadata.name.strip().casefold()
        if name not in configured:
            available = ", ".join(BUILTIN_ENGINE_NAMES)
            raise EngineRegistrationError(
                f"Cannot configure unsupported built-in engine: {name}.",
                f"Configured engines must be one of: {available}.",
            )
        configured[name] = engine

    fast_name = fast_alias.strip().casefold() if fast_alias is not None else "fast"
    if fast_name != "fast":
        available = ", ".join(BUILTIN_ENGINE_NAMES)
        raise EngineRegistrationError(
            f"Invalid fast alias target: {fast_alias}.",
            f"Choose one of: {available}.",
        )

    fallback_policy = (
        _configured_selection.fallbacks if fallbacks is None else fallbacks
    )
    registry = EngineRegistry(fallbacks=fallback_policy)
    for name in BUILTIN_ENGINE_NAMES:
        registry.register(configured[name])
    return registry


def _unavailable_engine(
    name: str,
    *,
    target: OpenAIModelTarget | None,
    configuration_error: CodeScoutError | None,
) -> UnavailableEngine:
    dependencies = {
        "fast": None,
        "verify": None,
    }
    return UnavailableEngine(
        metadata=EngineMetadata(name=name, provider="not-configured"),
        target=target,
        dependency=dependencies[name],
        configuration_error=configuration_error,
    )


def _global_targets() -> tuple[
    dict[str, OpenAIModelTarget],
    CodeScoutError | None,
    VerifyModelConfig | None,
    EngineSelectionConfig,
    int,
]:
    # Keep imports and non-model commands independent of global configuration.
    from code_scout.runtime.config import (
        EngineSelectionConfig,
        load_global_config,
    )

    try:
        config = load_global_config()
    except CodeScoutError as error:
        return {}, error, None, EngineSelectionConfig(), 12
    return (
        {
            "fast": config.fast.model,
            "verify": config.verify.root_model,
        },
        None,
        config.verify,
        config.selection,
        config.fast.max_turns,
    )
