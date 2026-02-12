import json
from pathlib import Path

import pytest

from rotator_library.providers.openai_codex_provider import (
    CodexSSETranslator,
    CodexStreamError,
)


FIXTURES_DIR = Path(__file__).parent / "fixtures" / "openai_codex"


def _load_events(name: str):
    return json.loads((FIXTURES_DIR / name).read_text())


def test_fixture_driven_event_sequence_to_expected_chunks():
    events = _load_events("stream_success_events.json")
    translator = CodexSSETranslator(model_id="openai_codex/gpt-5.1-codex")

    chunks = []
    for event in events:
        chunks.extend(translator.process_event(event))

    # content delta chunk present
    content_chunks = [
        c for c in chunks if c["choices"][0]["delta"].get("content")
    ]
    assert content_chunks
    assert content_chunks[-1]["choices"][0]["delta"]["content"] == "pong"

    # terminal chunk contains usage mapping
    final_chunk = chunks[-1]
    assert final_chunk["choices"][0]["finish_reason"] == "stop"
    assert final_chunk["usage"]["prompt_tokens"] == 21
    assert final_chunk["usage"]["completion_tokens"] == 13
    assert final_chunk["usage"]["total_tokens"] == 34


def test_tool_call_deltas_and_finish_reason_mapping():
    events = _load_events("stream_tool_call_events.json")
    translator = CodexSSETranslator(model_id="openai_codex/gpt-5.1-codex")

    chunks = []
    for event in events:
        chunks.extend(translator.process_event(event))

    tool_chunks = [
        c for c in chunks if c["choices"][0]["delta"].get("tool_calls")
    ]
    assert tool_chunks

    # Validate streaming argument assembly appears in deltas
    all_args = "".join(
        tc["function"]["arguments"]
        for chunk in tool_chunks
        for tc in chunk["choices"][0]["delta"]["tool_calls"]
    )
    assert "San" in all_args
    assert "Francisco" in all_args

    final_chunk = chunks[-1]
    assert final_chunk["choices"][0]["finish_reason"] == "tool_calls"
    assert final_chunk["usage"]["total_tokens"] == 60


def test_content_part_delta_alias_and_length_finish_reason():
    events = _load_events("stream_content_part_delta_events.json")
    translator = CodexSSETranslator(model_id="openai_codex/gpt-5.1-codex")

    chunks = []
    for event in events:
        chunks.extend(translator.process_event(event))

    text = "".join(
        c["choices"][0]["delta"].get("content", "")
        for c in chunks
    )
    assert text == "Hello world"

    final_chunk = chunks[-1]
    assert final_chunk["choices"][0]["finish_reason"] == "length"
    assert final_chunk["usage"]["total_tokens"] == 30


def test_error_event_propagation():
    translator = CodexSSETranslator(model_id="openai_codex/gpt-5.1-codex")

    with pytest.raises(CodexStreamError) as exc:
        translator.process_event(
            {
                "type": "error",
                "error": {
                    "code": "usage_limit_reached",
                    "message": "quota reached",
                    "type": "rate_limit_error",
                },
            }
        )

    assert exc.value.status_code == 429
    assert "quota" in str(exc.value).lower()


def test_unknown_event_tolerance():
    translator = CodexSSETranslator(model_id="openai_codex/gpt-5.1-codex")
    chunks = translator.process_event({"type": "response.some_unknown_event"})
    assert chunks == []
