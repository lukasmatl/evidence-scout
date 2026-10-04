from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExtractorProfile:
    name: str
    extractors: tuple[str, ...]

    @property
    def scan_extractors(self) -> tuple[str, ...]:
        if "tree_sitter" not in self.extractors:
            return self.extractors
        return (*self.extractors, "symbol_extraction")


EXTRACTOR_PROFILES = {
    profile.name: profile
    for profile in (
        ExtractorProfile(
            name="code-only",
            extractors=("graphify", "tree_sitter", "lsp"),
        ),
        ExtractorProfile(
            name="docs",
            extractors=("adr", "docs", "openapi"),
        ),
        ExtractorProfile(
            name="agent",
            extractors=("graphify", "openapi", "adr"),
        ),
        ExtractorProfile(
            name="full",
            extractors=(
                "graphify",
                "tree_sitter",
                "lsp",
                "adr",
                "openapi",
                "asyncapi",
                "infra",
                "docs",
            ),
        ),
    )
}
