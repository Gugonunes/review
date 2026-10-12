"""Team-level engineering productivity insights.

Validates raw engineering events, calculates deterministic metrics, sends only
the aggregated metrics to an injected LLM client and returns an API-ready dict.
Standard library only; compatible with Python 3.10+.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol, TypedDict

logger = logging.getLogger(__name__)

# Per-attempt timeout; with one retry the worst case is twice this value.
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_RETRIES = 1

# Strict "YYYY-MM-DDTHH:MM:SS[.ffffff]Z". Checked before parsing so the accepted
# format is the same on every Python version (fromisoformat grew more lenient in 3.11).
# re.ASCII: without it \d also matches non-ASCII digits such as full-width "２".
TIMESTAMP_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z", re.ASCII)

# Optional ```json ... ``` fence that models often wrap JSON replies in.
CODE_FENCE_PATTERN = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)


# --- Errors -----------------------------------------------------------------


class TeamInsightError(Exception):
    """Base class for every error raised by this module."""


class InvalidEventError(TeamInsightError, ValueError):
    """Input (team id or events) is malformed. Maps to HTTP 400."""


class InvalidLLMResponseError(TeamInsightError):
    """LLM reply is not valid JSON or has the wrong shape. Maps to HTTP 502."""


class LLMUnavailableError(TeamInsightError):
    """LLM kept failing with transient errors after the retry. Maps to HTTP 503."""


class TransientLLMError(Exception):
    """Raised by LLM client implementations for timeouts, throttling, etc."""


TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    TransientLLMError,
    TimeoutError,
    ConnectionError,
)


# --- Types ------------------------------------------------------------------


class LLMClient(Protocol):
    """Injected LLM dependency: takes a prompt, returns the raw text reply."""

    def complete(self, prompt: str, timeout_seconds: float) -> str: ...


class MetricsDict(TypedDict):
    merged_prs: int
    average_pr_cycle_time_hours: float | None
    deployments: int
    incidents: int


class InsightDict(TypedDict):
    summary: str
    recommendations: list[str]


class TeamInsightResponse(TypedDict):
    team_id: str
    metrics: MetricsDict
    insight: InsightDict


class EventType(str, Enum):
    PR_OPENED = "pr_opened"
    PR_MERGED = "pr_merged"
    DEPLOYMENT = "deployment"
    INCIDENT = "incident"


PR_EVENT_TYPES = frozenset({EventType.PR_OPENED, EventType.PR_MERGED})


@dataclass(frozen=True)
class Event:
    type: EventType
    timestamp: datetime
    id: str | None = None


@dataclass(frozen=True)
class TeamMetrics:
    merged_prs: int
    average_pr_cycle_time_hours: float | None
    deployments: int
    incidents: int

    def to_dict(self) -> MetricsDict:
        """Return the metrics as a JSON-serialisable dict."""
        return {
            "merged_prs": self.merged_prs,
            "average_pr_cycle_time_hours": self.average_pr_cycle_time_hours,
            "deployments": self.deployments,
            "incidents": self.incidents,
        }


# --- Input validation -------------------------------------------------------


def validate_team_id(team_id: object) -> str:
    """Ensure the team id is a non-empty string."""
    if not isinstance(team_id, str) or not team_id.strip():
        raise InvalidEventError("team_id must be a non-empty string")
    return team_id


def parse_timestamp(value: object, where: str) -> datetime:
    """Parse a strict 'YYYY-MM-DDTHH:MM:SS[.ffffff]Z' timestamp into an aware UTC datetime."""
    match = TIMESTAMP_PATTERN.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise InvalidEventError(
            f"{where}: 'timestamp' must be an ISO-8601 string like 2026-01-01T09:00:00Z"
        )
    fmt = "%Y-%m-%dT%H:%M:%S.%fZ" if match.group(1) else "%Y-%m-%dT%H:%M:%SZ"
    try:
        parsed = datetime.strptime(value, fmt)
    except ValueError:  # right shape, impossible value, e.g. month 13
        raise InvalidEventError(f"{where}: 'timestamp' is not a valid date/time") from None
    return parsed.replace(tzinfo=timezone.utc)


def parse_event(raw: object, index: int) -> Event:
    """Validate one raw event dict and convert it into an Event."""
    where = f"event[{index}]"
    if not isinstance(raw, dict):
        raise InvalidEventError(f"{where}: must be an object")

    raw_type = raw.get("type")
    try:
        event_type = EventType(raw_type)
    except ValueError:
        allowed = ", ".join(t.value for t in EventType)
        raise InvalidEventError(f"{where}: 'type' must be one of: {allowed}") from None

    if "timestamp" not in raw:
        raise InvalidEventError(f"{where}: missing 'timestamp'")
    timestamp = parse_timestamp(raw["timestamp"], where)

    event_id = None
    if event_type in PR_EVENT_TYPES:
        event_id = raw.get("id")
        if not isinstance(event_id, str) or not event_id.strip():
            raise InvalidEventError(f"{where}: '{event_type.value}' requires a non-empty string 'id'")

    return Event(type=event_type, timestamp=timestamp, id=event_id)


def parse_events(raw_events: object) -> list[Event]:
    """Validate the whole event list; any malformed record raises."""
    if not isinstance(raw_events, list):
        raise InvalidEventError("events must be a list")
    return [parse_event(raw, index) for index, raw in enumerate(raw_events)]


# --- Metrics ----------------------------------------------------------------


def index_pr_events(events: list[Event], event_type: EventType) -> dict[str, datetime]:
    """Map PR id -> timestamp for one PR event type; duplicate ids raise."""
    by_id: dict[str, datetime] = {}
    for event in events:
        if event.type is not event_type:
            continue
        assert event.id is not None  # guaranteed by parse_event
        if event.id in by_id:
            raise InvalidEventError(f"duplicate '{event_type.value}' event for id '{event.id}'")
        by_id[event.id] = event.timestamp
    return by_id


def calculate_metrics(events: list[Event]) -> TeamMetrics:
    """Compute deterministic team-level metrics from parsed events."""
    opened = index_pr_events(events, EventType.PR_OPENED)
    merged = index_pr_events(events, EventType.PR_MERGED)

    # Unmatched opened/merged events are ignored for cycle time.
    cycle_hours: list[float] = []
    for pr_id in sorted(opened.keys() & merged.keys()):
        delta = merged[pr_id] - opened[pr_id]
        if delta.total_seconds() < 0:
            raise InvalidEventError(f"pr '{pr_id}' was merged before it was opened")
        cycle_hours.append(delta.total_seconds() / 3600)

    average = round(sum(cycle_hours) / len(cycle_hours), 2) if cycle_hours else None

    return TeamMetrics(
        merged_prs=len(merged),
        average_pr_cycle_time_hours=average,
        deployments=sum(1 for e in events if e.type is EventType.DEPLOYMENT),
        incidents=sum(1 for e in events if e.type is EventType.INCIDENT),
    )


# --- LLM --------------------------------------------------------------------


def build_prompt(team_id: str, metrics: TeamMetrics) -> str:
    """Build the LLM prompt from aggregated metrics only (never raw events)."""
    payload = json.dumps({"team_id": team_id, "metrics": metrics.to_dict()}, sort_keys=True)
    return (
        "You are an engineering productivity assistant.\n"
        "Given these aggregated team-level metrics, write a short summary and "
        "actionable recommendations for the team as a whole.\n"
        "Do not mention, rank or compare individual engineers.\n"
        "Reply with JSON only, in exactly this shape:\n"
        '{"summary": "<string>", "recommendations": ["<string>", ...]}\n\n'
        f"Metrics:\n{payload}"
    )


def parse_insight(text: object) -> InsightDict:
    """Validate the LLM reply: JSON object with string summary and list of string recommendations."""
    if not isinstance(text, str):
        raise InvalidLLMResponseError("LLM response must be a string")
    text = text.strip()
    fence = CODE_FENCE_PATTERN.match(text)
    if fence:
        text = fence.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise InvalidLLMResponseError("LLM response is not valid JSON") from None

    if not isinstance(data, dict):
        raise InvalidLLMResponseError("LLM response must be a JSON object")

    summary = data.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise InvalidLLMResponseError("'summary' must be a non-empty string")

    recommendations = data.get("recommendations")
    if not isinstance(recommendations, list) or not all(
        isinstance(item, str) and item.strip() for item in recommendations
    ):
        raise InvalidLLMResponseError("'recommendations' must be a list of non-empty strings")

    return {"summary": summary, "recommendations": list(recommendations)}


def request_insight(
    llm_client: LLMClient,
    prompt: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_retries: int = MAX_RETRIES,
) -> InsightDict:
    """Call the LLM and validate its reply, retrying once on transient errors or invalid replies.

    The timeout applies per attempt, so the worst case is (1 + max_retries) * timeout_seconds.
    """
    attempts = 1 + max_retries
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return parse_insight(llm_client.complete(prompt, timeout_seconds=timeout_seconds))
        except (*TRANSIENT_ERRORS, InvalidLLMResponseError) as error:
            # Log the error type only: messages from third-party clients may echo payloads.
            logger.warning(
                "llm attempt %d/%d failed: %s", attempt, attempts, type(error).__name__
            )
            last_error = error

    if isinstance(last_error, InvalidLLMResponseError):
        raise last_error
    raise LLMUnavailableError(f"LLM failed after {attempts} attempts") from last_error


# --- Entry point ------------------------------------------------------------


def generate_team_insight(
    team_id: str,
    events: list[dict[str, Any]],
    llm_client: LLMClient,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> TeamInsightResponse:
    """Validate events, compute metrics, ask the LLM for an insight and return an API-ready dict."""
    started = time.monotonic()
    team_id = validate_team_id(team_id)
    metrics = calculate_metrics(parse_events(events))
    insight = request_insight(llm_client, build_prompt(team_id, metrics), timeout_seconds)
    # Aggregated metrics only; never the raw events, prompt or LLM reply.
    logger.info(
        "team insight generated team_id=%s events=%d metrics=%s duration_ms=%d",
        team_id,
        len(events),
        json.dumps(metrics.to_dict(), sort_keys=True),
        (time.monotonic() - started) * 1000,
    )
    return {"team_id": team_id, "metrics": metrics.to_dict(), "insight": insight}
