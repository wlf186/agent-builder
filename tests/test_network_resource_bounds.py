"""Focused regressions for network-client and runtime resource bounds."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.agent_engine import AgentEngine
from src.agent_manager import AgentInstance
from src.environment_manager import EnvironmentManager
from src.mcp_manager import (
    MAX_MCP_ARGUMENT_BYTES,
    MAX_MCP_RESULT_CHARS,
    MCPServerConnection,
    build_stdio_environment,
)
import src.mcp_manager as mcp_manager_module
from src.models import AgentConfig, LLMProvider, MCPConfig
from src.models import ModelProvider
from src.model_provider_tester import ModelProviderTester
from src.security import SecurityValidationError


def _agent_config() -> AgentConfig:
    return AgentConfig(
        name="bounded-client",
        persona="test",
        llm_provider=LLMProvider.OLLAMA,
        llm_model="test-model",
        llm_base_url="http://127.0.0.1:11434",
    )


@pytest.mark.asyncio
async def test_model_http_clients_are_hardened_and_closed_by_agent_shutdown() -> None:
    # Import before patching httpx: the ollama package creates its unrelated
    # module-level convenience client during import.
    import langchain_ollama

    config = _agent_config()
    engine = AgentEngine(config)
    instance = AgentInstance(config)
    sync_client = Mock()
    async_client = AsyncMock()
    model_sync_client = Mock()
    model_async_client = SimpleNamespace(close=AsyncMock())
    fake_llm = SimpleNamespace(
        _client=model_sync_client,
        _async_client=model_async_client,
    )

    with (
        patch("httpx.Client", return_value=sync_client) as sync_constructor,
        patch("httpx.AsyncClient", return_value=async_client) as async_constructor,
        patch.object(
            langchain_ollama, "ChatOllama", return_value=fake_llm
        ) as llm_constructor,
        patch.object(engine, "_bind_tools_to_llm"),
    ):
        engine._setup_llm()

    for constructor in (sync_constructor, async_constructor):
        assert constructor.call_count == 1
        assert constructor.call_args.kwargs["trust_env"] is False
        assert constructor.call_args.kwargs["follow_redirects"] is False

    for keyword in ("client_kwargs", "async_client_kwargs", "sync_client_kwargs"):
        assert llm_constructor.call_args.kwargs[keyword] == {
            "trust_env": False,
            "follow_redirects": False,
        }

    instance.engine = engine
    await instance.shutdown()

    async_client.aclose.assert_awaited_once_with()
    sync_client.close.assert_called_once_with()
    model_async_client.close.assert_awaited_once_with()
    model_sync_client.close.assert_called_once_with()
    assert engine._http_async_client is None
    assert engine._http_client is None

    # Shutdown is idempotent and must not attempt to close a pool twice.
    await instance.shutdown()
    async_client.aclose.assert_awaited_once_with()
    sync_client.close.assert_called_once_with()
    model_async_client.close.assert_awaited_once_with()
    model_sync_client.close.assert_called_once_with()


class _FakeMCPSession:
    def __init__(self) -> None:
        self.calls = 0
        self.content = "ok"
        self.error: Exception | None = None

    async def call_tool(self, _tool_name: str, _arguments: dict):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return SimpleNamespace(content=[SimpleNamespace(text=self.content)])


@pytest.mark.asyncio
async def test_mcp_argument_and_result_limits_are_enforced_at_one_megabyte() -> None:
    connection = MCPServerConnection(
        MCPConfig(name="bounded-mcp", command="unused")
    )
    session = _FakeMCPSession()
    connection.session = session

    empty_payload_size = len(
        json.dumps(
            {"payload": ""}, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    )
    exact_payload = "a" * (MAX_MCP_ARGUMENT_BYTES - empty_payload_size)
    exact_arguments = {"payload": exact_payload}
    assert len(
        json.dumps(
            exact_arguments, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    ) == MAX_MCP_ARGUMENT_BYTES

    assert await connection.call_tool("demo", exact_arguments) == "ok"
    assert session.calls == 1

    oversized_arguments = {"payload": exact_payload + "a"}
    argument_error = await connection.call_tool("demo", oversized_arguments)
    assert argument_error == "工具调用失败 (ValueError)"
    assert session.calls == 1

    session.content = "r" * MAX_MCP_RESULT_CHARS
    exact_result = await connection.call_tool("demo", {})
    assert len(exact_result) == MAX_MCP_RESULT_CHARS

    session.content += "r"
    result_error = await connection.call_tool("demo", {})
    assert result_error == "工具调用失败 (ValueError)"

    provider_secret = (
        "https://" + "user" + ":" + "not-a-secret" + "@private.invalid/token-value"
    )
    session.error = RuntimeError(provider_secret)
    provider_error = await connection.call_tool("demo", {})
    assert provider_error == "工具调用失败 (RuntimeError)"
    assert provider_secret not in provider_error
    await connection.disconnect()


def test_stdio_mcp_environment_overrides_external_cache_locations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "runtime"
    monkeypatch.setattr(mcp_manager_module, "RUNTIME_ROOT", runtime)
    external = tmp_path / "external"
    configured = {
        "HOME": str(external / "home"),
        "TMPDIR": str(external / "tmp"),
        "TEMP": str(external / "temp"),
        "TMP": str(external / "tmp2"),
        "XDG_RUNTIME_DIR": str(external / "xdg"),
        "HF_HOME": str(external / "hf"),
        "TORCHINDUCTOR_CACHE_DIR": str(external / "inductor"),
        "TRITON_CACHE_DIR": str(external / "triton"),
        "AGENT_BUILDER_API_TOKEN": "must-be-removed",
        "CUSTOM_SAFE_VALUE": "preserved",
    }

    environment = build_stdio_environment("service", configured)
    contained_keys = (
        "HOME",
        "TMPDIR",
        "TEMP",
        "TMP",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
        "UV_CACHE_DIR",
        "PIP_CACHE_DIR",
        "HF_HOME",
        "HUGGINGFACE_HUB_CACHE",
        "SENTENCE_TRANSFORMERS_HOME",
        "TRANSFORMERS_CACHE",
        "TORCH_HOME",
        "TORCH_EXTENSIONS_DIR",
        "TORCHINDUCTOR_CACHE_DIR",
        "TRITON_CACHE_DIR",
        "NUMBA_CACHE_DIR",
        "MPLCONFIGDIR",
        "PYTHONPYCACHEPREFIX",
        "npm_config_cache",
        "NPM_CONFIG_CACHE",
    )
    for key in contained_keys:
        Path(environment[key]).resolve().relative_to(runtime.resolve())
        assert Path(environment[key]).is_dir()
    assert environment["TEMP"] == environment["TMPDIR"] == environment["TMP"]
    assert environment["CUSTOM_SAFE_VALUE"] == "preserved"
    assert "AGENT_BUILDER_API_TOKEN" not in environment

    service_digest = __import__("hashlib").sha256(b"linked").hexdigest()[:12]
    linked_root = runtime / "mcp" / service_digest
    external.mkdir()
    linked_root.symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        build_stdio_environment("linked", {})
    assert list(external.iterdir()) == []


def test_work_directory_over_ten_thousand_entries_is_over_limit(
    tmp_path: Path,
) -> None:
    entries = [f"entry-{index}" for index in range(10_001)]
    fake_walk = [(str(tmp_path), [], entries)]

    with patch("src.environment_manager.os.walk", return_value=fake_walk):
        assert EnvironmentManager._directory_exceeds_limit(
            tmp_path, limit=1024**4
        )

    assert EnvironmentManager.MAX_WORKDIR_ENTRIES == 10_000


@pytest.mark.asyncio
async def test_model_connection_errors_do_not_echo_validation_or_provider_text() -> None:
    secret = "https://" + "user" + ":" + "not-a-secret" + "@private.invalid/token-value"
    with patch(
        "src.model_provider_tester.validate_outbound_url",
        new=AsyncMock(side_effect=SecurityValidationError(secret)),
    ):
        result = await ModelProviderTester.test_connection(
            ModelProvider.OLLAMA,
            "https://example.com",
        )
    assert result == (False, [], "URL被安全策略拒绝")
    assert secret not in result[2]

    client_context = AsyncMock()
    client_context.__aenter__.return_value = object()
    with patch("src.model_provider_tester.httpx.AsyncClient", return_value=client_context), patch(
        "src.model_provider_tester._get_json_limited",
        new=AsyncMock(side_effect=RuntimeError(secret)),
    ):
        result = await ModelProviderTester._test_ollama("https://example.com")
    assert result == (False, [], "连接失败 (RuntimeError)")
    assert secret not in result[2]
