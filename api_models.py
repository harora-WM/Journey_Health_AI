from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional


# ── Request ────────────────────────────────────────────────────────────────────

class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000,
                       description="Natural language query to get insights from Journey Health data")
    journey_ids: List[int] = Field(..., description="Journey IDs to include in the weak-link analysis")
    application_id: int = Field(..., description="Application ID")
    project_id: int = Field(..., description="Project ID")
    start_time: Optional[int] = Field(default=None, description="Start time in Unix epoch milliseconds. Only used when the query contains no time reference; ignored if the query mentions a time expression.")
    end_time: Optional[int] = Field(default=None, description="End time in Unix epoch milliseconds. Only used when the query contains no time reference; ignored if the query mentions a time expression.")
    range: str = Field(..., description="Time range type (e.g. CUSTOM)")
    timezone: Optional[str] = Field(default="UTC", description="IANA timezone name (e.g. 'America/New_York'). Defaults to 'UTC'.")


# ── Sub-models ─────────────────────────────────────────────────────────────────

class TimeResolution(BaseModel):
    start_time: Optional[int] = None
    end_time: Optional[int] = None
    time_range: Optional[str] = None
    effective_time_range: Optional[str] = None
    source: Optional[str] = None
    timezone: Optional[str] = None


class ResponseMetadata(BaseModel):
    model: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None


# ── Response models ────────────────────────────────────────────────────────────

class QueryResponse(BaseModel):
    success: bool
    query: str
    time_resolution: TimeResolution
    data: Dict[str, Any]
    conversational_response: str
    response_metadata: ResponseMetadata


class ErrorResponse(BaseModel):
    success: bool = False
    error: str
    query: Optional[str] = None
    detail: Optional[str] = None
