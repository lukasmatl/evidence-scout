from __future__ import annotations

from pathlib import Path

from code_scout.discovery import DiscoveredFile
from code_scout.extractors.frontend_api import (
    frontend_api_contracts,
    frontend_api_edges,
)


def test_frontend_api_extracts_fetch_with_static_method_and_path(tmp_path) -> None:
    source_file = tmp_path / "src" / "api" / "flightPlans.ts"
    source_file.parent.mkdir(parents=True)
    source_file.write_text(
        """
export async function optimize(request: unknown) {
  return fetch("/api/flight-plans/optimize", {
    method: "POST",
    body: JSON.stringify(request),
  });
}
""",
        encoding="utf-8",
    )

    contracts = frontend_api_contracts((_discovered_file(source_file, tmp_path),))
    edges = frontend_api_edges((_discovered_file(source_file, tmp_path),))

    assert len(contracts) == 1
    assert contracts[0].id == "contract:fixture:http:post:api-flight-plans-optimize"
    assert contracts[0].path == "/api/flight-plans/optimize"
    assert contracts[0].method == "POST"
    assert contracts[0].evidence[0].start_line == 3
    assert contracts[0].evidence[0].file_path == "src/api/flightPlans.ts"
    assert contracts[0].evidence[0].metadata["url"] == "/api/flight-plans/optimize"
    assert len(edges) == 1
    assert edges[0].relation_type == "consumes"
    assert edges[0].source_id == "file:fixture:src/api/flightPlans.ts"
    assert edges[0].evidence[0].metadata["url"] == "/api/flight-plans/optimize"


def test_frontend_api_extracts_axios_and_typed_client_calls(tmp_path) -> None:
    source_file = tmp_path / "src" / "api" / "client.ts"
    source_file.parent.mkdir(parents=True)
    source_file.write_text(
        """
axios.get("https://api.example.test/users?active=true");
apiClient.post<User>("/api/users", payload);
http.request({ method: "PATCH", url: "/api/users/current" });
""",
        encoding="utf-8",
    )

    contracts = frontend_api_contracts((_discovered_file(source_file, tmp_path),))

    assert {(contract.method, contract.path) for contract in contracts} == {
        ("GET", "/users"),
        ("POST", "/api/users"),
        ("PATCH", "/api/users/current"),
    }
    assert {contract.id for contract in contracts} == {
        "contract:fixture:http:get:users",
        "contract:fixture:http:post:api-users",
        "contract:fixture:http:patch:api-users-current",
    }
    assert contracts[0].evidence[0].metadata["url"] == (
        "https://api.example.test/users?active=true"
    )


def test_frontend_api_full_url_identity_ignores_host_query_and_fragment(
    tmp_path,
) -> None:
    source_file = tmp_path / "src" / "api" / "client.ts"
    source_file.parent.mkdir(parents=True)
    source_file.write_text(
        """
fetch("https://api.example.test/API//Users?active=true#list");
""",
        encoding="utf-8",
    )

    contracts = frontend_api_contracts((_discovered_file(source_file, tmp_path),))
    edges = frontend_api_edges((_discovered_file(source_file, tmp_path),))

    extracted = [
        (contract.method, contract.path, contract.id) for contract in contracts
    ]
    assert extracted == [("GET", "/API//Users", "contract:fixture:http:get:api-users")]
    assert edges[0].target_id == "contract:fixture:http:get:api-users"
    assert edges[0].evidence[0].metadata["url"] == (
        "https://api.example.test/API//Users?active=true#list"
    )


def test_frontend_api_dedupes_repeated_consumer_edges_but_keeps_evidence(
    tmp_path,
) -> None:
    source_file = tmp_path / "src" / "api" / "client.ts"
    source_file.parent.mkdir(parents=True)
    source_file.write_text(
        """
apiClient.get("/api/users");
apiClient.get("/api/users?active=true");
""",
        encoding="utf-8",
    )

    edges = frontend_api_edges((_discovered_file(source_file, tmp_path),))

    assert len(edges) == 1
    assert edges[0].target_id == "contract:fixture:http:get:api-users"
    assert [evidence.start_line for evidence in edges[0].evidence] == [2, 3]
    assert [evidence.metadata["url"] for evidence in edges[0].evidence] == [
        "/api/users",
        "/api/users?active=true",
    ]


def test_frontend_api_contract_ids_normalize_url_path_shape(tmp_path) -> None:
    source_file = tmp_path / "src" / "api" / "client.ts"
    source_file.parent.mkdir(parents=True)
    source_file.write_text(
        """
apiClient.get("/API//Users/");
""",
        encoding="utf-8",
    )

    contracts = frontend_api_contracts((_discovered_file(source_file, tmp_path),))

    extracted = [
        (contract.method, contract.path, contract.id) for contract in contracts
    ]
    assert extracted == [("GET", "/API//Users", "contract:fixture:http:get:api-users")]


def test_frontend_api_ignores_arbitrary_strings_and_dynamic_paths(tmp_path) -> None:
    source_file = tmp_path / "src" / "app.tsx"
    source_file.parent.mkdir(parents=True)
    source_file.write_text(
        """
const url = "/api/users";
const value = labels.get("/not-http-client");
fetch(`/api/users/${id}`, { method: "GET" });
worker.postMessage("/not-an-http-call");
""",
        encoding="utf-8",
    )

    contracts = frontend_api_contracts((_discovered_file(source_file, tmp_path),))

    assert contracts == ()


def test_frontend_api_keeps_fetch_method_lookup_inside_call(tmp_path) -> None:
    source_file = tmp_path / "src" / "api.ts"
    source_file.parent.mkdir(parents=True)
    source_file.write_text(
        """
fetch("/api/users");
const options = { method: "POST" };
""",
        encoding="utf-8",
    )

    contracts = frontend_api_contracts((_discovered_file(source_file, tmp_path),))

    assert [(contract.method, contract.path) for contract in contracts] == [
        ("GET", "/api/users")
    ]


def _discovered_file(source_file: Path, root: Path) -> DiscoveredFile:
    return DiscoveredFile(
        repository_id="fixture",
        path=source_file.relative_to(root).as_posix(),
        absolute_path=str(source_file),
        kind="source",
        extension=source_file.suffix,
        content_hash="hash",
        modified_at="2026-06-20T00:00:00Z",
        language="TypeScript",
    )
