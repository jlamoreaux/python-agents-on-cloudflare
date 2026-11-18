import os
import platform
import time

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from agent.core import BASE_TOOLS, run_agent
from tools import RUN_PYTHON

# The container has no Internet access. This hostname is intercepted by the
# gateway Worker (see src/index.ts), which forwards the request to Workers AI.
# No API key is ever present inside the container.
AI_URL = os.environ.get("AI_URL", "http://ai.internal/v1/chat/completions")
TOOLS = [*BASE_TOOLS, RUN_PYTHON]
STARTED_AT = time.time()

app = FastAPI()
client = httpx.AsyncClient(timeout=60)


class RunRequest(BaseModel):
    message: str = Field(min_length=1, max_length=1000)


async def chat(messages: list[dict], tools: list[dict]) -> dict:
    response = await client.post(AI_URL, json={"messages": messages, "tools": tools})
    response.raise_for_status()
    return response.json()


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.post("/run")
async def run(request: RunRequest) -> dict:
    try:
        result = await run_agent(request.message, chat, TOOLS)
    except httpx.HTTPStatusError as error:
        detail = error.response.text[:300]
        raise HTTPException(status_code=502, detail=f"model call failed: {detail}") from error
    except (httpx.HTTPError, KeyError, IndexError, TypeError) as error:
        raise HTTPException(status_code=502, detail=f"model call failed: {error!r}") from error
    return {
        **result,
        "runtime": "container",
        "python": platform.python_version(),
        "tools": [t.name for t in TOOLS],
        "uptime_s": round(time.time() - STARTED_AT, 1),
    }
