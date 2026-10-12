"""Shared test data and fake LLM clients."""

VALID_REPLY = '{"summary": "Healthy delivery flow.", "recommendations": ["Monitor cycle time."]}'

EXAMPLE_EVENTS = [
    {"type": "pr_opened", "id": "pr-1", "timestamp": "2026-01-01T09:00:00Z"},
    {"type": "pr_merged", "id": "pr-1", "timestamp": "2026-01-02T09:00:00Z"},
    {"type": "deployment", "timestamp": "2026-01-03T12:00:00Z"},
    {"type": "incident", "timestamp": "2026-01-04T12:00:00Z"},
]


class FakeLLMClient:
    """Returns a canned response, counts calls and asserts no raw PR id is sent."""

    def __init__(self, response: str):
        self.response = response
        self.calls = 0

    def complete(self, prompt: str, timeout_seconds: float) -> str:
        self.calls += 1
        assert "pr-1" not in prompt
        return self.response


class ScriptedLLMClient:
    """Returns (or raises) each scripted reply in order and records every call."""

    def __init__(self, *replies: object):
        self.replies = list(replies)
        self.prompts: list[str] = []
        self.timeouts: list[float] = []

    @property
    def calls(self) -> int:
        return len(self.prompts)

    def complete(self, prompt: str, timeout_seconds: float) -> str:
        self.prompts.append(prompt)
        self.timeouts.append(timeout_seconds)
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply  # type: ignore[return-value]
