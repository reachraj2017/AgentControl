"""
Benchmark REST server for opt-demo.

Exposes POST /chat so the ACP Eval Testing page can drive benchmark runs
through the full agent pipeline (orchestrator → searcher → summarizer →
translator) exactly as the Streamlit chat UI does.  Every call routes
through the gateway (OPENAI_BASE_URL) and emits OTel spans automatically.

Start with:
    python server.py          # port 8090 (default)
    SERVER_PORT=9000 python server.py
"""

import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import acp_setup  # noqa: F401 — must be after load_dotenv; initialises OTel + gateway env
from concurrent.futures import ThreadPoolExecutor

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

from runner import run_agent, reset_session

app = FastAPI(title="opt-demo benchmark server")
_executor = ThreadPoolExecutor(max_workers=4)


class ChatRequest(BaseModel):
    message:  str
    user_id:  str = "benchmark"
    run_id:   str = ""
    source:   str = "benchmark"


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/chat")
async def chat(req: ChatRequest):
    import asyncio
    loop = asyncio.get_event_loop()
    response, trace_id = await loop.run_in_executor(
        _executor,
        lambda: run_agent(req.message, req.user_id, req.source, run_id=req.run_id or None),
    )
    return {"response": response, "trace_id": trace_id}


@app.post("/reset")
def reset(user_id: str = "benchmark"):
    reset_session(user_id)
    return {"status": "ok"}


if __name__ == "__main__":
    port = int(os.getenv("SERVER_PORT", "8090"))
    print(f"[server] opt-demo benchmark server starting on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
