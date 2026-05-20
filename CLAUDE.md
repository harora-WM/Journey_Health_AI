# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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

## Running the Streamlit UI

```bash
source venv/bin/activate
streamlit run app.py
```

## Architecture

Three-layer pipeline per request:

```
app.py (Streamlit UI)
    → POST /query/journey/stream  (FastAPI — main.py)
        → JourneyHealthOrchestrator._prepare_context()
            → JourneyHealth_Adapter.get_data()       # fetches from Watermelon API
        → LLMResponseGenerator.generate_response()   # calls AWS Bedrock Claude
```

### Key files

| File | Role |
|---|---|
| `JourneyHealth_Adapter.py` | Fetches user journey performance records from Watermelon API |
| `config.py` | Single source of truth for all env vars — import this, never `os.getenv()` directly |
| `llm_response_generator.py` | Wraps AWS Bedrock Claude; builds system prompt and calls the model |
| `main.py` | FastAPI app + `JourneyHealthOrchestrator`; exposes `/query/journey` and `/query/journey/stream` |
| `api_models.py` | Pydantic request/response models (`QueryRequest`, `QueryResponse`, `ErrorResponse`) |
| `app.py` | Streamlit chat UI; streams SSE tokens from the FastAPI backend |

### Adapter internals (`JourneyHealth_Adapter.py`)

- **`get_access_token()`** — Keycloak OAuth2 password grant → Bearer token
- **`fetch_journey_health_data()`** — GET to the journey performance endpoint with token + query params
- **`get_data()`** — public entry point; returns `{data_source, filters, records, fetched_at}`

**Auth**: Keycloak at `wmsandbox5-auth.watermelon.us`, client `web_app`, password grant flow.
**API**: `wmsandbox5.watermelon.us/.../user-journeys/performance`, filtered by `application_id`, `project_id`, `start_time`/`end_time` (Unix ms), and `range` (default `CUSTOM`).
**SSL**: `verify=False` throughout — sandbox environment only.

## Key conventions

- Time values are Unix epoch **milliseconds** (not seconds).
- The API param is `range`; the Python adapter parameter is `range_type` (avoids shadowing the built-in). FastAPI receives `range` from the gateway and passes it as `range_type=range` to the adapter.
- The API may return a single object or a list; `fetch_journey_health_data` normalizes both to a list.
- All config is read via `config.py` which loads `.env` at import time.
