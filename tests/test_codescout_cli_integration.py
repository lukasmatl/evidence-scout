from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from code_scout.storage import open_storage
from tests.test_smoke import _build_smoke_wheel

_TRANSPORT_SHIM = r"""
import json
import os
import urllib.request

PROMPTS = {
    "Locate the router implementation": [
        {"file": "src/router.py", "class_name": "Router", "function_name": "dispatch"},
        {"file": "src/router.py", "function_name": "invalid_range"},
    ],
    "Which module defines the authentication policy concept?": [
        {"file": "src/auth.py", "class_name": "AuthenticationPolicy"},
        {"file": "escape.py"},
    ],
    "Locate every caller of route_request": [
        {"file": "src/service.py", "function_name": "serve_request"},
        {"file": "tests/test_service.py", "function_name": "test_route_request"},
    ],
}

def tool_call(call_id, name, arguments):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }

class Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self):
        return self.payload

original_urlopen = urllib.request.urlopen

def scripted_urlopen(request, timeout=None):
    log_path = os.environ.get("CODESCOUT_TEST_REQUEST_LOG")
    if not log_path:
        return original_urlopen(request, timeout=timeout)
    payload = json.loads(request.data)
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(json.dumps(payload, sort_keys=True) + "\n")
    user_content = next(
        message["content"] for message in payload["messages"]
        if message["role"] == "user"
    )
    query = next(query for query in PROMPTS if query in user_content)
    observed = any(message["role"] == "tool" for message in payload["messages"])
    if observed:
        calls = [tool_call(
            "finish", "localization_finish", {"locations": PROMPTS[query]}
        )]
    else:
        calls = [
            tool_call("search", "terminal", {
                "command": "rg -n route src tests", "security_risk": "LOW"
            }),
            tool_call("escape", "terminal", {
                "command": "cat ../outside-secret.txt", "security_risk": "LOW"
            }),
        ]
    return Response({
        "id": "scripted",
        "model": "OpenHands/CodeScout-4B",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": None, "tool_calls": calls},
            "finish_reason": "tool_calls",
        }],
    })

urllib.request.urlopen = scripted_urlopen
"""


@pytest.fixture(scope="module")
def installed_code_scout(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("installed-codescout")
    project_root = Path(__file__).resolve().parents[1]
    uv = os.environ.get("UV")
    if uv is None:
        uv = subprocess.run(
            ["/usr/bin/env", "sh", "-c", "command -v uv"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    wheel = _build_smoke_wheel(project_root, root / "dist")
    environment = root / "environment"
    subprocess.run(
        [uv, "venv", "--python", sys.executable, str(environment)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(environment / "bin" / "python"),
            str(wheel),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    site_packages = subprocess.run(
        [
            environment / "bin" / "python",
            "-c",
            "import sysconfig; print(sysconfig.get_paths()['purelib'])",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (Path(site_packages) / "codescout_test_transport.py").write_text(
        _TRANSPORT_SHIM,
        encoding="utf-8",
    )
    (Path(site_packages) / "codescout_test_transport.pth").write_text(
        "import codescout_test_transport\n",
        encoding="utf-8",
    )
    return environment / "bin" / "code-scout"


def _write_global_config(home: Path, base_url: str, model: str) -> None:
    home.mkdir(parents=True)
    (home / "config.toml").write_text(
        f"""
[engines.fast.model]
base_url = "{base_url}"
model = "{model}"
timeout_seconds = 10
max_tokens = 1000

[engines.verify.root_model]
base_url = "http://127.0.0.1:1/v1"
model = "unused-verify-root"

[engines.verify.sub_model]
base_url = "http://127.0.0.1:1/v1"
model = "unused-verify-sub"
""",
        encoding="utf-8",
    )


def _prepare_workspace(workspace: Path, outside_secret: Path) -> None:
    files = {
        "src/router.py": (
            "class Router:\n    def dispatch(self):\n        return route_request()\n"
        ),
        "src/auth.py": "class AuthenticationPolicy:\n    pass\n",
        "src/service.py": "def serve_request():\n    return route_request()\n",
        "tests/test_service.py": (
            "def test_route_request():\n    assert route_request() is not None\n"
        ),
    }
    for relative_path, content in files.items():
        path = workspace / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (workspace / "escape.py").symlink_to(outside_secret)
    with open_storage(workspace) as connection:
        connection.execute(
            "INSERT INTO workspaces (id, name, root_path) VALUES ('ws', 'ws', ?)",
            (str(workspace),),
        )
        connection.execute(
            "INSERT INTO repositories (id, workspace_id, name, path) "
            "VALUES ('repo', 'ws', 'repo', '.')"
        )
        connection.execute(
            "INSERT INTO files (id, repository_id, path) "
            "VALUES ('router', 'repo', 'src/router.py')"
        )
        connection.execute(
            "INSERT INTO symbols "
            "(id, file_id, name, kind, qualified_name, start_line, end_line) "
            "VALUES ('invalid', 'router', 'invalid_range', 'function', "
            "'invalid_range', 99, 101)"
        )


def _clean_environment(home: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["CODE_SCOUT_HOME"] = str(home)
    return environment


def _request_log(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize(
    ("query", "expected_citations", "expected_warning"),
    (
        (
            "Locate the router implementation",
            ("src/router.py:2",),
            "file has 3 lines",
        ),
        (
            "Which module defines the authentication policy concept?",
            ("src/auth.py:1",),
            "resolves outside the workspace",
        ),
        (
            "Locate every caller of route_request",
            ("src/service.py:1", "tests/test_service.py:1"),
            None,
        ),
    ),
)
def test_installed_codescout_cli_preserves_end_to_end_boundaries(
    tmp_path: Path,
    installed_code_scout: Path,
    query: str,
    expected_citations: tuple[str, ...],
    expected_warning: str | None,
) -> None:
    invocation_directory = tmp_path / "outside-repository-cwd"
    invocation_directory.mkdir()
    workspace = tmp_path / "isolated-workspace"
    workspace.mkdir()
    outside_secret = tmp_path / "outside-secret.txt"
    outside_secret.write_text("MUST_NOT_LEAK", encoding="utf-8")
    _prepare_workspace(workspace, outside_secret)
    home = tmp_path / "global-home"
    request_log = tmp_path / "requests.jsonl"
    _write_global_config(home, "http://127.0.0.1:1/v1", "OpenHands/CodeScout-4B")
    environment = _clean_environment(home)
    environment["CODESCOUT_TEST_REQUEST_LOG"] = str(request_log)
    completed = subprocess.run(
        [
            installed_code_scout,
            "--engine",
            "fast",
            "--workspace",
            str(workspace),
            "--query",
            query,
        ],
        cwd=invocation_directory,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert "MUST_NOT_LEAK" not in completed.stdout
    for citation in expected_citations:
        assert f"- {citation}" in completed.stdout
    if expected_warning is not None:
        assert expected_warning in completed.stdout
    requests = _request_log(request_log)
    assert len(requests) == 2
    observations = [
        message
        for message in requests[1]["messages"]  # type: ignore[index]
        if message["role"] == "tool"
    ]
    assert [message["tool_call_id"] for message in observations] == [
        "search",
        "escape",
    ]
    assert "src/service.py" in observations[0]["content"]
    assert "error" in observations[1]["content"]
    assert "MUST_NOT_LEAK" not in observations[1]["content"]
    assert not (invocation_directory / ".code-scout").exists()
    assert list((workspace / ".code-scout/trajectories/codescout").glob("*.jsonl"))


_INTEGRATION_BASE_URL = os.environ.get("CODESCOUT_INTEGRATION_BASE_URL")
_INTEGRATION_MODEL = os.environ.get("CODESCOUT_INTEGRATION_MODEL")


@pytest.mark.skipif(
    not (_INTEGRATION_BASE_URL and _INTEGRATION_MODEL),
    reason=(
        "set CODESCOUT_INTEGRATION_BASE_URL and CODESCOUT_INTEGRATION_MODEL "
        "to run the real-endpoint smoke test"
    ),
)
def test_installed_codescout_real_endpoint_returns_workspace_evidence(
    tmp_path: Path,
    installed_code_scout: Path,
) -> None:
    workspace = tmp_path / "real-endpoint-workspace"
    workspace.mkdir()
    source = workspace / "router.py"
    source.write_text(
        "def integration_smoke_router():\n    return 'ready'\n",
        encoding="utf-8",
    )
    home = tmp_path / "real-endpoint-home"
    _write_global_config(home, _INTEGRATION_BASE_URL, _INTEGRATION_MODEL)
    environment = _clean_environment(home)
    api_key = os.environ.get("CODESCOUT_INTEGRATION_API_KEY")
    if api_key is not None:
        config_path = home / "config.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                f'model = "{_INTEGRATION_MODEL}"',
                f'model = "{_INTEGRATION_MODEL}"\n'
                'api_key_env = "CODESCOUT_INTEGRATION_API_KEY"',
            ),
            encoding="utf-8",
        )
        environment["CODESCOUT_INTEGRATION_API_KEY"] = api_key

    completed = subprocess.run(
        [
            installed_code_scout,
            "--engine",
            "fast",
            "--workspace",
            str(workspace),
            "--query",
            "Locate integration_smoke_router and return its exact function.",
        ],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert completed.returncode == 0, completed.stderr
    assert "router.py:1" in completed.stdout
