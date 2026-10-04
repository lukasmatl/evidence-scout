from __future__ import annotations

import os
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RLM_REQUIREMENT = (
    "rlm @ git+https://github.com/rawwerks/rlm.git"
    "@9a95a2bb38a73499473bc830b5b1b0806e79ce47"
)
GRAPHIFY_PACKAGE = "graphifyy"


def _installer_arguments(tmp_path: Path, **extra_environment: str) -> list[list[str]]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    install_log = tmp_path / "uv-arguments.txt"
    uv = fake_bin / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$@" >> "$INSTALL_LOG"\n'
        'printf -- "---\\n" >> "$INSTALL_LOG"\n',
        encoding="utf-8",
    )
    code_scout = fake_bin / "code-scout"
    code_scout.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    uv.chmod(0o755)
    code_scout.chmod(0o755)
    environment = dict(os.environ)
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    environment["INSTALL_LOG"] = str(install_log)
    environment.update(extra_environment)

    subprocess.run(
        [PROJECT_ROOT / "scripts" / "install-local-tool.sh"],
        cwd=PROJECT_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    return [
        [line for line in block.splitlines() if line != "---"]
        for block in install_log.read_text(encoding="utf-8").strip().split("\n---\n")
    ]


def test_installer_adds_all_engine_providers_to_the_code_scout_tool_environment(
    tmp_path: Path,
) -> None:
    assert _installer_arguments(tmp_path) == [
        [
            "tool",
            "install",
            "--force",
            "--reinstall",
            "--refresh",
            "--python",
            "3.12",
            GRAPHIFY_PACKAGE,
        ],
        [
            "tool",
            "install",
            "--force",
            "--reinstall",
            "--refresh",
            "--python",
            "3.12",
            "--with",
            RLM_REQUIREMENT,
            str(PROJECT_ROOT),
        ],
    ]


def test_installer_allows_overriding_graphify_package(tmp_path: Path) -> None:
    fake_graphify = tmp_path / "graphify"

    arguments = _installer_arguments(
        tmp_path / "run",
        CODE_SCOUT_GRAPHIFY_PACKAGE=str(fake_graphify),
    )

    assert arguments[0][-1] == str(fake_graphify)
