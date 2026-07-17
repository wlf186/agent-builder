---
title: Getting Started
---

# Getting Started

Welcome to Agent Builder! This guide will help you create your first AI agent in 5 minutes.

## What is Agent Builder?

Agent Builder is a platform for creating AI assistants (agents) that can:

- Chat with you in natural language
- Use tools to perform tasks
- Search through your documents
- Work with other agents as a team

## Quick Start

An operator starts the complete project-local stack from the repository root:

```bash
./start.sh
```

No global Python, Node, Conda, or container runtime is required. When startup
reports that all services are healthy, open `http://127.0.0.1:20815`. Stop all
processes owned by this checkout with `./stop.sh`.

A fresh installation requires a supported glibc Linux host, Internet access,
and sufficient disk and memory. Review the repository
[deployment prerequisites](https://github.com/wlf186/agent-builder#supported-deployment)
before the first bootstrap.

### Step 0: Configure a Model Service

Agent Builder does not bundle an LLM or install Ollama. Open **Model Service
Configuration** and add a cloud provider with an API key, or point it at an
independently installed local Ollama service. See the
[model-service guide](/en/advanced/model-service-dialog) for details.

### Step 1: Create an Agent

1. Click the **"Create Agent"** button on the left sidebar
2. Enter a name for your agent (e.g., "My Assistant")
3. Write a description of what your agent should do in the **Persona** field
4. Click **Save** to create your agent

### Step 2: Start Chatting

1. Click on your newly created agent in the sidebar
2. Type a message in the chat input at the bottom
3. Press **Enter** or click **Send**
4. Watch the AI respond with real-time streaming

### Step 3: Add Knowledge (Optional)

1. Click the **Knowledge Base** icon in the agent settings
2. Create a new knowledge base or select an existing one
3. Upload documents (PDF, DOCX, TXT, MD)
4. Your agent can now search these documents for answers

## Next Steps

- Learn about [Chat Interface](/en/core/agent-chat) features
- Explore [Knowledge Bases](/en/core/knowledge-base-selector)
- Configure [Model Services](/en/advanced/model-service-dialog) before chatting
- Configure [MCP Services](/en/advanced/mcp-service-dialog) for tools

## Need Help?

If you encounter issues, check the agent status indicators or contact your administrator.
