import json
import logging
from datetime import datetime, timezone

import pytest

from app.team_insight import (
    InvalidEventError,
    InvalidLLMResponseError,
    LLMUnavailableError,
    TransientLLMError,
    build_prompt,
    calculate_metrics,
    generate_team_insight,
    parse_events,
    parse_insight,
)
from fakes import EXAMPLE_EVENTS, VALID_REPLY, ScriptedLLMClient


# --- Input validation -------------------------------------------------------


@pytest.mark.parametrize(
    "event",
    [
        "not a dict",
        {"type": "commit", "timestamp": "2026-01-01T09:00:00Z"},
        {"timestamp": "2026-01-01T09:00:00Z"},
        {"type": "deployment"},
        {"type": "deployment", "timestamp": "2026-01-01T09:00:00"},
        {"type": "deployment", "timestamp": "2026-01-01T09:00:00+00:00"},
        {"type": "deployment", "timestamp": "yesterdayZ"},
        {"type": "deployment", "timestamp": "2026-01-01Z"},
        {"type": "deployment", "timestamp": "20260101T090000Z"},
        {"type": "deployment", "timestamp": "2026-01-01 09:00:00Z"},
        {"type": "deployment", "timestamp": "2026-01-01T09:00Z"},
        {"type": "deployment", "timestamp": "2026-01-01T09:00:00z"},
        {"type": "deployment", "timestamp": "2026-13-01T09:00:00Z"},
        {"type": "deployment", "timestamp": 1735722000},
        {"type": "pr_opened", "timestamp": "2026-01-01T09:00:00Z"},
        {"type": "pr_merged", "id": "", "timestamp": "2026-01-01T09:00:00Z"},
    ],
)
def test_malformed_event_raises(event: object) -> None:
    with pytest.raises(InvalidEventError):
        parse_events([event])


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-01-01T09:00:00Z", datetime(2026, 1, 1, 9, tzinfo=timezone.utc)),
        ("2026-01-01T09:00:00.5Z", datetime(2026, 1, 1, 9, 0, 0, 500000, tzinfo=timezone.utc)),
    ],
)
def test_timestamp_parses_to_utc(value: str, expected: datetime) -> None:
    [event] = parse_events([{"type": "deployment", "timestamp": value}])
    assert event.timestamp == expected
    assert event.timestamp.tzinfo is timezone.utc


def test_events_must_be_a_list() -> None:
    with pytest.raises(InvalidEventError):
        parse_events({"type": "deployment"})


@pytest.mark.parametrize("team_id", ["", "   ", None])
def test_invalid_team_id_raises_before_llm_call(team_id: object) -> None:
    client = ScriptedLLMClient(VALID_REPLY)
    with pytest.raises(InvalidEventError):
        generate_team_insight(team_id, EXAMPLE_EVENTS, client)  # type: ignore[arg-type]
    assert client.calls == 0


# --- Metrics ----------------------------------------------------------------


def test_metrics_for_example_events() -> None:
    metrics = calculate_metrics(parse_events(EXAMPLE_EVENTS))
    assert metrics.to_dict() == {
        "merged_prs": 1,
        "average_pr_cycle_time_hours": 24.0,
        "deployments": 1,
        "incidents": 1,
    }


def test_metrics_for_no_events() -> None:
    metrics = calculate_metrics([])
    assert metrics.to_dict() == {
        "merged_prs": 0,
        "average_pr_cycle_time_hours": None,
        "deployments": 0,
        "incidents": 0,
    }


def test_unmatched_pr_events_are_ignored_for_cycle_time() -> None:
    events = parse_events(
        [
            {"type": "pr_opened", "id": "a", "timestamp": "2026-01-01T00:00:00Z"},
            {"type": "pr_merged", "id": "a", "timestamp": "2026-01-01T10:00:00Z"},
            {"type": "pr_opened", "id": "open-only", "timestamp": "2026-01-01T00:00:00Z"},
            {"type": "pr_merged", "id": "merged-only", "timestamp": "2026-01-09T00:00:00Z"},
        ]
    )
    metrics = calculate_metrics(events)
    assert metrics.merged_prs == 2
    assert metrics.average_pr_cycle_time_hours == 10.0


def test_average_cycle_time_is_order_independent_and_rounded() -> None:
    raw = [
        {"type": "pr_merged", "id": "b", "timestamp": "2026-01-01T02:00:00Z"},
        {"type": "pr_opened", "id": "a", "timestamp": "2026-01-01T00:00:00Z"},
        {"type": "pr_opened", "id": "b", "timestamp": "2026-01-01T00:00:00Z"},
        {"type": "pr_merged", "id": "a", "timestamp": "2026-01-01T01:20:00Z"},
    ]
    assert calculate_metrics(parse_events(raw)).average_pr_cycle_time_hours == 1.67
    assert calculate_metrics(parse_events(raw[::-1])).average_pr_cycle_time_hours == 1.67


def test_merged_before_opened_raises() -> None:
    events = parse_events(
        [
            {"type": "pr_opened", "id": "a", "timestamp": "2026-01-02T00:00:00Z"},
            {"type": "pr_merged", "id": "a", "timestamp": "2026-01-01T00:00:00Z"},
        ]
    )
    with pytest.raises(InvalidEventError):
        calculate_metrics(events)


def test_duplicate_pr_event_raises() -> None:
    events = parse_events(
        [
            {"type": "pr_merged", "id": "a", "timestamp": "2026-01-01T00:00:00Z"},
            {"type": "pr_merged", "id": "a", "timestamp": "2026-01-02T00:00:00Z"},
        ]
    )
    with pytest.raises(InvalidEventError):
        calculate_metrics(events)


# --- Prompt -----------------------------------------------------------------


def test_prompt_contains_only_aggregated_metrics() -> None:
    metrics = calculate_metrics(parse_events(EXAMPLE_EVENTS))
    prompt = build_prompt("platform", metrics)
    assert '"average_pr_cycle_time_hours": 24.0' in prompt
    assert '"merged_prs": 1' in prompt
    assert "pr-1" not in prompt
    for event in EXAMPLE_EVENTS:
        assert event["timestamp"] not in prompt
        assert event["timestamp"][:10] not in prompt


# --- LLM response validation ------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [
        "not json",
        "[]",
        '{"recommendations": []}',
        '{"summary": 1, "recommendations": []}',
        '{"summary": "  ", "recommendations": []}',
        '{"summary": "ok"}',
        '{"summary": "ok", "recommendations": "do it"}',
        '{"summary": "ok", "recommendations": ["fine", 2]}',
        '{"summary": "ok", "recommendations": ["fine", "  "]}',
        '```\nnot json\n```',
    ],
)
def test_invalid_llm_reply_is_rejected(reply: str) -> None:
    with pytest.raises(InvalidLLMResponseError):
        parse_insight(reply)


@pytest.mark.parametrize(
    "reply",
    [
        f"```json\n{VALID_REPLY}\n```",
        f"```\n{VALID_REPLY}\n```",
        f"  {VALID_REPLY}\n",
    ],
)
def test_llm_reply_in_code_fence_is_accepted(reply: str) -> None:
    assert parse_insight(reply)["summary"] == "Healthy delivery flow."


def test_empty_recommendation_list_is_accepted() -> None:
    assert parse_insight('{"summary": "ok", "recommendations": []}')["recommendations"] == []


def test_valid_llm_reply_drops_extra_keys() -> None:
    reply = '{"summary": "ok", "recommendations": ["a"], "extra": true}'
    assert parse_insight(reply) == {"summary": "ok", "recommendations": ["a"]}


# --- Retry ------------------------------------------------------------------


@pytest.mark.parametrize(
    "first_failure",
    [TransientLLMError("throttled"), TimeoutError(), ConnectionError(), "not json"],
)
def test_retries_once_then_succeeds(first_failure: str | Exception) -> None:
    client = ScriptedLLMClient(first_failure, VALID_REPLY)
    result = generate_team_insight("platform", EXAMPLE_EVENTS, client)
    assert result["insight"]["summary"] == "Healthy delivery flow."
    assert client.calls == 2


def test_two_transient_failures_raise_unavailable() -> None:
    client = ScriptedLLMClient(TimeoutError(), TimeoutError(), VALID_REPLY)
    with pytest.raises(LLMUnavailableError):
        generate_team_insight("platform", EXAMPLE_EVENTS, client)
    assert client.calls == 2


def test_two_invalid_replies_raise_invalid_response() -> None:
    client = ScriptedLLMClient("not json", '{"summary": 1}', VALID_REPLY)
    with pytest.raises(InvalidLLMResponseError):
        generate_team_insight("platform", EXAMPLE_EVENTS, client)
    assert client.calls == 2


def test_non_transient_error_is_not_retried() -> None:
    client = ScriptedLLMClient(RuntimeError("bug"), VALID_REPLY)
    with pytest.raises(RuntimeError):
        generate_team_insight("platform", EXAMPLE_EVENTS, client)
    assert client.calls == 1


# --- Logging ----------------------------------------------------------------


def test_logs_retries_and_result_without_raw_data(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="app.team_insight")
    client = ScriptedLLMClient(TransientLLMError("secret payload pr-1"), VALID_REPLY)
    generate_team_insight("platform", EXAMPLE_EVENTS, client)

    assert "llm attempt 1/2 failed: TransientLLMError" in caplog.text
    assert "team_id=platform" in caplog.text
    assert "duration_ms=" in caplog.text
    assert "pr-1" not in caplog.text
    assert "secret payload" not in caplog.text
    assert "Healthy delivery flow." not in caplog.text
    for event in EXAMPLE_EVENTS:
        assert event["timestamp"] not in caplog.text


# --- End to end -------------------------------------------------------------


def test_generate_team_insight_end_to_end() -> None:
    client = ScriptedLLMClient(VALID_REPLY)
    result = generate_team_insight("platform", EXAMPLE_EVENTS, client)
    assert result == {
        "team_id": "platform",
        "metrics": {
            "merged_prs": 1,
            "average_pr_cycle_time_hours": 24.0,
            "deployments": 1,
            "incidents": 1,
        },
        "insight": {
            "summary": "Healthy delivery flow.",
            "recommendations": ["Monitor cycle time."],
        },
    }
    assert client.calls == 1
    json.dumps(result)
