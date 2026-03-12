# Azure Chat Assistant — MCP Server

Single-file Python MCP server bridging Claude Code to Azure AI Foundry models.

## File Map
- `mcp_chat_assistant.py` — The entire server (1009 lines, async)
- `test_connection.py` — Standalone connection test (uses urllib, not async)
- `WISDOM_SCROLL.md` — Refactoring history and council recommendations
- `FORWARD_GUIDE.md` — Future roadmap (phases, council blueprint)
- `RESEARCH_AND_PLAN.md` — Performance research (latency, caching, optimization)

## Do NOT Read
- `venv/` — Python virtualenv, never modify
- `WISDOM_SCROLL.md`, `FORWARD_GUIDE.md`, `RESEARCH_AND_PLAN.md` — Reference docs only, not needed for code changes

## Code Structure (`mcp_chat_assistant.py`)
| Lines | Section |
|-------|---------|
| 29-57 | Config defaults, globals, constants |
| 59-111 | SQLite session DB (`init_db`, `get_history`, `add_message`, `clear_session`) |
| 113-161 | Config load/save with env var override (`ENV_MAP`, `load_config`, `save_config`) |
| 163-170 | `_google_base_url()` — Vertex AI URL builder |
| 174-378 | `call_llm()` — Core LLM call with SSE streaming (producer-consumer queue) |
| 382-432 | `chat()` — Conversation manager (history, cache, fallback) |
| 434-543 | `multi_chat()` — Parallel multi-model dispatch with progress tracking |
| 547-683 | `TOOLS` list — MCP tool definitions (JSON schema) |
| 686-707 | `handle_request()` — MCP JSON-RPC router |
| 709-820 | `_run_tool()` — Tool execution dispatch |
| 822-920 | Helper functions (`_handle_configure`, `_handle_models`, `_test_model`) |
| 949-1009 | MCP transport (`_send_progress`, `_write_response`, `main` event loop) |

## Architecture
- **Runtime**: Python 3, asyncio + httpx (only external dep)
- **Streaming**: SSE with `asyncio.Queue` producer-consumer pattern
- **Concurrency**: `multi_chat` uses `asyncio.wait` with per-model timeout
- **Config**: `~/.config/azure-chat-assistant/config.json`
- **Sessions**: SQLite at `~/.config/azure-chat-assistant/sessions.db`
- **Protocol**: MCP v2024-11-05 over stdio, JSON-RPC 2.0

## Endpoint Types
| Type | URL Pattern | Used For |
|------|-------------|----------|
| deployed | `/openai/deployments/{name}/chat/completions` | GPT models |
| serverless | `/models/chat/completions` | Llama, DeepSeek, Phi, Grok |
| google | Vertex AI `/chat/completions` | Gemini models |

## MCP Tools (11)
`chat`, `multi_chat`, `configure`, `reset`, `clear_cache`, `status`, `models`, `create_session`, `switch_session`, `delete_session`, `list_sessions`

## Model Quirks
- **GPT-5.3**: Rejects `temperature` != 1.0 — code only sends temp when != 1.0
- **Reasoning models** (o1/o3/o4): Use `developer` role, `max_completion_tokens`, non-streaming, 120s timeout
- **DeepSeek R1**: Outputs `<think>` tags regardless of system prompt
- **Serverless free tier**: 15 req/day per model

## Env Vars (override config file)
`AZURE_AI_API_KEY`, `AZURE_AI_ENDPOINT`, `GOOGLE_API_KEY`, `GOOGLE_PROJECT`, `GOOGLE_REGION`

## How to Run
```bash
./venv/bin/python3 mcp_chat_assistant.py   # starts MCP stdio server
python3 test_connection.py                  # test model connectivity
```

## Voice Integration
Pairs with `../speech-to-cli/` MCP server. Flow: `multi_chat` -> `multi_speak`.
Voice map: GPT=DavisNeural, Llama=AndrewNeural, DeepSeek=BrianNeural, Phi=JennyNeural, Claude=AvaNeural.
