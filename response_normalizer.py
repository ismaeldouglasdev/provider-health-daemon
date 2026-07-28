"""Response normalizer — normalize streaming & non-streaming responses.

Different downstream routers (CF Workers, NVIDIA NIM, Ollama, Groq) return
responses in slightly different shapes:
  - SSE chunk field names differ ("delta" vs "message")
  - Usage stats embedded inline vs at end
  - Error shapes differ (some return {error}, others {detail})
  - Content field may be "text" or "message.content"

This module normalizes all responses into a consistent OpenAI-compatible
shape before the proxy returns them to the client.

Normalized SSE chunk:
  {"id": "..", "object": "chat.completion.chunk",
   "created": <ts>, "model": "<model>",
   "choices": [{"index": 0, "delta": {"role": "assistant", "content": "..."},
                 "finish_reason": null}]}

Normalized JSON response:
  {"id": "..", "object": "chat.completion",
   "created": <ts>, "model": "<model>",
   "choices": [{"index": 0, "message": {"role": "assistant", "content": "..."},
                 "finish_reason": "stop"}],
   "usage": {"prompt_tokens": N, "completion_tokens": N, "total_tokens": N}}
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

log = logging.getLogger(__name__)


def _ts() -> int:
    """Current unix timestamp (seconds)."""
    import time
    return int(time.time())


# ── SSE chunk normalization ──────────────────────────────────────────

def _is_streaming_chunk(line: str) -> bool:
    return line.startswith("data: ") and "[DONE]" not in line


def _normalize_chunk_text(text: str) -> str:
    """Wrap plain text content into OpenAI-style delta if needed."""
    return text.get("content", "") if isinstance(text, dict) else str(text)


def normalize_sse_chunk(chunk: dict) -> dict:
    """Normalize a single SSE JSON chunk to OpenAI completion chunk shape.

    Handles variations:
      - {choices: [{delta: {content: "..."}}]}  (OpenAI standard)
      - {choices: [{text: "..."}]}               (some older APIs)
      - {token: {text: "..."}, ...}              (HuggingFace TGI style)
    """
    # Already standard OpenAI shape
    if "choices" in chunk and isinstance(chunk["choices"], list):
        for c in chunk["choices"]:
            if not isinstance(c, dict):
                continue
            # Some APIs return {delta: {content:}} — already correct
            if "delta" in c and isinstance(c["delta"], dict):
                pass  # Fine as-is
            # Some return {message: {content:}} instead of {delta:}
            elif "message" in c and isinstance(c["message"], dict):
                c["delta"] = c.pop("message")
            # Some return {text: "..."} instead of {delta: {content: "..."}}
            elif "text" in c:
                c["delta"] = {"content": c.pop("text")}
        return chunk

    # HuggingFace TGI style: {"token": {"text": "...", "special": false}, ...}
    if "token" in chunk:
        text = chunk.get("token", {}).get("text", "")
        return {
            "id": chunk.get("id", ""),
            "object": "chat.completion.chunk",
            "created": chunk.get("created", _ts()),
            "model": chunk.get("model", ""),
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": text},
                    "finish_reason": "stop" if chunk.get("generated_text") else None,
                }
            ],
        }

    # Fallback: wrap raw text
    return {
        "id": chunk.get("id", ""),
        "object": "chat.completion.chunk",
        "created": chunk.get("created", _ts()),
        "model": chunk.get("model", ""),
        "choices": [
            {
                "index": 0,
                "delta": {"content": _normalize_chunk_text(chunk.get("text", ""))},
                "finish_reason": chunk.get("finish_reason"),
            }
        ],
    }


# ── Response body normalization ──────────────────────────────────────

def _extract_content_from_choices(choices: list) -> str:
    """Extract message content from any choice format."""
    for c in choices:
        if not isinstance(c, dict):
            continue
        msg = c.get("message") or c.get("delta") or {}
        if isinstance(msg, dict):
            content = msg.get("content", "")
            if content:
                return content
        if "text" in c:
            return c["text"]
    return ""


def _extract_usage(data: dict) -> dict:
    """Extract or synthesize usage info."""
    usage = data.get("usage", {})
    if usage:
        return {
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)),
        }
    # Synthesize from x_usage or similar
    for key in ("x_usage", "extra", "details"):
        extra = data.get(key, {})
        if isinstance(extra, dict) and "tokens" in extra:
            tokens = extra["tokens"]
            return {
                "prompt_tokens": tokens.get("input", 0),
                "completion_tokens": tokens.get("output", 0),
                "total_tokens": tokens.get("input", 0) + tokens.get("output", 0),
            }
    return {}


def normalize_response(body: str | bytes | dict) -> dict:
    """Normalize a complete (non-streaming) response to standard OpenAI shape.

    Args:
        body: Raw response body as string, bytes, or already-parsed dict.

    Returns:
        Normalized dict with OpenAI-compatible shape.
    """
    if isinstance(body, (str, bytes)):
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="replace")
        data = json.loads(body)
    else:
        data = body

    if not isinstance(data, dict):
        return {"error": {"message": f"unexpected response type: {type(data).__name__}", "type": "parse_error"}}

    # Already standard shape
    if "choices" in data and isinstance(data["choices"], list) and "usage" in data:
        # Fix any non-standard fields in choices
        for c in data["choices"]:
            _ensure_standard_choice(c)
        return data

    # May have choices but no usage, or choices with unusual shape
    if "choices" in data and isinstance(data["choices"], list):
        for c in data["choices"]:
            _ensure_standard_choice(c)
        data["usage"] = data.get("usage", _extract_usage(data))
        return data

    # No choices yet — try to build one
    content = _extract_content_from_choices(data.get("choices", []))
    if not content:
        content = data.get("response", "") or data.get("generated_text", "") or data.get("output", "")
        if isinstance(content, list):
            content = " ".join(str(item) for item in content)

    result = {
        "id": data.get("id", ""),
        "object": "chat.completion",
        "created": data.get("created", _ts()),
        "model": data.get("model", ""),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": str(content) if content else ""},
                "finish_reason": data.get("finish_reason", "stop"),
            }
        ],
        "usage": _extract_usage(data),
    }

    return result


def _ensure_standard_choice(choice: dict) -> None:
    """Mutate a choice dict to ensure standard {'message': {'role', 'content'}} shape."""
    # Message already present
    if "message" in choice and isinstance(choice["message"], dict):
        return
    # Delta → message
    if "delta" in choice:
        choice["message"] = choice.pop("delta")
    # Text → message
    if "text" in choice:
        choice["message"] = {"role": "assistant", "content": choice.pop("text")}


# ── Streaming passthrough / line-by-line normalization ───────────────

def normalize_streaming_body(body: str | bytes) -> str:
    """Normalize an entire SSE body (multi-line) to standard OpenAI chunks.

    Parses, normalizes each chunk, and re-serializes to SSE format.
    """
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")

    output_lines: list[str] = []
    for line in body.split("\n"):
        if not _is_streaming_chunk(line):
            output_lines.append(line)
            continue
        try:
            chunk = json.loads(line[6:])
            normalized = normalize_sse_chunk(chunk)
            output_lines.append("data: " + json.dumps(normalized, ensure_ascii=False))
        except json.JSONDecodeError:
            output_lines.append(line)

    return "\n".join(output_lines)


# ── Error normalization ──────────────────────────────────────────────

def normalize_error(body: str | bytes | dict) -> dict:
    """Normalize error responses to a consistent shape.

    Different routers return errors differently:
      CF Workers:   {"error": "message string"}
      NVIDIA:       {"detail": "message string"}
      Ollama:       {"error": "message string"}
      OpenAI std:   {"error": {"message": "...", "type": "..."}}

    Normalized shape:
      {"error": {"message": "...", "type": "...", "code": <status>}}
    """
    if isinstance(body, (str, bytes)):
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="replace")
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return {"error": {"message": body[:500], "type": "parse_error", "code": None}}
    elif isinstance(body, dict):
        data = body
    else:
        return {"error": {"message": str(body), "type": "unknown", "code": None}}

    err = data.get("error") or data.get("detail") or {}
    if isinstance(err, str):
        return {"error": {"message": err, "type": "api_error", "code": data.get("code")}}

    if isinstance(err, dict):
        return {
            "error": {
                "message": err.get("message", err.get("msg", "")),
                "type": err.get("type", "api_error"),
                "code": err.get("code", data.get("code")),
            }
        }

    return {"error": {"message": str(data)[:500], "type": "unknown", "code": None}}
