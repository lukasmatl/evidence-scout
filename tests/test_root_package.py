from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ROOT_PACKAGE = PROJECT_ROOT / "src" / "code_scout"


def root_implementation_files(package_root: Path = ROOT_PACKAGE) -> list[Path]:
    """Return Python files that violate the empty root-package boundary."""
    return sorted(
        [path for path in package_root.glob("*.py") if path.name != "__init__.py"]
    )


def test_root_implementation_file_detection(tmp_path: Path) -> None:
    (tmp_path / "__init__.py").touch()
    implementation = tmp_path / "feature.py"
    implementation.touch()

    assert root_implementation_files(tmp_path) == [implementation]


def test_root_package_contains_only_initializer() -> None:
    assert root_implementation_files() == []
