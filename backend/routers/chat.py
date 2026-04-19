"""Chat endpoints for AI interaction."""

import os
import json
import asyncio
from collections.abc import AsyncIterator
from typing import Any
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from ollama import AsyncClient
from models.chat import ChatRequest, ChatResponseData

router = APIRouter(prefix="/chat", tags=["chat"])

MODEL_NAME = "gemma4:26b"
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
ollama_client = AsyncClient(host=OLLAMA_HOST)


def _sse_event(event: str, data: dict[str, Any]) -> str:
    """Format one SSE event block."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@router.post("")
async def chat_with_gemma(payload: ChatRequest) -> dict[str, Any]:
    """Run a direct chat completion against gemma4:26b through Ollama."""
    try:
        messages = [message.model_dump() for message in payload.messages]
        ollama_response = await ollama_client.chat(
            model=MODEL_NAME,
            messages=messages,
            options={"temperature": payload.temperature},
        )

        if isinstance(ollama_response, dict):
            message_block = ollama_response.get("message", {})
            content = message_block.get("content", "") if isinstance(message_block, dict) else ""
        else:
            message_block = getattr(ollama_response, "message", None)
            content = getattr(message_block, "content", "") if message_block is not None else ""

        response_data = ChatResponseData(model=MODEL_NAME, response=content.strip())
        return {
            "success": True,
            "data": response_data.model_dump(),
            "error": None,
        }
    except Exception as exc:
        return {
            "success": False,
            "data": None,
            "error": f"Chat request failed: {exc}",
        }


@router.post("/stream")
async def chat_with_gemma_stream(payload: ChatRequest) -> StreamingResponse:
    """Stream chat response with SSE events for thinking and token updates."""

    async def event_generator() -> AsyncIterator[str]:
        try:
            yield _sse_event("thinking", {"text": "Gemma4 is thinking..."})
            await asyncio.sleep(0)

            messages = [message.model_dump() for message in payload.messages]
            stream = await ollama_client.chat(
                model=MODEL_NAME,
                messages=messages,
                options={"temperature": payload.temperature},
                stream=True,
            )

            has_streamed_token = False
            async for chunk in stream:
                if isinstance(chunk, dict):
                    message_block = chunk.get("message", {})
                    token = message_block.get("content", "") if isinstance(message_block, dict) else ""
                else:
                    message_block = getattr(chunk, "message", None)
                    token = getattr(message_block, "content", "") if message_block is not None else ""

                if isinstance(token, str) and token:
                    has_streamed_token = True
                    yield _sse_event("token", {"token": token})
                    await asyncio.sleep(0)

            yield _sse_event(
                "done",
                {
                    "model": MODEL_NAME,
                    "response_started": has_streamed_token,
                },
            )
            await asyncio.sleep(0)
        except Exception as exc:
            yield _sse_event("error", {"message": f"Chat stream failed: {exc}"})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
