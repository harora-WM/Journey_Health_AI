from pydantic import BaseModel, Field
from typing import Any, Dict, Optional


# ── Request ────────────────────────────────────────────────────────────────────

class QueryRequest(BaseModel):
    application_id: int = Field(..., description="Application ID")
    project_id: int = Field(..., description="Project ID")
    start_time: int = Field(..., description="Start time in Unix epoch milliseconds")
    end_time: int = Field(..., description="End time in Unix epoch milliseconds")
    range: str = Field(..., description="Time range type (e.g. CUSTOM)")


# ── Sub-models ─────────────────────────────────────────────────────────────────

class ResponseMetadata(BaseModel):
    model: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None


# ── Response models ────────────────────────────────────────────────────────────

class QueryResponse(BaseModel):
    success: bool
    data: Dict[str, Any]
    conversational_response: str
    response_metadata: ResponseMetadata


class ErrorResponse(BaseModel):
    success: bool = False
    error: str
    detail: Optional[str] = None
