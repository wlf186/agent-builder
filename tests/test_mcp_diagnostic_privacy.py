"""Diagnostic failures must not echo provider or validation exception text."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.mcp_diagnostic import MCPDiagnostic
from src.models import MCPConnectionType, MCPServiceConfig
from src.security import SecurityValidationError


def _diagnostic() -> MCPDiagnostic:
    return MCPDiagnostic(
        MCPServiceConfig(
            name="diagnostic",
            connection_type=MCPConnectionType.SSE,
            url="https://example.com/sse",
        )
    )


def test_config_validation_does_not_echo_invalid_value() -> None:
    secret = "https://" + "user" + ":" + "not-a-secret" + "@private.invalid/token-value"
    diagnostic = _diagnostic()
    with patch(
        "src.mcp_diagnostic.validate_outbound_url_syntax",
        side_effect=SecurityValidationError(secret),
    ):
        result = diagnostic._check_config()
    rendered = result.model_dump_json()
    assert secret not in rendered
    assert "SecurityValidationError" in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method_name,expected",
    (
        ("_check_dns", "DNS解析失败 (RuntimeError)"),
        ("_check_tcp_connection", "网络连接失败 (RuntimeError)"),
        ("_check_tls_connection", "TLS连接失败 (RuntimeError)"),
    ),
)
async def test_network_diagnostics_return_only_error_type(
    method_name: str,
    expected: str,
) -> None:
    secret = "private provider path /outside/token-value"
    diagnostic = _diagnostic()
    with patch(
        "src.mcp_diagnostic.validate_outbound_url",
        new=AsyncMock(side_effect=RuntimeError(secret)),
    ), patch(
        "src.mcp_diagnostic.resolve_outbound_target",
        new=AsyncMock(side_effect=RuntimeError(secret)),
    ):
        result = await getattr(diagnostic, method_name)()
    assert result.message == expected
    assert secret not in result.model_dump_json()
