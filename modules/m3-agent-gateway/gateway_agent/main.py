"""M3 Gateway Agent — FastAPI service.

Endpoints:
  GET  /health
  POST /chat
"""

import structlog
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

log = structlog.get_logger()

app = FastAPI(title="M3 Gateway Agent")


class ChatRequest(BaseModel):
    message: str
    history: list[dict] = []


class ChatResponse(BaseModel):
    response: str
    tool_calls: list[dict] = []


@app.get("/health")
def health():
    return {"status": "ok", "service": "gateway-agent"}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    from agent import chat as agent_chat
    try:
        response_text, tool_calls = agent_chat(req.message, req.history)
        return ChatResponse(response=response_text, tool_calls=tool_calls)
    except Exception as exc:
        log.error("chat_error", error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))
