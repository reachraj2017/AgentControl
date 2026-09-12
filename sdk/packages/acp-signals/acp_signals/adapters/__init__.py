"""
acp_signals.adapters
~~~~~~~~~~~~~~~~~~~~~
One small, framework-specific translation module per supported agent
framework. Each adapter hooks the framework's OWN documented extension point
(a callback/hook/plugin interface the framework ships for exactly this kind
of purpose) and calls ``acp_signals.handoff()`` / ``acp_signals.tool_span()``.

None of these reach into undocumented or private framework internals — that
is the failure pattern this design deliberately avoids (see
docs/external-agent-integration-findings.md, Issues 1/2/5).

Submodules are NOT imported here automatically — each has its own optional
dependency (the target framework), so importing ``acp_signals.adapters``
itself never fails regardless of which frameworks are installed. Import the
specific adapter you need directly, e.g.::

    from acp_signals.adapters.langchain import ACPCallbackHandler

Confidence level per adapter (see each module's docstring for detail):

    openai_agents.py  — best-effort, verify against your installed `openai-agents` version
    google_adk.py     — best-effort, verify against your installed `google-adk` version
    langchain.py      — high confidence (BaseCallbackHandler is a long-stable public interface)
    crewai.py         — best-effort, verify against your installed `crewai` version
"""
