# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env .env.local   # or create .env from scratch — see Required env vars below
```

### Required env vars (`.env`)

All variables are mandatory — `config.py` will crash at import time if any numeric ones are missing.

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
| `JOURNEY_HEALTH_API_URL` | Full performance endpoint URL |
| `JOURNEY_HEALTH_RANGE_TYPE` | Default range type, e.g. `CUSTOM` |
| `WM_USERNAME` | Watermelon sandbox username |
| `WM_PASSWORD` | Watermelon sandbox password |

## Running the adapter standalone

```bash
source venv/bin/activate
python JourneyHealth_Adapter.py
```

Output is written to `journey_health_output.json`.

## Running the FastAPI server

```bash
source venv/bin/activate
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
```

Interactive API docs available at `http://localhost:8000/docs` once running.

## Running the Streamlit UI

```bash
source venv/bin/activate
streamlit run app.py
```

The UI defaults to `http://localhost:8000` but the API URL is configurable in the sidebar.

## Architecture

Four-step pipeline per query:

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
| `JourneyHealth_Adapter.py` | Fetches user journey performance records from Watermelon API |
| `timestamp.py` | Converts natural language time expressions to UTC ms timestamps; deterministic regex → LLM fallback → 2-hour hard fallback |
| `config.py` | Single source of truth for all env vars — import this, never `os.getenv()` directly |
| `llm_response_generator.py` | Wraps AWS Bedrock Claude; builds system prompt and calls the model |
| `main.py` | FastAPI app + `JourneyHealthOrchestrator`; exposes `/query/journey` and `/query/journey/stream` |
| `api_models.py` | Pydantic models: `QueryRequest`, `QueryResponse`, `TimeResolution`, `ErrorResponse` |
| `app.py` | Streamlit chat UI; streams SSE tokens from the FastAPI backend |

### Adapter internals (`JourneyHealth_Adapter.py`)

- **`get_access_token()`** — Keycloak OAuth2 password grant → Bearer token
- **`fetch_journey_health_data()`** — GET to the journey performance endpoint with token + query params
- **`get_data()`** — public entry point; returns `{data_source, filters, records, fetched_at}`

**Auth**: Keycloak at `wmsandbox5-auth.watermelon.us`, client `web_app`, password grant flow.
**API**: `wmsandbox5.watermelon.us/.../user-journeys/performance`, filtered by `application_id`, `project_id`, `start_time`/`end_time` (Unix ms), and `range` (default `CUSTOM`).
**SSL**: `verify=False` throughout — sandbox environment only.

### LLM response generator (`llm_response_generator.py`)

`LLMResponseGenerator` wraps Bedrock with two call paths:
- `generate_response(user_query, orchestrator_output)` — blocking; returns `{success, user_query, response, metadata}`
- `generate_response_stream(user_query, orchestrator_output)` — yields raw text chunks via `invoke_model_with_response_stream`

The system prompt encodes the full Journey Health data schema, health status definitions (HEALTHY / AT_RISK / UNHEALTHY), burn rate severity thresholds, and a mandatory **Optimization Roadmap** section that ranks transactions by P1 (dual-breach) → P2 (EB only) → P3 (latency only).

### FastAPI endpoints (`main.py`)

| Endpoint | Description |
|---|---|
| `GET /` | Health check |
| `POST /query/journey` | Blocking — returns full `QueryResponse` JSON |
| `POST /query/journey/stream` | SSE stream — events: `metadata`, `token`, `done`, `error` |

The `JourneyHealthOrchestrator` is initialized once at startup via `lifespan`; if init fails, both endpoints return `503`.

## Key conventions

- `QueryRequest.query` is the natural language input; `start_time`/`end_time` are optional overrides used only when the query contains no time expression.
- Time values are Unix epoch **milliseconds** (not seconds).
- The API param is `range`; the Python adapter parameter is `range_type` (avoids shadowing the built-in). FastAPI receives `range` from the gateway and passes it as `range_type=range` to the adapter.
- `TimestampResolver` resolution priority: deterministic regex → LLM (Bedrock) fallback → 2-hour hard fallback. The `source` field in `TimeResolution` records which path was taken (`deterministic`, `llm`, `fallback`).
- The API may return a single object or a list; `fetch_journey_health_data` normalizes both to a list.
- All config is read via `config.py` which loads `.env` at import time.
