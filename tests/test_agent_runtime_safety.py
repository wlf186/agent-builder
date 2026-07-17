"""Regressions for RAG scheduling, MCP reuse, and tool-log privacy."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from src.agent_engine import AgentEngine
from src.agent_manager import AgentInstance
from src.execution_engine import ExecutionEngine
from src.file_storage_manager import FileStorageManager
from src.mcp_tool_adapter import MCPToolAdapter, summarize_tool_arguments
from src.models import AgentConfig, LLMProvider


def _config(*, knowledge_bases: list[str] | None = None) -> AgentConfig:
    return AgentConfig(
        name="runtime-safety",
        persona="test",
        llm_provider=LLMProvider.OLLAMA,
        llm_model="test-model",
        llm_base_url="http://127.0.0.1:11434",
        knowledge_bases=knowledge_bases or [],
    )


async def _wait_until(predicate, timeout: float = 1.0) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(poll(), timeout=timeout)


class _BlockingEmbedder:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.thread_ids: list[int] = []

    def encode_single(self, _query: str):
        self.thread_ids.append(threading.get_ident())
        self.started.set()
        if not self.release.wait(timeout=2):
            raise TimeoutError("test did not release embedder")
        return [0.1, 0.2]


class _Collection:
    def __init__(self) -> None:
        self.thread_ids: list[int] = []

    def query(self, **_kwargs):
        self.thread_ids.append(threading.get_ident())
        return {
            "ids": [["chunk-1"]],
            "distances": [[0.1]],
            "documents": [["project-local documentation"]],
            "metadatas": [[{
                "doc_id": "doc-1",
                "filename": "guide.md",
                "chunk_index": 0,
            }]],
        }


class _KnowledgeBaseManager:
    def __init__(self, collection: _Collection) -> None:
        self._configs = {"kb-1": object()}
        self.collection = collection
        self.collection_thread_ids: list[int] = []

    def _get_collection(self, _kb_id: str):
        self.collection_thread_ids.append(threading.get_ident())
        return self.collection


@pytest.mark.asyncio
async def test_retriever_initialization_embedding_and_query_leave_event_loop() -> None:
    event_loop_thread = threading.get_ident()
    embedder = _BlockingEmbedder()
    collection = _Collection()
    kb_manager = _KnowledgeBaseManager(collection)
    engine = AgentEngine(
        _config(knowledge_bases=["kb-1"]),
        kb_manager=kb_manager,
        embedder=embedder,
    )

    # Construction must not synchronously open Chroma collections.
    assert kb_manager.collection_thread_ids == []

    retrieval = asyncio.create_task(engine._retrieve_for_query("where is it?"))
    await _wait_until(embedder.started.is_set)

    # A timer can still run while SentenceTransformer is blocked in its worker.
    ticked = False

    async def tick() -> None:
        nonlocal ticked
        await asyncio.sleep(0)
        ticked = True

    await asyncio.wait_for(tick(), timeout=0.2)
    assert ticked
    assert kb_manager.collection_thread_ids[0] != event_loop_thread
    assert embedder.thread_ids[0] != event_loop_thread

    embedder.release.set()
    context = await asyncio.wait_for(retrieval, timeout=1)
    assert "project-local documentation" in context
    assert collection.thread_ids[0] != event_loop_thread
    await engine.aclose()


@pytest.mark.asyncio
async def test_cancelled_rag_work_keeps_worker_slot_and_shutdown_drains_it() -> None:
    engine = AgentEngine(_config())
    engine.RAG_MAX_WORKERS = 1
    first_started = threading.Event()
    first_release = threading.Event()
    second_started = threading.Event()

    def first_call() -> str:
        first_started.set()
        if not first_release.wait(timeout=2):
            raise TimeoutError("test did not release worker")
        return "first"

    def second_call() -> str:
        second_started.set()
        return "second"

    first = asyncio.create_task(engine._run_rag_blocking(first_call))
    await _wait_until(first_started.is_set)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    # Cancelling the asyncio waiter cannot stop its Python thread. The slot
    # therefore stays occupied and a second call must not start early.
    second = asyncio.create_task(engine._run_rag_blocking(second_call))
    await asyncio.sleep(0.05)
    assert not second_started.is_set()

    closing = asyncio.create_task(engine.aclose())
    await asyncio.sleep(0.05)
    assert not closing.done()

    first_release.set()
    with pytest.raises(RuntimeError, match="已关闭"):
        await asyncio.wait_for(second, timeout=1)
    await asyncio.wait_for(closing, timeout=1)
    assert engine._rag_futures == set()
    assert engine._rag_executor is None


@pytest.mark.asyncio
async def test_file_and_execution_registry_reads_leave_event_loop(tmp_path) -> None:
    event_loop_thread = threading.get_ident()
    files = FileStorageManager(tmp_path / "files")
    executions = ExecutionEngine(
        SimpleNamespace(),
        files,
        tmp_path / "data",
    )

    async def assert_offloaded(owner, method_name: str, call) -> None:
        started = threading.Event()
        release = threading.Event()
        worker_threads: list[int] = []

        def blocking_read(*_args):
            worker_threads.append(threading.get_ident())
            started.set()
            if not release.wait(timeout=2):
                raise TimeoutError("test did not release disk worker")
            return [] if method_name == "_list_files_sync" else None

        setattr(owner, method_name, blocking_read)
        pending = asyncio.create_task(call())
        await _wait_until(started.is_set)
        await asyncio.wait_for(asyncio.sleep(0), timeout=0.2)
        assert len(worker_threads) == 1
        assert worker_threads[0] != event_loop_thread
        release.set()
        await asyncio.wait_for(pending, timeout=1)

    await assert_offloaded(
        files,
        "_list_files_sync",
        lambda: files.list_files("agent"),
    )
    await assert_offloaded(
        executions,
        "_get_execution_status_sync",
        lambda: executions.get_execution_status("agent", "deadbeef"),
    )


class _LocalRestConnection:
    def __init__(self) -> None:
        self._session = None
        self._is_local_rest = True
        self.is_connected = True
        self.tools = [object()]
        self.disconnect_calls = 0
        self.connect_calls = 0

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.is_connected = False

    async def connect(self) -> bool:
        self.connect_calls += 1
        self.is_connected = True
        return True


@pytest.mark.asyncio
async def test_connected_local_rest_mcp_is_reused_without_session() -> None:
    connection = _LocalRestConnection()
    instance = AgentInstance(_config())
    instance.mcp_manager = SimpleNamespace(servers={"local": connection})

    await instance._ensure_mcp_connections()

    assert connection.disconnect_calls == 0
    assert connection.connect_calls == 0

    connection.is_connected = False
    await instance._ensure_mcp_connections()
    assert connection.disconnect_calls == 1
    assert connection.connect_calls == 1


class _ToolManager:
    def __init__(self, result: str) -> None:
        self.result = result
        self.arguments = None
        self.all_tools = [SimpleNamespace(name="demo")]

    def get_tool(self, name: str):
        return self.all_tools[0] if name == "demo" else None

    async def call_tool(self, _name: str, arguments: dict) -> str:
        self.arguments = arguments
        return self.result


@pytest.mark.asyncio
async def test_tool_logs_and_adapter_placeholder_never_render_values(capsys) -> None:
    payload_secret = "private-query-value"
    token_secret = "token-value-that-must-not-be-logged"
    result_secret = "private-tool-result"
    arguments = {"query": payload_secret, "api_token": token_secret}
    manager = _ToolManager(result_secret)
    engine = AgentEngine(_config(), mcp_manager=manager)

    result = await engine._execute_tool("demo", {"kwargs": arguments})
    output = capsys.readouterr().out

    assert result == result_secret
    assert manager.arguments == arguments
    assert payload_secret not in output
    assert token_secret not in output
    assert result_secret not in output
    assert "query" in output
    assert "<redacted>" in output
    assert "argument_length" in output
    assert f"result_length={len(result_secret)}" in output

    adapter = MCPToolAdapter(manager)
    mcp_tool = SimpleNamespace(
        name="demo",
        description="demo tool",
        input_schema=None,
    )
    converted = adapter.convert_tool(mcp_tool)
    placeholder = converted.func(kwargs=arguments)
    assert await converted.coroutine(kwargs=arguments) == result_secret
    adapter_output = capsys.readouterr().out
    summary = summarize_tool_arguments(arguments)
    assert payload_secret not in placeholder
    assert token_secret not in placeholder
    assert payload_secret not in adapter_output
    assert token_secret not in adapter_output
    assert "query" in placeholder
    assert "<redacted>" in placeholder
    assert "query" in adapter_output
    assert "<redacted>" in adapter_output
    assert summary["argument_length"] > 0
    await engine.aclose()
