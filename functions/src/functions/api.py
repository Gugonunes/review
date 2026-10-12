"""Lambda handler for POST /teams/{team_id}/insights (API Gateway HTTP API, payload v2)."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
from typing import Any

from app.team_insight import (
    InvalidEventError,
    InvalidLLMResponseError,
    LLMClient,
    LLMUnavailableError,
    generate_team_insight,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

MAX_EVENTS = 10_000

_llm_client: LLMClient | None = None


def create_llm_client() -> LLMClient:
    """Build the production LLM client. No provider is wired yet."""
    # Implement LLMClient for the chosen provider here, reading its API key from
    # os.environ["LLM_API_KEY"] (see sst.config.ts). Map timeouts, throttling and
    # 5xx responses to app.team_insight.TransientLLMError so they are retried.
    raise NotImplementedError("No LLM provider is configured")


def get_llm_client() -> LLMClient:
    """Create the LLM client once per container so warm invocations reuse it."""
    global _llm_client
    if _llm_client is None:
        _llm_client = create_llm_client()
    return _llm_client


def json_response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    """Build an API Gateway proxy response with a JSON body."""
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }


def error_response(status: int, message: str) -> dict[str, Any]:
    """Build a JSON error response."""
    return json_response(status, {"error": message})


def parse_body(event: dict[str, Any]) -> object:
    """Decode the request body (optionally base64) and parse it as JSON."""
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body, validate=True).decode("utf-8")
    return json.loads(body)


def handle(event: dict[str, Any], llm_client: LLMClient) -> dict[str, Any]:
    """Validate the HTTP request, run generate_team_insight and map errors to status codes."""
    team_id = (event.get("pathParameters") or {}).get("team_id")

    try:
        body = parse_body(event)
    except (ValueError, binascii.Error):  # JSONDecodeError and UnicodeDecodeError are ValueErrors
        return error_response(400, "Request body must be valid JSON")
    if not isinstance(body, dict):
        return error_response(400, "Request body must be a JSON object")

    events = body.get("events")
    if isinstance(events, list) and len(events) > MAX_EVENTS:
        return error_response(413, f"At most {MAX_EVENTS} events per request")

    try:
        result = generate_team_insight(
            team_id,  # type: ignore[arg-type]  # validated inside
            events,  # type: ignore[arg-type]
            llm_client,
            timeout_seconds=float(os.environ.get("LLM_TIMEOUT_SECONDS", "10")),
        )
    except InvalidEventError as error:
        return error_response(400, str(error))
    except InvalidLLMResponseError:
        return error_response(502, "The insight provider returned an invalid response")
    except LLMUnavailableError:
        return error_response(503, "The insight provider is unavailable, try again later")

    return json_response(200, dict(result))


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entry point; never leaks internal errors to the caller."""
    try:
        return handle(event, get_llm_client())
    except NotImplementedError:
        return error_response(501, "Insight generation is not configured yet")
    except Exception:
        logger.exception(
            "unhandled error request_id=%s", getattr(context, "aws_request_id", None)
        )
        return error_response(500, "Internal server error")
