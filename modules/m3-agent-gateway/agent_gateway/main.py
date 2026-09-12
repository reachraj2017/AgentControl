"""
Agent Gateway — Universal LLM Control Plane
============================================
Every LLM call from any agent system flows through here.
Exposes an OpenAI-compatible API on port 8080.

Per-request flow:
  1. Resolve routing   — change store policy → target model + backend
  2. Enforce           — governance CB / HITL / policy check (if phase2 enabled)
  3. Inject mods       — approved prompt prefix / suffix / few-shot
  4. Forward           — httpx call to actual LLM backend
  5. Log + span        — ClickHouse gateway_call_log + OTel span (async)
  6. Shadow            — duplicate to alternate model at configured sample rate (async)
  7. Return            — original LLM response + gateway metadata field

Calling convention — include these headers on every request:
  X-Gateway-System-Id:   identifies the calling system  (opt-demo, my-system, …)
  X-Gateway-Agent-Role:  agent role  (orchestrator | searcher | summarizer | translator)
  X-Gateway-Run-Id:      run ID for trace correlation

Operations surface (no auth required in dev):
  GET  /gateway/status           current state + 24 h call stats
  GET  /gateway/calls            recent call log
  GET  /gateway/routing          active routing policies
  POST /gateway/routing          create routing override
  PUT  /gateway/routing/{id}     update routing policy (model/backend/reason)
  DEL  /gateway/routing/{id}     disable routing policy
  GET  /gateway/mods             active prompt modifications
  POST /gateway/mods             create prompt mod
  PUT  /gateway/mods/{id}        update prompt mod (type/content/evidence)
  DEL  /gateway/mods/{id}        disable prompt mod
  GET  /gateway/shadow           active shadow rules
  POST /gateway/shadow           create shadow rule
  PUT  /gateway/shadow/{id}      update shadow rule (model/backend/sample_rate)
  DEL  /gateway/shadow/{id}      disable shadow rule
  GET  /gateway/changes          proposed changes (filter by status=)
  POST /gateway/changes          propose a change
  PUT  /gateway/changes/{id}     approve or reject; auto-applies when approved
"""

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import operations
import protocol_adapters as pa
from change_store import ChangeStore
from db import GatewayDB
from proxy import ProxyHandler
from telemetry import init_telemetry

_LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
)
log = logging.getLogger("gateway")

# ── Singletons ────────────────────────────────────────────────────────────────

db           = GatewayDB()
change_store = ChangeStore(db=db)
proxy        = ProxyHandler(change_store=change_store, db=db)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Agent Gateway starting…")
    db.ensure_tables()
    init_telemetry()
    asyncio.create_task(change_store.refresh_loop())
    log.info("Agent Gateway ready on port 8080")
    yield
    log.info("Agent Gateway shutting down")


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Agent Gateway",
    version="1.0.0",
    description="Universal LLM control plane: routing, enforcement, improvement, observability.",
    lifespan=lifespan,
)

operations.set_deps(db, change_store)
app.include_router(operations.router, prefix="/gateway", tags=["Operations"])


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["Health"])
async def health():
    return {"status": "ok", "service": "agent-gateway"}


# ── OpenAI-compatible API ─────────────────────────────────────────────────────

@app.get("/v1/models", tags=["LLM Proxy"])
async def list_models():
    """Return a model list that downstream clients can query."""
    return {
        "object": "list",
        "data": [
            {"id": "gpt-4o-mini",       "object": "model", "owned_by": "openai"},
            {"id": "gpt-4o",            "object": "model", "owned_by": "openai"},
            {"id": "gpt-4.1",           "object": "model", "owned_by": "openai"},
            {"id": "gpt-4.1-mini",      "object": "model", "owned_by": "openai"},
            {"id": "claude-sonnet-4-6", "object": "model", "owned_by": "anthropic"},
        ],
    }


@app.post("/v1/chat/completions", tags=["LLM Proxy"])
async def chat_completions(request: Request):
    """
    OpenAI-compatible chat completions endpoint.
    Passes through to the upstream LLM after applying gateway policies.

    Routing, enforcement, and modifications are controlled via the
    /gateway/* operations endpoints — no code changes needed.
    """
    body = await request.json()
    hdrs = dict(request.headers)

    # Extract gateway context from custom headers (default gracefully)
    system_id       = hdrs.get("x-gateway-system-id",   "unknown")
    agent_role      = hdrs.get("x-gateway-agent-role",   "unknown")
    run_id          = hdrs.get("x-gateway-run-id",       str(uuid.uuid4()))
    trace_id        = hdrs.get("x-gateway-trace-id",     "")
    conversation_id = hdrs.get("x-gateway-conversation-id", "")
    # Fall back to W3C traceparent if explicit header not set
    if not trace_id:
        traceparent = hdrs.get("traceparent", "")
        if traceparent and traceparent.count("-") >= 3:
            trace_id = traceparent.split("-")[1]

    return await proxy.handle(
        body=body,
        system_id=system_id,
        agent_role=agent_role,
        run_id=run_id,
        trace_id=trace_id,
        headers=hdrs,
        conversation_id=conversation_id,
        protocol="openai.chat",
    )


# ── Protocol completeness ──────────────────────────────────────────────────────
# Each handler normalises its
# provider-native request into chat/completions, calls the SAME proxy.handle()
# core (auth, routing, enforcement, caching, logging all reused unchanged),
# then translates the response back into the caller's own dialect.

def _gw_context(hdrs: dict) -> dict:
    system_id  = hdrs.get("x-gateway-system-id",   "unknown")
    agent_role = hdrs.get("x-gateway-agent-role",   "unknown")
    run_id     = hdrs.get("x-gateway-run-id",       str(uuid.uuid4()))
    trace_id   = hdrs.get("x-gateway-trace-id",     "")
    conversation_id = hdrs.get("x-gateway-conversation-id", "")
    if not trace_id:
        traceparent = hdrs.get("traceparent", "")
        if traceparent and traceparent.count("-") >= 3:
            trace_id = traceparent.split("-")[1]
    return dict(system_id=system_id, agent_role=agent_role, run_id=run_id,
                trace_id=trace_id, conversation_id=conversation_id)


@app.post("/v1/responses", tags=["LLM Proxy"])
async def responses_api(request: Request):
    """OpenAI Responses API — default transport for the OpenAI Agents SDK,
    required for hosted tools (WebSearchTool, FileSearchTool, ComputerTool)."""
    body = await request.json()
    hdrs = dict(request.headers)
    ctx  = _gw_context(hdrs)
    chat_body = pa.responses_request_to_chat(body)

    result = await proxy.handle(body=chat_body, headers=hdrs, protocol="openai.responses", **ctx)
    if isinstance(result, JSONResponse):
        chat_json = json.loads(result.body)
        return JSONResponse(
            status_code=result.status_code,
            content=pa.chat_response_to_responses(chat_json, body.get("model", "")),
        )
    return result  # streaming — passthrough as chat-shaped SSE for now (see README TODO)


@app.post("/v1/messages", tags=["LLM Proxy"])
async def messages_api(request: Request):
    """Native Anthropic Messages API — Claude Agent SDK, Anthropic SDK, LangChain-Anthropic."""
    body = await request.json()
    hdrs = dict(request.headers)
    ctx  = _gw_context(hdrs)
    chat_body = pa.anthropic_request_to_chat(body)

    result = await proxy.handle(body=chat_body, headers=hdrs, protocol="anthropic.messages", **ctx)
    if isinstance(result, JSONResponse):
        chat_json = json.loads(result.body)
        return JSONResponse(
            status_code=result.status_code,
            content=pa.chat_response_to_anthropic(chat_json, body.get("model", "")),
        )
    return result


@app.post("/v1beta/models/{model_and_method}", tags=["LLM Proxy"])
async def gemini_generate_content(model_and_method: str, request: Request):
    """Gemini native generateContent — Google ADK native, Gemini SDK.
    Path shape: /v1beta/models/{model}:generateContent (or :streamGenerateContent)."""
    if ":" not in model_and_method:
        return JSONResponse(status_code=404, content={"error": {"message": "unknown path"}})
    model, method = model_and_method.split(":", 1)
    body = await request.json()
    hdrs = dict(request.headers)
    ctx  = _gw_context(hdrs)
    chat_body = pa.gemini_request_to_chat(body, model)

    result = await proxy.handle(body=chat_body, headers=hdrs, protocol="google.generateContent", **ctx)
    if isinstance(result, JSONResponse):
        chat_json = json.loads(result.body)
        return JSONResponse(
            status_code=result.status_code,
            content=pa.chat_response_to_gemini(chat_json, model),
        )
    return result  # streamGenerateContent: passthrough for now (see README TODO)


@app.post("/v1/embeddings", tags=["LLM Proxy"])
async def embeddings_api(request: Request):
    body = await request.json()
    hdrs = dict(request.headers)
    ctx  = _gw_context(hdrs)
    return await proxy.handle_embeddings(
        body=body, system_id=ctx["system_id"], agent_role=ctx["agent_role"],
        run_id=ctx["run_id"], trace_id=ctx["trace_id"], headers=hdrs,
    )


# ── Checkpoint / handoff / tool-span ───────────────────────────────────────────
# Same front door as LLM traffic, for signals that never cross the LLM wire
# (pre-action gates, sub-agent handoffs, non-LLM tool executions).

@app.post("/v1/checkpoint", tags=["Structural Signals"])
async def checkpoint(request: Request):
    body = await request.json()
    return await proxy.handle_checkpoint(body, dict(request.headers))


@app.post("/v1/handoff", tags=["Structural Signals"])
async def handoff(request: Request):
    body = await request.json()
    return await proxy.handle_handoff(body, dict(request.headers))


@app.post("/v1/tool-span", tags=["Structural Signals"])
async def tool_span(request: Request):
    body = await request.json()
    return await proxy.handle_tool_span(body, dict(request.headers))
