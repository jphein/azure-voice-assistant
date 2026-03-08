#!/usr/bin/env python3
"""
Azure Voice Assistant MCP Server — standalone voice assistant powered by Azure AI models.

Uses Azure AI Foundry for LLM (GPT-5.3, grok-3, Llama, etc.) and delegates to the
azure-speech MCP server for TTS/STT when used alongside it, or works as a pure
chat tool on its own.

Tools:
  - chat:      Send a message to the LLM, get a response (with conversation history)
  - configure: View/change settings dynamically (model, API key, region, etc.)
  - reset:     Clear conversation history
  - models:    List available models and test connectivity
"""

import json
import os
import sys
import threading
import time
import urllib.request
import urllib.error

# ── Config ──────────────────────────────────────────────────────────────────

CONFIG_PATH = os.path.expanduser("~/.config/azure-voice-assistant/config.json")

DEFAULTS = {
    "api_key": "",
    "endpoint": "https://claud-assistant-resource.services.ai.azure.com",
    "deployment": "gpt-5.3-chat",
    "model": "gpt-5.3-chat-2026-03-03",
    "model_type": "deployed",       # "deployed" (OpenAI endpoint) or "serverless" (unified inference)
    "max_completion_tokens": 2048,
    "temperature": 1.0,
    "system_prompt": "You are a helpful voice assistant. Keep responses concise and conversational.",
    "conversation_max_turns": 50,    # max history turns before auto-trimming
    "voice": "",                     # default TTS voice (empty = use speech config)
}

CONFIG = {}
_conversation_history = []          # list of {"role": ..., "content": ...}
_stdout_lock = threading.Lock()

# ── Config management ───────────────────────────────────────────────────────

def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                disk = json.load(f)
            # Flatten nested structure if present (from test_connection.py format)
            if "azure_ai" in disk:
                for k, v in disk["azure_ai"].items():
                    cfg[k] = v
            else:
                cfg.update(disk)
        except Exception:
            pass
    return cfg


def save_config():
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    # Only save non-default values
    disk = {}
    for k, v in CONFIG.items():
        if k.startswith("_"):
            continue
        if k in DEFAULTS and CONFIG[k] == DEFAULTS[k]:
            continue
        disk[k] = v
    # Always save key/endpoint/deployment
    for k in ("api_key", "endpoint", "deployment", "model", "model_type"):
        disk[k] = CONFIG[k]
    with open(CONFIG_PATH, "w") as f:
        json.dump(disk, f, indent=4)


CONFIG = load_config()

# ── LLM call ────────────────────────────────────────────────────────────────

def call_llm(messages, progress_token=None):
    """Call Azure AI model with streaming. Returns (response_text, usage_dict, latency_ms)."""
    api_key = CONFIG.get("api_key", "")
    endpoint = CONFIG.get("endpoint", "")
    deployment = CONFIG.get("deployment", "")
    model = CONFIG.get("model", deployment)
    model_type = CONFIG.get("model_type", "deployed")
    max_tokens = CONFIG.get("max_completion_tokens", 2048)
    temperature = CONFIG.get("temperature", 1.0)

    if not api_key:
        return "Error: No API key configured. Use configure tool to set api_key.", {}, 0
    if not endpoint:
        return "Error: No endpoint configured.", {}, 0

    # Build request based on model type
    if model_type == "deployed":
        url = f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version=2024-10-21"
        body = {
            "messages": messages,
            "max_completion_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
    else:
        url = f"{endpoint}/models/chat/completions?api-version=2024-05-01-preview"
        body = {
            "messages": messages,
            "model": model,
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }

    # Only include temperature if not default (some models reject non-default values)
    if temperature != 1.0:
        body["temperature"] = temperature

    payload = json.dumps(body).encode()
    req = urllib.request.Request(url, data=payload, headers={
        "api-key": api_key,
        "Content-Type": "application/json",
    })

    if progress_token:
        _send_progress(progress_token, 0.1, "Thinking...")

    t0 = time.perf_counter()
    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        body_text = ""
        try:
            body_text = e.read().decode()[:300]
        except Exception:
            pass
        return f"Error {e.code}: {body_text}", {}, 0
    except Exception as e:
        return f"Error: {e}", {}, 0

    # Parse SSE stream
    full_text = ""
    usage = {}
    ttft = 0  # time to first token
    buf = ""
    last_progress = 0

    for raw_line in resp:
        line = raw_line.decode("utf-8", errors="replace").rstrip("\n\r")
        if not line.startswith("data: "):
            continue
        data_str = line[6:]
        if data_str == "[DONE]":
            break
        try:
            chunk = json.loads(data_str)
        except json.JSONDecodeError:
            continue

        # Extract usage from final chunk
        if chunk.get("usage"):
            usage = chunk["usage"]

        choices = chunk.get("choices", [])
        if not choices:
            continue

        delta = choices[0].get("delta", {})
        content = delta.get("content", "")
        if content:
            if not ttft:
                ttft = (time.perf_counter() - t0) * 1000
            full_text += content

            # Send progress with streaming text
            if progress_token:
                now = time.perf_counter()
                if now - last_progress > 0.3:  # throttle to ~3/sec
                    preview = full_text[-80:] if len(full_text) > 80 else full_text
                    _send_progress(progress_token, 0.5, f"...{preview}")
                    last_progress = now

    latency = (time.perf_counter() - t0) * 1000

    if not full_text:
        return "Error: No response from model.", usage, latency

    # Add TTFT to usage for diagnostics
    usage["_ttft_ms"] = round(ttft)

    return full_text, usage, latency

# ── Conversation management ─────────────────────────────────────────────────

def chat(user_message, progress_token=None):
    """Send a message, get a response, maintain history."""
    global _conversation_history

    # Build messages with system prompt
    messages = []
    sys_prompt = CONFIG.get("system_prompt", "")
    if sys_prompt:
        messages.append({"role": "system", "content": sys_prompt})

    # Add history
    messages.extend(_conversation_history)

    # Add new user message
    messages.append({"role": "user", "content": user_message})

    # Call LLM
    response, usage, latency = call_llm(messages, progress_token)

    # Only add to history if successful (no error prefix)
    if not response.startswith("Error"):
        _conversation_history.append({"role": "user", "content": user_message})
        _conversation_history.append({"role": "assistant", "content": response})

        # Trim history if too long
        max_turns = CONFIG.get("conversation_max_turns", 50)
        while len(_conversation_history) > max_turns * 2:
            _conversation_history.pop(0)
            _conversation_history.pop(0)

    return response, usage, latency

# ── MCP protocol ────────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "chat",
        "description": (
            "Send a message to the Azure AI assistant and get a response. "
            "Maintains conversation history across calls. "
            "Use 'reset' tool to clear history and start fresh."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "The message to send to the assistant.",
                },
            },
            "required": ["message"],
        },
    },
    {
        "name": "configure",
        "description": (
            "View or change assistant settings. Call with no arguments to see current config. "
            "Pass any setting as key-value to update. Changes save to disk immediately."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "api_key": {"type": "string", "description": "Azure AI API key."},
                "endpoint": {"type": "string", "description": "Azure AI endpoint URL."},
                "deployment": {"type": "string", "description": "Deployed model name (for OpenAI-compatible endpoint)."},
                "model": {"type": "string", "description": "Model ID (for serverless endpoint or display)."},
                "model_type": {
                    "type": "string",
                    "enum": ["deployed", "serverless"],
                    "description": "Endpoint type: 'deployed' for OpenAI-compat, 'serverless' for unified inference.",
                },
                "max_completion_tokens": {"type": "integer", "description": "Max tokens in response."},
                "temperature": {"type": "number", "description": "Sampling temperature (0.0-2.0)."},
                "system_prompt": {"type": "string", "description": "System prompt for the assistant."},
                "conversation_max_turns": {"type": "integer", "description": "Max conversation turns before trimming."},
                "voice": {"type": "string", "description": "Default Azure TTS voice for voice_chat (overrides speech config)."},
            },
        },
    },
    {
        "name": "reset",
        "description": "Clear conversation history and start fresh.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "models",
        "description": (
            "List and test available models. Shows which models are accessible "
            "from the current endpoint."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "test": {
                    "type": "boolean",
                    "description": "If true, send a test message to each model to verify it works.",
                    "default": False,
                },
            },
        },
    },
]


def handle_request(req):
    method = req.get("method")
    params = req.get("params", {})
    req_id = req.get("id")
    progress_token = params.get("_meta", {}).get("progressToken")

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": "azure-voice-assistant", "version": "1.0.0"},
            },
        }
    elif method == "notifications/initialized":
        return None
    elif method == "notifications/cancelled":
        return None
    elif method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
    elif method == "tools/call":
        tool_name = params.get("name")
        args = params.get("arguments", {})

        if tool_name == "chat":
            message = args.get("message", "")
            if not message:
                return _result(req_id, "Error: 'message' is required.")

            response, usage, latency = chat(message, progress_token)

            # Format response with metadata
            model_label = CONFIG.get('model', CONFIG.get('deployment', '?'))
            ttft = usage.get('_ttft_ms', 0)
            meta = f"\n\n---\n_{model_label} | "
            meta += f"{usage.get('prompt_tokens', '?')}→{usage.get('completion_tokens', '?')} tokens | "
            meta += f"TTFT {ttft}ms | {latency:.0f}ms total_"

            return _result(req_id, response + meta)

        elif tool_name == "configure":
            return _handle_configure(req_id, args)

        elif tool_name == "reset":
            global _conversation_history
            count = len(_conversation_history) // 2
            _conversation_history = []
            return _result(req_id, f"Conversation cleared ({count} turns removed).")

        elif tool_name == "models":
            return _handle_models(req_id, args, progress_token)

        else:
            return {
                "jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
            }

    return None


def _result(req_id, text):
    return {
        "jsonrpc": "2.0", "id": req_id,
        "result": {"content": [{"type": "text", "text": text}]},
    }


def _handle_configure(req_id, args):
    settable = {
        "api_key", "endpoint", "deployment", "model", "model_type",
        "max_completion_tokens", "temperature", "system_prompt",
        "conversation_max_turns", "voice",
    }

    updated = []
    for k, v in args.items():
        if k not in settable:
            continue
        if k == "api_key":
            CONFIG[k] = str(v)
        elif k == "endpoint":
            CONFIG[k] = str(v).rstrip("/")
        elif k in ("deployment", "model", "model_type", "system_prompt", "voice"):
            CONFIG[k] = str(v)
        elif k == "max_completion_tokens":
            CONFIG[k] = max(1, min(int(v), 128000))
        elif k == "temperature":
            CONFIG[k] = max(0.0, min(float(v), 2.0))
        elif k == "conversation_max_turns":
            CONFIG[k] = max(1, min(int(v), 500))
        else:
            CONFIG[k] = v
        updated.append(f"{k}={CONFIG[k]}" if k != "api_key" else f"{k}=***{str(v)[-4:]}")

    if updated:
        save_config()
        return _result(req_id, "Updated: " + ", ".join(updated))

    # Show current config
    lines = ["[Azure AI]"]
    lines.append(f"  endpoint:    {CONFIG.get('endpoint', '')}")
    lines.append(f"  api_key:     ***{CONFIG.get('api_key', '')[-4:]}" if CONFIG.get("api_key") else "  api_key:     (not set)")
    lines.append(f"  deployment:  {CONFIG.get('deployment', '')}")
    lines.append(f"  model:       {CONFIG.get('model', '')}")
    lines.append(f"  model_type:  {CONFIG.get('model_type', '')}")
    lines.append("")
    lines.append("[Generation]")
    lines.append(f"  max_completion_tokens: {CONFIG.get('max_completion_tokens', '')}")
    lines.append(f"  temperature:           {CONFIG.get('temperature', '')}")
    lines.append("")
    lines.append("[Voice]")
    lines.append(f"  voice:                 {CONFIG.get('voice', '') or '(using speech config)'}")
    lines.append("")
    lines.append("[Conversation]")
    lines.append(f"  system_prompt:         {CONFIG.get('system_prompt', '')}")
    lines.append(f"  max_turns:             {CONFIG.get('conversation_max_turns', '')}")
    lines.append(f"  current_turns:         {len(_conversation_history) // 2}")

    return _result(req_id, "\n".join(lines))


def _handle_models(req_id, args, progress_token):
    do_test = args.get("test", False)
    api_key = CONFIG.get("api_key", "")
    endpoint = CONFIG.get("endpoint", "")

    if not api_key or not endpoint:
        return _result(req_id, "Error: api_key and endpoint must be configured first.")

    lines = [f"Endpoint: {endpoint}\n"]

    # Check deployed models
    lines.append("[Deployed (OpenAI endpoint)]")
    deployment = CONFIG.get("deployment", "")
    if deployment:
        if do_test:
            text, usage, latency = _test_model_deployed(deployment)
            status = f"OK ({latency:.0f}ms)" if not text.startswith("Error") else text[:60]
            lines.append(f"  {deployment}: {status}")
        else:
            lines.append(f"  {deployment} (current)")
    lines.append("")

    # Check serverless models
    serverless_models = [
        "grok-3", "grok-3-mini",
        "Meta-Llama-3.1-405B-Instruct", "Llama-3.3-70B-Instruct",
        "Llama-4-Maverick-17B-128E-Instruct-FP8", "Llama-4-Scout-17B-16E-Instruct",
        "DeepSeek-R1", "MAI-DS-R1", "cohere-command-a",
        "mistral-small-2503", "Phi-4", "Phi-4-mini-reasoning",
    ]

    lines.append("[Serverless (unified inference)]")
    if do_test:
        if progress_token:
            _send_progress(progress_token, 0.1, "Testing models...")
        for i, m in enumerate(serverless_models):
            text, usage, latency = _test_model_serverless(m)
            status = f"OK ({latency:.0f}ms)" if not text.startswith("Error") else "unavailable"
            lines.append(f"  {m}: {status}")
            if progress_token:
                _send_progress(progress_token, (i + 1) / len(serverless_models), f"Tested {m}")
    else:
        for m in serverless_models:
            marker = " (current)" if m == CONFIG.get("model") else ""
            lines.append(f"  {m}{marker}")

    return _result(req_id, "\n".join(lines))


def _test_model_deployed(deployment):
    try:
        endpoint = CONFIG["endpoint"]
        url = f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version=2024-10-21"
        payload = json.dumps({
            "messages": [{"role": "user", "content": "Say hi"}],
            "max_completion_tokens": 16,
        }).encode()
        req = urllib.request.Request(url, data=payload, headers={
            "api-key": CONFIG["api_key"], "Content-Type": "application/json",
        })
        t0 = time.perf_counter()
        resp = urllib.request.urlopen(req, timeout=15)
        latency = (time.perf_counter() - t0) * 1000
        data = json.loads(resp.read().decode())
        text = data["choices"][0]["message"]["content"]
        return text, data.get("usage", {}), latency
    except Exception as e:
        return f"Error: {e}", {}, 0


def _test_model_serverless(model):
    try:
        endpoint = CONFIG["endpoint"]
        url = f"{endpoint}/models/chat/completions?api-version=2024-05-01-preview"
        payload = json.dumps({
            "messages": [{"role": "user", "content": "Say hi"}],
            "model": model,
            "max_tokens": 16,
        }).encode()
        req = urllib.request.Request(url, data=payload, headers={
            "api-key": CONFIG["api_key"], "Content-Type": "application/json",
        })
        t0 = time.perf_counter()
        resp = urllib.request.urlopen(req, timeout=15)
        latency = (time.perf_counter() - t0) * 1000
        data = json.loads(resp.read().decode())
        text = data["choices"][0]["message"]["content"]
        return text, data.get("usage", {}), latency
    except Exception as e:
        return f"Error: {e}", {}, 0


# ── MCP transport ───────────────────────────────────────────────────────────

def _send_progress(token, progress, message=""):
    _write_response({
        "jsonrpc": "2.0",
        "method": "notifications/progress",
        "params": {"progressToken": token, "progress": progress, "total": 1.0, "message": message},
    })


def _write_response(resp):
    with _stdout_lock:
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


_request_queue = []
_request_cond = threading.Condition()


def _stdin_reader():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = req.get("method")
        if method == "notifications/cancelled":
            continue

        with _request_cond:
            _request_queue.append(req)
            _request_cond.notify()

    with _request_cond:
        _request_queue.append(None)
        _request_cond.notify()


def main():
    reader = threading.Thread(target=_stdin_reader, daemon=True)
    reader.start()

    while True:
        with _request_cond:
            while not _request_queue:
                _request_cond.wait()
            req = _request_queue.pop(0)

        if req is None:
            break

        resp = handle_request(req)
        if resp is not None:
            _write_response(resp)


if __name__ == "__main__":
    main()
