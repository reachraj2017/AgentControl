"""
Runner — Opt-Demo
=================
Wraps Google ADK with ACP SDK instrumentation (OTel spans, governance gate checks).

Routing strategy:
  - Simple queries (search only / translate only / summarize only) go through
    the appropriate sub-agent runner directly — one LLM decision, reliable.
  - Multi-step pipeline (search + summarize + translate) is driven EXPLICITLY
    in code by calling each sub-agent runner in sequence.  The orchestrator is
    bypassed for steps 2/3 because GPT-4o-mini consistently routes every step
    to the searcher when given the full conversation context.
  - Passthrough (ambiguous) queries are sent to the orchestrator directly.

Span hierarchy for a pipeline query:
  agent.task  [orchestrator, root]
    agent.task  [searcher]
      agent.tool_call  [web_search]
    agent.task  [summarizer]
    agent.task  [translator]

All spans share run.id so the eval runner groups them correctly.
"""

import acp_setup  # noqa: F401 — must be first import; initialises OTel provider

import asyncio
import os
import re
import sys
import time
import uuid

import google.genai.types as genai_types
from google.adk.events import Event
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from opentelemetry import trace, context as otel_context


# ── Session / runner store ────────────────────────────────────────────────────
_session_service = InMemorySessionService()
_sessions: dict[str, str] = {}        # user_id → session_id
_runners: dict[str, Runner] = {}       # agent_name → Runner
APP_NAME = "opt-demo"


def _get_runner(agent_name: str) -> Runner:
    """Lazily create and cache a Runner for each agent."""
    if agent_name not in _runners:
        from agents import (
            create_orchestrator_agent,
            create_searcher_agent,
            create_summarizer_agent,
            create_translator_agent,
        )
        factories = {
            "orchestrator": create_orchestrator_agent,
            "searcher":     create_searcher_agent,
            "summarizer":   create_summarizer_agent,
            "translator":   create_translator_agent,
        }
        _runners[agent_name] = Runner(
            agent=factories[agent_name](),
            app_name=APP_NAME,
            session_service=_session_service,
        )
    return _runners[agent_name]


# ── Query classification ──────────────────────────────────────────────────────

def _classify_query(user_input: str) -> dict:
    """
    Classify the user query and return a routing dict.

    Types:
      pipeline          — search + summarize and/or translate
      search            — search / find only
      summarize         — summarize only (uses session history for content)
      translate         — translate only (uses session history for content)
      summarize_translate — summarize + translate, no search (uses session history)
      passthrough       — everything else; fall through to orchestrator
    """
    # Strip trailing sentence punctuation before classifying — every regex
    # below anchors on end-of-string (\s*$) to find a trailing language/word,
    # and a trailing '.'/'?'/'!' (near-universal in natural sentences) was
    # silently defeating all of them, e.g. "translate 'good morning' to
    # french." never matched, silently falling back to the no-explicit-text
    # "translate the previous response" path.
    user_input = user_input.rstrip()
    user_input = re.sub(r'[.!?]+$', '', user_input).rstrip()
    lower = user_input.lower()

    has_search    = bool(re.search(r'\b(search|find)\b', lower))
    has_summarize = bool(re.search(r'\b(summarize|summarise|summary|condense|shorten)\b', lower))
    has_translate = bool(re.search(r'\btranslat', lower))

    # Word count (default 20)
    wc   = re.search(r'\b(\d+)\s*[\-\s]?words?\b', lower)
    word_count = int(wc.group(1)) if wc else 20

    # Target language — try "translate ... to <lang>" first, then "in <lang>" at end.
    # The filler-word group matches [^\s]+ rather than \w+ so a quoted/punctuated
    # inline phrase ("translate 'good morning' to french") doesn't break the chain
    # before it ever reaches "to <lang>".
    lang = re.search(r'\btranslat\w*(?:\s+[^\s]+){0,4}\s+(?:to|into|in)\s+(\w+)', lower)
    if not lang:
        lang = re.search(r'\b(?:to|into|in)\s+(\w+)\s*$', lower)
    language = lang.group(1).capitalize() if lang else None

    # Search topic
    m = re.search(
        r'\b(?:search|find)(?:\s+for)?\s+(.+?)'
        r'(?:\s+(?:and\s+)?then\b|\s*[,;]|\s+and\b|$)',
        lower,
    )
    topic = m.group(1).strip() if m else user_input

    if has_search and (has_summarize or has_translate):
        return {
            "type":       "pipeline",
            "topic":      topic,
            "word_count": word_count if has_summarize else None,
            "language":   language,
        }
    if has_search:
        return {"type": "search", "topic": topic}
    if has_summarize and has_translate:
        return {"type": "summarize_translate", "word_count": word_count, "language": language}
    if has_summarize:
        # Check for explicit inline text: "summarize: <text>" or "summarize this: <text>"
        inline = re.search(r'\b(?:summarize|summarise)\b[\s:]+(.{20,})', user_input, re.IGNORECASE)
        return {"type": "summarize", "word_count": word_count,
                "text": inline.group(1).strip() if inline else None}
    if has_translate:
        # Extract explicit inline text: "translate <text> to/in/into <lang>"
        # Use original user_input (not lower) to preserve casing
        inline = re.search(
            r'\btranslat\w*\s+(.+?)\s+(?:to|into|in)\s+\w+\s*$',
            user_input, re.IGNORECASE,
        )
        explicit_text = inline.group(1).strip() if inline else None
        # Reject if the "text" is just a filler word (≤ 1 word) — means no inline content
        if explicit_text and len(explicit_text.split()) < 2:
            explicit_text = None
        return {"type": "translate", "language": language, "text": explicit_text}
    return {"type": "passthrough"}


# ── Text extraction (protobuf-safe) ──────────────────────────────────────────

def _extract_text(event) -> str:
    content = getattr(event, "content", None)
    if not content:
        return ""
    parts = getattr(content, "parts", None) or []
    for part in parts:
        text = getattr(part, "text", None) or ""
        if text.strip():
            return text
        fr = getattr(part, "function_response", None)
        if fr:
            resp = getattr(fr, "response", None)
            if isinstance(resp, dict):
                for key in ("result", "output", "content"):
                    val = resp.get(key)
                    if val and isinstance(val, str) and val.strip():
                        return val
    return ""


# ── Public API ────────────────────────────────────────────────────────────────

def run_agent(user_input: str, user_id: str = "default",
              source: str = "production", conversation_id: str | None = None,
              run_id: str | None = None) -> tuple[str, str]:
    """Run the orchestrator and return (response_text, trace_id)."""
    from acp_setup import get_acp_tracer, get_gov_client
    tracer = get_acp_tracer()
    gov    = get_gov_client()
    run_id = run_id or str(uuid.uuid4())

    with tracer.start_as_current_span("agent.task") as root_span:
        root_span.set_attribute("agent.role",   "orchestrator")
        root_span.set_attribute("agent.id",     "orchestrator")
        root_span.set_attribute("task.input",   user_input[:2000])
        root_span.set_attribute("run.id",       run_id)
        root_span.set_attribute("trace.source", source)
        if conversation_id:
            root_span.set_attribute("conversation.id", conversation_id)

        ctx      = root_span.get_span_context()
        trace_id = format(ctx.trace_id, "032x") if ctx and ctx.trace_id else ""
        root_ctx = otel_context.get_current()

        loop = asyncio.new_event_loop()
        try:
            response = loop.run_until_complete(
                _run_async(user_input, user_id, root_span, root_ctx, tracer, run_id, gov, source, conversation_id)
            )
        except Exception as e:
            root_span.set_attribute("task.status", "failure")
            root_span.record_exception(e)
            raise
        finally:
            loop.close()

        root_span.set_attribute("task.output", response[:2000])
        root_span.set_attribute("task.status", "success")

    return response, trace_id


# ── Session helpers ───────────────────────────────────────────────────────────

async def _ensure_session(user_id: str) -> str:
    """Return the session_id for this user, creating one if needed."""
    if user_id not in _sessions:
        s = await _session_service.create_session(app_name=APP_NAME, user_id=user_id)
        _sessions[user_id] = s.id
    return _sessions[user_id]


async def _get_last_assistant_message(user_id: str) -> str:
    """Return the most recent assistant message from the user's main session."""
    try:
        session_id = await _ensure_session(user_id)
        session = await _session_service.get_session(
            app_name=APP_NAME, user_id=user_id, session_id=session_id
        )
        if not session:
            return ""
        events = getattr(session, "events", []) or []
        for event in reversed(events):
            if getattr(event, "author", "") not in ("user", ""):
                text = _extract_text(event)
                if text.strip():
                    return text
    except Exception as e:
        print(f"[runner] get_last_assistant_message failed: {e}", file=sys.stderr, flush=True)
    return ""


async def _inject_history(user_id: str, user_query: str, assistant_response: str):
    """
    Append the pipeline exchange to the user's main session so
    follow-up single-step queries have conversation context.
    """
    try:
        session_id = await _ensure_session(user_id)
        session = await _session_service.get_session(
            app_name=APP_NAME, user_id=user_id, session_id=session_id
        )
        if not session:
            return
        inv_id = str(uuid.uuid4())
        await _session_service.append_event(
            session=session,
            event=Event(
                invocation_id=inv_id,
                author="user",
                content=genai_types.Content(
                    role="user",
                    parts=[genai_types.Part(text=user_query)],
                ),
            ),
        )
        await _session_service.append_event(
            session=session,
            event=Event(
                invocation_id=inv_id,
                author="orchestrator",
                content=genai_types.Content(
                    role="model",
                    parts=[genai_types.Part(text=assistant_response)],
                ),
            ),
        )
    except Exception as e:
        print(f"[runner] history injection failed: {e}", file=sys.stderr, flush=True)


# ── Routing ───────────────────────────────────────────────────────────────────

async def _run_async(user_input, user_id, root_span, root_ctx, tracer, run_id, gov,
                     source="production", conversation_id=None):
    # Always ensure the user's main session exists for context propagation.
    await _ensure_session(user_id)
    session_id = _sessions[user_id]

    intent = _classify_query(user_input)
    itype  = intent["type"]

    # ── Multi-step pipeline: search + summarize and/or translate ──────────────
    if itype == "pipeline":
        return await _run_pipeline(
            intent, user_input, user_id, root_span, root_ctx, tracer, run_id, gov, source, conversation_id
        )

    # ── Search only ───────────────────────────────────────────────────────────
    if itype == "search":
        topic = intent["topic"]
        result = await _call_agent(
            runner=_get_runner("searcher"), agent_name="searcher",
            user_id=user_id, session_id=session_id,
            message=f"Search for information about: {topic}",
            root_span=root_span, root_ctx=root_ctx, tracer=tracer, run_id=run_id,
            open_child_span=True, gov=gov, source=source, conversation_id=conversation_id,
        )
        await _inject_history(user_id, user_input, result)
        return result

    # ── Summarize only ────────────────────────────────────────────────────────
    if itype == "summarize":
        wc            = intent["word_count"]
        explicit_text = intent.get("text")
        summarize_msg = (
            f"Summarize the following text in EXACTLY {wc} words:\n\n{explicit_text}"
            if explicit_text
            else f"Summarize the previous response in EXACTLY {wc} words."
        )
        result = await _call_agent(
            runner=_get_runner("summarizer"), agent_name="summarizer",
            user_id=user_id, session_id=session_id,
            message=summarize_msg,
            root_span=root_span, root_ctx=root_ctx, tracer=tracer, run_id=run_id,
            open_child_span=True, gov=gov, source=source, conversation_id=conversation_id,
        )
        await _inject_history(user_id, user_input, result)
        return result

    # ── Translate only ────────────────────────────────────────────────────────
    if itype == "translate":
        lang          = intent.get("language") or "English"
        explicit_text = intent.get("text")
        translate_msg = (
            f"Translate the following text to {lang}:\n\n{explicit_text}"
            if explicit_text
            else f"Translate the previous response to {lang}."
        )
        result = await _call_agent(
            runner=_get_runner("translator"), agent_name="translator",
            user_id=user_id, session_id=session_id,
            message=translate_msg,
            root_span=root_span, root_ctx=root_ctx, tracer=tracer, run_id=run_id,
            open_child_span=True, gov=gov, source=source, conversation_id=conversation_id,
        )
        await _inject_history(user_id, user_input, result)
        return result

    # ── Summarize + translate, no search (content from session history) ───────
    if itype == "summarize_translate":
        wc           = intent["word_count"]
        lang         = intent.get("language") or "English"
        last_content = await _get_last_assistant_message(user_id)

        uid_m  = f"_pipe_{uuid.uuid4().hex}"
        sess_m = await _session_service.create_session(app_name=APP_NAME, user_id=uid_m)
        summarize_msg = (
            f"Summarize the following text in EXACTLY {wc} words:\n\n{last_content}"
            if last_content
            else f"Summarize the previous response in EXACTLY {wc} words."
        )
        summary = await _call_agent(
            runner=_get_runner("summarizer"), agent_name="summarizer",
            user_id=uid_m, session_id=sess_m.id,
            message=summarize_msg,
            root_span=root_span, root_ctx=root_ctx, tracer=tracer, run_id=run_id,
            open_child_span=True, gov=gov, source=source, conversation_id=conversation_id,
        )

        uid_t  = f"_pipe_{uuid.uuid4().hex}"
        sess_t = await _session_service.create_session(app_name=APP_NAME, user_id=uid_t)
        result = await _call_agent(
            runner=_get_runner("translator"), agent_name="translator",
            user_id=uid_t, session_id=sess_t.id,
            message=f"Translate the following text to {lang}:\n\n{summary}",
            root_span=root_span, root_ctx=root_ctx, tracer=tracer, run_id=run_id,
            open_child_span=True, gov=gov, source=source, conversation_id=conversation_id,
        )
        await _inject_history(user_id, user_input, result)
        return result

    # ── Passthrough: genuinely ambiguous — let orchestrator decide ────────────
    return await _call_agent(
        runner=_get_runner("orchestrator"), agent_name="orchestrator",
        user_id=user_id, session_id=session_id,
        message=user_input,
        root_span=root_span, root_ctx=root_ctx, tracer=tracer, run_id=run_id,
        open_child_span=False, gov=gov, source=source, conversation_id=conversation_id,
    )


# ── Pipeline (multi-step) ─────────────────────────────────────────────────────

async def _run_pipeline(intent, user_input, user_id, root_span, root_ctx, tracer,
                        run_id, gov, source="production", conversation_id=None):
    """
    Drive search → [summarize] → [translate] by calling each sub-agent
    runner directly with explicit content.  After completion, inject the
    exchange into the user's main orchestrator session for multi-turn context.
    """
    topic      = intent["topic"]
    word_count = intent["word_count"]
    language   = intent["language"]
    result     = ""

    # ── Step 1: search ────────────────────────────────────────────────────────
    uid_s  = f"_pipe_{uuid.uuid4().hex}"
    sess_s = await _session_service.create_session(app_name=APP_NAME, user_id=uid_s)
    result = await _call_agent(
        runner=_get_runner("searcher"),
        agent_name="searcher",
        user_id=uid_s, session_id=sess_s.id,
        message=f"Search for information about: {topic}",
        root_span=root_span, root_ctx=root_ctx, tracer=tracer, run_id=run_id,
        open_child_span=True, gov=gov, source=source, conversation_id=conversation_id,
    )

    # ── Step 2: summarize (receives step 1 output as explicit text) ───────────
    if word_count is not None:
        uid_m  = f"_pipe_{uuid.uuid4().hex}"
        sess_m = await _session_service.create_session(app_name=APP_NAME, user_id=uid_m)
        result = await _call_agent(
            runner=_get_runner("summarizer"),
            agent_name="summarizer",
            user_id=uid_m, session_id=sess_m.id,
            message=f"Summarize the following text in EXACTLY {word_count} words:\n\n{result}",
            root_span=root_span, root_ctx=root_ctx, tracer=tracer, run_id=run_id,
            open_child_span=True, gov=gov, source=source, conversation_id=conversation_id,
        )

    # ── Step 3: translate (receives step 2 output as explicit text) ───────────
    if language is not None:
        uid_t  = f"_pipe_{uuid.uuid4().hex}"
        sess_t = await _session_service.create_session(app_name=APP_NAME, user_id=uid_t)
        result = await _call_agent(
            runner=_get_runner("translator"),
            agent_name="translator",
            user_id=uid_t, session_id=sess_t.id,
            message=f"Translate the following text to {language}:\n\n{result}",
            root_span=root_span, root_ctx=root_ctx, tracer=tracer, run_id=run_id,
            open_child_span=True, gov=gov, source=source, conversation_id=conversation_id,
        )

    result = result or "(No response)"

    # Store this exchange in the user's main session so follow-up queries have context.
    await _inject_history(user_id, user_input, result)

    return result


# ── Core streaming helper ─────────────────────────────────────────────────────

async def _call_agent(
    runner,
    agent_name: str,
    user_id: str,
    session_id: str,
    message: str,
    root_span,
    root_ctx,
    tracer,
    run_id: str,
    open_child_span: bool,
    gov=None,
    source: str = "production",
    conversation_id: str | None = None,
) -> str:
    """
    Send one message to a runner and return its final response text.
    If open_child_span=True, wraps the call in an agent.task child span.
    Re-attaches root OTel context before each call so all spans land in the
    same trace regardless of ADK's internal context mutations.

    Governance gate check (ACP M2) is performed before the LLM runs.
    """
    # ACP M2: governance gate check before agent invocation
    if gov is not None:
        try:
            result = gov.check_policy("agent_invoke_rate", 1.0)
            if result.get("decision") == "block":
                return (
                    f"[GOVERNANCE BLOCK] {agent_name} blocked: "
                    f"{result.get('message', 'Policy enforcement blocked this agent.')}"
                )
        except Exception:
            pass  # fail open — governance service unreachable

    # Re-attach root context — ensures child spans stay in the same trace
    token      = otel_context.attach(root_ctx)
    parent_ctx = trace.set_span_in_context(root_span)

    child_span  = None
    child_token = None
    if open_child_span:
        child_span = tracer.start_span("agent.task", context=parent_ctx)
        child_span.set_attribute("agent.id",     agent_name)
        child_span.set_attribute("agent.role",   agent_name)
        child_span.set_attribute("task.input",   message[:2000])
        child_span.set_attribute("run.id",       run_id)
        child_span.set_attribute("trace.source", source)
        if conversation_id:
            child_span.set_attribute("conversation.id", conversation_id)
        # Make child_span the current span so LLM calls are nested inside this agent span.
        child_token = otel_context.attach(trace.set_span_in_context(child_span))
        parent_ctx  = trace.set_span_in_context(child_span)

    content = genai_types.Content(role="user", parts=[genai_types.Part(text=message)])
    response_parts: list[str] = []
    output_text = ""

    try:
        async for event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=content,
        ):
            if agent_name == "searcher":
                _maybe_emit_tool_span(event, tracer, run_id, parent_ctx)

            if event.is_final_response():
                text = _extract_text(event)
                if text:
                    response_parts.append(text)

        output_text = "\n".join(response_parts) if response_parts else ""

    except Exception as e:
        if child_span:
            child_span.set_attribute("task.status", "failure")
            child_span.record_exception(e)
        raise
    finally:
        if child_token is not None:
            otel_context.detach(child_token)
        otel_context.detach(token)

    if child_span:
        child_span.set_attribute("task.output", output_text[:2000])
        child_span.set_attribute("task.status", "success")
        child_span.end()

    return output_text


def _maybe_emit_tool_span(event, tracer, run_id: str, parent_ctx) -> None:
    """Emit an agent.tool_call span when searcher calls web_search."""
    try:
        parts = getattr(getattr(event, "content", None), "parts", None) or []
        for part in parts:
            fc = getattr(part, "function_call", None)
            if fc and getattr(fc, "name", "") == "web_search":
                args = getattr(fc, "args", {}) or {}
                span = tracer.start_span("agent.tool_call", context=parent_ctx)
                span.set_attribute("agent.id",    "searcher")
                span.set_attribute("tool.name",   "web_search")
                span.set_attribute("tool.input",  (args.get("query", "") or "")[:500])
                span.set_attribute("run.id",      run_id)
                span.set_attribute("task.status", "success")
                span.end()
                return
    except Exception as e:
        print(f"[runner] tool_span error: {e}", file=sys.stderr, flush=True)


def reset_session(user_id: str = "default"):
    """Clear a user's session so the next call creates a fresh one."""
    _sessions.pop(user_id, None)
