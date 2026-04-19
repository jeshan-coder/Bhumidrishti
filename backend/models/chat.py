"""Chat-related Pydantic models for AI interaction."""

from typing import Literal
from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    """Single message in a chat conversation."""

    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1)


class ChatRequest(BaseModel):
    """Request payload for chat endpoints."""

    messages: list[ChatMessage] = Field(min_length=1)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)


class ChatResponseData(BaseModel):
    """Response data for successful chat completion."""

    model: str
    response: str
