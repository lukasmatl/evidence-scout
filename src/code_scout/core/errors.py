from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CodeScoutError(Exception):
    """Base class for expected code-scout failures shown by the CLI."""

    message: str
    hint: str | None = None

    def __post_init__(self) -> None:
        super().__init__(self.message)


class ConfigError(CodeScoutError):
    """Configuration or workspace setup is invalid."""


class CommandUsageError(CodeScoutError):
    """A command invocation contains invalid user-supplied arguments."""


class StorageError(CodeScoutError):
    """Local code-scout storage could not be read or written."""


class ExtractionError(CodeScoutError):
    """Workspace signal extraction failed in an expected way."""


class ExportError(CodeScoutError):
    """Architecture memory could not be exported."""


class GitChangeError(CodeScoutError):
    """A requested read-only Git comparison could not be collected."""
