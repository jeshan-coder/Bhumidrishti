"""Chat endpoints for AI interaction."""

import os
import json
import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from ollama import AsyncClient
from db.postgres import get_pool
from models.chat import ChatRequest, ChatResponseData
from prompts.base_system_prompt import build_bhumidrishti_system_prompt
from services.ai_runtime import ACTIVE_GEMMA_MODEL
from services.gemma_pipeline import CHAT_TOOLS, dispatch_tool
from services.tools import resolve_tool_name, parse_colon_tool_call

router = APIRouter(prefix="/chat", tags=["chat"])

MODEL_NAME = ACTIVE_GEMMA_MODEL
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
ollama_client = AsyncClient(host=OLLAMA_HOST)

# This variable stores the module logger for chat endpoint debugging.
logger = logging.getLogger(__name__)

# This variable stores the chat and tool-agent addendum appended to the shared BhumiDrishti base prompt.
CHAT_AGENT_SYSTEM_ADDENDUM = """
You are a disaster response assistant. Answer the user's question directly. Use tools to fetch live data.

TOOL ROUTING — follow these rules every time:
1. User mentions "assessment", "ASS-XXXXX id", severity, damage_type, structural_risk, site name, worker, status, or asks to list/filter buildings → call get_assessments.
2. User provides NEW coordinates (lat/lon), OSM ID, or a map polygon to look up one specific building → call get_building_info.
3. User asks about sites (list all, one site, how many, site counts, site status) → call get_sites.
4. User asks about field teams or workers → call get_field_teams.
5. Never reuse coordinates from earlier in the conversation for a new question.
6. Never invent tool names. Only valid names: get_building_info, get_assessments, get_sites, get_field_teams, dispatch_assessments, update_assessment_status.

get_assessments — key parameters:
- assessment_id="ASS-XXXXX"  → exact single record lookup
- severity=N (exact) | severity_min/max/gt/lt for ranges
- damage_type, structural_risk, site_name, status, worker_name, response_team
- recommended_action — use for "immediate search rescue" → "immediate_search_rescue", "urgent evacuation" → "urgent_evacuation", "structural assessment" → "structural_assessment"
- occupant_status — "trapped", "potentially_trapped", "evacuated", "unknown"
- include_geometry=true (default, keeps map overlay)
- limit=N (default 20, increase to 100+ when user wants all results)
- single=true ONLY when user asks for exactly one record — NEVER for plural queries like "show me buildings…"

get_building_info — only with user-provided data from current message:
- lat + lon  (always "lon", not "lng")  |  osm_id  |  geometry (GeoJSON)

get_sites — key rules:
- "all sites", "list sites", "what sites", "available sites", "every site" → call with NO filters at all (empty args)
- "site named X" or "the X site" → site_name="X" (partial match)
- "site ID 5" → site_id=5
- NEVER add status="active" unless the user explicitly says the word "active"
- "available", "existing", "recorded" are NOT status filters — they mean list everything
- Sites can have status "active", "processing", or "completed" — omitting status returns all of them

Dispatch rules: never assign to a busy team; ask which team if not specified.
Output: plain language answers; no raw JSON unless user asks.
""".strip()

# This variable stores the system prompt used by text chat and tool-agent endpoints.
CHAT_SYSTEM_PROMPT = build_bhumidrishti_system_prompt(CHAT_AGENT_SYSTEM_ADDENDUM)


def _sse_event(event: str, data: dict[str, Any]) -> str:
    """Format one SSE event block."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


# Maximum characters for any single assistant message in chat history.
# Long listing responses (20 buildings, etc.) will be trimmed to prevent
# the Gemma context window from filling up before the model can respond.
_MAX_HISTORY_ASSISTANT_CHARS = 1800


def _trim_history_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Truncate overly long assistant messages in history to avoid context overflow.

    Only trims messages that are not the last one — the most recent exchange is
    kept intact so the model has full context for the current turn.
    """
    if len(messages) <= 1:
        return messages

    trimmed = []
    for i, msg in enumerate(messages):
        is_last = i == len(messages) - 1
        role = msg.get("role", "")
        content = msg.get("content", "")
        if not is_last and role == "assistant" and isinstance(content, str) and len(content) > _MAX_HISTORY_ASSISTANT_CHARS:
            truncated = content[:_MAX_HISTORY_ASSISTANT_CHARS]
            msg = {**msg, "content": truncated + "\n[...response truncated in history]"}
            logger.debug(
                "chat.history.assistant_truncated original_chars=%s truncated_to=%s",
                len(content), _MAX_HISTORY_ASSISTANT_CHARS,
            )
        trimmed.append(msg)
    return trimmed


def _build_messages(payload: ChatRequest) -> list[dict[str, Any]]:
    """Build chat messages with the default system prompt prepended."""
    incoming_messages = [message.model_dump() for message in payload.messages]
    incoming_messages = _trim_history_messages(incoming_messages)
    return [{"role": "system", "content": CHAT_SYSTEM_PROMPT}, *incoming_messages]


def _message_field(message_block: Any, field_name: str) -> Any:
    """Read a field from either dict-style or object-style message blocks."""
    if isinstance(message_block, dict):
        return message_block.get(field_name)

    return getattr(message_block, field_name, None)


@router.post("")
async def chat_with_gemma(payload: ChatRequest) -> dict[str, Any]:
    """Run a direct chat completion against the active Gemma model through Ollama."""
    try:
        logger.info("chat.request.started messages=%s temperature=%s", len(payload.messages), payload.temperature)
        messages = _build_messages(payload)
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
        logger.info(
            "chat.request.completed model=%s response_chars=%s",
            MODEL_NAME,
            len(response_data.response),
        )
        return {
            "success": True,
            "data": response_data.model_dump(),
            "error": None,
        }
    except Exception as exc:
        logger.exception("chat.request.failed error=%s", exc)
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
            logger.info("chat.stream.started messages=%s temperature=%s", len(payload.messages), payload.temperature)

            messages = _build_messages(payload)
            has_streamed_token = False
            max_iterations = 5
            iteration = 0
            force_answer_without_tools = False

            # This variable stores database pool used for optional tool execution.
            db_pool = get_pool()

            while iteration < max_iterations:
                iteration += 1
                logger.info("chat.stream.iteration.started iteration=%s", iteration)
                chat_kwargs: dict[str, Any] = {
                    "model": MODEL_NAME,
                    "messages": messages,
                    "options": {
                        "temperature": payload.temperature,
                        "num_ctx": 32768,   # explicit context window; prevents KV-cache bleed from prior sessions
                    },
                    "stream": True,
                }
                if not force_answer_without_tools:
                    chat_kwargs["tools"] = CHAT_TOOLS
                stream = await ollama_client.chat(**chat_kwargs)

                assistant_content_parts: list[str] = []
                aggregated_tool_calls: list[dict[str, Any]] = []

                async for chunk in stream:
                    if isinstance(chunk, dict):
                        message_block = chunk.get("message")
                    else:
                        message_block = getattr(chunk, "message", None)

                    if message_block is None:
                        continue

                    thinking_chunk = _message_field(message_block, "thinking")
                    if isinstance(thinking_chunk, str) and thinking_chunk:
                        yield _sse_event("thinking", {"text": thinking_chunk, "mode": "append"})
                        await asyncio.sleep(0)

                    token = _message_field(message_block, "content")
                    if isinstance(token, str) and token:
                        has_streamed_token = True
                        assistant_content_parts.append(token)
                        yield _sse_event("token", {"token": token})
                        await asyncio.sleep(0)

                    tool_calls_chunk = _message_field(message_block, "tool_calls")
                    if isinstance(tool_calls_chunk, list):
                        for tool_call in tool_calls_chunk:
                            if isinstance(tool_call, dict):
                                function_block = tool_call.get("function")
                            else:
                                function_block = getattr(tool_call, "function", None)

                            if function_block is None:
                                continue

                            if isinstance(function_block, dict):
                                tool_name = function_block.get("name")
                                raw_arguments = function_block.get("arguments")
                            else:
                                tool_name = getattr(function_block, "name", None)
                                raw_arguments = getattr(function_block, "arguments", None)

                            tool_args: dict[str, Any] = {}

                            if isinstance(raw_arguments, dict):
                                tool_args = raw_arguments
                            elif isinstance(raw_arguments, str):
                                try:
                                    parsed_arguments = json.loads(raw_arguments)
                                    if isinstance(parsed_arguments, dict):
                                        tool_args = parsed_arguments
                                except json.JSONDecodeError:
                                    tool_args = {}

                            if isinstance(tool_name, str) and tool_name:
                                raw_tool_name = tool_name

                                # Step 1: resolve known name aliases.
                                canonical_name = resolve_tool_name(tool_name)
                                if canonical_name != tool_name:
                                    logger.warning(
                                        "chat.stream.tool_call.alias_resolved iteration=%s hallucinated=%s canonical=%s",
                                        iteration, tool_name, canonical_name,
                                    )
                                tool_name = canonical_name

                                # Step 2: parse colon-embedded params (e.g. get_assessments:severity:5 → args={severity:5}).
                                tool_name, tool_args = parse_colon_tool_call(tool_name, tool_args)
                                if tool_name != canonical_name:
                                    logger.warning(
                                        "chat.stream.tool_call.colon_parsed iteration=%s original=%s canonical=%s args=%s",
                                        iteration, raw_tool_name, tool_name, tool_args,
                                    )

                                logger.info(
                                    "chat.stream.tool_call.detected iteration=%s raw_name=%s resolved_name=%s args=%s",
                                    iteration,
                                    raw_tool_name,
                                    tool_name,
                                    tool_args,
                                )
                                aggregated_tool_calls.append(
                                    {
                                        "function": {
                                            "name": tool_name,
                                            "arguments": tool_args,
                                        }
                                    }
                                )
                                yield _sse_event(
                                    "tool_call",
                                    {
                                        "name": tool_name,
                                        "arguments": tool_args,
                                    },
                                )
                                await asyncio.sleep(0)

                assistant_content = "".join(assistant_content_parts)
                messages.append(
                    {
                        "role": "assistant",
                        "content": assistant_content,
                        "tool_calls": aggregated_tool_calls,
                    }
                )
                logger.info(
                    "chat.stream.iteration.completed iteration=%s token_chars=%s tool_calls=%s",
                    iteration,
                    len(assistant_content),
                    len(aggregated_tool_calls),
                )

                if aggregated_tool_calls:
                    if db_pool is None:
                        logger.error("chat.stream.db_pool_missing")
                        yield _sse_event("error", {"message": "Database pool is not initialized for tool execution"})
                        return

                    for tool_call in aggregated_tool_calls:
                        function_block = tool_call.get("function", {})
                        tool_name = function_block.get("name")
                        tool_args = function_block.get("arguments", {})

                        if not isinstance(tool_name, str):
                            logger.warning("chat.stream.tool_call.invalid_name tool_call=%s", tool_call)
                            continue
                        if not isinstance(tool_args, dict):
                            tool_args = {}

                        logger.info("chat.stream.tool_call.executing tool=%s args=%s", tool_name, tool_args)
                        tool_result = await dispatch_tool(tool_name, tool_args, db_pool)
                        filters_applied = tool_result.get("filters_applied") if isinstance(tool_result, dict) else None
                        count = tool_result.get("count") if isinstance(tool_result, dict) else None
                        success = tool_result.get("success") if isinstance(tool_result, dict) else None
                        logger.info(
                            "chat.stream.tool_call.completed tool=%s success=%s count=%s filters_applied=%s",
                            tool_name, success, count, filters_applied,
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "content": json.dumps(tool_result, ensure_ascii=False),
                            }
                        )

                        yield _sse_event(
                            "tool_result",
                            {
                                "name": tool_name,
                                "result": tool_result,
                            },
                        )
                        await asyncio.sleep(0)

                    # If every tool call in this iteration returned an error (unknown tool name,
                    # etc.), allow one more iteration so the model can retry with the correct name.
                    all_tool_errors = all(
                        isinstance(db_pool, object)
                        and isinstance(
                            messages[-len(aggregated_tool_calls) + i]
                            if i < len(aggregated_tool_calls) else {},
                            dict,
                        )
                        for i in range(len(aggregated_tool_calls))
                    )
                    # Re-derive from the tool messages we just appended.
                    tool_messages_this_iter = [
                        m for m in messages
                        if m.get("role") == "tool"
                    ][-len(aggregated_tool_calls):]
                    all_tool_errors = all(
                        '"error"' in m.get("content", "") and '"success"' not in m.get("content", "")
                        for m in tool_messages_this_iter
                    )

                    if all_tool_errors and not force_answer_without_tools:
                        logger.warning(
                            "chat.stream.all_tool_errors iteration=%s — injecting correction, allowing retry",
                            iteration,
                        )
                        messages.append({
                            "role": "system",
                            "content": (
                                "All your tool calls failed because you used incorrect tool names. "
                                "Valid tool names are exactly: get_assessments, get_building_info, "
                                "get_sites, get_field_teams, dispatch_assessments, update_assessment_status. "
                                "Call the correct tool now to answer the user's question."
                            ),
                        })
                        # Don't set force_answer_without_tools — let the model retry with the right name.
                    else:
                        messages.append(
                            {
                                "role": "system",
                                "content": (
                                    "Tool results are now available in the conversation. "
                                    "Answer the user's latest question directly using those tool results. "
                                    "Do not call any more tools."
                                ),
                            }
                        )
                        force_answer_without_tools = True
                    continue

                # If the model produced nothing (no tokens, no tool calls), inject a nudge
                # and retry once. This happens when context is borderline full or the model
                # stalls — a short retry message usually unblocks it.
                if not assistant_content and not aggregated_tool_calls and not force_answer_without_tools:
                    logger.warning(
                        "chat.stream.empty_response iteration=%s — injecting nudge and retrying",
                        iteration,
                    )
                    messages.append({
                        "role": "system",
                        "content": (
                            "Your last response was empty. "
                            "Please answer the user's most recent question now. "
                            "If you need to call a tool, call it. Otherwise provide a direct text answer."
                        ),
                    })
                    continue

                break

            yield _sse_event(
                "done",
                {
                    "model": MODEL_NAME,
                    "response_started": has_streamed_token,
                    "iterations": iteration,
                },
            )
            logger.info(
                "chat.stream.completed model=%s response_started=%s iterations=%s",
                MODEL_NAME,
                has_streamed_token,
                iteration,
            )
            await asyncio.sleep(0)
        except Exception as exc:
            logger.exception("chat.stream.failed error=%s", exc)
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
