from __future__ import annotations

import importlib.util
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.request import Request, urlopen

from code_scout.engines.contracts import OpenAIModelTarget


class AvailabilityState(StrEnum):
    MISSING_DEPENDENCY = "missing_dependency"
    MISSING_EXECUTABLE = "missing_executable"
    MISSING_ENDPOINT_CONFIGURATION = "missing_endpoint_configuration"
    UNREACHABLE_ENDPOINT = "unreachable_endpoint"
    READY = "ready"


@dataclass(frozen=True)
class ProviderAvailability:
    state: AvailabilityState
    message: str

    @property
    def ready(self) -> bool:
        return self.state is AvailabilityState.READY


EndpointProbe = Callable[[str, float], None]


def check_provider_availability(
    *,
    target: OpenAIModelTarget | None,
    dependency: str | None = None,
    executable: str | None = None,
    probe_endpoint: bool = False,
    endpoint_probe: EndpointProbe | None = None,
    environment: Mapping[str, str] | None = None,
) -> ProviderAvailability:
    """Classify provider readiness without probing the network by default."""

    try:
        dependency_missing = (
            dependency is not None and importlib.util.find_spec(dependency) is None
        )
    except (ImportError, ModuleNotFoundError, ValueError):
        dependency_missing = True
    if dependency_missing:
        return ProviderAvailability(
            AvailabilityState.MISSING_DEPENDENCY,
            f"Optional Python dependency '{dependency}' is not installed.",
        )
    if executable is not None and _find_executable(executable, environment) is None:
        return ProviderAvailability(
            AvailabilityState.MISSING_EXECUTABLE,
            f"Required executable '{executable}' was not found on PATH.",
        )
    if target is None:
        return ProviderAvailability(
            AvailabilityState.MISSING_ENDPOINT_CONFIGURATION,
            "No OpenAI-compatible endpoint is configured for this provider.",
        )
    if probe_endpoint:
        probe = _default_endpoint_probe if endpoint_probe is None else endpoint_probe
        try:
            probe(target.base_url, target.timeout_seconds)
        except Exception:
            return ProviderAvailability(
                AvailabilityState.UNREACHABLE_ENDPOINT,
                f"Configured endpoint {target.base_url} could not be reached.",
            )
    return ProviderAvailability(
        AvailabilityState.READY,
        "Provider configuration is ready.",
    )


def _find_executable(
    executable: str,
    environment: Mapping[str, str] | None,
) -> Path | None:
    env = os.environ if environment is None else environment
    for entry in env.get("PATH", "").split(os.pathsep):
        candidate = Path(entry or ".") / executable
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _default_endpoint_probe(base_url: str, timeout: float) -> None:
    request = Request(base_url, method="GET")
    with urlopen(request, timeout=timeout):  # noqa: S310
        return
