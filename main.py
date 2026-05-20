#!/usr/bin/env python3
"""
Journey Health Orchestrator
Coordinates Journey Health data fetching and LLM response generation.

Pipeline per request:
  1. JourneyHealth_Adapter — fetch user journey performance data
  2. LLMResponseGenerator  — produce a conversational answer
"""

import json
import logging
import traceback
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

import config
from JourneyHealth_Adapter import get_data as fetch_journey_health
from llm_response_generator import LLMResponseGenerator
from api_models import QueryRequest, QueryResponse, ErrorResponse


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class JourneyHealthOrchestrator:
    """
    Orchestrates the two-step pipeline: adapter fetch → LLM response.
    Timestamps and range are always supplied by the caller.
    """

    def __init__(self):
        self.username = config.USERNAME
        self.password = config.PASSWORD

        print("Initializing LLM Response Generator...")
        self.response_generator = LLMResponseGenerator()
        print("✅ LLM Response Generator ready\n")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _prepare_context(
        self,
        application_id: int,
        project_id: int,
        start_time: int,
        end_time: int,
        range: str,
    ) -> Dict[str, Any]:
        """Fetch Journey Health data for the given time window."""

        print("=" * 80)
        print("JOURNEY HEALTH ORCHESTRATOR — Processing Request")
        print("=" * 80)
        print(f"   Window : {start_time} → {end_time}  |  Range : {range}\n")

        print("📊 Fetching Journey Health data...")
        journey_health_data = fetch_journey_health(
            application_id=application_id,
            project_id=project_id,
            start_time=start_time,
            end_time=end_time,
            username=self.username,
            password=self.password,
            range_type=range,
        )

        if not journey_health_data:
            return {
                "success": False,
                "error": "Journey Health adapter returned no data",
            }
        print("   ✅ Journey Health data retrieved\n")

        print("=" * 80)
        print("✅ CONTEXT PREPARATION COMPLETE")
        print("=" * 80 + "\n")

        return {
            "success": True,
            "data": journey_health_data,
        }

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def process_query(
        self,
        application_id: int,
        project_id: int,
        start_time: int,
        end_time: int,
        range: str,
    ) -> Dict[str, Any]:
        """Full blocking pipeline: fetch → LLM response."""
        result = self._prepare_context(application_id, project_id, start_time, end_time, range)
        if not result.get("success"):
            return result

        conversational = self.response_generator.generate_response(
            orchestrator_output=result['data'],
        )
        result["conversational_response"] = conversational.get("response", "")
        result["response_metadata"] = conversational.get("metadata", {})
        return result

    def process_query_stream(
        self,
        application_id: int,
        project_id: int,
        start_time: int,
        end_time: int,
        range: str,
    ):
        """
        Full streaming pipeline: fetch → LLM token stream.

        Yields (event_type, payload) tuples:
            ("error",    detail_str)
            ("metadata", result_dict)
            ("token",    text_chunk)
            ("done",     full_text)
        """
        result = self._prepare_context(application_id, project_id, start_time, end_time, range)
        if not result.get("success"):
            yield ("error", result.get("error", "Orchestrator returned failure"))
            return

        yield ("metadata", result)

        full_text = ""
        for chunk in self.response_generator.generate_response_stream(result['data']):
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
    description="Journey Health analysis powered by user journey performance data and AWS Bedrock",
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
        f"application_id={body.application_id}, project_id={body.project_id}, "
        f"start_time={body.start_time}, end_time={body.end_time}, range={body.range}"
    )
    result = _orchestrator.process_query(
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
        f"application_id={body.application_id}, project_id={body.project_id}, "
        f"start_time={body.start_time}, end_time={body.end_time}, range={body.range}"
    )

    def event_stream():
        try:
            for event_type, payload in _orchestrator.process_query_stream(
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
