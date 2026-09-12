"""Protocol adapters — translate provider-native request/response bodies to and
from the gateway's internal OpenAI-chat-completions shape.

design/v2-gateway-capture-m1-ingest.md §5.5 (protocol completeness): the
OpenAI Agents SDK defaults to the Responses API, Claude Agent SDK / native
Anthropic clients use the Messages API, and Google ADK / Gemini clients use
generateContent. None of these are chat-completions. Rather than duplicate
ProxyHandler.handle()'s auth/routing/enforcement/cache/logging core per
protocol, each adapter here normalises the inbound request into chat messages,
calls the SAME ProxyHandler.handle() (protocol=<name> passed through for the
call record), and translates the chat-completion-shaped result back into the
caller's own dialect. One core, three dialects at the edges.
"""

import json
import time
import uuid
from typing import Any


def _ensure_backend_prefix(model: str, backend: str) -> str:
    """Prefix a bare model name with its LiteLLM backend, unless the caller
    already supplied one (any 'xxx/model' form is left untouched).

    Native provider endpoints (Anthropic /v1/messages, Gemini generateContent)
    receive bare model names from real provider SDKs — those SDKs have no
    reason to know ACP's 'anthropic/claude-...' convention, since that's not
    part of the real Anthropic/Gemini API contract. Unlike bare model names on
    /v1/chat/completions (which default to OpenAI — see proxy.py's documented
    backward-compatible convention), a bare model name on a *protocol-specific*
    endpoint unambiguously means that endpoint's own provider.
    """
    if not model or "/" in model:
        return model
    return f"{backend}/{model}"


# ── OpenAI Responses API ──────────────────────────────────────────────────────

def responses_request_to_chat(body: dict) -> dict:
    """Translate a /v1/responses request body into a chat/completions body."""
    messages: list[dict] = []
    instructions = body.get("instructions")
    if instructions:
        messages.append({"role": "system", "content": instructions})

    inp = body.get("input", "")
    if isinstance(inp, str):
        messages.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        for item in inp:
            if not isinstance(item, dict):
                continue
            role = item.get("role", "user")
            content = item.get("content", "")
            if isinstance(content, list):
                # content blocks: [{type: "input_text"/"output_text", text: ...}, ...]
                text = " ".join(
                    part.get("text", "") for part in content
                    if isinstance(part, dict) and "text" in part
                )
                messages.append({"role": role, "content": text})
            else:
                messages.append({"role": role, "content": content})

    chat_body = {
        "model":    body.get("model", "gpt-4o-mini"),
        "messages": messages,
        "stream":   bool(body.get("stream", False)),
    }
    if body.get("tools"):
        chat_body["tools"] = body["tools"]
    return chat_body


def chat_response_to_responses(chat_resp: dict, model_requested: str) -> dict:
    """Translate a chat/completions response body into a /v1/responses body."""
    choices = chat_resp.get("choices", [])
    message = (choices[0].get("message") if choices else {}) or {}
    text = message.get("content", "") or ""
    usage = chat_resp.get("usage") or {}

    return {
        "id":         f"resp_{uuid.uuid4().hex}",
        "object":     "response",
        "created_at": int(time.time()),
        "model":      chat_resp.get("model", model_requested),
        "status":     "completed",
        "output": [{
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }],
        "output_text": text,
        "usage": {
            "input_tokens":  usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens":  usage.get("total_tokens", 0),
        },
        "gateway": chat_resp.get("gateway", {}),
    }


# ── Anthropic native Messages API ─────────────────────────────────────────────

def anthropic_request_to_chat(body: dict) -> dict:
    """Translate a /v1/messages (Anthropic) request body into chat/completions."""
    messages: list[dict] = []
    system = body.get("system")
    if system:
        if isinstance(system, list):
            system = " ".join(b.get("text", "") for b in system if isinstance(b, dict))
        messages.append({"role": "system", "content": system})

    for m in body.get("messages", []):
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue
        # content blocks: text / tool_use / tool_result
        text_parts, tool_calls = [], []
        for block in content if isinstance(content, list) else []:
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "tool_use":
                tool_calls.append({
                    "id":       block.get("id", ""),
                    "type":     "function",
                    "function": {"name": block.get("name", ""), "arguments": json.dumps(block.get("input", {}))},
                })
            elif btype == "tool_result":
                messages.append({
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id", ""),
                    "content": block.get("content", ""),
                })
        msg: dict[str, Any] = {"role": role, "content": " ".join(text_parts)}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        messages.append(msg)

    chat_body = {
        "model":      _ensure_backend_prefix(body.get("model", "claude-sonnet-4-6"), "anthropic"),
        "messages":   messages,
        "stream":     bool(body.get("stream", False)),
        "max_tokens": body.get("max_tokens", 1024),
    }
    if body.get("tools"):
        chat_body["tools"] = [
            {"type": "function", "function": {
                "name": t.get("name", ""), "description": t.get("description", ""),
                "parameters": t.get("input_schema", {}),
            }} for t in body["tools"]
        ]
    return chat_body


def chat_response_to_anthropic(chat_resp: dict, model_requested: str) -> dict:
    """Translate a chat/completions response body into an Anthropic Messages body."""
    choices = chat_resp.get("choices", [])
    message = (choices[0].get("message") if choices else {}) or {}
    text = message.get("content", "") or ""
    usage = chat_resp.get("usage") or {}
    finish_reason = (choices[0].get("finish_reason") if choices else "stop") or "stop"

    content_blocks = [{"type": "text", "text": text}] if text else []
    for tc in (message.get("tool_calls") or []):
        fn = tc.get("function", {})
        try:
            args = json.loads(fn.get("arguments", "{}"))
        except Exception:
            args = {}
        content_blocks.append({
            "type": "tool_use", "id": tc.get("id", ""),
            "name": fn.get("name", ""), "input": args,
        })

    return {
        "id":      f"msg_{uuid.uuid4().hex}",
        "type":    "message",
        "role":    "assistant",
        "model":   chat_resp.get("model", model_requested),
        "content": content_blocks,
        "stop_reason": "tool_use" if message.get("tool_calls") else "end_turn",
        "usage": {
            "input_tokens":  usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
        "gateway": chat_resp.get("gateway", {}),
    }


# ── Google Gemini generateContent ─────────────────────────────────────────────

def gemini_request_to_chat(body: dict, model: str) -> dict:
    """Translate a generateContent request body into chat/completions."""
    messages: list[dict] = []
    system_instruction = body.get("systemInstruction")
    if system_instruction:
        parts = system_instruction.get("parts", []) if isinstance(system_instruction, dict) else []
        text = " ".join(p.get("text", "") for p in parts if isinstance(p, dict))
        if text:
            messages.append({"role": "system", "content": text})

    for c in body.get("contents", []):
        role = "assistant" if c.get("role") == "model" else "user"
        parts = c.get("parts", [])
        text = " ".join(p.get("text", "") for p in parts if isinstance(p, dict) and "text" in p)
        messages.append({"role": role, "content": text})

    return {"model": _ensure_backend_prefix(model, "gemini"), "messages": messages, "stream": False}


def chat_response_to_gemini(chat_resp: dict, model_requested: str) -> dict:
    """Translate a chat/completions response body into a generateContent body."""
    choices = chat_resp.get("choices", [])
    message = (choices[0].get("message") if choices else {}) or {}
    text = message.get("content", "") or ""
    usage = chat_resp.get("usage") or {}

    return {
        "candidates": [{
            "content": {"role": "model", "parts": [{"text": text}]},
            "finishReason": "STOP",
            "index": 0,
        }],
        "usageMetadata": {
            "promptTokenCount":     usage.get("prompt_tokens", 0),
            "candidatesTokenCount": usage.get("completion_tokens", 0),
            "totalTokenCount":      usage.get("total_tokens", 0),
        },
        "modelVersion": chat_resp.get("model", model_requested),
        "gateway": chat_resp.get("gateway", {}),
    }
