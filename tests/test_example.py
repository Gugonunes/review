"""The reference example from the task statement, run with FakeLLMClient."""

from app.team_insight import generate_team_insight
from fakes import FakeLLMClient


def test_reference_example() -> None:
    client = FakeLLMClient(
        '{"summary": "Healthy delivery flow.", '
        '"recommendations": ["Monitor cycle time."]}'
    )

    result = generate_team_insight(
        "platform",
        [
            {
                "type": "pr_opened",
                "id": "pr-1",
                "timestamp": "2026-01-01T09:00:00Z",
            },
            {
                "type": "pr_merged",
                "id": "pr-1",
                "timestamp": "2026-01-02T09:00:00Z",
            },
            {
                "type": "deployment",
                "timestamp": "2026-01-03T12:00:00Z",
            },
            {
                "type": "incident",
                "timestamp": "2026-01-04T12:00:00Z",
            },
        ],
        client,
    )

    assert result["metrics"]["merged_prs"] == 1
    assert result["metrics"]["average_pr_cycle_time_hours"] == 24.0
    assert result["metrics"]["deployments"] == 1
    assert result["metrics"]["incidents"] == 1
    assert result["insight"]["summary"] == "Healthy delivery flow."
    assert client.calls == 1
