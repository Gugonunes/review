import base64
import json
from typing import Any

import pytest

from app.team_insight import TransientLLMError
from fakes import EXAMPLE_EVENTS, VALID_REPLY, ScriptedLLMClient
from functions import api


def http_event(body: object = None, team_id: str | None = "platform", **extra: Any) -> dict[str, Any]:
    """Build a minimal API Gateway HTTP API (payload v2) event."""
    event: dict[str, Any] = {
        "pathParameters": {"team_id": team_id} if team_id is not None else None,
        "body": body if isinstance(body, str) or body is None else json.dumps(body),
        "isBase64Encoded": False,
    }
    event.update(extra)
    return event


def body_of(response: dict[str, Any]) -> dict[str, Any]:
    return json.loads(response["body"])


def test_success_returns_200_with_insight() -> None:
    client = ScriptedLLMClient(VALID_REPLY)
    response = api.handle(http_event({"events": EXAMPLE_EVENTS}), client)
    assert response["statusCode"] == 200
    assert response["headers"]["content-type"] == "application/json"
    assert body_of(response)["metrics"]["merged_prs"] == 1
    assert body_of(response)["insight"]["summary"] == "Healthy delivery flow."


def test_base64_body_is_decoded() -> None:
    raw = base64.b64encode(json.dumps({"events": EXAMPLE_EVENTS}).encode()).decode()
    response = api.handle(http_event(raw, isBase64Encoded=True), ScriptedLLMClient(VALID_REPLY))
    assert response["statusCode"] == 200


@pytest.mark.parametrize("body", [None, "", "not json", "[]", '"events"'])
def test_bad_body_returns_400(body: str | None) -> None:
    client = ScriptedLLMClient(VALID_REPLY)
    assert api.handle(http_event(body), client)["statusCode"] == 400
    assert client.calls == 0


def test_bad_base64_returns_400() -> None:
    response = api.handle(http_event("%%%", isBase64Encoded=True), ScriptedLLMClient(VALID_REPLY))
    assert response["statusCode"] == 400


@pytest.mark.parametrize(
    "event",
    [
        http_event({"events": [{"type": "nope"}]}),
        http_event({}),  # missing events
        http_event({"events": EXAMPLE_EVENTS}, team_id=None),
    ],
)
def test_invalid_input_returns_400(event: dict[str, Any]) -> None:
    client = ScriptedLLMClient(VALID_REPLY)
    response = api.handle(event, client)
    assert response["statusCode"] == 400
    assert "error" in body_of(response)
    assert client.calls == 0


def test_too_many_events_returns_413() -> None:
    events = [{"type": "deployment", "timestamp": "2026-01-01T09:00:00Z"}] * (api.MAX_EVENTS + 1)
    client = ScriptedLLMClient(VALID_REPLY)
    assert api.handle(http_event({"events": events}), client)["statusCode"] == 413
    assert client.calls == 0


def test_invalid_llm_reply_returns_502() -> None:
    client = ScriptedLLMClient("not json", "still not json")
    assert api.handle(http_event({"events": EXAMPLE_EVENTS}), client)["statusCode"] == 502


def test_llm_unavailable_returns_503() -> None:
    client = ScriptedLLMClient(TransientLLMError(), TransientLLMError())
    assert api.handle(http_event({"events": EXAMPLE_EVENTS}), client)["statusCode"] == 503


def test_timeout_comes_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "4")
    client = ScriptedLLMClient(VALID_REPLY)
    api.handle(http_event({"events": EXAMPLE_EVENTS}), client)
    assert client.timeouts == [4.0]


def test_handler_returns_501_until_a_provider_is_wired(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "_llm_client", None)
    response = api.handler(http_event({"events": EXAMPLE_EVENTS}), None)
    assert response["statusCode"] == 501


def test_handler_reuses_client_and_hides_internal_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ScriptedLLMClient(RuntimeError("secret internal detail"))
    monkeypatch.setattr(api, "_llm_client", client)
    response = api.handler(http_event({"events": EXAMPLE_EVENTS}), None)
    assert response["statusCode"] == 500
    assert "secret internal detail" not in response["body"]
    assert api.get_llm_client() is client
