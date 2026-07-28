"""Tests for ResponseNormalizer."""

import json
import pytest
from response_normalizer import (
    normalize_response,
    normalize_sse_chunk,
    normalize_streaming_body,
    normalize_error,
)


class TestNormalizeSSEChunk:
    def test_standard_delta_passthrough(self):
        chunk = {
            "id": "1",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"content": "hello"}, "finish_reason": None}],
        }
        result = normalize_sse_chunk(chunk)
        assert result is chunk  # already standard
        assert result["choices"][0]["delta"]["content"] == "hello"

    def test_message_to_delta(self):
        chunk = {
            "id": "2",
            "choices": [{"index": 0, "message": {"content": "world"}, "finish_reason": None}],
        }
        result = normalize_sse_chunk(chunk)
        assert "delta" in result["choices"][0]
        assert result["choices"][0]["delta"]["content"] == "world"

    def test_text_to_delta(self):
        chunk = {"id": "3", "choices": [{"index": 0, "text": "foo", "finish_reason": None}]}
        result = normalize_sse_chunk(chunk)
        assert result["choices"][0]["delta"]["content"] == "foo"

    def test_hf_tgi_style(self):
        chunk = {
            "id": "4",
            "token": {"text": "bar", "special": False},
            "generated_text": None,
        }
        result = normalize_sse_chunk(chunk)
        assert result["choices"][0]["delta"]["content"] == "bar"
        assert result["choices"][0]["finish_reason"] is None

    def test_hf_tgi_finished(self):
        chunk = {
            "id": "5",
            "token": {"text": "baz", "special": True},
            "generated_text": "full output",
        }
        result = normalize_sse_chunk(chunk)
        assert result["choices"][0]["finish_reason"] == "stop"

    def test_fallback_raw_text(self):
        chunk = {"text": "fallback text"}
        result = normalize_sse_chunk(chunk)
        assert result["choices"][0]["delta"]["content"] == "fallback text"


class TestNormalizeResponse:
    def test_standard_passthrough(self):
        resp = {
            "id": "r1",
            "object": "chat.completion",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        result = normalize_response(resp)
        assert result["choices"][0]["message"]["content"] == "hello"

    def test_no_usage(self):
        resp = {
            "id": "r2",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
        }
        result = normalize_response(resp)
        assert result["usage"] == {}
        assert result["choices"][0]["message"]["content"] == "hi"

    def test_generated_text_fallback(self):
        resp = {"id": "r3", "generated_text": "completion text"}
        result = normalize_response(resp)
        assert result["choices"][0]["message"]["content"] == "completion text"

    def test_response_field_fallback(self):
        resp = {"id": "r4", "response": "direct response"}
        result = normalize_response(resp)
        assert result["choices"][0]["message"]["content"] == "direct response"

    def test_text_in_choice(self):
        resp = {"id": "r5", "choices": [{"index": 0, "text": "text content"}]}
        result = normalize_response(resp)
        assert result["choices"][0]["message"]["content"] == "text content"

    def test_delta_in_choice(self):
        resp = {"id": "r6", "choices": [{"index": 0, "delta": {"content": "delta content"}}]}
        result = normalize_response(resp)
        assert result["choices"][0]["message"]["content"] == "delta content"

    def test_from_json_string(self):
        resp_str = json.dumps({"choices": [{"message": {"content": "from string"}}]})
        result = normalize_response(resp_str)
        assert result["choices"][0]["message"]["content"] == "from string"

    def test_from_bytes(self):
        resp_bytes = json.dumps({"choices": [{"message": {"content": "from bytes"}}]}).encode()
        result = normalize_response(resp_bytes)
        assert result["choices"][0]["message"]["content"] == "from bytes"

    def test_non_dict_returns_error(self):
        result = normalize_response([1, 2, 3])
        assert "error" in result

    def test_usage_synthesized_from_x_usage(self):
        resp = {
            "choices": [{"message": {"content": "ok"}}],
            "x_usage": {"tokens": {"input": 7, "output": 3}},
        }
        result = normalize_response(resp)
        assert result["usage"]["prompt_tokens"] == 7
        assert result["usage"]["completion_tokens"] == 3


class TestNormalizeStreamingBody:
    def test_streaming_body(self):
        sse = (
            'data: {"id":"1","choices":[{"index":0,"text":"hello"}]}\n'
            'data: {"id":"2","choices":[{"index":0,"text":" world"}]}\n'
            "data: [DONE]\n"
        )
        result = normalize_streaming_body(sse)
        lines = result.strip().split("\n")
        assert len(lines) == 3
        chunk1 = json.loads(lines[0][6:])
        assert chunk1["choices"][0]["delta"]["content"] == "hello"

    def test_passthrough_non_data_lines(self):
        sse = ": keep this comment\n\ndata: [DONE]\n"
        result = normalize_streaming_body(sse)
        assert ": keep this comment" in result


class TestNormalizeError:
    def test_openai_error(self):
        err = {"error": {"message": "rate limit", "type": "rate_limit_error", "code": 429}}
        result = normalize_error(err)
        assert result["error"]["message"] == "rate limit"

    def test_cf_string_error(self):
        err = {"error": "Worker exceeded limits"}
        result = normalize_error(err)
        assert result["error"]["message"] == "Worker exceeded limits"

    def test_nvidia_detail_error(self):
        err = {"detail": "Model not found"}
        result = normalize_error(err)
        assert result["error"]["message"] == "Model not found"

    def test_ollama_error(self):
        err = {"error": "llama: model not loaded"}
        result = normalize_error(err)
        assert result["error"]["message"] == "llama: model not loaded"

    def test_raw_string(self):
        result = normalize_error("Internal Server Error")
        assert result["error"]["message"].startswith("Internal")

    def test_bytes_input(self):
        result = normalize_error(b'{"error": {"message": "bytes error"}}')
        assert result["error"]["message"] == "bytes error"
