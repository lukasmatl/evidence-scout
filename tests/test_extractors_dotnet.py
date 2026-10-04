from __future__ import annotations

from code_scout.discovery import DiscoveredFile
from code_scout.extractors import SymbolExtraction
from code_scout.extractors.dotnet import DotnetExtractor


def test_dotnet_project_fixture_emits_symbols_and_dependency_edges(tmp_path) -> None:
    extraction = _extract(
        tmp_path,
        path="src/App/App.csproj",
        extension=".csproj",
        source="""<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <AssemblyName>Contoso.App</AssemblyName>
    <RootNamespace>Contoso.App.Root</RootNamespace>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Newtonsoft.Json" Version="13.0.3" />
    <ProjectReference Include="..\\Shared\\Shared.csproj" />
  </ItemGroup>
</Project>
""",
    )

    assert tuple((symbol.kind, symbol.name) for symbol in extraction.symbols) == (
        ("assembly", "Contoso.App"),
        ("namespace", "Contoso.App.Root"),
    )
    assert tuple(
        (edge.relation_type, edge.target_type, edge.target_id)
        for edge in extraction.edges
    ) == (
        (
            "declares_symbol",
            "symbol",
            "symbol:repo:src/App/App.csproj:dotnet-assembly:contoso-app",
        ),
        (
            "declares_symbol",
            "symbol",
            "symbol:repo:src/App/App.csproj:dotnet-namespace:contoso-app-root",
        ),
        ("depends_on", "package", "Newtonsoft.Json"),
        ("references_project", "file", "file:repo:src/Shared/Shared.csproj"),
    )
    assert extraction.edges[2].evidence[0].metadata == {
        "repository_id": "repo",
        "package_name": "Newtonsoft.Json",
        "version": "13.0.3",
    }
    assert extraction.edges[3].evidence[0].metadata["project_path"] == (
        "src/Shared/Shared.csproj"
    )


def test_dotnet_solution_fixtures_emit_project_reference_edges(tmp_path) -> None:
    sln_extraction = _extract(
        tmp_path,
        path="Workspace.sln",
        extension=".sln",
        source="""Microsoft Visual Studio Solution File, Format Version 12.00
Project("{GUID}") = "App", "src\\App\\App.csproj", "{PROJECT-GUID}"
EndProject
""",
    )
    slnx_extraction = _extract(
        tmp_path,
        path="Workspace.slnx",
        extension=".slnx",
        source="""<Solution>
  <Project Path="tests/App.Tests/App.Tests.csproj" />
</Solution>
""",
    )

    assert tuple(
        (edge.relation_type, edge.source_id, edge.target_id)
        for edge in sln_extraction.edges
    ) == (
        (
            "references_project",
            "file:repo:Workspace.sln",
            "file:repo:src/App/App.csproj",
        ),
    )
    assert tuple(
        (edge.relation_type, edge.source_id, edge.target_id)
        for edge in slnx_extraction.edges
    ) == (
        (
            "references_project",
            "file:repo:Workspace.slnx",
            "file:repo:tests/App.Tests/App.Tests.csproj",
        ),
    )


def test_dotnet_razor_fixtures_emit_model_inject_and_route_facts(tmp_path) -> None:
    razor_extraction = _extract(
        tmp_path,
        path="Components/Counter.razor",
        extension=".razor",
        source="""@page "/counter"
@model Contoso.Pages.CounterModel
@inject Contoso.Services.ClockService Clock
""",
    )
    cshtml_extraction = _extract(
        tmp_path,
        path="Pages/Index.cshtml",
        extension=".cshtml",
        source="""@page "/"
@inject Microsoft.Extensions.Logging.ILogger<IndexModel> Logger
@model IndexModel
""",
    )

    assert tuple(symbol.name for symbol in razor_extraction.symbols) == (
        "Contoso.Pages.CounterModel",
    )
    assert tuple(service.name for service in razor_extraction.services) == (
        "Contoso.Services.ClockService",
    )
    assert tuple(contract.path for contract in razor_extraction.contracts) == (
        "/counter",
    )
    assert tuple(edge.relation_type for edge in razor_extraction.edges) == (
        "declares_symbol",
        "injects",
        "declares_contract",
    )

    assert tuple(symbol.name for symbol in cshtml_extraction.symbols) == ("IndexModel",)
    assert tuple(service.name for service in cshtml_extraction.services) == (
        "Microsoft.Extensions.Logging.ILogger<IndexModel>",
    )
    assert tuple(contract.path for contract in cshtml_extraction.contracts) == ("/",)
    assert tuple(edge.relation_type for edge in cshtml_extraction.edges) == (
        "declares_symbol",
        "injects",
        "declares_contract",
    )


def _extract(
    tmp_path,
    *,
    path: str,
    extension: str,
    source: str,
) -> SymbolExtraction:
    source_path = tmp_path / path
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(source, encoding="utf-8")

    return DotnetExtractor().extract(
        DiscoveredFile(
            repository_id="repo",
            path=path,
            absolute_path=str(source_path),
            kind="metadata" if extension in {".csproj", ".sln", ".slnx"} else "source",
            extension=extension,
            content_hash="hash",
            modified_at="2026-06-20T00:00:00+00:00",
            language="Razor" if extension in {".razor", ".cshtml"} else "MSBuild",
        ),
    )
