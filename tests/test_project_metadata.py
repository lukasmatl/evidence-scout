from __future__ import annotations

import tomllib
from pathlib import Path


def test_project_metadata_declares_src_package_layout() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text())

    assert pyproject["project"]["name"] == "code-scout"
    assert pyproject["project"]["version"] == "0.1.0"
    assert pyproject["project"]["requires-python"] == ">=3.11"
    assert pyproject["project"]["dependencies"] == []
    assert pyproject["project"]["scripts"] == {
        "code-scout": "code_scout.commands.cli:app"
    }
    assert pyproject["dependency-groups"]["dev"] == []
    assert pyproject["tool"]["uv"]["package"] is True
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/code_scout",
    ]
    assert pyproject["tool"]["ruff"]["line-length"] == 88
    assert pyproject["tool"]["ruff"]["format"]["quote-style"] == "double"
    assert pyproject["tool"]["ruff"]["lint"]["select"] == ["E", "F", "I", "UP", "B"]
    assert pyproject["tool"]["pytest"]["ini_options"]["addopts"] == "-ra"
    assert pyproject["tool"]["pytest"]["ini_options"]["pythonpath"] == ["src"]
    assert pyproject["tool"]["pytest"]["ini_options"]["testpaths"] == ["tests"]
