---
title: "MCP Diagnostics"
category: advanced
component: MCPDiagnosticResult
generated: true
related:
  - MCPServiceDialog
---
# MCP Diagnostics

Diagnose MCP service connection issues with multi-layer health checks. View detailed results for config, DNS, network, TLS, and MCP protocol layers.

## How to Use

1. Open MCP Service Configuration in edit mode
2. Click the "Diagnose" button to start diagnostics
3. View the overall status: Healthy, Degraded, or Down
4. Check each layer result for detailed information
5. Expand "Details" to see raw diagnostic data
6. Follow the recommendation section for fix suggestions

## Tips

- Each layer tests a specific aspect: config validation, DNS resolution, network connectivity, TLS/SSL, and MCP protocol
- Latency is measured in milliseconds for each layer
- Use recommendations to identify and fix common issues

## Related

- [MCPServiceDialog](/en/advanced/mcp-service-dialog)
