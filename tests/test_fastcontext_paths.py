from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from code_scout.engines import (
    CodeScoutRequest,
    FastContextEngine,
    OpenAIModelTarget,
)
from code_scout.engines.fastcontext import (
    FastContextPathResolver,
    guard_fastcontext_tools,
)


class RecordingTool:
    description = "test tool"
    parameters: dict[str, object] = {}

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[dict[str, object]] = []

    async def call(self, parameters: str, **_kwargs: object) -> str:
        parsed = json.loads(parameters)
        self.calls.append(parsed)
        return str(parsed["path"])

    def schema(self) -> dict[str, object]:
        return {"name": self.name}


def _agent_with_tools(*tools: RecordingTool) -> SimpleNamespace:
    toolset = SimpleNamespace(tools={tool.name: tool for tool in tools})
    return SimpleNamespace(toolset=toolset)


def _target() -> OpenAIModelTarget:
    return OpenAIModelTarget(
        base_url="http://localhost:8000/v1",
        model="fastcontext-model",
    )


def test_resolver_corrects_truncated_absolute_path(tmp_path: Path) -> None:
    workspace = tmp_path / "repository"
    source = workspace / "src" / "router.py"
    source.parent.mkdir(parents=True)
    source.write_text("router = True\n", encoding="utf-8")
    resolver = FastContextPathResolver(workspace)

    resolved = resolver.resolve(f"/{workspace.name}/src/router.py", tool="Read")

    assert resolved == source
    assert resolver.metadata() == (
        {
            "status": "corrected",
            "tool": "Read",
            "requested_path": "/repository/src/router.py",
            "resolved_path": str(source),
            "reason": "removed_repository_prefix",
        },
    )


def test_resolver_preserves_correct_absolute_path(tmp_path: Path) -> None:
    source = tmp_path / "src" / "router.py"
    source.parent.mkdir()
    source.write_text("router = True\n", encoding="utf-8")
    resolver = FastContextPathResolver(tmp_path)

    assert resolver.resolve(str(source), tool="Read") == source
    assert resolver.metadata() == ()


def test_resolver_rejects_invented_symlink_escape_and_ambiguous_paths(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repository"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private\n", encoding="utf-8")
    (workspace / "escape.txt").symlink_to(outside)
    resolver = FastContextPathResolver(workspace)

    assert resolver.resolve("invented.py", tool="Read") is None
    assert resolver.resolve("escape.txt", tool="Read") is None

    direct = workspace / workspace.name / "src"
    corrected = workspace / "src"
    direct.mkdir(parents=True)
    corrected.mkdir()
    assert resolver.resolve(f"/{workspace.name}/src", tool="Glob") is None

    assert [event["reason"] for event in resolver.metadata()] == [
        "not_found",
        "symlink_escape",
        "ambiguous",
    ]


@pytest.mark.parametrize("tool_name", ["Read", "Glob", "Grep"])
def test_guard_confines_all_fastcontext_file_tools(
    tmp_path: Path,
    tool_name: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    tool = RecordingTool(tool_name)
    agent = _agent_with_tools(tool)
    resolver = FastContextPathResolver(workspace)
    guard_fastcontext_tools(agent, resolver, required=False)

    guarded = agent.toolset.tools[tool_name]
    result = asyncio.run(guarded.call(json.dumps({"path": str(outside)})))

    assert "rejected" in result.lower()
    assert tool.calls == []
    assert resolver.metadata()[0]["reason"] == "outside_workspace"


def test_guard_defaults_search_tools_to_selected_workspace(tmp_path: Path) -> None:
    glob = RecordingTool("Glob")
    grep = RecordingTool("Grep")
    agent = _agent_with_tools(glob, grep)
    resolver = FastContextPathResolver(tmp_path)
    guard_fastcontext_tools(agent, resolver, required=False)

    asyncio.run(agent.toolset.tools["Glob"].call(json.dumps({"pattern": "*.py"})))
    asyncio.run(agent.toolset.tools["Grep"].call(json.dumps({"pattern": "router"})))

    assert glob.calls[0]["path"] == str(tmp_path.resolve())
    assert grep.calls[0]["path"] == str(tmp_path.resolve())


def test_guard_replaces_provider_hardcoded_ripgrep_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    grep = RecordingTool("Grep")
    grep._rg_path = "/usr/bin/rg"  # type: ignore[attr-defined]
    agent = _agent_with_tools(grep)
    monkeypatch.setattr(
        "code_scout.engines.fastcontext.shutil.which",
        lambda _: "/opt/homebrew/bin/rg",
    )

    guard_fastcontext_tools(
        agent,
        FastContextPathResolver(tmp_path),
        required=False,
    )

    assert grep._rg_path == "/opt/homebrew/bin/rg"  # type: ignore[attr-defined]


def test_engine_records_path_corrections_and_rejections_in_metadata(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repository"
    source = workspace / "src" / "router.py"
    source.parent.mkdir(parents=True)
    source.write_text("router = True\n", encoding="utf-8")
    read = RecordingTool("Read")
    grep = RecordingTool("Grep")
    glob = RecordingTool("Glob")

    class ExercisingAgent:
        llm = SimpleNamespace()

        def __init__(self) -> None:
            self.toolset = SimpleNamespace(
                tools={tool.name: tool for tool in (read, grep, glob)}
            )

        async def run(self, **_kwargs: object) -> str:
            await self.toolset.tools["Read"].call(
                json.dumps({"path": f"/{workspace.name}/src/router.py"})
            )
            await self.toolset.tools["Grep"].call(
                json.dumps({"pattern": "secret", "path": str(tmp_path)})
            )
            return "<final_answer>src/router.py:1</final_answer>"

    engine = FastContextEngine(
        _target(),
        factory=lambda **_: ExercisingAgent(),
    )

    result = engine.execute(
        CodeScoutRequest(
            query="Find the router",
            workspace=workspace,
            engine="fastcontext",
        )
    )

    assert [event["status"] for event in result.metadata["path_events"]] == [
        "corrected",
        "rejected",
    ]
    assert read.calls == [{"path": str(source)}]
    assert grep.calls == []
