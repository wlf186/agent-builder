---
title: "MCP Service Configuration"
category: advanced
component: MCPServiceDialog
generated: true
related:
  - MCPDiagnosticResult
---
# MCP Service Configuration

Configure MCP (Model Context Protocol) services to extend agent capabilities with external tools. Supports SSE connections with optional authentication.

## How to Use

1. Click "Add MCP Service" to open the configuration dialog
2. Enter a unique service name (e.g., weather-api)
3. Enter the MCP service URL endpoint
4. Select authentication type: None, Bearer Token, or API Key
5. If authentication is required, enter the credentials
6. Optionally add custom headers in JSON format
7. Toggle the enable switch and click "Save"
8. Use "Diagnose" button (edit mode) to check connection health

## Tips

- MCP services use SSE (Server-Sent Events) for real-time communication
- Use the diagnostic tool to troubleshoot connection issues
- Custom headers must be valid JSON format

## Related

- [MCPDiagnosticResult](/en/advanced/mcp-diagnostic-result)
