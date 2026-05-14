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

router = APIRouter(prefix="/chat", tags=["chat"])

MODEL_NAME = ACTIVE_GEMMA_MODEL
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
ollama_client = AsyncClient(host=OLLAMA_HOST)

# This variable stores the module logger for chat endpoint debugging.
logger = logging.getLogger(__name__)

# This variable stores the chat and tool-agent addendum appended to the shared BhumiDrishti base prompt.
CHAT_AGENT_SYSTEM_ADDENDUM = """
Chat and agent interaction mode:
- Answer the user's exact question directly and concisely.
- The messages after this system prompt are the current chat conversation history.
- Use the current chat history to answer follow-up questions like "what location did I say earlier?"
- Do not claim you lack access to this current conversation history.

Tool usage policy:
- Use tools only when needed for factual location-specific answers.
- Do not call all tools by default.
- If user asks a specific question (for example only building info), call only the relevant tool.
- Call multiple tools only when user asks for a broader analysis.
- There is only one building information tool: get_building_info.
- Never call get_building_info_by_geometry or any other invented building tool name.
- If the user asks about a selected map building and provides an OSM ID, call get_building_info with osm_id.
- If the user provides GeoJSON building geometry, pass it as geometry to get_building_info for spatial lookup.
- Prefer osm_id or geometry over lat/lon for exact building-specific questions.
- For coordination questions over existing records, use:
  - get_assessments: query one or many existing assessment records. Use it for map/list filtering,
    triage lists, or questions about province, site name, damage_type, structural_risk,
    building_type/material/area/width/height, severity/action priority, flood_zone/flood risk,
    elevation/slope, road_access/nearest road, confidence, worker_name, response_team/team name,
    status, verified_by_ground, created_at, updated_at, responded_at, or spatial filters.
    For spatial assessment filtering, pass geometry for GeoJSON polygons/features or lat/lon with
    within_meters for radius search. Keep include_geometry true when results should display on the map.
    Use single=true only when the user asks for one exact/latest/top assessment.
  - get_sites: list sites with summary counts, filter by name/status/id, or spatial containment.
  - get_field_teams: list available/busy teams before assigning.
  - dispatch_assessments: assign one or many assessments to a team (or single worker) and set responded.
  - update_assessment_status: change assessment status (responded or closed).

Analysis and spatial query tool:
- execute_read_query: write and run any PostgreSQL/PostGIS SELECT for complex analysis.
  Use this for custom aggregations, cross-table joins, spatial/GIS analysis, or any query
  the specific tools cannot express.

  When to use:
    - Complex analysis: counts, averages, distributions, histograms across any columns.
    - Spatial / GIS analysis: proximity, containment, overlap, area, distance, clustering.
    - Cross-table joins: e.g. assessments joined with turkey_buildings for footprint area.
    - Any question needing raw SQL flexibility.

  PostGIS functions you may use freely:
    ST_Distance(a.geom::geography, b.geom::geography)                           -- metres between features
    ST_DWithin(geom::geography, ST_SetSRID(ST_MakePoint(lon,lat),4326)::geography, metres)  -- radius filter
    ST_Contains(s.boundary, ST_SetSRID(ST_MakePoint(lon,lat),4326))             -- point-in-polygon
    ST_Intersects(a.geom, s.boundary)                                            -- geometry overlap
    ST_Area(geom::geography)                                                     -- area in m2
    ST_Centroid(geom)                                                            -- polygon centroid
    ST_AsGeoJSON(geom)                                                           -- geometry as GeoJSON string (use for displayable output)
    ST_AsText(geom)                                                              -- geometry as WKT
    ST_Buffer, ST_Union, ST_ConvexHull, ST_Envelope, ST_Collect, etc.

  Geometry rule: ALWAYS wrap geometry columns with ST_AsGeoJSON(geom) in your SELECT
  when you want readable or displayable geometry. Raw geometry columns return WKB hex
  and the tool will return a hint to re-run with ST_AsGeoJSON.

  Rules:
    1. Only SELECT or WITH…SELECT (CTE) — never INSERT/UPDATE/DELETE/DROP/CREATE/ALTER.
    2. Include LIMIT (results capped at 500 rows automatically if omitted).
    3. Prefer get_assessments / get_sites / get_nearest_shelter for simple lookups.

  Available tables (geometry columns are PostGIS GEOMETRY SRID 4326):
    assessments       — id, site_id, lat, lon, severity, damage_type, flood_zone, geom, ...
    sites             — id, name, province, district, boundary (POLYGON), ...
    batches           — id, site_id, status, created_at, ...
    field_teams       — id, name, status, ...
    field_team_members— id, team_id, worker_name, ...
    turkey_buildings  — osm_id, geom (POLYGON), building type, ...
    turkey_lines      — osm_id, geom (LINESTRING), road name, highway type, ...
    turkey_points     — osm_id, geom (POINT), amenity, name, ...
    turkey_provinces  — name, geom (POLYGON)
    turkey_districts_pts — name, geom (POINT)

Dispatch policy:
- Never assign to a busy team.
- If user asks to dispatch but no team is provided, ask which team to use.
- Suggest available teams from get_field_teams.

Output policy:
- Provide the final answer in plain language (not strict assessment JSON) unless user explicitly asks for assessment JSON.
- Keep responses practical for disaster field workers.
""".strip()

# This variable stores the system prompt used by text chat and tool-agent endpoints.
CHAT_SYSTEM_PROMPT = build_bhumidrishti_system_prompt(CHAT_AGENT_SYSTEM_ADDENDUM)


def _sse_event(event: str, data: dict[str, Any]) -> str:
    """Format one SSE event block."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _build_messages(payload: ChatRequest) -> list[dict[str, Any]]:
    """Build chat messages with the default system prompt prepended."""
    incoming_messages = [message.model_dump() for message in payload.messages]
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
            max_iterations = 2
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
                    "options": {"temperature": payload.temperature},
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
                                logger.info(
                                    "chat.stream.tool_call.detected iteration=%s tool=%s args=%s",
                                    iteration,
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
                        logger.info("chat.stream.tool_call.completed tool=%s result=%s", tool_name, tool_result)
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
