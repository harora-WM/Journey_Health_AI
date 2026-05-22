# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Create `.env` in the project root with all variables from the table below — `config.py` crashes at import time if any numeric ones (`RESPONSE_MAX_TOKENS`, `RESPONSE_TEMPERATURE`) are missing.

### Required env vars (`.env`)

| Variable | Description |
|---|---|
| `AWS_REGION` | e.g. `us-east-1` |
| `AWS_ACCESS_KEY_ID` | IAM key with Bedrock invoke permissions |
| `AWS_SECRET_ACCESS_KEY` | IAM secret |
| `BEDROCK_MODEL_ID` | e.g. `anthropic.claude-3-5-sonnet-20240620-v1:0` |
| `RESPONSE_MAX_TOKENS` | Integer; controls LLM output length |
| `RESPONSE_TEMPERATURE` | Float; e.g. `0.1` |
| `KEYCLOAK_URL` | Full token endpoint URL |
| `KEYCLOAK_CLIENT_ID` | e.g. `web_app` |
| `JOURNEY_WEAKLINK_API_URL` | Full weak-link endpoint URL |
| `JOURNEY_SUMMARY_API_BASE_URL` | Base URL for the summary endpoint (app ID is appended per call) |
| `JOURNEY_HEALTH_RANGE_TYPE` | Default range type, e.g. `CUSTOM` |
| `WM_USERNAME` | Watermelon sandbox username |
| `WM_PASSWORD` | Watermelon sandbox password |

## Running

**Start order matters:** the Streamlit UI talks to the FastAPI server, so start the FastAPI server first.

```bash
# FastAPI server (http://localhost:8000/docs for interactive API docs)
# workers=1 is required — _orchestrator is a module-level singleton; multiple workers would duplicate it
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1

# Streamlit UI (configure API URL and IDs in sidebar before querying)
streamlit run app.py

# Adapter standalone (fetches live data, writes journey_health_output.json — uses hardcoded test IDs in __main__)
python JourneyHealth_Adapter.py

# Timestamp resolver standalone — accepts an optional query argument
python timestamp.py "show errors in the last 2 hours"
python timestamp.py   # runs all built-in test queries
```

## Testing the API manually

```bash
curl -s -X POST http://localhost:8000/query/journey \
  -H "Content-Type: application/json" \
  -d '{"query":"last 2 hours","journey_ids":[2338008,2331452],"application_id":2327006,"project_id":2329158,"range":"CUSTOM"}' \
  | python -m json.tool

# SSE streaming endpoint
curl -N -X POST http://localhost:8000/query/journey/stream \
  -H "Content-Type: application/json" \
  -d '{"query":"last 2 hours","journey_ids":[2338008,2331452],"application_id":2327006,"project_id":2329158,"range":"CUSTOM"}'
```

There is no automated test suite.

## Architecture

Three-step backend pipeline per query:

```
app.py (Streamlit chat UI)
    → POST /query/journey/stream  (FastAPI — main.py)
        → JourneyHealthOrchestrator._resolve_timestamps()
            → TimestampResolver.resolve_time_range()  # extracts window from natural language
        → JourneyHealthOrchestrator._prepare_context()
            → JourneyHealth_Adapter.get_data()        # fetches from Watermelon API
        → LLMResponseGenerator.generate_response()    # calls AWS Bedrock Claude
```

### Key files

| File | Role |
|---|---|
| `JourneyHealth_Adapter.py` | Fetches weak-link and summary (ERROR + RESPONSE) data from Watermelon API |
| `timestamp.py` | Converts natural language time expressions to UTC ms timestamps |
| `config.py` | Single source of truth for all env vars — import this, never `os.getenv()` directly |
| `llm_response_generator.py` | Wraps AWS Bedrock Claude; builds system prompt and calls the model |
| `main.py` | FastAPI app + `JourneyHealthOrchestrator`; exposes `/query/journey` and `/query/journey/stream` |
| `api_models.py` | Pydantic models: `QueryRequest`, `QueryResponse`, `TimeResolution`, `ErrorResponse` |
| `app.py` | Streamlit chat UI; streams SSE tokens from the FastAPI backend |

### Adapter internals (`JourneyHealth_Adapter.py`)

- **`get_access_token()`** — Keycloak OAuth2 password grant → Bearer token (one token reused for all three calls within a single `get_data()` invocation; no caching between requests)
- **`fetch_weak_link_data()`** — GET `/api/user-journeys/weak-link/journies`; takes comma-joined `journey_ids`
- **`fetch_journey_summary()`** — GET `/api/user-journeys/summary/all/{application_id}`; called twice (once with `data_for=ERROR`, once with `data_for=RESPONSE`) because the endpoint does not accept comma-separated values
- **`get_data()`** — public entry point; returns `{data_source, filters, weak_link_records, summary_error_records, summary_response_records, fetched_at}`

**Auth**: Keycloak password grant flow. All URLs and credentials come from `config.py` (`KEYCLOAK_URL`, `KEYCLOAK_CLIENT_ID`, `JOURNEY_WEAKLINK_API_URL`, `JOURNEY_SUMMARY_API_BASE_URL`).  
**SSL**: `verify=False` throughout — sandbox environment only.  
**Response normalization**: both endpoints may return a single object or a list; both fetch functions normalize to a list.  
**Partial success**: `get_data()` returns `None` only if all three calls fail; individual `None` sections are returned as empty lists.

### Timestamp resolver (`timestamp.py`)

`TimestampResolver.resolve_time_range(query, timezone_str="UTC")` runs three tiers in order:
1. **Deterministic** — regex/rule-based parser handles ~30 patterns (relative windows, named periods, explicit ranges, calendar boundaries)
2. **LLM fallback** — asks Claude Sonnet via Bedrock when regex fails; returns `{"ambiguous": true}` for queries with no time reference. Creates a new boto3 client per call (unlike `LLMResponseGenerator` which reuses one created at init).
3. **Hard fallback** — last 2 hours

`timezone_str` is threaded through both `_parse_deterministic()` and `_parse_with_llm()` so all wall-clock expressions resolve in the caller's timezone. The LLM prompt also receives the timezone name so it reasons correctly.

Return dict keys: `primary_range` (`time_range`, `start_time`, `end_time`, `duration_days`), `source` (`deterministic` / `llm` / `fallback`), `index` (`HOURLY` if ≤3 days, `DAILY` if >3 days), `index_reason` (human-readable explanation).

### LLM response generator (`llm_response_generator.py`)

`LLMResponseGenerator` wraps Bedrock with two call paths:
- `generate_response()` — blocking; returns `{success, user_query, response, metadata}`
- `generate_response_stream()` — yields raw text chunks via `invoke_model_with_response_stream`

The system prompt (~200 lines of markdown) is built once at `__init__` and reused across all queries. It encodes the full Journey Health data schema, health status definitions (HEALTHY / AT_RISK / UNHEALTHY / UNDER_REVIEW), burn rate severity thresholds, and a mandatory **Optimization Roadmap** section that ranks transactions by P1 (dual-breach) → P2 (EB only) → P3 (latency only). The roadmap section is constrained to ≤350 tokens in the system prompt to keep responses concise.

Both call paths use the Bedrock **Messages API** (`anthropic_version: "bedrock-2023-05-31"`) — not the older text completion format. The boto3 client is created once at `__init__` and reused (unlike `TimestampResolver`, which creates a new client per LLM fallback call).

### FastAPI endpoints (`main.py`)

| Endpoint | Description |
|---|---|
| `GET /` | Health check |
| `POST /query/journey` | Blocking — returns full `QueryResponse` JSON |
| `POST /query/journey/stream` | SSE stream — events: `metadata`, `token`, `done`, `error` |

`JourneyHealthOrchestrator` is initialized once at startup via `lifespan`; if init fails, both endpoints return `503`.

### Streamlit UI (`app.py`)

The sidebar configures: API URL (defaults to `http://localhost:8000`), journey IDs, application ID, project ID, range type, and optional start/end time overrides. The chat input is disabled until all three required fields are set.

The streaming response uses a two-phase protocol:
1. **Phase 1** — waits for the `metadata` SSE event (arrives after Watermelon API fetch completes)
2. **Phase 2** — streams `token` events into the chat bubble via `st.write_stream`

## Key conventions

- `QueryRequest.query` is the natural language input. `journey_ids` (required) are passed to the weak-link endpoint. `start_time`/`end_time` are optional overrides used only when the query contains no time expression — query-derived timestamps always win.
- `QueryRequest.timezone` is an IANA timezone name (e.g. `America/New_York`), defaulting to `UTC`. It controls how wall-clock expressions ("yesterday", "this morning", "between 3pm and 5pm") are interpreted. Returned timestamps are always UTC milliseconds regardless of this value. Echoed back in `TimeResolution.timezone`.
- Time values are Unix epoch **milliseconds** (not seconds).
- The API param is `range`; the Python adapter parameter is `range_type` (avoids shadowing the built-in). FastAPI receives `range` from the gateway and passes it as `range_type=range` to the adapter.
- Minimum 2-hour window is enforced by `_resolve_timestamps`: if the resolved window is shorter, `start` is shifted back to `end - 2h`.
- All config is read via `config.py` which loads `.env` at import time.
- `requirements.txt` includes `dateparser` but it is not imported anywhere — only `python-dateutil` (`dateutil`) is used.
