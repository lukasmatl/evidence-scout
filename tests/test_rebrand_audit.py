from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path

from tests.identity_boundaries import RETIRED_PROJECT_TOKEN

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# These are the complete shipped surfaces: package source, tests and their
# owned fixtures, scripts, documentation, IDE/CI configuration, and packaging
# metadata. Runtime environments, caches, orchestration state, and task inputs
# are outside this allowlist because they are not shipped repository surfaces.
SHIPPED_DIRECTORIES = (
    Path(".github"),
    Path("docs"),
    Path("scripts"),
    Path("src"),
    Path("tests"),
)
SHIPPED_FILES = (
    Path(".gitignore"),
    Path(".idea/.gitignore"),
    Path(".idea/code-scout.iml"),
    Path(".idea/misc.xml"),
    Path(".idea/modules.xml"),
    Path(".idea/vcs.xml"),
    Path("AGENTS.md"),
    Path("CLAUDE.md"),
    Path("CONTRIBUTING.md"),
    Path("README.md"),
    Path("pyproject.toml"),
    Path("uv.lock"),
)

# This vendor-authored API document is immutable extractor input. It is the
# audit's only content exclusion; version-control internals are outside the
# shipped allowlist and therefore cannot be traversed.
EXCLUDED_INPUTS = {
    Path(
        "tests/fixtures/okf-export-gaps/external/openapi/vendor-flight-plan.json"
    ): "immutable third-party OpenAPI input",
}


def _is_excluded(relative_path: Path) -> bool:
    return any(
        relative_path == excluded or excluded in relative_path.parents
        for excluded in EXCLUDED_INPUTS
    )


def iter_shipped_files(project_root: Path = PROJECT_ROOT) -> Iterator[Path]:
    candidates: set[Path] = set()
    for relative_directory in SHIPPED_DIRECTORIES:
        directory = project_root / relative_directory
        if directory.is_dir():
            candidates.update(
                path
                for path in directory.rglob("*")
                if path.is_file() and "__pycache__" not in path.parts
            )
    for relative_file in SHIPPED_FILES:
        path = project_root / relative_file
        if path.is_file():
            candidates.add(path)

    for path in sorted(candidates):
        if not _is_excluded(path.relative_to(project_root)):
            yield path


def find_retired_identity(
    paths: Iterable[Path], project_root: Path = PROJECT_ROOT
) -> list[str]:
    token = RETIRED_PROJECT_TOKEN.casefold()
    violations: list[str] = []
    for path in paths:
        relative_path = path.relative_to(project_root)
        if token in relative_path.as_posix().casefold():
            violations.append(f"{relative_path}: legacy identity in path")
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(content.splitlines(), start=1):
            if token in line.casefold():
                violations.append(f"{relative_path}:{line_number}: {line.strip()}")
    return violations


def test_shipped_repository_has_only_code_scout_identity() -> None:
    violations = find_retired_identity(iter_shipped_files())

    assert violations == [], "Retired identity found:\n" + "\n".join(violations)


def test_audit_is_case_insensitive_and_checks_paths(tmp_path: Path) -> None:
    content_violation = tmp_path / "content.txt"
    content_violation.write_text(RETIRED_PROJECT_TOKEN.upper(), encoding="utf-8")
    path_violation = tmp_path / f"old-{RETIRED_PROJECT_TOKEN.upper()}.txt"
    path_violation.write_text("clean", encoding="utf-8")

    assert find_retired_identity(
        (content_violation, path_violation), project_root=tmp_path
    ) == [
        f"content.txt:1: {RETIRED_PROJECT_TOKEN.upper()}",
        f"old-{RETIRED_PROJECT_TOKEN.upper()}.txt: legacy identity in path",
    ]


def test_audit_exclusions_are_documented_existing_immutable_inputs() -> None:
    assert EXCLUDED_INPUTS == {
        Path(
            "tests/fixtures/okf-export-gaps/external/openapi/vendor-flight-plan.json"
        ): "immutable third-party OpenAPI input",
    }
    audited_files = set(iter_shipped_files())
    for relative_path, reason in EXCLUDED_INPUTS.items():
        assert reason
        assert (PROJECT_ROOT / relative_path).is_file()
        assert _is_excluded(relative_path)
        assert PROJECT_ROOT / relative_path not in audited_files
