"""GatewayClient — drop-in OpenAI client wrapper that routes via the gateway.

Usage in any agent system:

    from gateway_client import make_openai_client
    client = make_openai_client(system_id="opt-demo", agent_role="searcher")
    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "Search for climate change news"}],
    )

The client sends the gateway identification headers automatically.
The model you request is honoured unless a routing policy overrides it.
All enforcement, prompt modification, shadow mode, and logging happen
transparently in the gateway — the calling code is unchanged.
"""

import os
import uuid

from openai import AsyncOpenAI, OpenAI

_GATEWAY_URL = os.getenv("GATEWAY_URL", "http://localhost:8080/v1")
_GATEWAY_KEY = os.getenv("GATEWAY_API_KEY", "gateway-local")  # gateway accepts any key


def make_openai_client(
    system_id:  str = "unknown",
    agent_role: str = "unknown",
    run_id:     str | None = None,
    trace_id:   str = "",
    async_client: bool = True,
) -> AsyncOpenAI | OpenAI:
    """Return an OpenAI client configured to route through the gateway.

    Args:
        system_id:    identifies your agent system in the gateway call log
        agent_role:   agent role (orchestrator/searcher/summarizer/translator/…)
        run_id:       run correlation ID; auto-generated if not provided
        trace_id:     optional OTel trace ID to link gateway span into an existing trace
        async_client: True → AsyncOpenAI, False → OpenAI
    """
    rid = run_id or str(uuid.uuid4())
    default_headers = {
        "X-Gateway-System-Id":  system_id,
        "X-Gateway-Agent-Role": agent_role,
        "X-Gateway-Run-Id":     rid,
        "X-Gateway-Trace-Id":   trace_id,
    }
    cls = AsyncOpenAI if async_client else OpenAI
    return cls(
        base_url=_GATEWAY_URL,
        api_key=_GATEWAY_KEY,
        default_headers=default_headers,
    )


# ── Quick smoke-test ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import asyncio

    async def _test():
        client = make_openai_client(
            system_id="gateway-test",
            agent_role="searcher",
        )
        print(f"Sending test request via gateway at {_GATEWAY_URL} …")
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "Say 'gateway OK' and nothing else."}],
            max_tokens=10,
        )
        print("Response:", resp.choices[0].message.content)
        gw = getattr(resp, "gateway", None)
        if gw:
            print("Gateway metadata:", gw)

    asyncio.run(_test())
