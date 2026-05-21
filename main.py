#!/usr/bin/env python3
"""
Journey Health Orchestrator
Coordinates time resolution, Journey Health data fetching, and LLM response generation.

Pipeline per query:
  1. TimestampResolver      — extract start/end from user query, API input, or 2-hour fallback
  2. JourneyHealth_Adapter  — fetch weak-link and summary (ERROR + RESPONSE) data
  3. LLMResponseGenerator   — produce a conversational answer
"""

import json
import logging
import traceback
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

import config
from timestamp import TimestampResolver
from JourneyHealth_Adapter import get_data as fetch_journey_health
from llm_response_generator import LLMResponseGenerator
from api_models import QueryRequest, QueryResponse, ErrorResponse


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class JourneyHealthOrchestrator:
    """
    Orchestrates the three-step pipeline:
      time resolution → adapter fetch → LLM response
    """

    TWO_HOURS_MS = 2 * 60 * 60 * 1000

    def __init__(self):
        self.username = config.USERNAME
        self.password = config.PASSWORD

        print("Initializing Timestamp Resolver...")
        self.timestamp_resolver = TimestampResolver()
        print("✅ Timestamp Resolver ready")

        print("Initializing LLM Response Generator...")
        self.response_generator = LLMResponseGenerator()
        print("✅ LLM Response Generator ready\n")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_timestamps(
        self,
        user_query: str,
        api_start: Optional[int],
        api_end: Optional[int],
    ) -> tuple[int, int, str, str]:
        """
        Resolve start/end timestamps from the user query.

        Priority:
          - Query-derived timestamps always win when the query mentions time.
          - api_start / api_end are used only when no time expression was found.
          - A minimum 2-hour window is enforced by shifting start backwards.

        Returns:
            (start_time_ms, end_time_ms, effective_time_range_label, source)
        """
        resolution = self.timestamp_resolver.resolve_time_range(user_query)
        primary_range = resolution.get('primary_range', {})
        source = resolution.get('source', 'fallback')

        if source != 'fallback':
            start = primary_range.get('start_time')
            end = primary_range.get('end_time')
        else:
            start = api_start or primary_range.get('start_time')
            end = api_end or primary_range.get('end_time')

        # Enforce minimum 2-hour gap
        if start is not None and end is not None:
            if (end - start) < self.TWO_HOURS_MS:
                start = end - self.TWO_HOURS_MS

        # Build human-readable label
        if start is not None and end is not None:
            dur_secs = (end - start) / 1000
            if dur_secs < 3600:
                v = round(dur_secs / 60)
                label = f"last {v} minute{'s' if v != 1 else ''}"
            elif dur_secs < 86400:
                v = dur_secs / 3600
                label = f"last {v:.0f} hour{'s' if v != 1 else ''}"
            else:
                v = dur_secs / 86400
                label = f"last {v:.0f} day{'s' if v != 1 else ''}"
        else:
            label = primary_range.get('time_range', 'unknown window')

        return start, end, label, source

    def _prepare_context(
        self,
        user_query: str,
        journey_ids: List[int],
        application_id: int,
        project_id: int,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        range: str = "CUSTOM",
    ) -> Dict[str, Any]:
        """Steps 1 and 2: resolve timestamps then fetch Journey Health data."""

        print("=" * 80)
        print("JOURNEY HEALTH ORCHESTRATOR — Processing Query")
        print("=" * 80)
        print(f"\n📝 Query: {user_query}\n")

        # ── Step 1: Time resolution ─────────────────────────────────────────
        print("🕐 Step 1: Resolving time range...")
        start, end, effective_time_range, ts_source = self._resolve_timestamps(
            user_query, start_time, end_time
        )
        print(f"   Source : {ts_source}")
        print(f"   Window : {effective_time_range}  ({start} → {end})\n")

        # ── Step 2: Fetch Journey Health data ───────────────────────────────
        print("📊 Step 2: Fetching Journey Health data...")
        journey_health_data = fetch_journey_health(
            journey_ids=journey_ids,
            application_id=application_id,
            project_id=project_id,
            start_time=start,
            end_time=end,
            username=self.username,
            password=self.password,
            range_type=range,
        )

        if not journey_health_data:
            return {
                "success": False,
                "error": "Journey Health adapter returned no data",
                "query": user_query,
            }
        print("   ✅ Journey Health data retrieved\n")

        print("=" * 80)
        print("✅ CONTEXT PREPARATION COMPLETE")
        print("=" * 80 + "\n")

        return {
            "success": True,
            "query": user_query,
            "time_resolution": {
                "start_time": start,
                "end_time": end,
                "time_range": user_query,
                "effective_time_range": effective_time_range,
                "source": ts_source,
            },
            "data": journey_health_data,
        }

    def _llm_input(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """Merge journey health data with time_resolution for the LLM prompt builder."""
        return {**result['data'], 'time_resolution': result['time_resolution']}

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def process_query(
        self,
        user_query: str,
        journey_ids: List[int],
        application_id: int,
        project_id: int,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        range: str = "CUSTOM",
    ) -> Dict[str, Any]:
        """Full blocking pipeline: time resolution → fetch → LLM response."""
        result = self._prepare_context(user_query, journey_ids, application_id, project_id, start_time, end_time, range)
        if not result.get("success"):
            return result

        conversational = self.response_generator.generate_response(
            user_query=user_query,
            orchestrator_output=self._llm_input(result),
        )
        result["conversational_response"] = conversational.get("response", "")
        result["response_metadata"] = conversational.get("metadata", {})
        return result

    def process_query_stream(
        self,
        user_query: str,
        journey_ids: List[int],
        application_id: int,
        project_id: int,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        range: str = "CUSTOM",
    ):
        """
        Full streaming pipeline: time resolution → fetch → LLM token stream.

        Yields (event_type, payload) tuples:
            ("error",    detail_str)
            ("metadata", result_dict)
            ("token",    text_chunk)
            ("done",     full_text)
        """
        result = self._prepare_context(user_query, journey_ids, application_id, project_id, start_time, end_time, range)
        if not result.get("success"):
            yield ("error", result.get("error", "Orchestrator returned failure"))
            return

        yield ("metadata", result)

        full_text = ""
        for chunk in self.response_generator.generate_response_stream(
            user_query, self._llm_input(result)
        ):
            full_text += chunk
            yield ("token", chunk)

        yield ("done", full_text)


# ---------------------------------------------------------------------------
# FastAPI Application
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("journey_health_api")

_orchestrator: Optional[JourneyHealthOrchestrator] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _orchestrator
    logger.info("Initializing JourneyHealthOrchestrator...")
    try:
        _orchestrator = JourneyHealthOrchestrator()
        logger.info("JourneyHealthOrchestrator ready")
    except Exception as exc:
        logger.error(f"Failed to initialize orchestrator: {exc}")
        _orchestrator = None
    yield
    logger.info("Shutting down Journey Health API")


app = FastAPI(
    title="Journey Health Advisor API",
    description="Journey Health analysis powered by weak-link and summary data from AWS Bedrock",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=ErrorResponse(
            error="Internal server error",
            detail=traceback.format_exc()[-500:],
        ).model_dump(),
    )


@app.get("/")
def root():
    return {"status": "ok"}


@app.post(
    "/query/journey",
    response_model=QueryResponse,
    responses={
        400: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
    tags=["journey_health"],
)
def run_query(body: QueryRequest):
    """Fetch Journey Health data and return a conversational health analysis."""
    if _orchestrator is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Orchestrator not initialized",
        )
    logger.info(
        f"Query: {body.query!r}, journey_ids={body.journey_ids}, application_id={body.application_id}, "
        f"project_id={body.project_id}, start_time={body.start_time}, end_time={body.end_time}, range={body.range}"
    )
    result = _orchestrator.process_query(
        user_query=body.query,
        journey_ids=body.journey_ids,
        application_id=body.application_id,
        project_id=body.project_id,
        start_time=body.start_time,
        end_time=body.end_time,
        range=body.range,
    )
    if not result.get("success"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=result.get("error", "Orchestrator returned failure"),
        )
    print("✅ Query response complete")
    return result


@app.post("/query/journey/stream", tags=["journey_health"])
def run_query_stream(body: QueryRequest):
    """
    Fetch Journey Health data and stream a conversational health analysis (SSE).

    Event types:
    - `metadata` — sent once after data fetch; contains the full data payload
    - `token`    — one per LLM text chunk
    - `done`     — sent when generation is complete; contains full_text
    - `error`    — sent if anything fails
    """
    if _orchestrator is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Orchestrator not initialized",
        )
    logger.info(
        f"Stream query: {body.query!r}, journey_ids={body.journey_ids}, application_id={body.application_id}, "
        f"project_id={body.project_id}, range={body.range}"
    )

    def event_stream():
        try:
            for event_type, payload in _orchestrator.process_query_stream(
                user_query=body.query,
                journey_ids=body.journey_ids,
                application_id=body.application_id,
                project_id=body.project_id,
                start_time=body.start_time,
                end_time=body.end_time,
                range=body.range,
            ):
                if event_type == "error":
                    yield f"data: {json.dumps({'type': 'error', 'detail': payload})}\n\n"
                elif event_type == "metadata":
                    yield f"data: {json.dumps({'type': 'metadata', 'data': payload}, default=str)}\n\n"
                elif event_type == "token":
                    yield f"data: {json.dumps({'type': 'token', 'text': payload})}\n\n"
                elif event_type == "done":
                    yield f"data: {json.dumps({'type': 'done', 'full_text': payload})}\n\n"
                    print("✅ Streaming response complete")
        except Exception as e:
            logger.error(f"Streaming error: {e}")
            yield f"data: {json.dumps({'type': 'error', 'detail': str(e)})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, workers=1)
