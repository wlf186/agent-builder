---
title: "Model Service Configuration"
category: advanced
component: ModelServiceDialog
generated: true
related:
  - AgentChat
---
# Model Service Configuration

Configure LLM model services for AI agents. Supports multiple providers including Zhipu AI, Alibaba Bailian, and local Ollama.

## How to Use

1. Click the settings icon or "Add Model Service" button
2. Enter a unique name for the model service
3. Select a provider (Zhipu AI, Alibaba Bailian, or Ollama)
4. Enter the service URL (auto-filled for known providers)
5. Enter the API Key (required for cloud providers)
6. Click "Test Connection" to verify connectivity and fetch available models
7. Select a model from the dropdown list
8. Toggle the enable switch and click "Save"

## Tips

- API keys submitted through this password field are stored server-side and are never returned in plaintext
- Test connection before saving to ensure the service is accessible
- Ollama does not require an API key for local installations

## Related

- [AgentChat](/en/core/agent-chat)
