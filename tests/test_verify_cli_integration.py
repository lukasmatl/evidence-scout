from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from code_scout.evidence.embeddings import LocalEmbeddingProvider
from code_scout.models.graph import Edge, Evidence, File, Repository, Snapshot, Symbol
from code_scout.storage import (
    RawEvidenceUnit,
    create_snapshot,
    open_storage,
    refresh_evidence_embeddings,
    upsert_edge,
    upsert_file,
    upsert_raw_evidence_units,
    upsert_repository,
    upsert_symbol,
    upsert_workspace,
)
from tests.test_smoke import _build_smoke_wheel

_REAL_RLM_PACKAGE = os.environ.get("CODE_SCOUT_VERIFY_INTEGRATION_RLM_PACKAGE")
_REAL_ROOT_BASE_URL = os.environ.get("CODE_SCOUT_VERIFY_INTEGRATION_ROOT_BASE_URL")
_REAL_ROOT_MODEL = os.environ.get("CODE_SCOUT_VERIFY_INTEGRATION_ROOT_MODEL")
_REAL_SUB_BASE_URL = os.environ.get("CODE_SCOUT_VERIFY_INTEGRATION_SUB_BASE_URL")
_REAL_SUB_MODEL = os.environ.get("CODE_SCOUT_VERIFY_INTEGRATION_SUB_MODEL")
_REAL_SANDBOX_IMAGE = os.environ.get("CODE_SCOUT_VERIFY_INTEGRATION_SANDBOX_IMAGE")
_REAL_INTEGRATION_READY = os.environ.get(
    "CODE_SCOUT_VERIFY_INTEGRATION"
) == "1" and all(
    (
        _REAL_RLM_PACKAGE,
        _REAL_ROOT_BASE_URL,
        _REAL_ROOT_MODEL,
        _REAL_SUB_BASE_URL,
        _REAL_SUB_MODEL,
        _REAL_SANDBOX_IMAGE,
    )
)


_BOUNDARY_SHIM = r"""
import json
import os
import sys
import types


def _location(path, start, end, name):
    return {
        "relative_path": path,
        "body_location": {"start_line": start - 1, "end_line": end - 1},
        "name_path": name,
        "kind": "symbol",
    }


def _fake_serena(self, name, arguments, timeout):
    if os.environ.get("CODE_SCOUT_VERIFY_SERENA") == "unavailable":
        from code_scout.evidence.serena import SerenaMCPError
        raise SerenaMCPError("scripted unavailable boundary")
    requested = str(
        arguments.get("name_path_pattern", arguments.get("relative_path", ""))
    )
    locations = {
        "IOrderService": [
            _location("services/orders/OrderService.cs", 1, 3, "OrderService")
        ],
        "AuthenticationMiddleware": [
            _location(
                "gateway/AuthMiddleware.py", 1, 3, "AuthenticationMiddleware"
            )
        ],
        "handle_order": [_location("gateway/Routes.py", 1, 2, "handle_order")],
        "services/orders/OrderService.cs": [
            _location("services/orders/OrderService.cs", 1, 3, "OrderService")
        ],
    }
    return locations.get(requested, [])


def _source(record):
    if "source_file" in record:
        return (
            record["source_file"],
            record["start_line"],
            record["end_line"],
        )
    sources = record.get("source_provenance", [])
    if not sources:
        raise AssertionError("selected evidence has no exact source provenance")
    item = sources[0]
    return item["source_file"], item["start_line"], item["end_line"]


def _log_boundary(request, calls):
    log_path = os.environ.get("CODE_SCOUT_VERIFY_BOUNDARY_LOG")
    if not log_path:
        return
    with open(log_path, "a", encoding="utf-8") as stream:
        stream.write(json.dumps({
            "query": request.question,
            "tools": [item["tool"] for item in calls],
            "tool_results": calls,
            "root_model": request.root_model.target.model,
            "sub_model": request.sub_model.target.model,
            "root_api_key": request.root_model.api_key,
            "sub_api_key": request.sub_model.api_key,
        }, sort_keys=True) + "\n")


class _Controller:
    def run(self, request):
        from code_scout.engines.verify import RLMClaim, RLMResponse
        from code_scout.engines.contracts import Citation
        from code_scout.evidence.serena import SerenaMCPTransport

        SerenaMCPTransport.call_tool = _fake_serena

        pack = json.loads(request.external_context)
        question = request.question.casefold()
        calls = []
        if "dede unavailable" in question:
            calls.append(request.tools["inspect_impact_paths"]())
            _log_boundary(request, calls)
            return RLMResponse(
                "No dede path was available.",
                unresolved_questions=("The optional dede graph is unavailable.",),
            )
        if "no valid final claim" in question:
            _log_boundary(request, calls)
            return RLMResponse(
                "No structured final claim.",
                unresolved_questions=("No valid final claim was produced.",),
            )
        if "implementation" in question:
            calls.append(request.tools["find_implementations"](symbol="IOrderService"))
            selected = next(
                item for item in pack["evidence"]
                if item["id"] == "evidence:order-implementation"
            )
            text = "OrderService is the IOrderService implementation."
        elif "authentication middleware" in question:
            calls.append(request.tools["find_definitions"](symbol="AuthenticationMiddleware"))
            selected = next(
                item for item in pack["evidence"]
                if item["id"] == "evidence:auth-middleware"
            )
            text = "AuthenticationMiddleware validates bearer tokens."
        elif "every caller" in question:
            calls.append(request.tools["list_callers"](symbol_id="symbol:handle-order"))
            selected = next(
                item for item in pack["relationships"]
                if item["relation_type"] == "calls"
            )
            text = "OrderController calls handle_order."
        elif "blast radius" in question:
            calls.append(
                request.tools["traverse_neighbors"](
                    node_id="symbol:order-service", direction="both"
                )
            )
            selected = next(
                item for item in pack["relationships"]
                if item["relation_type"] == "depends_on"
            )
            text = "Changing OrderService affects the gateway order handler."
        elif ".net" in question:
            calls.append(request.tools["inspect_impact_paths"]())
            selected = next(
                item for item in pack["relationships"]
                if item["id"] == "dede:http-cross-repository"
            )
            text = "The gateway endpoint reaches OrderService across repositories."
        else:
            return RLMResponse(
                "No structured final claim.",
                unresolved_questions=("No valid final claim was produced.",),
            )

        path, start, end = _source(selected)
        claim = RLMClaim(
            text,
            (selected["id"],),
            (Citation(path, start, end),),
            0.99,
        )
        _log_boundary(request, calls)
        return RLMResponse(
            "diagnostic prose", claims=(claim,), iterations=2, sub_calls=1
        )


def create_code_scout_controller(**kwargs):
    if kwargs.get("allow_host_execution") is not False:
        raise AssertionError("verify must refuse host execution")
    if kwargs.get("isolation", {}).get("kind") != "docker":
        raise AssertionError("verify must request docker isolation")
    return _Controller()


if os.environ.get("CODE_SCOUT_VERIFY_TEST_BOUNDARIES") == "1":
    module = types.ModuleType("rlm")
    module.create_code_scout_controller = create_code_scout_controller
    sys.modules["rlm"] = module
"""


@pytest.fixture(scope="module")
def installed_verify(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("installed-verify")
    project_root = Path(__file__).resolve().parents[1]
    uv = _find_uv()
    wheel = _build_smoke_wheel(project_root, root / "dist")
    environment = root / "environment"
    subprocess.run(
        [uv, "venv", "--python", sys.executable, str(environment)],
        check=True,
        capture_output=True,
        text=True,
    )
    if _REAL_INTEGRATION_READY:
        assert _REAL_RLM_PACKAGE is not None
        subprocess.run(
            [
                uv,
                "pip",
                "install",
                "--python",
                str(environment / "bin" / "python"),
                _REAL_RLM_PACKAGE,
            ],
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
    (Path(site_packages) / "code_scout_verify_test_boundaries.py").write_text(
        _BOUNDARY_SHIM,
        encoding="utf-8",
    )
    (Path(site_packages) / "code_scout_verify_test_boundaries.pth").write_text(
        "import code_scout_verify_test_boundaries\n",
        encoding="utf-8",
    )
    fake_bin = root / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    return environment / "bin" / "code-scout", fake_bin


def _find_uv() -> str:
    configured = os.environ.get("UV")
    if configured is not None:
        return configured
    return subprocess.run(
        ["/usr/bin/env", "sh", "-c", "command -v uv"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write_global_config(home: Path) -> None:
    home.mkdir(parents=True)
    (home / "config.toml").write_text(
        """
[engines.fast.model]
base_url = "http://127.0.0.1:1/v1"
model = "unused-fast"

[engines.verify]
isolation = "docker"
isolation_image = "code-scout-test-sandbox"
max_iterations = 4
max_sub_calls = 8
max_output_characters = 20000
timeout_seconds = 20
max_total_tokens = 10000

[engines.verify.root_model]
base_url = "https://root.example.test/v1"
model = "verify-root-test"
api_key_env = "VERIFY_ROOT_KEY"
timeout_seconds = 10
max_tokens = 2000

[engines.verify.sub_model]
base_url = "https://sub.example.test/v1"
model = "verify-sub-test"
api_key_env = "VERIFY_SUB_KEY"
timeout_seconds = 9
max_tokens = 1000
""",
        encoding="utf-8",
    )


def _write_real_global_config(home: Path) -> None:
    assert _REAL_ROOT_BASE_URL is not None
    assert _REAL_ROOT_MODEL is not None
    assert _REAL_SUB_BASE_URL is not None
    assert _REAL_SUB_MODEL is not None
    assert _REAL_SANDBOX_IMAGE is not None
    home.mkdir(parents=True)
    root_key = os.environ.get("CODE_SCOUT_VERIFY_INTEGRATION_ROOT_API_KEY")
    sub_key = os.environ.get("CODE_SCOUT_VERIFY_INTEGRATION_SUB_API_KEY")
    root_auth = 'api_key_env = "VERIFY_INTEGRATION_ROOT_KEY"' if root_key else ""
    sub_auth = 'api_key_env = "VERIFY_INTEGRATION_SUB_KEY"' if sub_key else ""
    (home / "config.toml").write_text(
        f'''
[engines.fast.model]
base_url = "http://127.0.0.1:1/v1"
model = "unused-fast"

[engines.verify]
isolation = "docker"
isolation_image = "{_REAL_SANDBOX_IMAGE}"
max_iterations = 4
max_sub_calls = 8
max_output_characters = 20000
timeout_seconds = 240
max_total_tokens = 20000

[engines.verify.root_model]
base_url = "{_REAL_ROOT_BASE_URL}"
model = "{_REAL_ROOT_MODEL}"
{root_auth}
timeout_seconds = 180
max_tokens = 4000

[engines.verify.sub_model]
base_url = "{_REAL_SUB_BASE_URL}"
model = "{_REAL_SUB_MODEL}"
{sub_auth}
timeout_seconds = 180
max_tokens = 2000
''',
        encoding="utf-8",
    )


def _workspace_config(workspace: Path, *, stale_dede: bool = False) -> None:
    completed_at = "2000-01-01T00:00:00Z" if stale_dede else "2999-01-01T00:00:00Z"
    config = workspace / ".code-scout" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        """workspace:
  id: verify-integration
  name: Verify integration
repositories:
  - id: gateway
    path: gateway
  - id: orders
    path: services/orders
navigation:
  serena:
    enabled: true
    url: http://127.0.0.1:1/mcp
    timeout_seconds: 1
enrichment:
  dede:
    enabled: true
    graph_path: .code-scout/dede.json
    executable: definitely-missing-dede
    max_age_seconds: 60
""",
        encoding="utf-8",
    )
    (workspace / ".code-scout" / "dede.json").write_text(
        json.dumps(
            {
                "schemaVersion": "1.0",
                "scanMetadata": {"completedAtUtc": completed_at, "rootPath": "."},
                "nodes": [
                    {
                        "id": "endpoint:gateway-orders",
                        "type": "Endpoint",
                        "displayName": "POST /orders",
                        "repositoryName": "gateway",
                        "projectName": "Gateway",
                        "certainty": "Certain",
                        "sourceLocation": {
                            "filePath": "gateway/Routes.py",
                            "startLine": 1,
                            "endLine": 2,
                        },
                    },
                    {
                        "id": "method:OrderService.Create",
                        "type": "Method",
                        "displayName": "OrderService.Create",
                        "repositoryName": "orders",
                        "projectName": "Orders",
                        "certainty": "Certain",
                        "sourceLocation": {
                            "filePath": "services/orders/OrderService.cs",
                            "startLine": 1,
                            "endLine": 3,
                        },
                    },
                ],
                "edges": [
                    {
                        "id": "http-cross-repository",
                        "sourceId": "endpoint:gateway-orders",
                        "targetId": "method:OrderService.Create",
                        "type": "HTTP_CALLS",
                        "certainty": "Certain",
                        "sourceLocation": {
                            "filePath": "gateway/Routes.py",
                            "startLine": 1,
                            "endLine": 2,
                        },
                    }
                ],
                "impactPaths": [
                    {
                        "id": "impact:gateway-to-orders",
                        "nodeIds": [
                            "endpoint:gateway-orders",
                            "method:OrderService.Create",
                        ],
                        "edgeIds": ["http-cross-repository"],
                        "certainty": "Certain",
                        "unresolved": False,
                    }
                ],
                "warnings": [],
                "unresolved": [],
                "statistics": {"nodeCount": 2, "edgeCount": 1},
            }
        ),
        encoding="utf-8",
    )


def _seed_scanned_workspace(workspace: Path, *, stale_dede: bool = False) -> None:
    files = {
        "services/orders/OrderService.cs": (
            "class OrderService : IOrderService {\n  public void Create() {}\n}\n"
        ),
        "gateway/AuthMiddleware.py": (
            "class AuthenticationMiddleware:\n"
            "    def validate_bearer(self):\n"
            "        return True\n"
        ),
        "gateway/Routes.py": "def handle_order():\n    return order_service.Create()\n",
        "gateway/OrderController.py": (
            "def create_order():\n    return handle_order()\n"
        ),
    }
    for relative, content in files.items():
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _workspace_config(workspace, stale_dede=stale_dede)

    definitions = (
        (
            "evidence:order-implementation",
            "symbol:order-service",
            "services/orders/OrderService.cs",
            "OrderService IOrderService implementation",
            1,
            3,
        ),
        (
            "evidence:auth-middleware",
            "symbol:auth-middleware",
            "gateway/AuthMiddleware.py",
            "authentication middleware bearer token validation",
            1,
            3,
        ),
        (
            "evidence:handle-order",
            "symbol:handle-order",
            "gateway/Routes.py",
            "handle order gateway route blast radius caller",
            1,
            2,
        ),
        (
            "evidence:order-controller",
            "symbol:order-controller",
            "gateway/OrderController.py",
            "every caller order controller create order",
            1,
            2,
        ),
    )
    with open_storage(workspace) as connection:
        upsert_workspace(
            connection,
            id="workspace:verify-integration",
            name="verify-integration",
            root_path=str(workspace),
        )
        upsert_repository(
            connection,
            "workspace:verify-integration",
            Repository("repository:gateway", "gateway", "gateway"),
        )
        upsert_repository(
            connection,
            "workspace:verify-integration",
            Repository("repository:orders", "orders", "services/orders"),
        )
        for index, (_evidence_id, symbol_id, relative, _text, start, end) in enumerate(
            definitions
        ):
            repository = (
                "repository:orders"
                if relative.endswith(".cs")
                else "repository:gateway"
            )
            file_id = f"file:{index}"
            upsert_file(connection, File(file_id, repository, relative))
            upsert_symbol(
                connection,
                Symbol(
                    symbol_id,
                    file_id,
                    symbol_id.removeprefix("symbol:"),
                    "class"
                    if "middleware" in symbol_id or "service" in symbol_id
                    else "function",
                    symbol_id.removeprefix("symbol:"),
                    start,
                    end,
                    evidence=(Evidence(relative, "lsp", 0.98, start, end),),
                ),
            )
        units = tuple(
            RawEvidenceUnit(
                id=evidence_id,
                owner_entity_type="symbol",
                owner_entity_id=symbol_id,
                source_file=relative,
                start_line=start,
                end_line=end,
                text=text,
                provenance="lsp",
                confidence=0.98,
            )
            for evidence_id, symbol_id, relative, text, start, end in definitions
        )
        upsert_raw_evidence_units(connection, units)
        refresh_evidence_embeddings(
            connection,
            units,
            provider=LocalEmbeddingProvider(),
        )
        upsert_edge(
            connection,
            Edge(
                "symbol",
                "symbol:order-controller",
                "symbol",
                "symbol:handle-order",
                "calls",
                0.98,
                evidence=(Evidence("gateway/OrderController.py", "lsp", 0.98, 1, 2),),
            ),
        )
        upsert_edge(
            connection,
            Edge(
                "symbol",
                "symbol:handle-order",
                "symbol",
                "symbol:order-service",
                "depends_on",
                0.98,
                evidence=(Evidence("gateway/Routes.py", "lsp", 0.98, 1, 2),),
            ),
        )
        create_snapshot(
            connection,
            Snapshot(
                "snapshot:verify-integration",
                "workspace:verify-integration",
                "2026-07-16T00:00:00Z",
            ),
        )
        connection.commit()


def _environment(home: Path, fake_bin: Path, boundary_log: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.update(
        {
            "CODE_SCOUT_HOME": str(home),
            "CODE_SCOUT_VERIFY_TEST_BOUNDARIES": "1",
            "CODE_SCOUT_VERIFY_SERENA": "available",
            "CODE_SCOUT_VERIFY_BOUNDARY_LOG": str(boundary_log),
            "VERIFY_ROOT_KEY": "root-test-secret",
            "VERIFY_SUB_KEY": "sub-test-secret",
            "PATH": f"{fake_bin}{os.pathsep}{environment.get('PATH', '')}",
        }
    )
    return environment


def _source_digests(workspace: Path) -> dict[str, str]:
    return {
        path.relative_to(workspace).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in workspace.rglob("*")
        if path.is_file() and ".code-scout/trajectories/verify" not in path.as_posix()
    }


@pytest.mark.parametrize(
    ("query", "expected_text", "expected_citation", "expected_tool"),
    (
        (
            "Find the IOrderService implementation",
            "OrderService is the IOrderService implementation.",
            "services/orders/OrderService.cs:1-3",
            "find_implementations",
        ),
        (
            "Explain the authentication middleware",
            "AuthenticationMiddleware validates bearer tokens.",
            "gateway/AuthMiddleware.py:1-3",
            "find_definitions",
        ),
        (
            "Locate every caller of handle_order",
            "OrderController calls handle_order.",
            "gateway/OrderController.py:1-2",
            "list_callers",
        ),
        (
            "Show the blast radius of OrderService",
            "Changing OrderService affects the gateway order handler.",
            "gateway/Routes.py:1-2",
            "traverse_neighbors",
        ),
        (
            "Trace the cross-repository .NET path",
            "The gateway endpoint reaches OrderService across repositories.",
            "gateway/Routes.py:1-2",
            "inspect_impact_paths",
        ),
    ),
)
def test_installed_verify_cli_exercises_real_evidence_boundaries(
    tmp_path: Path,
    installed_verify: tuple[Path, Path],
    query: str,
    expected_text: str,
    expected_citation: str,
    expected_tool: str,
) -> None:
    executable, fake_bin = installed_verify
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _seed_scanned_workspace(workspace)
    before = _source_digests(workspace)
    home = tmp_path / "home"
    _write_global_config(home)
    boundary_log = tmp_path / "boundary.jsonl"
    invocation_directory = tmp_path / "outside-repository-cwd"
    invocation_directory.mkdir()

    completed = subprocess.run(
        [
            executable,
            "--engine",
            "verify",
            "--workspace",
            str(workspace),
            "--query",
            query,
        ],
        cwd=invocation_directory,
        env=_environment(home, fake_bin, boundary_log),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert expected_text in completed.stdout
    assert f"- {expected_citation}" in completed.stdout
    assert _source_digests(workspace) == before
    assert not (invocation_directory / ".code-scout").exists()
    trajectories = list(
        (workspace / ".code-scout" / "trajectories" / "verify").glob("*.jsonl")
    )
    assert len(trajectories) == 1
    event = json.loads(boundary_log.read_text(encoding="utf-8"))
    assert event["tools"] == [expected_tool]
    assert event["root_model"] == "verify-root-test"
    assert event["sub_model"] == "verify-sub-test"
    assert event["root_api_key"] == "root-test-secret"
    assert event["sub_api_key"] == "sub-test-secret"
    assert "root-test-secret" not in trajectories[0].read_text(encoding="utf-8")
    assert "sub-test-secret" not in trajectories[0].read_text(encoding="utf-8")


def test_installed_verify_cli_preserves_frozen_result_when_serena_is_unavailable(
    tmp_path: Path,
    installed_verify: tuple[Path, Path],
) -> None:
    executable, fake_bin = installed_verify
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _seed_scanned_workspace(workspace)
    home = tmp_path / "home"
    _write_global_config(home)
    boundary_log = tmp_path / "boundary.jsonl"
    environment = _environment(home, fake_bin, boundary_log)
    environment["CODE_SCOUT_VERIFY_SERENA"] = "unavailable"

    completed = subprocess.run(
        [
            executable,
            "--engine",
            "verify",
            "--workspace",
            str(workspace),
            "--query",
            "Explain the authentication middleware",
        ],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert "AuthenticationMiddleware validates bearer tokens." in completed.stdout
    event = json.loads(boundary_log.read_text(encoding="utf-8"))
    tool_result = event["tool_results"][0]
    assert tool_result["provenance"]["provider"] == "code-scout"
    assert tool_result["warnings"][0]["code"] == "serena-unavailable"


def test_installed_verify_cli_reports_dede_unavailable_without_losing_base_evidence(
    tmp_path: Path,
    installed_verify: tuple[Path, Path],
) -> None:
    executable, fake_bin = installed_verify
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _seed_scanned_workspace(workspace)
    (workspace / ".code-scout" / "dede.json").unlink()
    home = tmp_path / "home"
    _write_global_config(home)
    boundary_log = tmp_path / "boundary.jsonl"

    completed = subprocess.run(
        [
            executable,
            "--engine",
            "verify",
            "--workspace",
            str(workspace),
            "--query",
            "Show the dede unavailable cross-repository path",
        ],
        cwd=tmp_path,
        env=_environment(home, fake_bin, boundary_log),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Question: The optional dede graph is unavailable." in completed.stdout
    assert "No defensible claims were returned." in completed.stdout
    event = json.loads(boundary_log.read_text(encoding="utf-8"))
    assert event["tools"] == ["inspect_impact_paths"]
    assert event["tool_results"][0]["status"] == "unavailable"


def test_installed_verify_cli_downgrades_stale_dede_evidence(
    tmp_path: Path,
    installed_verify: tuple[Path, Path],
) -> None:
    executable, fake_bin = installed_verify
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _seed_scanned_workspace(workspace, stale_dede=True)
    home = tmp_path / "home"
    _write_global_config(home)
    boundary_log = tmp_path / "boundary.jsonl"

    completed = subprocess.run(
        [
            executable,
            "--engine",
            "verify",
            "--workspace",
            str(workspace),
            "--query",
            "Trace the cross-repository .NET path",
        ],
        cwd=tmp_path,
        env=_environment(home, fake_bin, boundary_log),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Confirmed\n- None." in completed.stdout
    assert (
        "Inferred\n- The gateway endpoint reaches OrderService across repositories."
        in completed.stdout
    )
    assert "provider confidence or evidence provenance is not confirmatory" in (
        completed.stdout.casefold()
    )


def test_installed_verify_cli_reports_no_prior_scan_without_calling_rlm(
    tmp_path: Path,
    installed_verify: tuple[Path, Path],
) -> None:
    executable, fake_bin = installed_verify
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "service.py"
    source.write_text("def service():\n    return True\n", encoding="utf-8")
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    home = tmp_path / "home"
    _write_global_config(home)
    boundary_log = tmp_path / "boundary.jsonl"

    completed = subprocess.run(
        [
            executable,
            "--engine",
            "verify",
            "--workspace",
            str(workspace),
            "--query",
            "Find the service implementation",
        ],
        cwd=tmp_path,
        env=_environment(home, fake_bin, boundary_log),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert "Workspace has not been scanned" in completed.stderr
    assert "Run `code-scout scan`" in completed.stderr
    assert not boundary_log.exists()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_digest
    state_files = [
        path.relative_to(workspace).as_posix()
        for path in (workspace / ".code-scout").rglob("*")
        if path.is_file()
    ]
    assert state_files == []


def test_installed_verify_cli_renders_no_valid_final_claim_as_unresolved(
    tmp_path: Path,
    installed_verify: tuple[Path, Path],
) -> None:
    executable, fake_bin = installed_verify
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _seed_scanned_workspace(workspace)
    home = tmp_path / "home"
    _write_global_config(home)
    boundary_log = tmp_path / "boundary.jsonl"

    completed = subprocess.run(
        [
            executable,
            "--engine",
            "verify",
            "--workspace",
            str(workspace),
            "--query",
            "Return no valid final claim",
        ],
        cwd=tmp_path,
        env=_environment(home, fake_bin, boundary_log),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Confirmed\n- None." in completed.stdout
    assert "Question: No valid final claim was produced." in completed.stdout
    assert "No defensible claims were returned." in completed.stdout
    assert "Citations:" not in completed.stdout
    assert "The RLM returned no structured claims" in completed.stdout


@pytest.mark.skipif(
    not _REAL_INTEGRATION_READY,
    reason=(
        "set CODE_SCOUT_VERIFY_INTEGRATION=1 plus the RLM package, root/sub "
        "endpoint/model, and sandbox image variables to run the real-RLM smoke test"
    ),
)
def test_installed_verify_cli_real_rlm_uses_configured_isolated_sandbox(
    tmp_path: Path,
    installed_verify: tuple[Path, Path],
) -> None:
    executable, _fake_bin = installed_verify
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _seed_scanned_workspace(workspace)
    before = _source_digests(workspace)
    home = tmp_path / "home"
    _write_real_global_config(home)
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["CODE_SCOUT_HOME"] = str(home)
    root_key = os.environ.get("CODE_SCOUT_VERIFY_INTEGRATION_ROOT_API_KEY")
    sub_key = os.environ.get("CODE_SCOUT_VERIFY_INTEGRATION_SUB_API_KEY")
    if root_key:
        environment["VERIFY_INTEGRATION_ROOT_KEY"] = root_key
    if sub_key:
        environment["VERIFY_INTEGRATION_SUB_KEY"] = sub_key

    completed = subprocess.run(
        [
            executable,
            "--engine",
            "verify",
            "--workspace",
            str(workspace),
            "--query",
            "Find the IOrderService implementation and cite its exact source range.",
        ],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert completed.returncode == 0, completed.stderr
    assert any(
        heading in completed.stdout for heading in ("Confirmed\n-", "Inferred\n-")
    )
    assert _source_digests(workspace) == before
    assert list((workspace / ".code-scout" / "trajectories" / "verify").glob("*.jsonl"))
