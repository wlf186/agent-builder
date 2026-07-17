from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from builtin_mcp_services.sse_server import (
    app,
    execute_calculator_tool,
    execute_joke_tool,
)


def test_calculator_uses_bounded_arithmetic() -> None:
    assert execute_calculator_tool("evaluate", {"expression": "(2 + 3) * 4"}).endswith("20")
    assert "指数超出安全范围" in execute_calculator_tool(
        "evaluate", {"expression": "9**99999999"}
    )
    assert "不允许" in execute_calculator_tool(
        "evaluate", {"expression": "__import__('os')"}
    )
    assert "表达式过长" in execute_calculator_tool(
        "evaluate", {"expression": "1+" * 200 + "1"}
    )
    assert "指数超出安全范围" in execute_calculator_tool(
        "power", {"a": 2, "b": 100_000}
    )
    assert "安全范围" in execute_calculator_tool(
        "multiply", {"a": 10**15, "b": 10**15}
    )


def test_builtin_service_bounds_body_and_cors() -> None:
    client = TestClient(app)
    oversized = client.post(
        "/calculator/tools/call",
        content=b"x" * (65 * 1024),
        headers={"content-type": "application/json"},
    )
    assert oversized.status_code == 413

    allowed = client.options(
        "/calculator/tools/call",
        headers={
            "Origin": "http://127.0.0.1:20815",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "http://127.0.0.1:20815"

    denied = client.options(
        "/calculator/tools/call",
        headers={
            "Origin": "https://attacker.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert denied.status_code == 400


def test_builtin_service_source_contains_no_runtime_eval() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "builtin_mcp_services"
        / "sse_server.py"
    ).read_text(encoding="utf-8")
    forbidden = "ev" + "al("
    assert forbidden not in source


def test_builtin_tool_failures_do_not_echo_exception_text() -> None:
    private_error = "private provider failure /private/runtime/path"
    with patch(
        "builtin_mcp_services.sse_server.random.choice",
        side_effect=RuntimeError(private_error),
    ):
        result = execute_joke_tool("get_joke", {})

    assert result == "获取笑话失败，请稍后重试"
    assert private_error not in result
