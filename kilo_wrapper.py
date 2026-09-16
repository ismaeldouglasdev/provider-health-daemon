#!/usr/bin/env python3
"""OpenAI-compatible wrapper server for Kilo CLI.

Exposes /v1/models and /v1/chat/completions endpoints that forward to
`kilo run --attach` for execution.
"""

import asyncio
import json
import logging
import os
import subprocess
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

KILO_SERVER_URL = os.environ.get("KILO_SERVER_URL", "http://127.0.0.1:4096")
KILO_WRAPPER_PORT = int(os.environ.get("KILO_WRAPPER_PORT", "4097"))
KILO_WRAPPER_HOST = os.environ.get("KILO_WRAPPER_HOST", "127.0.0.1")

# Default models that Kilo can use (from configured providers)
DEFAULT_MODELS = [
    "kilo/auto",
    "kilo/claude-haiku-4.5",
    "kilo/claude-sonnet-4.5",
    "kilo/gpt-4o",
    "kilo/gpt-4o-mini",
    "kilo/gemini-3.7-flash",
    "kilo/qwen3.8-max",
]

kilo_server_proc: Optional[subprocess.Popen] = None
kilo_server_ready = False


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    stream: bool = False


class ModelInfo(BaseModel):
    id: str
    object: str = "model"
    created: int = 1700000000
    owned_by: str = "kilo"


class ModelsResponse(BaseModel):
    object: str = "list"
    data: list[ModelInfo]


async def start_kilo_server():
    """Start kilo serve in background."""
    global kilo_server_proc, kilo_server_ready
    if kilo_server_proc and kilo_server_proc.poll() is None:
        return

    log.info("Starting Kilo server on %s", KILO_SERVER_URL)
    kilo_server_proc = subprocess.Popen(
        ["kilo", "serve", "--port", "4096", "--hostname", "127.0.0.1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait for server to be ready in background
    asyncio.create_task(wait_for_kilo_ready())


async def wait_for_kilo_ready():
    """Wait for kilo server to be ready."""
    global kilo_server_ready
    for _ in range(100):
        await asyncio.sleep(0.2)
        try:
            proc = await asyncio.create_subprocess_exec(
                "kilo", "run", "--attach", KILO_SERVER_URL, "--format", "json", "ping",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=3)
            if proc.returncode == 0:
                kilo_server_ready = True
                log.info("Kilo server ready")
                return
        except Exception:
            pass
    log.warning("Kilo server did not become ready in time")


async def stop_kilo_server():
    """Stop kilo serve."""
    global kilo_server_proc
    if kilo_server_proc:
        kilo_server_proc.terminate()
        try:
            await asyncio.wait_for(asyncio.to_thread(kilo_server_proc.wait), timeout=5)
        except asyncio.TimeoutError:
            kilo_server_proc.kill()
            await asyncio.to_thread(kilo_server_proc.wait)
        kilo_server_proc = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    await start_kilo_server()
    yield
    await stop_kilo_server()


app = FastAPI(title="Kilo OpenAI Wrapper", lifespan=lifespan)


@app.get("/v1/models", response_model=ModelsResponse)
async def list_models():
    """List available models."""
    data = [ModelInfo(id=model_id) for model_id in DEFAULT_MODELS]
    return ModelsResponse(data=data)


@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    """Handle chat completions by forwarding to Kilo."""
    if not kilo_server_ready:
        raise HTTPException(status_code=503, detail="Kilo server not ready")

    model = request.model
    if model.startswith("kilo/"):
        model = model[5:]  # Strip "kilo/" prefix

    # Convert messages to a single prompt
    prompt_parts = []
    for msg in request.messages:
        if msg.role == "system":
            prompt_parts.append(f"System: {msg.content}")
        elif msg.role == "user":
            prompt_parts.append(msg.content)
        elif msg.role == "assistant":
            prompt_parts.append(f"Assistant: {msg.content}")
    prompt = "\n\n".join(prompt_parts)

    # Run kilo with the prompt
    cmd = [
        "kilo", "run",
        "--attach", KILO_SERVER_URL,
        "--format", "json",
        "--model", model,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await proc.communicate(input=prompt.encode())

    if proc.returncode != 0:
        error = stderr.decode().strip() or "Kilo execution failed"
        log.error("Kilo error: %s", error)
        raise HTTPException(status_code=500, detail=error)

    # Parse JSON stream output to get final response
    response_text = ""
    for line in stdout.decode().strip().split("\n"):
        if not line:
            continue
        try:
            event = json.loads(line)
            if event.get("type") == "text":
                response_text = event.get("part", {}).get("text", "")
        except json.JSONDecodeError:
            pass

    if not response_text:
        response_text = "(no response)"

    # Return OpenAI-compatible response
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(asyncio.get_event_loop().time()),
        "model": request.model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": response_text},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": len(prompt) // 4,
            "completion_tokens": len(response_text) // 4,
            "total_tokens": (len(prompt) + len(response_text)) // 4,
        },
    }


@app.get("/health")
async def health():
    return {"status": "ok", "kilo_ready": kilo_server_ready}


def main():
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host=KILO_WRAPPER_HOST, port=KILO_WRAPPER_PORT, log_level="info")


if __name__ == "__main__":
    main()