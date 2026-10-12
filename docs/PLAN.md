# Team Insight Service — Plan

## What we are building

One function:

```python
def generate_team_insight(
    team_id: str,
    events: list[dict[str, Any]],
    llm_client: LLMClient,
) -> TeamInsightResponse: ...
```

1. Validate and parse the raw events.
2. Calculate deterministic metrics from them.
3. Send **only the aggregated metrics** to the injected LLM client (retry at most once on transient failure).
4. Validate the LLM response.
5. Return a structured dict, ready to be returned by an API endpoint.

## Deliverables → where they live

| Deliverable | Where |
| --- | --- |
| Working `generate_team_insight` | `team_insight.py` → `generate_team_insight` |
| Input validation with clear exceptions | `team_insight.py` → `parse_events`, error classes |
| Deterministic metric calculation | `team_insight.py` → `calculate_metrics` |
| Prompt with only aggregated metrics | `team_insight.py` → `build_prompt` |
| LLM response validation | `team_insight.py` → `parse_insight` |
| Bounded retry for transient LLM failures | `team_insight.py` → `request_insight` |
| At least three focused tests | `tests/` (more than three, listed below) |
| Production notes (FastAPI / Lambda) | `README.md` section, outline below |

## Constraints

- Python **3.10+**, standard library only in the core (`datetime`, `json`, `typing`, `dataclasses`, `enum`).
- No real network calls, no LLM provider SDK. The LLM client is injected.
- Metrics are team-level only. Nothing ranks or compares individual engineers.
- No raw event data in the prompt.
- Timestamps are ISO 8601 ending in `Z`.
- Unmatched `pr_opened` / `pr_merged` are ignored for cycle time, but malformed records raise.
- LLM call is retried at most once after the first attempt (max 2 calls).
- LLM output is rejected unless it is valid JSON with a string `summary` and a list of string `recommendations`.

### Python 3.10 compatibility notes

- `datetime.fromisoformat` differs between 3.10 and 3.11+ → check a strict `YYYY-MM-DDTHH:MM:SS[.ffffff]Z` regex, then parse with `strptime`.
- Use `timezone.utc`, not `datetime.UTC` (3.11+).
- Use `str, Enum`, not `StrEnum` (3.11+).
- `X | None` in annotations is fine on 3.10.
- `pyproject.toml` currently says `requires-python = ">=3.12"` → lower to `>=3.10`.

## Input

```python
team_id = "platform"
events = [
    {"type": "pr_opened",  "id": "pr-1", "timestamp": "2026-01-01T09:00:00Z"},
    {"type": "pr_merged",  "id": "pr-1", "timestamp": "2026-01-02T09:00:00Z"},
    {"type": "deployment",               "timestamp": "2026-01-03T12:00:00Z"},
    {"type": "incident",                 "timestamp": "2026-01-04T12:00:00Z"},
]
```

| Event type | Required fields |
| --- | --- |
| `pr_opened` | `type`, `id`, `timestamp` |
| `pr_merged` | `type`, `id`, `timestamp` |
| `deployment` | `type`, `timestamp` |
| `incident` | `type`, `timestamp` |

## Output

```json
{
  "team_id": "platform",
  "metrics": {
    "merged_prs": 1,
    "average_pr_cycle_time_hours": 24.0,
    "deployments": 1,
    "incidents": 1
  },
  "insight": {
    "summary": "The team merged one pull request with an average cycle time of 24 hours.",
    "recommendations": ["Continue monitoring pull request cycle time."]
  }
}
```

Typed with `TypedDict`s (`MetricsDict`, `InsightDict`, `TeamInsightResponse`) so it is JSON-serialisable as-is.

## Exceptions (`errors.py`)

```
TeamInsightError(Exception)            # base, catch-all for callers
├── InvalidEventError(TeamInsightError, ValueError)   # bad team_id or event → HTTP 400
├── InvalidLLMResponseError(TeamInsightError)         # bad JSON / shape    → HTTP 502
└── LLMUnavailableError(TeamInsightError)             # retries exhausted   → HTTP 503
TransientLLMError(Exception)           # raised BY the LLM client for timeouts/throttling
```

Messages say which event (index) and which field failed, e.g.
`"event[2]: missing 'timestamp'"`. Messages never echo the whole raw event.

## Phase 1 — Inputs and metrics

### 1. Parse events (`events.py`)

- `EventType(str, Enum)`: `PR_OPENED`, `PR_MERGED`, `DEPLOYMENT`, `INCIDENT`.
- `Event` frozen dataclass: `type: EventType`, `timestamp: datetime`, `id: str | None`.
- `parse_timestamp(value: str) -> datetime`: must be a string ending in `Z`; returns aware UTC.
- `parse_events(raw: list[dict[str, Any]]) -> list[Event]`.

Raises `InvalidEventError` when:

- `team_id` is not a non-empty string
- `events` is not a list
- an event is not a dict
- `type` is missing or unknown
- `timestamp` is missing, not a string, does not end in `Z`, or does not parse
- a PR event has no non-empty string `id`

Extra unknown keys on an event are ignored.

### 2. Calculate metrics (`metrics.py`)

- `TeamMetrics` frozen dataclass: `merged_prs: int`, `average_pr_cycle_time_hours: float | None`,
  `deployments: int`, `incidents: int`, plus `to_dict() -> MetricsDict`.
- `calculate_metrics(events: list[Event]) -> TeamMetrics`.

Rules:

- `merged_prs` = number of `pr_merged` events.
- Cycle time = `merged.timestamp - opened.timestamp` for the same `id`.
- `average_pr_cycle_time_hours` = mean of matched pairs, rounded to 2 decimals; `None` if no pairs.
- `deployments` / `incidents` = count of those events.
- Order of events in the list does not matter.

| Case | Behaviour |
| --- | --- |
| Empty event list | counts `0`, average `None` |
| `pr_merged` without `pr_opened` | counted in `merged_prs`, ignored for cycle time |
| `pr_opened` without `pr_merged` | ignored for cycle time |
| Merged before opened (same id) | `InvalidEventError` |
| Duplicate `pr_opened` or `pr_merged` for one id | `InvalidEventError` |

## Phase 2 — LLM

### 3. Client contract (`llm.py`)

```python
class LLMClient(Protocol):
    def complete(self, prompt: str, timeout_seconds: float) -> str: ...
```

`generate_team_insight` takes an optional `timeout_seconds` (default 10s) and passes it through.

Any provider can be wrapped behind this. The wrapper is responsible for mapping
its timeout/throttling errors to `TransientLLMError`.

### 4. Prompt — `build_prompt(team_id, metrics) -> str`

- Contains `team_id` and the four metrics (serialised with `json.dumps`), nothing else.
- Never receives the event list, so raw ids/timestamps cannot leak by construction.
- Instructs the model: team-level only, do not mention or compare individuals,
  reply with JSON only: `{"summary": "...", "recommendations": ["..."]}`.

### 5. Validation — `parse_insight(text: str) -> InsightDict`

Raises `InvalidLLMResponseError` when:

- text is not valid JSON
- top level is not an object
- `summary` is missing or not a string (or blank)
- `recommendations` is missing, not a list, or has a non-string item

Extra keys are dropped; only `summary` and `recommendations` are returned.

### 6. Retry — `request_insight(client, prompt, timeout_seconds, max_retries=1) -> InsightDict`

- Call the LLM and validate the reply with `parse_insight`.
- Retried once (max 2 calls) on a transient error (`TransientLLMError`, `TimeoutError`,
  `ConnectionError`) **or** an invalid reply, since LLM output is not deterministic.
- Second transient failure → `LLMUnavailableError` (chained with `from`).
- Second invalid reply → `InvalidLLMResponseError`.
- Any other exception is not retried and propagates.
- No sleep in the core, so tests stay fast. Backoff with jitter can be added in production.

### 7. Entry point — `generate_team_insight`

```
validate team_id
→ parse_events
→ calculate_metrics
→ build_prompt(team_id, metrics)
→ request_insight(llm_client, prompt)   # call + validate + retry
→ {"team_id", "metrics", "insight"}
```

Invalid input fails before the LLM is ever called.

## Files

Everything lives in one module, split into sections (errors, types, input
validation, metrics, LLM, entry point).

```
src/app/team_insight.py      # the whole service
tests/fakes.py               # FakeLLMClient, ScriptedLLMClient, example data
tests/test_example.py        # reference example with FakeLLMClient
tests/test_team_insight.py   # pytest unit + end-to-end tests
tests/test_edge_cases.py     # niche scenarios
```

## Tests

`ScriptedLLMClient(*replies)` (in the test file): each item is either a string to return or an
exception to raise; it records every prompt it receives.

**events**
- example input parses into 4 `Event`s with UTC datetimes
- unknown type → `InvalidEventError`
- missing timestamp / non-`Z` timestamp / garbage timestamp → `InvalidEventError`
- PR event without id → `InvalidEventError`
- non-dict event, non-list events, empty `team_id` → `InvalidEventError`

**metrics**
- example → `1, 24.0, 1, 1`
- empty list → `0, None, 0, 0`
- unmatched merged / unmatched opened ignored for cycle time
- several PRs averaged and rounded
- shuffled order gives the same result
- merged before opened, duplicate id → `InvalidEventError`

**llm**
- prompt contains the metric values
- prompt does not contain `pr-1` or any event timestamp
- valid JSON parses; invalid JSON, missing `summary`, non-list or non-string recommendations rejected
- transient error then success → returns, 2 calls
- transient error twice → `LLMUnavailableError`, 2 calls (never 3)
- non-transient error → propagates, 1 call

**service (end-to-end with fake client)**
- example input returns exactly the expected output
- invalid event → raises, LLM called 0 times
- invalid LLM JSON → `InvalidLLMResponseError`, 1 call
- output is `json.dumps`-able

## Production considerations (outline for README)

**FastAPI**
- `POST /teams/{team_id}/insights` with Pydantic request/response models mirroring the TypedDicts.
- LLM client provided via `Depends`, so tests override it.
- Map errors: `InvalidEventError` → 400/422, `InvalidLLMResponseError` → 502, `LLMUnavailableError` → 503.
- Plain `def` endpoint (the core is sync) so FastAPI runs it in a threadpool.
- Request size limit on `events`, auth, rate limiting per team.

**AWS Lambda**
- Thin handler: parse API Gateway body → call function → map errors to status codes.
- Create the LLM client outside the handler to reuse it across warm invocations.
- Lambda timeout must cover 2 LLM attempts; API Gateway caps at 29s.
- Provider API key from Secrets Manager / env var, never logged.

**Persistence (DynamoDB / Aurora / MySQL)**
- Store the result, not the raw events or prompt: DynamoDB `PK=team_id`, `SK=generated_at`,
  or an `insights` table in Aurora/MySQL. Useful for history and audit.

**Caching (Redis / ElastiCache)**
- Metrics are deterministic → cache the LLM insight by `hash(team_id + metrics)` with a TTL.
  Same metrics skip the LLM call: lower cost and latency.

**General**
- Structured logs with team id, metrics, attempts, latency; never raw events or keys.
- Metrics for LLM failures/retries; timeouts on the client; cost budget per team.

## Order of work

1. ~~Remove old skeleton, set `requires-python >= 3.10`.~~ Done.
2. ~~`team_insight.py`: validation, metrics, prompt, response validation, retry, entry point.~~ Done.
3. ~~`tests/test_team_insight.py`.~~ Done.
4. ~~README: usage + production considerations.~~ Done.
