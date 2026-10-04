from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from pathlib import Path

import code_scout
from tests.identity_boundaries import RETIRED_EXECUTABLE, RETIRED_IMPORT_NAMESPACE


def _build_smoke_wheel(project_root: Path, dist_dir: Path) -> Path:
    wheel = dist_dir / "code_scout-0.1.0-py3-none-any.whl"
    source_root = project_root / "src"
    dist_info = "code_scout-0.1.0.dist-info"
    dist_dir.mkdir()
    with zipfile.ZipFile(wheel, "w") as archive:
        for source in sorted((source_root / "code_scout").rglob("*.py")):
            archive.write(source, source.relative_to(source_root))
        archive.writestr(
            f"{dist_info}/METADATA",
            "Metadata-Version: 2.4\nName: code-scout\nVersion: 0.1.0\n",
        )
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\nGenerator: code-scout-smoke-test\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(
            f"{dist_info}/entry_points.txt",
            "[console_scripts]\ncode-scout = code_scout.commands.cli:app\n",
        )
        archive.writestr(f"{dist_info}/RECORD", "")
    return wheel


def test_package_exposes_version_metadata() -> None:
    assert code_scout.__version__ == "0.1.0"
    assert code_scout.version() == code_scout.__version__


def test_installed_wheel_imports_and_exposes_only_code_scout_executable(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).resolve().parents[1]
    uv = os.environ.get("UV")
    if uv is None:
        uv = subprocess.run(
            ["/usr/bin/env", "sh", "-c", "command -v uv"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    assert uv

    dist_dir = tmp_path / "dist"
    environment = tmp_path / "environment"
    wheel = _build_smoke_wheel(project_root, dist_dir)
    subprocess.run(
        [uv, "venv", "--python", sys.executable, str(environment)],
        check=True,
        capture_output=True,
        text=True,
    )
    python = environment / "bin" / "python"
    executable = environment / "bin" / "code-scout"
    subprocess.run(
        [uv, "pip", "install", "--python", str(python), str(wheel)],
        check=True,
        capture_output=True,
        text=True,
    )

    clean_environment = os.environ.copy()
    clean_environment.pop("PYTHONPATH", None)
    subprocess.run(
        [
            python,
            "-c",
            "import code_scout; import importlib.util; "
            "assert importlib.util.find_spec("
            f"{RETIRED_IMPORT_NAMESPACE!r}) is None",
        ],
        cwd=tmp_path,
        env=clean_environment,
        check=True,
    )
    subprocess.run(
        [executable, "--help"],
        cwd=tmp_path,
        env=clean_environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert not (environment / "bin" / RETIRED_EXECUTABLE).exists()
