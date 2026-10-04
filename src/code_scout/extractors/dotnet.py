from __future__ import annotations

import posixpath
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from code_scout.discovery import DiscoveredFile
from code_scout.extractors import RepositoryFrameworkHint, SymbolExtraction
from code_scout.models.graph import Contract, Edge, Evidence, Service, Symbol
from code_scout.utils.identifiers import canonical_http_contract_id, slugify_identifier

EXTRACTOR_NAME = "dotnet"
DOTNET_CONFIDENCE = 1.0
PROJECT_EXTENSIONS = frozenset({".csproj", ".fsproj", ".vbproj"})
RAZOR_EXTENSIONS = frozenset({".razor", ".cshtml"})
CSHARP_EXTENSIONS = frozenset({".cs"})

SLN_PROJECT_PATTERN = re.compile(
    r'^Project\([^)]+\)\s*=\s*"[^"]*",\s*"([^"]+\.[^"]*proj)"',
    re.IGNORECASE,
)
RAZOR_MODEL_PATTERN = re.compile(r"^\s*@model\s+(?P<name>\S+)", re.MULTILINE)
RAZOR_INJECT_PATTERN = re.compile(
    r"^\s*@inject\s+(?P<service_type>\S+)\s+(?P<property_name>\w+)",
    re.MULTILINE,
)
RAZOR_PAGE_PATTERN = re.compile(r'^\s*@page\s+"(?P<route>[^"]+)"', re.MULTILINE)
ATTRIBUTE_PATTERN = re.compile(r"\[(?P<name>[\w.]+)(?:\((?P<args>.*)\))?\]")
CLASS_PATTERN = re.compile(
    r"\b(?:public|internal|private|protected)?\s*"
    r"(?:sealed\s+|abstract\s+|partial\s+)*class\s+(?P<name>\w+)",
)
METHOD_PATTERN = re.compile(
    r"\b(?:public|internal|private|protected)\s+"
    r"(?:async\s+)?(?P<return_type>[\w.<>,?\[\]\s]+?)\s+(?P<name>\w+)\s*\(",
)
HTTP_ATTRIBUTE_METHODS = {
    "HttpGet": "GET",
    "HttpPost": "POST",
    "HttpPut": "PUT",
    "HttpPatch": "PATCH",
    "HttpDelete": "DELETE",
}
MINIMAL_API_METHODS = {
    "MapGet": "GET",
    "MapPost": "POST",
    "MapPut": "PUT",
    "MapPatch": "PATCH",
    "MapDelete": "DELETE",
}
BODY_HTTP_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
BODY_BINDING_ATTRIBUTES = frozenset({"FromBody", "FromForm"})
NON_BODY_BINDING_ATTRIBUTES = frozenset(
    {
        "FromHeader",
        "FromQuery",
        "FromRoute",
        "FromServices",
        "FromKeyedServices",
    },
)
INFRASTRUCTURE_TYPES = frozenset(
    {
        "ActionResult",
        "CancellationToken",
        "ClaimsPrincipal",
        "Controller",
        "ControllerBase",
        "HttpContext",
        "HttpRequest",
        "HttpResponse",
        "IActionResult",
        "IFormFile",
        "IFormFileCollection",
        "ILogger",
        "IResult",
        "ModelStateDictionary",
        "BadRequest",
        "Conflict",
        "Forbid",
        "NoContent",
        "NotFound",
        "ProblemHttpResult",
        "RedirectHttpResult",
        "SignInHttpResult",
        "SignOutHttpResult",
        "UnauthorizedHttpResult",
        "ProblemDetails",
        "StatusCodes",
        "Task",
        "TypedResults",
        "ValueTask",
        "ValidationProblemDetails",
    },
)
PRIMITIVE_TYPES = frozenset(
    {
        "bool",
        "byte",
        "char",
        "DateOnly",
        "DateTime",
        "DateTimeOffset",
        "decimal",
        "double",
        "float",
        "Guid",
        "int",
        "long",
        "nint",
        "nuint",
        "object",
        "sbyte",
        "short",
        "string",
        "TimeOnly",
        "TimeSpan",
        "uint",
        "ulong",
        "ushort",
        "void",
    },
)
RESPONSE_WRAPPER_TYPES = frozenset(
    {
        "Accepted",
        "AcceptedAtRoute",
        "AcceptedAtRouteHttpResult",
        "Created",
        "CreatedAtRoute",
        "CreatedAtRouteHttpResult",
        "JsonHttpResult",
        "Ok",
        "OkObjectResult",
        "Results",
        "Task",
        "TypedResults",
        "ValueTask",
    },
)


@dataclass(frozen=True)
class ProjectReference:
    path: str
    line_number: int | None = None


@dataclass(frozen=True)
class ProjectSymbol:
    name: str
    kind: str
    line_number: int | None = None


@dataclass(frozen=True)
class PackageReference:
    name: str
    version: str | None = None
    line_number: int | None = None


@dataclass(frozen=True)
class RazorInject:
    service_type: str
    property_name: str
    line_number: int | None = None


@dataclass(frozen=True)
class RazorRoute:
    path: str
    line_number: int | None = None


@dataclass(frozen=True)
class CSharpAttribute:
    name: str
    arguments: str
    line_number: int


@dataclass(frozen=True)
class AspNetEndpoint:
    controller_name: str
    action_name: str
    method: str
    path: str
    operation_id: str
    producer_hint: str
    request_dtos: tuple[str, ...]
    response_dtos: tuple[str, ...]
    line_number: int


@dataclass(frozen=True)
class ProjectMetadata:
    assembly_name: str | None = None
    is_web_sdk: bool = False
    target_frameworks: tuple[str, ...] = ()


class DotnetExtractor:
    """Extract .NET solution and project metadata."""

    def extract(self, discovered_file: DiscoveredFile) -> SymbolExtraction:
        extension = discovered_file.extension.lower()
        if extension == ".sln":
            references = _project_references_from_sln(discovered_file)
            return SymbolExtraction(
                edges=tuple(
                    _project_reference_edge(discovered_file, reference)
                    for reference in _dedupe_references(references)
                ),
            )
        if extension == ".slnx":
            references = _project_references_from_slnx(discovered_file)
            return SymbolExtraction(
                edges=tuple(
                    _project_reference_edge(discovered_file, reference)
                    for reference in _dedupe_references(references)
                ),
            )
        if extension in PROJECT_EXTENSIONS:
            return _project_file_extraction(discovered_file)
        if extension in RAZOR_EXTENSIONS:
            return _razor_file_extraction(discovered_file)
        if extension in CSHARP_EXTENSIONS:
            return _csharp_file_extraction(discovered_file)
        return SymbolExtraction()


def _project_references_from_sln(
    discovered_file: DiscoveredFile,
) -> tuple[ProjectReference, ...]:
    references: list[ProjectReference] = []
    for line_number, line in enumerate(
        Path(discovered_file.absolute_path)
        .read_text(
            encoding="utf-8",
            errors="replace",
        )
        .splitlines(),
        start=1,
    ):
        match = SLN_PROJECT_PATTERN.match(line)
        if match is None:
            continue

        reference_path = _resolve_reference_path(discovered_file.path, match.group(1))
        if reference_path:
            references.append(ProjectReference(reference_path, line_number))
    return tuple(references)


def _project_references_from_slnx(
    discovered_file: DiscoveredFile,
) -> tuple[ProjectReference, ...]:
    source = Path(discovered_file.absolute_path).read_text(
        encoding="utf-8",
        errors="replace",
    )
    try:
        root = ET.fromstring(source)
    except ET.ParseError:
        return ()

    references: list[ProjectReference] = []
    for element in root.iter():
        path = element.attrib.get("Path")
        if path is None:
            continue

        reference_path = _resolve_reference_path(discovered_file.path, path)
        if reference_path:
            references.append(
                ProjectReference(reference_path, _line_number_for_path(source, path)),
            )
    return tuple(references)


def _project_file_extraction(discovered_file: DiscoveredFile) -> SymbolExtraction:
    source = Path(discovered_file.absolute_path).read_text(
        encoding="utf-8",
        errors="replace",
    )
    try:
        root = ET.fromstring(source)
    except ET.ParseError:
        return SymbolExtraction()

    project_metadata = ProjectMetadata(is_web_sdk=_is_web_sdk_project(root))
    project_symbols: list[ProjectSymbol] = []
    package_references: list[PackageReference] = []
    project_references: list[ProjectReference] = []

    for element in root.iter():
        name = _local_name(element.tag)
        if name in {"AssemblyName", "RootNamespace"}:
            value = _text_value(element)
            if value is None:
                continue
            if name == "AssemblyName":
                project_metadata = ProjectMetadata(
                    assembly_name=value,
                    is_web_sdk=project_metadata.is_web_sdk,
                    target_frameworks=project_metadata.target_frameworks,
                )
            project_symbols.append(
                ProjectSymbol(
                    name=value,
                    kind="assembly" if name == "AssemblyName" else "namespace",
                    line_number=_line_number_for_element(source, name),
                ),
            )
        elif name in {"TargetFramework", "TargetFrameworks"}:
            target_frameworks = _target_framework_values(_text_value(element))
            if target_frameworks:
                project_metadata = ProjectMetadata(
                    assembly_name=project_metadata.assembly_name,
                    is_web_sdk=project_metadata.is_web_sdk,
                    target_frameworks=(
                        *project_metadata.target_frameworks,
                        *target_frameworks,
                    ),
                )
        elif name == "PackageReference":
            package_name = _attribute_value(element, "Include")
            if package_name is None:
                continue
            package_references.append(
                PackageReference(
                    name=package_name,
                    version=_attribute_value(element, "Version")
                    or _child_text_value(element, "Version"),
                    line_number=_line_number_for_path(source, package_name),
                ),
            )
        elif name == "ProjectReference":
            include = _attribute_value(element, "Include")
            if include is None:
                continue
            reference_path = _resolve_reference_path(discovered_file.path, include)
            if reference_path:
                project_references.append(
                    ProjectReference(
                        reference_path,
                        _line_number_for_path(source, include),
                    ),
                )

    symbols = tuple(
        _project_symbol(discovered_file, project_symbol)
        for project_symbol in project_symbols
    )
    return SymbolExtraction(
        symbols=symbols,
        services=_project_services(discovered_file, project_metadata),
        framework_hints=_project_framework_hints(discovered_file, project_metadata),
        edges=(
            *tuple(_symbol_edge(discovered_file, symbol) for symbol in symbols),
            *tuple(
                _package_reference_edge(discovered_file, package_reference)
                for package_reference in package_references
            ),
            *tuple(
                _project_reference_edge(discovered_file, reference)
                for reference in _dedupe_references(tuple(project_references))
            ),
        ),
    )


def _razor_file_extraction(discovered_file: DiscoveredFile) -> SymbolExtraction:
    source = Path(discovered_file.absolute_path).read_text(
        encoding="utf-8",
        errors="replace",
    )
    model_symbols = tuple(
        _project_symbol(
            discovered_file,
            ProjectSymbol(
                name=match.group("name"),
                kind="model",
                line_number=_line_number_for_offset(source, match.start()),
            ),
        )
        for match in RAZOR_MODEL_PATTERN.finditer(source)
    )
    injects = tuple(
        RazorInject(
            service_type=match.group("service_type"),
            property_name=match.group("property_name"),
            line_number=_line_number_for_offset(source, match.start()),
        )
        for match in RAZOR_INJECT_PATTERN.finditer(source)
    )
    routes = tuple(
        RazorRoute(
            path=match.group("route"),
            line_number=_line_number_for_offset(source, match.start()),
        )
        for match in RAZOR_PAGE_PATTERN.finditer(source)
    )
    deduped_routes = _dedupe_razor_routes(routes)
    contracts = tuple(
        _razor_route_contract(discovered_file, route) for route in deduped_routes
    )

    return SymbolExtraction(
        symbols=model_symbols,
        services=tuple(
            _injected_service(discovered_file, inject) for inject in injects
        ),
        contracts=contracts,
        edges=(
            *tuple(_symbol_edge(discovered_file, symbol) for symbol in model_symbols),
            *tuple(_inject_edge(discovered_file, inject) for inject in injects),
            *tuple(
                _contract_edge(discovered_file, contract, route.line_number)
                for contract, route in zip(contracts, deduped_routes, strict=True)
            ),
        ),
    )


def _csharp_file_extraction(discovered_file: DiscoveredFile) -> SymbolExtraction:
    source = Path(discovered_file.absolute_path).read_text(
        encoding="utf-8",
        errors="replace",
    )
    project_metadata = _nearest_project_metadata(discovered_file)
    endpoints = _aspnet_endpoints_from_source(
        source,
        producer_hint=(
            project_metadata.assembly_name
            or _controller_hint_from_path(discovered_file.path)
        ),
    )
    contracts = tuple(
        _aspnet_endpoint_contract(discovered_file, endpoint) for endpoint in endpoints
    )
    return SymbolExtraction(
        contracts=contracts,
        edges=tuple(
            edge
            for contract, endpoint in zip(contracts, endpoints, strict=True)
            for edge in (
                _contract_edge(discovered_file, contract, endpoint.line_number),
                *(
                    ()
                    if project_metadata.assembly_name is None
                    else (
                        _service_contract_edge(
                            discovered_file,
                            service_name=project_metadata.assembly_name,
                            contract=contract,
                            line_number=endpoint.line_number,
                        ),
                    )
                ),
            )
        ),
    )


def _aspnet_endpoints_from_source(
    source: str,
    *,
    producer_hint: str,
) -> tuple[AspNetEndpoint, ...]:
    endpoints: list[AspNetEndpoint] = []
    pending_attributes: list[CSharpAttribute] = []
    controller_name: str | None = None
    controller_route: str | None = None

    for line_number, line in enumerate(source.splitlines(), start=1):
        attributes = _attributes_from_line(line, line_number)
        if attributes:
            pending_attributes.extend(attributes)
            continue

        class_match = CLASS_PATTERN.search(line)
        if class_match is not None:
            candidate_name = class_match.group("name")
            if _is_controller(candidate_name, tuple(pending_attributes)):
                controller_name = candidate_name
                controller_route = _route_template(
                    tuple(pending_attributes),
                    attribute_name="Route",
                )
            else:
                controller_name = None
                controller_route = None
            pending_attributes = []
            continue

        method_match = METHOD_PATTERN.search(line)
        if method_match is not None:
            if controller_name is not None:
                endpoints.extend(
                    _action_endpoints(
                        controller_name=controller_name,
                        controller_route=controller_route,
                        method_name=method_match.group("name"),
                        return_type=method_match.group("return_type"),
                        method_attributes=tuple(pending_attributes),
                        line_number=line_number,
                        source=source,
                        producer_hint=producer_hint,
                    ),
                )
            pending_attributes = []
            continue

        if line.strip() and not line.strip().startswith("//"):
            pending_attributes = []

    endpoints.extend(_minimal_api_endpoints_from_source(source, producer_hint))
    return tuple(endpoints)


def _attributes_from_line(line: str, line_number: int) -> tuple[CSharpAttribute, ...]:
    return tuple(
        CSharpAttribute(
            name=match.group("name")
            .rsplit(".", maxsplit=1)[-1]
            .removesuffix(
                "Attribute",
            ),
            arguments=(match.group("args") or "").strip(),
            line_number=line_number,
        )
        for match in ATTRIBUTE_PATTERN.finditer(line)
    )


def _is_controller(name: str, attributes: tuple[CSharpAttribute, ...]) -> bool:
    attribute_names = {attribute.name for attribute in attributes}
    return name.endswith("Controller") or "ApiController" in attribute_names


def _action_endpoints(
    *,
    controller_name: str,
    controller_route: str | None,
    method_name: str,
    return_type: str,
    method_attributes: tuple[CSharpAttribute, ...],
    line_number: int,
    source: str,
    producer_hint: str,
) -> tuple[AspNetEndpoint, ...]:
    http_attributes = tuple(
        attribute
        for attribute in method_attributes
        if attribute.name in HTTP_ATTRIBUTE_METHODS
    )
    if not http_attributes:
        return ()

    route_attribute = _route_template(method_attributes, attribute_name="Route")
    request_dtos = _request_dtos_for_method(
        source,
        line_number,
        http_method=HTTP_ATTRIBUTE_METHODS[http_attributes[0].name],
    )
    response_dtos = _ordered_unique_names(
        (
            *_response_dtos_from_attributes(method_attributes),
            *_response_dtos_from_return_type(return_type),
        ),
    )
    return tuple(
        AspNetEndpoint(
            controller_name=controller_name,
            action_name=method_name,
            method=HTTP_ATTRIBUTE_METHODS[attribute.name],
            path=_compose_route(
                controller_route,
                _route_template((attribute,), attribute_name=attribute.name)
                or route_attribute,
                controller_name=controller_name,
                action_name=method_name,
            ),
            operation_id=f"{controller_name.removesuffix('Controller')}.{method_name}",
            producer_hint=producer_hint,
            request_dtos=request_dtos,
            response_dtos=response_dtos,
            line_number=attribute.line_number,
        )
        for attribute in http_attributes
    )


def _route_template(
    attributes: tuple[CSharpAttribute, ...],
    *,
    attribute_name: str,
) -> str | None:
    for attribute in attributes:
        if attribute.name != attribute_name:
            continue
        return _first_string_literal(attribute.arguments)
    return None


def _first_string_literal(arguments: str) -> str | None:
    match = re.search(r'@"([^"]+)"|"([^"]+)"', arguments)
    if match is None:
        return None
    return match.group(1) or match.group(2)


def _compose_route(
    controller_route: str | None,
    action_route: str | None,
    *,
    controller_name: str,
    action_name: str,
) -> str:
    parts = [
        _replace_route_tokens(controller_route or "", controller_name, action_name),
        _replace_route_tokens(action_route or "", controller_name, action_name),
    ]
    if any(part.startswith(("http://", "https://", "/")) for part in parts if part):
        parts = [next(part for part in parts if part)]
    route = "/".join(part.strip("/") for part in parts if part.strip("/"))
    return f"/{route}" if route else "/"


def _replace_route_tokens(
    route: str,
    controller_name: str,
    action_name: str,
) -> str:
    controller_token = controller_name.removesuffix("Controller")
    return (
        route.replace("[controller]", controller_token)
        .replace("[Controller]", controller_token)
        .replace("[action]", action_name)
        .replace("[Action]", action_name)
    )


def _request_dtos_for_method(
    source: str,
    method_line_number: int,
    *,
    http_method: str,
) -> tuple[str, ...]:
    lines = source.splitlines()
    signature = "\n".join(lines[method_line_number - 1 : method_line_number + 20])
    return _request_dtos_from_parameters(
        _parameters_from_signature(signature),
        http_method=http_method,
    )


def _request_dtos_from_parameters(
    parameters: tuple[str, ...],
    *,
    http_method: str,
) -> tuple[str, ...]:
    candidates: list[str] = []
    fallback_shapes: list[str] = []

    for parameter in parameters:
        attributes = _parameter_attribute_names(parameter)
        parameter_type = _parameter_type(parameter)
        if parameter_type is None:
            continue

        dto_names = _dto_names_from_type(parameter_type)
        if not dto_names:
            continue

        if attributes & BODY_BINDING_ATTRIBUTES:
            candidates.extend(dto_names)
        elif attributes & NON_BODY_BINDING_ATTRIBUTES:
            fallback_shapes.extend(dto_names)
        elif http_method in BODY_HTTP_METHODS and not _is_primitive_or_infrastructure(
            parameter_type,
        ):
            candidates.extend(dto_names)
        else:
            fallback_shapes.extend(dto_names)

    return _ordered_unique_names(tuple(candidates or fallback_shapes))


def _response_dtos_from_attributes(
    attributes: tuple[CSharpAttribute, ...],
) -> tuple[str, ...]:
    return _ordered_unique_names(
        tuple(
            dto
            for attribute in attributes
            if attribute.name == "ProducesResponseType"
            for match in re.finditer(
                r"typeof\((?P<type>[\w.<>,?\[\]\s]+)\)",
                attribute.arguments,
            )
            for dto in _dto_names_from_type(match.group("type"))
        ),
    )


def _response_dtos_from_return_type(return_type: str) -> tuple[str, ...]:
    return _ordered_unique_names(
        tuple(dto for dto in _dto_names_from_type(return_type, response_position=True)),
    )


def _minimal_api_endpoints_from_source(
    source: str,
    producer_hint: str,
) -> tuple[AspNetEndpoint, ...]:
    endpoints: list[AspNetEndpoint] = []
    pattern = re.compile(
        r"\.(?P<name>MapGet|MapPost|MapPut|MapPatch|MapDelete)\s*\(",
    )

    for match in pattern.finditer(source):
        call = _balanced_span(source, match.end() - 1, "(", ")")
        if call is None:
            continue
        route = _first_string_literal(call)
        if route is None:
            continue

        lambda_match = re.search(
            r"(?:async\s+)?(?:\((?P<parenthesized>[^)]*)\)|(?P<single>\w+))\s*=>",
            call,
            re.DOTALL,
        )
        parameters = (
            _split_top_level(lambda_match.group("parenthesized") or "")
            if lambda_match is not None and lambda_match.group("parenthesized")
            else ()
        )
        response_dtos = _minimal_api_response_dtos(call)
        line_number = _line_number_for_offset(source, match.start())
        method = MINIMAL_API_METHODS[match.group("name")]
        operation_name = _minimal_api_operation_name(method, route)
        endpoints.append(
            AspNetEndpoint(
                controller_name=producer_hint,
                action_name=operation_name,
                method=method,
                path=_normalize_route_path(route),
                operation_id=f"{producer_hint}.{operation_name}",
                producer_hint=producer_hint,
                request_dtos=_request_dtos_from_parameters(
                    parameters,
                    http_method=method,
                ),
                response_dtos=response_dtos,
                line_number=line_number,
            ),
        )

    return tuple(endpoints)


def _minimal_api_response_dtos(call: str) -> tuple[str, ...]:
    response_dtos: list[str] = []
    delegate_match = re.search(
        r"(?:async\s+)?(?:(?P<return_type>[\w.<>,?\[\]\s]+?)\s+)?"
        r"(?:\([^)]*\)|\w+)\s*=>",
        call,
        re.DOTALL,
    )
    if delegate_match is not None and delegate_match.group("return_type"):
        response_dtos.extend(
            _response_dtos_from_return_type(delegate_match.group("return_type")),
        )

    for result_match in re.finditer(
        r"(?:TypedResults|Results)\.(?:Ok|Created|CreatedAtRoute|Accepted)"
        r"\s*(?:<(?P<type>[\w.<>,?\[\]\s]+)>)?\s*\(",
        call,
    ):
        explicit_type = result_match.group("type")
        if explicit_type:
            response_dtos.extend(_dto_names_from_type(explicit_type))
            continue
        invocation = _balanced_span(call, result_match.end() - 1, "(", ")")
        if invocation is None:
            continue
        response_dtos.extend(_constructor_response_dtos(invocation))

    return _ordered_unique_names(tuple(response_dtos))


def _constructor_response_dtos(arguments: str) -> tuple[str, ...]:
    return tuple(
        _simple_type_name(match.group("type"))
        for match in re.finditer(r"\bnew\s+(?P<type>[\w.]+)\s*\(", arguments)
        if _is_dto_name(_simple_type_name(match.group("type")))
    )


def _parameters_from_signature(signature: str) -> tuple[str, ...]:
    start = signature.find("(")
    if start == -1:
        return ()
    arguments = _balanced_span(signature, start, "(", ")")
    if arguments is None:
        return ()
    return _split_top_level(arguments)


def _parameter_attribute_names(parameter: str) -> set[str]:
    return {
        match.group("name").rsplit(".", maxsplit=1)[-1].removesuffix("Attribute")
        for match in re.finditer(r"\[(?P<name>[\w.]+)(?:\([^]]*\))?\]", parameter)
    }


def _parameter_type(parameter: str) -> str | None:
    without_attributes = re.sub(r"\[[^]]+\]", " ", parameter).strip()
    without_default = without_attributes.split("=", maxsplit=1)[0].strip()
    tokens = without_default.split()
    if len(tokens) < 2:
        return None
    return " ".join(token for token in tokens[:-1] if token not in {"in", "ref", "out"})


def _dto_names_from_type(
    type_expression: str,
    *,
    response_position: bool = False,
) -> tuple[str, ...]:
    expression = _clean_type_expression(type_expression)
    if not expression:
        return ()

    outer = _outer_type_name(expression)
    inner = _generic_inner(expression)
    if inner is None:
        if _is_dto_name(outer):
            return (outer,)
        return ()

    if outer in {"ActionResult", "IActionResult"}:
        return _dto_names_from_type(inner, response_position=True)
    if outer in {"Task", "ValueTask"}:
        return _dto_names_from_type(inner, response_position=response_position)
    if outer == "Results":
        return _ordered_unique_names(
            tuple(
                dto
                for part in _split_top_level(inner)
                for dto in _dto_names_from_type(part, response_position=True)
            ),
        )
    if outer in RESPONSE_WRAPPER_TYPES:
        return _dto_names_from_type(inner, response_position=True)

    if _is_collection_type(outer):
        return _dto_names_from_type(inner, response_position=response_position)

    if _is_dto_name(outer):
        return (outer,)
    return _ordered_unique_names(
        tuple(
            dto
            for part in _split_top_level(inner)
            for dto in _dto_names_from_type(part, response_position=response_position)
        ),
    )


def _clean_type_expression(type_expression: str) -> str:
    return (
        type_expression.strip()
        .removeprefix("async ")
        .replace("?", "")
        .replace("[]", "")
    )


def _outer_type_name(type_expression: str) -> str:
    return _simple_type_name(type_expression.split("<", maxsplit=1)[0].strip())


def _generic_inner(type_expression: str) -> str | None:
    start = type_expression.find("<")
    if start == -1:
        return None
    return _balanced_span(type_expression, start, "<", ">")


def _simple_type_name(type_expression: str) -> str:
    return type_expression.strip().split(".")[-1]


def _is_collection_type(type_name: str) -> bool:
    return type_name in {
        "Collection",
        "HashSet",
        "ICollection",
        "IEnumerable",
        "IList",
        "IReadOnlyCollection",
        "IReadOnlyList",
        "ISet",
        "List",
        "ReadOnlyCollection",
        "Seq",
    }


def _is_primitive_or_infrastructure(type_expression: str) -> bool:
    outer = _outer_type_name(_clean_type_expression(type_expression))
    return outer in PRIMITIVE_TYPES or outer in INFRASTRUCTURE_TYPES


def _is_dto_name(name: str) -> bool:
    return (
        bool(re.fullmatch(r"[A-Za-z_]\w*", name))
        and name not in PRIMITIVE_TYPES
        and name not in INFRASTRUCTURE_TYPES
    )


def _split_top_level(value: str) -> tuple[str, ...]:
    parts: list[str] = []
    start = 0
    angle_depth = 0
    paren_depth = 0
    bracket_depth = 0
    for index, char in enumerate(value):
        if char == "<":
            angle_depth += 1
        elif char == ">" and angle_depth:
            angle_depth -= 1
        elif char == "(":
            paren_depth += 1
        elif char == ")" and paren_depth:
            paren_depth -= 1
        elif char == "[":
            bracket_depth += 1
        elif char == "]" and bracket_depth:
            bracket_depth -= 1
        elif (
            char == "," and angle_depth == 0 and paren_depth == 0 and bracket_depth == 0
        ):
            part = value[start:index].strip()
            if part:
                parts.append(part)
            start = index + 1
    final_part = value[start:].strip()
    if final_part:
        parts.append(final_part)
    return tuple(parts)


def _balanced_span(
    source: str,
    start_index: int,
    open_char: str,
    close_char: str,
) -> str | None:
    if start_index >= len(source) or source[start_index] != open_char:
        return None
    depth = 0
    for index in range(start_index, len(source)):
        char = source[index]
        if char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return source[start_index + 1 : index]
    return None


def _normalize_route_path(route: str) -> str:
    return route if route.startswith("/") else f"/{route}"


def _minimal_api_operation_name(method: str, route: str) -> str:
    route_slug = slugify_identifier(route, fallback="root").replace("-", "_")
    return f"{method.lower()}_{route_slug}"


def _ordered_unique_names(names: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    unique: list[str] = []
    for name in names:
        if not name or name in seen:
            continue
        seen.add(name)
        unique.append(name)
    return tuple(unique)


def _nearest_project_metadata(discovered_file: DiscoveredFile) -> ProjectMetadata:
    current = Path(discovered_file.absolute_path).parent
    while True:
        projects = sorted(current.glob("*.csproj"))
        if projects:
            return _project_metadata_from_path(projects[0])
        if current.parent == current:
            return ProjectMetadata()
        current = current.parent


def _project_metadata_from_path(path: Path) -> ProjectMetadata:
    source = path.read_text(encoding="utf-8", errors="replace")
    try:
        root = ET.fromstring(source)
    except ET.ParseError:
        return ProjectMetadata(assembly_name=path.stem)

    assembly_name = path.stem
    for element in root.iter():
        if _local_name(element.tag) == "AssemblyName":
            assembly_name = _text_value(element) or assembly_name
            break
    return ProjectMetadata(
        assembly_name=assembly_name,
        is_web_sdk=_is_web_sdk_project(root),
    )


def _controller_hint_from_path(path: str) -> str:
    return Path(path).stem


def _local_name(tag: str) -> str:
    return tag.rsplit("}", maxsplit=1)[-1]


def _text_value(element: ET.Element) -> str | None:
    if element.text is None:
        return None
    value = element.text.strip()
    return value or None


def _child_text_value(element: ET.Element, child_name: str) -> str | None:
    for child in element:
        if _local_name(child.tag) == child_name:
            return _text_value(child)
    return None


def _attribute_value(element: ET.Element, attribute_name: str) -> str | None:
    value = element.attrib.get(attribute_name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _is_web_sdk_project(root: ET.Element) -> bool:
    sdk_attribute = _attribute_value(root, "Sdk")
    if _contains_web_sdk(sdk_attribute):
        return True

    for element in root.iter():
        if _local_name(element.tag) != "Sdk":
            continue
        if _contains_web_sdk(_attribute_value(element, "Name")):
            return True
        if _contains_web_sdk(_text_value(element)):
            return True
    return False


def _contains_web_sdk(value: str | None) -> bool:
    if value is None:
        return False
    return any(
        sdk.strip() == "Microsoft.NET.Sdk.Web" for sdk in re.split(r"[;,]", value)
    )


def _target_framework_values(value: str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(target.strip() for target in value.split(";") if target.strip())


def _project_services(
    discovered_file: DiscoveredFile,
    metadata: ProjectMetadata,
) -> tuple[Service, ...]:
    if not metadata.is_web_sdk:
        return ()

    service_name = metadata.assembly_name or Path(discovered_file.path).stem
    return (
        Service(
            id=_service_id(discovered_file.repository_id, service_name),
            name=service_name,
            repository_id=discovered_file.repository_id,
            description="ASP.NET Core web project",
            evidence=(
                Evidence(
                    file_path=discovered_file.path,
                    extractor_name=EXTRACTOR_NAME,
                    confidence=DOTNET_CONFIDENCE,
                    start_line=1,
                    end_line=1,
                    metadata={
                        "repository_id": discovered_file.repository_id,
                        "service_name": service_name,
                        "project_type": "aspnet_core_web",
                    },
                ),
            ),
        ),
    )


def _project_framework_hints(
    discovered_file: DiscoveredFile,
    metadata: ProjectMetadata,
) -> tuple[RepositoryFrameworkHint, ...]:
    if not metadata.is_web_sdk and not any(
        target.startswith("net") for target in metadata.target_frameworks
    ):
        return ()
    return (
        RepositoryFrameworkHint(
            repository_id=discovered_file.repository_id,
            framework="ASP.NET Core",
        ),
    )


def _resolve_reference_path(solution_path: str, reference_path: str) -> str:
    normalized_reference = reference_path.strip().replace("\\", "/")
    if not normalized_reference:
        return ""

    if _is_rooted_path(normalized_reference):
        combined = normalized_reference.lstrip("/")
    else:
        combined = posixpath.join(
            posixpath.dirname(solution_path),
            normalized_reference,
        )
    return posixpath.normpath(combined)


def _is_rooted_path(path: str) -> bool:
    return path.startswith("/") or bool(re.match(r"^[A-Za-z]:/", path))


def _dedupe_references(
    references: tuple[ProjectReference, ...],
) -> tuple[ProjectReference, ...]:
    seen: set[str] = set()
    deduped: list[ProjectReference] = []
    for reference in references:
        if reference.path in seen:
            continue
        seen.add(reference.path)
        deduped.append(reference)
    return tuple(deduped)


def _dedupe_razor_routes(routes: tuple[RazorRoute, ...]) -> tuple[RazorRoute, ...]:
    seen: set[str] = set()
    deduped: list[RazorRoute] = []
    for route in routes:
        if route.path in seen:
            continue
        seen.add(route.path)
        deduped.append(route)
    return tuple(deduped)


def _line_number_for_path(source: str, path: str) -> int | None:
    for line_number, line in enumerate(source.splitlines(), start=1):
        if path in line:
            return line_number
    return None


def _line_number_for_offset(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


def _line_number_for_element(source: str, element_name: str) -> int | None:
    pattern = re.compile(rf"<(?:\w+:)?{re.escape(element_name)}(?:\s|>)")
    for line_number, line in enumerate(source.splitlines(), start=1):
        if pattern.search(line):
            return line_number
    return None


def _project_symbol(
    discovered_file: DiscoveredFile,
    project_symbol: ProjectSymbol,
) -> Symbol:
    return Symbol(
        id=_symbol_id(
            discovered_file.repository_id,
            discovered_file.path,
            project_symbol.kind,
            project_symbol.name,
        ),
        file_id=_file_id(discovered_file.repository_id, discovered_file.path),
        name=project_symbol.name,
        kind=project_symbol.kind,
        qualified_name=project_symbol.name,
        start_line=project_symbol.line_number,
        end_line=project_symbol.line_number,
    )


def _injected_service(discovered_file: DiscoveredFile, inject: RazorInject) -> Service:
    return Service(
        id=_service_id(discovered_file.repository_id, inject.service_type),
        name=inject.service_type,
        repository_id=discovered_file.repository_id,
        description=f"Razor injected service `{inject.property_name}`",
    )


def _razor_route_contract(
    discovered_file: DiscoveredFile,
    route: RazorRoute,
) -> Contract:
    return Contract(
        id=_razor_route_contract_id(discovered_file.repository_id, route.path),
        name=route.path,
        kind="razor_route",
        path=route.path,
        evidence=(
            Evidence(
                file_path=discovered_file.path,
                extractor_name=EXTRACTOR_NAME,
                confidence=DOTNET_CONFIDENCE,
                start_line=route.line_number,
                end_line=route.line_number,
                metadata={
                    "repository_id": discovered_file.repository_id,
                    "route": route.path,
                },
            ),
        ),
    )


def _aspnet_endpoint_contract(
    discovered_file: DiscoveredFile,
    endpoint: AspNetEndpoint,
) -> Contract:
    return Contract(
        id=_aspnet_contract_id(
            discovered_file.repository_id,
            endpoint.path,
            endpoint.method,
        ),
        name=f"{endpoint.method} {endpoint.path}",
        kind="http",
        path=endpoint.path,
        method=endpoint.method,
        producer_hint=endpoint.producer_hint,
        operation_id=endpoint.operation_id,
        request_dtos=endpoint.request_dtos,
        response_dtos=endpoint.response_dtos,
        evidence=(
            Evidence(
                file_path=discovered_file.path,
                extractor_name=EXTRACTOR_NAME,
                confidence=DOTNET_CONFIDENCE,
                start_line=endpoint.line_number,
                end_line=endpoint.line_number,
                metadata={
                    "repository_id": discovered_file.repository_id,
                    "controller": endpoint.controller_name,
                    "action": endpoint.action_name,
                    "method": endpoint.method,
                    "route": endpoint.path,
                },
            ),
        ),
    )


def _symbol_edge(discovered_file: DiscoveredFile, symbol: Symbol) -> Edge:
    return Edge(
        source_type="file",
        source_id=_file_id(discovered_file.repository_id, discovered_file.path),
        target_type="symbol",
        target_id=symbol.id,
        relation_type="declares_symbol",
        confidence=DOTNET_CONFIDENCE,
        evidence=(
            Evidence(
                file_path=discovered_file.path,
                extractor_name=EXTRACTOR_NAME,
                confidence=DOTNET_CONFIDENCE,
                start_line=symbol.start_line,
                end_line=symbol.end_line,
                metadata={
                    "repository_id": discovered_file.repository_id,
                    "symbol_name": symbol.name,
                    "symbol_kind": symbol.kind,
                },
            ),
        ),
    )


def _inject_edge(discovered_file: DiscoveredFile, inject: RazorInject) -> Edge:
    return Edge(
        source_type="file",
        source_id=_file_id(discovered_file.repository_id, discovered_file.path),
        target_type="service",
        target_id=_service_id(discovered_file.repository_id, inject.service_type),
        relation_type="injects",
        confidence=DOTNET_CONFIDENCE,
        evidence=(
            Evidence(
                file_path=discovered_file.path,
                extractor_name=EXTRACTOR_NAME,
                confidence=DOTNET_CONFIDENCE,
                start_line=inject.line_number,
                end_line=inject.line_number,
                metadata={
                    "repository_id": discovered_file.repository_id,
                    "service_type": inject.service_type,
                    "property_name": inject.property_name,
                },
            ),
        ),
    )


def _contract_edge(
    discovered_file: DiscoveredFile,
    contract: Contract,
    line_number: int | None,
) -> Edge:
    return Edge(
        source_type="file",
        source_id=_file_id(discovered_file.repository_id, discovered_file.path),
        target_type="contract",
        target_id=contract.id,
        relation_type="declares_contract",
        confidence=DOTNET_CONFIDENCE,
        evidence=(
            Evidence(
                file_path=discovered_file.path,
                extractor_name=EXTRACTOR_NAME,
                confidence=DOTNET_CONFIDENCE,
                start_line=line_number,
                end_line=line_number,
                metadata={
                    "repository_id": discovered_file.repository_id,
                    "contract_kind": contract.kind,
                    "route": contract.path,
                },
            ),
        ),
    )


def _service_contract_edge(
    discovered_file: DiscoveredFile,
    *,
    service_name: str,
    contract: Contract,
    line_number: int | None,
) -> Edge:
    return Edge(
        source_type="service",
        source_id=_service_id(discovered_file.repository_id, service_name),
        target_type="contract",
        target_id=contract.id,
        relation_type="produces",
        confidence=DOTNET_CONFIDENCE,
        evidence=(
            Evidence(
                file_path=discovered_file.path,
                extractor_name=EXTRACTOR_NAME,
                confidence=DOTNET_CONFIDENCE,
                start_line=line_number,
                end_line=line_number,
                metadata={
                    "repository_id": discovered_file.repository_id,
                    "service_name": service_name,
                    "contract_kind": contract.kind,
                    "route": contract.path,
                },
            ),
        ),
    )


def _package_reference_edge(
    discovered_file: DiscoveredFile,
    reference: PackageReference,
) -> Edge:
    return Edge(
        source_type="file",
        source_id=_file_id(discovered_file.repository_id, discovered_file.path),
        target_type="package",
        target_id=reference.name,
        relation_type="depends_on",
        confidence=DOTNET_CONFIDENCE,
        evidence=(
            Evidence(
                file_path=discovered_file.path,
                extractor_name=EXTRACTOR_NAME,
                confidence=DOTNET_CONFIDENCE,
                start_line=reference.line_number,
                end_line=reference.line_number,
                metadata={
                    "repository_id": discovered_file.repository_id,
                    "package_name": reference.name,
                    "version": reference.version,
                },
            ),
        ),
    )


def _project_reference_edge(
    discovered_file: DiscoveredFile,
    reference: ProjectReference,
) -> Edge:
    return Edge(
        source_type="file",
        source_id=_file_id(discovered_file.repository_id, discovered_file.path),
        target_type="file",
        target_id=_file_id(discovered_file.repository_id, reference.path),
        relation_type="references_project",
        confidence=DOTNET_CONFIDENCE,
        evidence=(
            Evidence(
                file_path=discovered_file.path,
                extractor_name=EXTRACTOR_NAME,
                confidence=DOTNET_CONFIDENCE,
                start_line=reference.line_number,
                end_line=reference.line_number,
                metadata={
                    "repository_id": discovered_file.repository_id,
                    "project_path": reference.path,
                },
            ),
        ),
    )


def _file_id(repository_id: str, path: str) -> str:
    return f"file:{repository_id}:{path}"


def _symbol_id(repository_id: str, path: str, kind: str, name: str) -> str:
    kind_slug = slugify_identifier(kind, fallback="symbol")
    name_slug = slugify_identifier(name, fallback="symbol")
    return f"symbol:{repository_id}:{path}:dotnet-{kind_slug}:{name_slug}"


def _service_id(repository_id: str, name: str) -> str:
    name_slug = slugify_identifier(name, fallback="service")
    return f"service:{repository_id}:{name_slug}"


def _razor_route_contract_id(repository_id: str, route_path: str) -> str:
    route_slug = slugify_identifier(route_path, fallback="root")
    return f"contract:{repository_id}:razor_route:{route_slug}"


def _aspnet_contract_id(repository_id: str, route_path: str, method: str) -> str:
    return canonical_http_contract_id(repository_id, method, route_path)
