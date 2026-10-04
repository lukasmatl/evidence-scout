from __future__ import annotations

from code_scout.discovery import DiscoveredFile
from code_scout.extractors import SymbolExtraction
from code_scout.extractors.dotnet import DOTNET_CONFIDENCE, DotnetExtractor


def test_dotnet_extracts_project_references_from_sln(tmp_path) -> None:
    extraction = _extract(
        tmp_path,
        path="workspace/App.sln",
        extension=".sln",
        source="\n".join(
            (
                "Microsoft Visual Studio Solution File, Format Version 12.00",
                'Project("{GUID}") = "Api", "src\\Api\\Api.csproj", "{PROJECT-GUID}"',
                "EndProject",
                'Project("{GUID}") = "Lib", "..\\shared\\Lib\\Lib.fsproj", "{GUID}"',
                "EndProject",
                'Project("{GUID}") = "Solution Items", "Solution Items", "{GUID}"',
                "EndProject",
            ),
        ),
    )

    assert extraction.symbols == ()
    assert tuple(edge.relation_type for edge in extraction.edges) == (
        "references_project",
        "references_project",
    )
    assert tuple(edge.source_id for edge in extraction.edges) == (
        "file:repo:workspace/App.sln",
        "file:repo:workspace/App.sln",
    )
    assert tuple(edge.target_id for edge in extraction.edges) == (
        "file:repo:workspace/src/Api/Api.csproj",
        "file:repo:shared/Lib/Lib.fsproj",
    )
    _assert_confidence(extraction)
    assert tuple(edge.evidence[0].start_line for edge in extraction.edges) == (2, 4)


def test_dotnet_extracts_project_references_from_slnx(tmp_path) -> None:
    extraction = _extract(
        tmp_path,
        path="nested/Workspace.slnx",
        extension=".slnx",
        source="""<Solution>
  <Folder Name="src">
    <Project Path="src/App/App.csproj" />
    <Project Path="tests/App.Tests/App.Tests.vbproj" />
  </Folder>
</Solution>
""",
    )

    assert tuple(edge.target_id for edge in extraction.edges) == (
        "file:repo:nested/src/App/App.csproj",
        "file:repo:nested/tests/App.Tests/App.Tests.vbproj",
    )
    assert tuple(
        edge.evidence[0].metadata["project_path"] for edge in extraction.edges
    ) == (
        "nested/src/App/App.csproj",
        "nested/tests/App.Tests/App.Tests.vbproj",
    )
    _assert_confidence(extraction)


def test_dotnet_extracts_project_symbols_and_references_from_project_file(
    tmp_path,
) -> None:
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
    <ProjectReference Include="..\\Shared\\Shared.fsproj" />
  </ItemGroup>
</Project>
""",
    )

    assert tuple(symbol.kind for symbol in extraction.symbols) == (
        "assembly",
        "namespace",
    )
    assert tuple(symbol.name for symbol in extraction.symbols) == (
        "Contoso.App",
        "Contoso.App.Root",
    )
    assert tuple(symbol.file_id for symbol in extraction.symbols) == (
        "file:repo:src/App/App.csproj",
        "file:repo:src/App/App.csproj",
    )
    assert tuple(edge.relation_type for edge in extraction.edges) == (
        "declares_symbol",
        "declares_symbol",
        "depends_on",
        "references_project",
    )
    package_edge = extraction.edges[2]
    assert package_edge.target_type == "package"
    assert package_edge.target_id == "Newtonsoft.Json"
    assert package_edge.evidence[0].metadata == {
        "repository_id": "repo",
        "package_name": "Newtonsoft.Json",
        "version": "13.0.3",
    }
    project_edge = extraction.edges[3]
    assert project_edge.target_id == "file:repo:src/Shared/Shared.fsproj"
    assert project_edge.evidence[0].metadata["project_path"] == (
        "src/Shared/Shared.fsproj"
    )
    _assert_confidence(extraction)


def test_dotnet_project_xml_handles_namespaces_and_child_package_versions(
    tmp_path,
) -> None:
    extraction = _extract(
        tmp_path,
        path="src/App/App.vbproj",
        extension=".vbproj",
        source="""<msb:Project xmlns:msb="http://schemas.microsoft.com/developer/msbuild/2003">
  <msb:PropertyGroup>
    <msb:AssemblyName>Contoso.VisualBasic</msb:AssemblyName>
  </msb:PropertyGroup>
  <msb:ItemGroup>
    <msb:PackageReference Include="Dapper">
      <msb:Version>2.1.66</msb:Version>
    </msb:PackageReference>
  </msb:ItemGroup>
</msb:Project>
""",
    )

    assert tuple(symbol.name for symbol in extraction.symbols) == (
        "Contoso.VisualBasic",
    )
    assert len(extraction.edges) == 2
    assert extraction.edges[1].relation_type == "depends_on"
    assert extraction.edges[1].target_id == "Dapper"
    assert extraction.edges[1].evidence[0].metadata["version"] == "2.1.66"


def test_dotnet_web_project_emits_service_and_framework_hint(tmp_path) -> None:
    extraction = _extract(
        tmp_path,
        path="src/Api/Api.csproj",
        extension=".csproj",
        source="""<Project Sdk="Microsoft.NET.Sdk.Web">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
    <AssemblyName>Contoso.Api</AssemblyName>
  </PropertyGroup>
</Project>
""",
    )

    assert tuple(service.name for service in extraction.services) == ("Contoso.Api",)
    assert tuple(service.id for service in extraction.services) == (
        "service:repo:contoso-api",
    )
    assert tuple(service.repository_id for service in extraction.services) == ("repo",)
    assert tuple(hint.framework for hint in extraction.framework_hints) == (
        "ASP.NET Core",
    )
    assert tuple(hint.repository_id for hint in extraction.framework_hints) == ("repo",)


def test_dotnet_web_project_service_falls_back_to_project_filename(
    tmp_path,
) -> None:
    extraction = _extract(
        tmp_path,
        path="src/Billing.Api/Billing.Api.csproj",
        extension=".csproj",
        source="""<Project>
  <Sdk Name="Microsoft.NET.Sdk.Web" />
  <PropertyGroup>
    <TargetFrameworks>net8.0;net9.0</TargetFrameworks>
  </PropertyGroup>
</Project>
""",
    )

    assert tuple(service.name for service in extraction.services) == ("Billing.Api",)
    assert tuple(hint.framework for hint in extraction.framework_hints) == (
        "ASP.NET Core",
    )


def test_dotnet_extracts_razor_model_inject_and_page_route(tmp_path) -> None:
    extraction = _extract(
        tmp_path,
        path="Components/Counter.razor",
        extension=".razor",
        source="""@page "/counter"
@model Contoso.Pages.CounterModel
@inject Contoso.Services.ClockService Clock

<h1>Counter</h1>
""",
    )

    assert tuple(symbol.kind for symbol in extraction.symbols) == ("model",)
    assert tuple(symbol.name for symbol in extraction.symbols) == (
        "Contoso.Pages.CounterModel",
    )
    assert extraction.symbols[0].qualified_name == "Contoso.Pages.CounterModel"
    assert extraction.symbols[0].start_line == 2
    assert tuple(service.name for service in extraction.services) == (
        "Contoso.Services.ClockService",
    )
    assert tuple(contract.kind for contract in extraction.contracts) == ("razor_route",)
    assert tuple(contract.path for contract in extraction.contracts) == ("/counter",)
    assert tuple(edge.relation_type for edge in extraction.edges) == (
        "declares_symbol",
        "injects",
        "declares_contract",
    )
    assert extraction.edges[1].target_type == "service"
    assert extraction.edges[1].target_id == (
        "service:repo:contoso-services-clockservice"
    )
    assert extraction.edges[1].evidence[0].metadata == {
        "repository_id": "repo",
        "service_type": "Contoso.Services.ClockService",
        "property_name": "Clock",
    }
    assert extraction.edges[2].target_type == "contract"
    assert extraction.edges[2].target_id == "contract:repo:razor_route:counter"
    assert extraction.contracts[0].evidence[0].metadata == {
        "repository_id": "repo",
        "route": "/counter",
    }
    _assert_confidence(extraction)


def test_dotnet_extracts_cshtml_model_inject_and_page_route(tmp_path) -> None:
    extraction = _extract(
        tmp_path,
        path="Pages/Index.cshtml",
        extension=".cshtml",
        source="""@page "/"
@inject Microsoft.Extensions.Logging.ILogger<IndexModel> Logger
@model IndexModel

<p>Hello</p>
""",
    )

    assert tuple(symbol.name for symbol in extraction.symbols) == ("IndexModel",)
    assert tuple(service.name for service in extraction.services) == (
        "Microsoft.Extensions.Logging.ILogger<IndexModel>",
    )
    assert tuple(contract.path for contract in extraction.contracts) == ("/",)
    assert tuple(edge.relation_type for edge in extraction.edges) == (
        "declares_symbol",
        "injects",
        "declares_contract",
    )
    assert extraction.contracts[0].id == "contract:repo:razor_route:root"
    assert extraction.edges[2].evidence[0].start_line == 1
    _assert_confidence(extraction)


def test_dotnet_extracts_aspnet_core_controller_contracts(tmp_path) -> None:
    _write_text(
        tmp_path / "src" / "GAPP.API" / "GAPP.API.csproj",
        """<Project Sdk="Microsoft.NET.Sdk.Web">
  <PropertyGroup>
    <AssemblyName>GAPP.API</AssemblyName>
  </PropertyGroup>
</Project>
""",
    )
    extraction = _extract(
        tmp_path,
        path="src/GAPP.API/Controllers/SchedulesController.cs",
        extension=".cs",
        source="""using Microsoft.AspNetCore.Mvc;

namespace GAPP.API.Controllers;

[ApiController]
[Route("api/[controller]")]
public sealed class SchedulesController : ControllerBase
{
    [HttpGet("{scheduleId}")]
    public ActionResult<ScheduleResponse> Read(string scheduleId)
    {
        return Ok();
    }

    [HttpPost("search")]
    [ProducesResponseType(typeof(SearchSchedulesResponse), StatusCodes.Status200OK)]
    public ActionResult<SearchSchedulesResponse> Search(
        [FromBody] SearchSchedulesRequest request)
    {
        return Ok();
    }
}
""",
    )

    assert tuple(contract.id for contract in extraction.contracts) == (
        "contract:repo:http:get:api-schedules-scheduleid",
        "contract:repo:http:post:api-schedules-search",
    )
    assert tuple(contract.path for contract in extraction.contracts) == (
        "/api/Schedules/{scheduleId}",
        "/api/Schedules/search",
    )
    assert tuple(contract.method for contract in extraction.contracts) == (
        "GET",
        "POST",
    )
    assert tuple(contract.operation_id for contract in extraction.contracts) == (
        "Schedules.Read",
        "Schedules.Search",
    )
    assert tuple(contract.producer_hint for contract in extraction.contracts) == (
        "GAPP.API",
        "GAPP.API",
    )
    assert extraction.contracts[1].request_dtos == ("SearchSchedulesRequest",)
    assert extraction.contracts[1].response_dtos == ("SearchSchedulesResponse",)
    assert tuple(
        (edge.source_type, edge.relation_type, edge.target_type)
        for edge in extraction.edges
    ) == (
        ("file", "declares_contract", "contract"),
        ("service", "produces", "contract"),
        ("file", "declares_contract", "contract"),
        ("service", "produces", "contract"),
    )
    assert extraction.contracts[0].evidence[0].start_line == 9
    assert extraction.edges[1].source_id == "service:repo:gapp-api"
    _assert_confidence(extraction)


def test_dotnet_extracts_controller_dtos_from_implicit_body_and_wrapped_responses(
    tmp_path,
) -> None:
    _write_text(
        tmp_path / "src" / "Billing.Api" / "Billing.Api.csproj",
        """<Project Sdk="Microsoft.NET.Sdk.Web">
  <PropertyGroup>
    <AssemblyName>Billing.Api</AssemblyName>
  </PropertyGroup>
</Project>
""",
    )
    extraction = _extract(
        tmp_path,
        path="src/Billing.Api/Controllers/InvoicesController.cs",
        extension=".cs",
        source="""using Microsoft.AspNetCore.Http.HttpResults;
using Microsoft.AspNetCore.Mvc;

namespace Billing.Api.Controllers;

[ApiController]
[Route("api/[controller]")]
public sealed class InvoicesController : ControllerBase
{
    [HttpPost("{accountId}")]
    public async Task<ActionResult<InvoiceResponse>> Create(
        Guid accountId,
        CreateInvoiceRequest request,
        CancellationToken cancellationToken)
    {
        return Ok(new InvoiceResponse());
    }

    [HttpGet("{invoiceId}")]
    public Results<Ok<InvoiceResponse>, NotFound> Read(
        [FromRoute] Guid invoiceId,
        HttpContext httpContext)
    {
        return TypedResults.Ok(new InvoiceResponse());
    }

    [HttpGet("summary")]
    public InvoiceSummaryResponse Summary()
    {
        return new InvoiceSummaryResponse();
    }
}
""",
    )

    create_contract = extraction.contracts[0]
    read_contract = extraction.contracts[1]
    summary_contract = extraction.contracts[2]

    assert create_contract.request_dtos == ("CreateInvoiceRequest",)
    assert create_contract.response_dtos == ("InvoiceResponse",)
    assert read_contract.request_dtos == ()
    assert read_contract.response_dtos == ("InvoiceResponse",)
    assert summary_contract.response_dtos == ("InvoiceSummaryResponse",)


def test_dotnet_extracts_typed_minimal_api_request_and_response_dtos(
    tmp_path,
) -> None:
    _write_text(
        tmp_path / "src" / "Inventory.Api" / "Inventory.Api.csproj",
        """<Project Sdk="Microsoft.NET.Sdk.Web">
  <PropertyGroup>
    <AssemblyName>Inventory.Api</AssemblyName>
  </PropertyGroup>
</Project>
""",
    )
    extraction = _extract(
        tmp_path,
        path="src/Inventory.Api/Program.cs",
        extension=".cs",
        source="""using Microsoft.AspNetCore.Http.HttpResults;

var builder = WebApplication.CreateBuilder(args);
var app = builder.Build();

app.MapPost("/items", (
    CreateItemRequest request,
    CancellationToken cancellationToken) =>
    TypedResults.Ok(new CreateItemResponse()));

app.MapGet("/items/{itemId}", Results<Ok<ItemResponse>, NotFound> (
    Guid itemId,
    HttpContext httpContext) =>
    TypedResults.Ok(new ItemResponse()));
""",
    )

    assert tuple(contract.path for contract in extraction.contracts) == (
        "/items",
        "/items/{itemId}",
    )
    assert extraction.contracts[0].request_dtos == ("CreateItemRequest",)
    assert extraction.contracts[0].response_dtos == ("CreateItemResponse",)
    assert extraction.contracts[1].request_dtos == ()
    assert extraction.contracts[1].response_dtos == ("ItemResponse",)


def test_dotnet_project_xml_handles_missing_optional_values(tmp_path) -> None:
    extraction = _extract(
        tmp_path,
        path="src/App/App.fsproj",
        extension=".fsproj",
        source="""<Project>
  <PropertyGroup>
    <AssemblyName />
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Expecto" />
    <PackageReference Version="1.2.3" />
    <ProjectReference />
  </ItemGroup>
</Project>
""",
    )

    assert extraction.symbols == ()
    assert len(extraction.edges) == 1
    assert extraction.edges[0].relation_type == "depends_on"
    assert extraction.edges[0].target_id == "Expecto"
    assert extraction.edges[0].evidence[0].metadata["version"] is None


def test_dotnet_project_xml_parse_errors_return_empty_extraction(tmp_path) -> None:
    extraction = _extract(
        tmp_path,
        path="src/App/App.csproj",
        extension=".csproj",
        source="<Project>",
    )

    assert extraction == SymbolExtraction()


def test_dotnet_returns_empty_extraction_for_non_dotnet_files(tmp_path) -> None:
    extraction = _extract(
        tmp_path,
        path="src/App/App.txt",
        extension=".txt",
        source="<Project />\n",
    )

    assert extraction == SymbolExtraction()


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
            kind="metadata",
            extension=extension,
            content_hash="hash",
            modified_at="2026-06-20T00:00:00+00:00",
            language="Solution",
        ),
    )


def _write_text(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _assert_confidence(extraction: SymbolExtraction) -> None:
    assert tuple(edge.confidence for edge in extraction.edges) == (
        DOTNET_CONFIDENCE,
    ) * len(extraction.edges)
    assert tuple(edge.evidence[0].confidence for edge in extraction.edges) == (
        DOTNET_CONFIDENCE,
    ) * len(extraction.edges)
    assert {edge.evidence[0].extractor_name for edge in extraction.edges} == {"dotnet"}
