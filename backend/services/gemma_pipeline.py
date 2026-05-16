"""Gemma-4 analysis pipeline for ground photos and video frames."""

import asyncio
import base64
import json
import logging
import time
from typing import Any, Awaitable, Callable

import asyncpg
import ollama
from prompts.gemma_system_prompt import (
    ORTHOPHOTO_ASSESSMENT_SYSTEM_PROMPT,
    PHOTO_ASSESSMENT_SYSTEM_PROMPT,
)
from services.ai_runtime import ACTIVE_GEMMA_MODEL, get_model_context_window
from services.gis import query_osrm_route  # still used by get_nearest_shelter (via tools)

# =============================================================
# SYSTEM PROMPT
# =============================================================

# This variable switches the active prompt to the centralized photo assessment prompt file.
SYSTEM_PROMPT = PHOTO_ASSESSMENT_SYSTEM_PROMPT

# This variable stores the orthophoto-specific prompt with aerial assessment addendum.
ORTHOPHOTO_SYSTEM_PROMPT = ORTHOPHOTO_ASSESSMENT_SYSTEM_PROMPT

# This variable stores the module logger used for pipeline debugging.
logger = logging.getLogger(__name__)

def get_num_ctx(model: str) -> int:
    """Return the appropriate num_ctx for a given model name."""
    return get_model_context_window(model)


def _strip_nulls(obj: Any) -> Any:
    """Recursively remove None/null values from dicts and lists.

    Reduces token count of tool results sent back to the model — large tool
    responses (e.g. get_location_info) contain dozens of null fields that
    consume context window without providing any useful information.
    """
    if isinstance(obj, dict):
        return {k: _strip_nulls(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_strip_nulls(item) for item in obj if item is not None]
    return obj


def _safe_json(data: Any) -> str:
    """Serialize any payload for logs without crashing logger calls."""
    try:
        return json.dumps(data, default=str, ensure_ascii=False)
    except Exception:
        return str(data)


def _log_agent_event(stage: str, payload: dict[str, Any]) -> None:
    """Write one structured agent log line with stage and payload."""
    logger.info("agent_stage=%s payload=%s", stage, _safe_json(payload))


def request_assessment_json_repair(
    model: str,
    system_prompt: str,
    raw_assistant_output: str,
) -> str:
    """Request a strict JSON-only rewrite when the model returns prose instead of JSON."""
    # This variable stores the strict repair instruction used to coerce valid JSON output.
    repair_instruction = (
        "The previous assistant output was not valid JSON. "
        "Rewrite it as ONE valid JSON object only, with no markdown and no extra text.\n\n"
        "Required keys: severity, damage_type, damage_description, structural_risk, "
        "building_type, building_floors, building_material, estimated_occupants, occupant_status, "
        "recommended_action, action_priority, flood_zone, elevation_m, slope_degrees, slope_risk, "
        "nearest_shelter, shelter_distance_m, shelter_type, road_access, nearest_road, road_distance_m, "
        "reasoning, warnings, confidence, turkish_summary.\n\n"
        "Rules: severity must be 1-5, action_priority 1-5, confidence 0-1, warnings must be an array.\n\n"
        "Previous output:\n"
        f"{raw_assistant_output}"
    )

    repair_response = ollama.chat(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": repair_instruction},
        ],
        options={"temperature": 0.0, "num_ctx": get_num_ctx(model)},
    )

    return repair_response.message.content or ""

# =============================================================
# TOOLS  --  all schemas live in services/tools.py
# =============================================================

# TOOLS = assessment-only tools (photo-assessment pipeline)
# CHAT_TOOLS = assessment + coordination tools (chat agent)
from services.tools import ASSESSMENT_TOOLS as TOOLS, CHAT_TOOLS  # noqa: E402


# =============================================================
# PROMPT BUILDER
# =============================================================

def build_user_prompt(
    lat: float,
    lon: float,
    input_type: str,
    image_count: int,
    building_type: str | None = None,
    building_floors: str | None = None,
    field_note: str | None = None,
    pre_image_available: bool = False
) -> str:
    """Build the user message prompt for each assessment."""
    input_descriptions = {
        "ground_photo": f"{image_count} ground-level photo(s) taken by field worker",
        "drone_images": f"{image_count} drone oblique image(s) from aerial survey",
        "video": f"{image_count} frame(s) extracted from field worker video"
    }
    input_desc = input_descriptions.get(input_type, f"{image_count} image(s)")
 
    osm_context = ""
    if building_type and building_type != "yes":
        osm_context += f"\n- Building type from OSM: {building_type}"
    if building_floors:
        osm_context += f"\n- Floors from OSM: {building_floors}"
    if not osm_context:
        osm_context = "\n- No OSM building data available — estimate from image"
 
    note_section = ""
    if field_note:
        note_section = f"""
FIELD WORKER NOTE (ground truth — prioritize this):
{field_note}
"""
    pre_image_section = ""
    if pre_image_available:
        pre_image_section = """
PRE-EARTHQUAKE REFERENCE IMAGE:
The first image in the list is a pre-earthquake satellite reference image
showing this building before the February 2023 earthquake.
The remaining images show the current post-earthquake state.
Use the pre-earthquake image to understand the original building structure
and compare against current damage.
"""
 
    prompt = f"""You are assessing earthquake damage at:
Latitude: {lat}
Longitude: {lon}
Province: {'Hatay' if lon < 37.5 else 'Adiyaman'}, Turkey
Input type: {input_desc}
 
OSM building data:{osm_context}
{note_section}{pre_image_section}
INSTRUCTIONS:
1. Call get_building_info({lat}, {lon}) first
2. Call get_flood_zone({lat}, {lon})
3. Call get_location_info({lat}, {lon})
4. Call get_nearest_road({lat}, {lon})
5. Call get_elevation_slope({lat}, {lon})
6. Call get_nearest_shelter({lat}, {lon})
7. Analyze all {image_count} provided image(s) for structural damage
8. Combine visual analysis with all tool results
9. Return your assessment as a single JSON object
 
Remember: Call ALL SIX tools before returning JSON.
Return ONLY the JSON object. No other text."""
 
    return prompt


def build_orthophoto_user_prompt(
    lat: float,
    lon: float,
    osm_id: int | str,
    batch_id: str,
    site_name: str,
    building_index: int,
    total_buildings: int,
    width_m: float,
    height_m: float,
    area_m2: float,
    pre_available: bool,
    is_dark: bool,
) -> str:
    """Build the user message prompt for an orthophoto (aerial) building assessment."""
    if pre_available:
        pre_section = (
            "IMAGE 1: PRE-EARTHQUAKE REFERENCE\n"
            "This shows the building BEFORE the earthquake.\n"
            "Use this to understand the original structure and compare against current damage."
        )
        post_index = 2
    else:
        pre_section = (
            "No pre-earthquake reference image available.\n"
            "Assess the post-earthquake image only."
        )
        post_index = 1

    dark_warning = ""
    if is_dark:
        dark_warning = (
            "\nWARNING: Parts of this image contain no data (dark areas).\n"
            "Base your assessment only on the visible portions.\n"
            "Note limited visibility in your reasoning.\n"
        )

    prompt = f"""Assessment type: AERIAL ORTHOPHOTO
OSM Building ID: {osm_id}
Approximate dimensions: {width_m:.0f}m × {height_m:.0f}m
Approximate area: {area_m2:.0f}m²
Site name: {site_name}
Batch: {batch_id} — Building {building_index} of {total_buildings}

Latitude: {lat}
Longitude: {lon}

{pre_section}

IMAGE {post_index}: POST-EARTHQUAKE
This is the current state. Assess this image for damage.

IMPORTANT: These two images show the SAME location at different times.
The building outlines may look different because the building was
damaged or destroyed between the two images.
The green box marks the same geographic location in both images.
Compare what was there before versus what is there now.

The GREEN polygon outline is an approximate target cue.
It may be slightly shifted or may cover only a fraction of the target building because
of orthophoto distortion, georeferencing mismatch, or chip construction.
Use surrounding context to identify the most likely complete building connected to the
green outline, including nearby roads, neighboring buildings, shadows, debris patterns,
and footprint continuity between the pre-earthquake and post-earthquake images.
Assess the full visible building associated with the green outline, not only the pixels
inside the polygon. Do not switch to a different nearby building only because it appears
more damaged. If the target remains ambiguous, mention this in reasoning and add
"partial_view_only" or "poor_image_quality" to warnings as appropriate.
{dark_warning}
INSTRUCTIONS:
1. Call get_building_info({lat}, {lon}) first
2. Call get_flood_zone({lat}, {lon})
3. Call get_location_info({lat}, {lon})
4. Call get_nearest_road({lat}, {lon})
5. Call get_elevation_slope({lat}, {lon})
6. Call get_nearest_shelter({lat}, {lon})
7. Analyze the provided aerial chip image(s) for structural damage
8. Combine visual analysis with all tool results
9. Return your assessment as a single JSON object

Remember: Call ALL SIX tools before returning JSON.
Return ONLY the JSON object. No other text."""

    return prompt


# =============================================================
# TOOL IMPLEMENTATIONS
# Each function lives in its own module under services/tools/.
# These re-imports keep the existing call-sites working.
# =============================================================

from services.tools.get_building_info import get_building_info  # noqa: E402
from services.tools.get_flood_zone import get_flood_zone  # noqa: E402
from services.tools.get_location_info import get_location_info  # noqa: E402
from services.tools.get_nearest_road import get_nearest_road  # noqa: E402
from services.tools.get_elevation_slope import get_elevation_slope  # noqa: E402
from services.tools.get_nearest_shelter import get_nearest_shelter  # noqa: E402
from services.tools.get_assessments import get_assessments  # noqa: E402
from services.tools.get_sites import get_sites  # noqa: E402
from services.tools.get_field_teams import get_field_teams, _ensure_field_team_tables  # noqa: E402
from services.tools.get_field_workers import get_field_workers  # noqa: E402
from services.tools.dispatch_assessments import dispatch_assessments  # noqa: E402
from services.tools.update_assessment_status import update_assessment_status  # noqa: E402
from services.tools._shared import _AsyncNullContext  # noqa: E402



async def dispatch_tool(tool_name: str, tool_args: dict, db) -> dict:
    """Delegate to the centralized dispatcher in services.tools.

    Kept here as a public alias so existing call-sites (assessment agent loop,
    routers) do not need to change their imports.
    """
    from services.tools import dispatch_tool as _central_dispatch  # noqa: PLC0415
    tool_started_at = time.perf_counter()
    _log_agent_event("dispatch_tool_started", {"tool_name": tool_name, "tool_args": tool_args})
    try:
        result = await _central_dispatch(tool_name, tool_args, db)
    except Exception as exc:
        _log_agent_event(
            "dispatch_tool_failed",
            {
                "tool_name": tool_name,
                "error": str(exc),
                "elapsed_ms": round((time.perf_counter() - tool_started_at) * 1000, 2),
            },
        )
        raise
    _log_agent_event(
        "dispatch_tool_completed",
        {
            "tool_name": tool_name,
            "result": result,
            "elapsed_ms": round((time.perf_counter() - tool_started_at) * 1000, 2),
        },
    )
    return result


# =============================================================
# AGENT LOOP
# =============================================================

def encode_image(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
    except Exception as e:
        logging.error(f"Failed to read image at {path}: {e}")
        return ""


async def run_assessment_agent(
    image_paths: list[str],
    lat: float,
    lon: float,
    input_type: str,
    db,
    field_note: str | None = None,
    pre_image_path: str | None = None,
    building_type: str | None = None,
    building_floors: str | None = None,
    model: str = ACTIVE_GEMMA_MODEL,
    progress_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    orthophoto_context: dict[str, Any] | None = None,
) -> dict:
    _log_agent_event(
        "assessment_started",
        {
            "lat": lat,
            "lon": lon,
            "input_type": input_type,
            "image_path_count": len(image_paths),
            "pre_image_available": pre_image_path is not None,
            "field_note_present": bool(field_note),
            "model": model,
        },
    )

    # This variable stores monotonic start time for full assessment timing.
    assessment_started_at = time.perf_counter()

    all_image_paths = []
    if pre_image_path:
        all_image_paths.append(pre_image_path)
    all_image_paths.extend(image_paths[:5])

    _log_agent_event(
        "assessment_image_paths_prepared",
        {
            "all_image_paths": all_image_paths,
            "all_image_count": len(all_image_paths),
            "truncated_to_max_images": len(image_paths) > 5,
        },
    )

    images_b64 = [encode_image(p) for p in all_image_paths if p]
    images_b64 = [img for img in images_b64 if img] # remove empty strings on failure

    _log_agent_event(
        "assessment_images_encoded",
        {
            "requested_count": len(all_image_paths),
            "encoded_count": len(images_b64),
            "encoding_failed_count": max(0, len(all_image_paths) - len(images_b64)),
        },
    )
    
    is_orthophoto = input_type == "orthophoto" and orthophoto_context is not None
    if is_orthophoto:
        ctx = orthophoto_context or {}
        user_prompt = build_orthophoto_user_prompt(
            lat=lat,
            lon=lon,
            osm_id=ctx.get("osm_id", "unknown"),
            batch_id=ctx.get("batch_id", ""),
            site_name=ctx.get("site_name", ""),
            building_index=int(ctx.get("building_index", 1)),
            total_buildings=int(ctx.get("total_buildings", 1)),
            width_m=float(ctx.get("width_m", 0)),
            height_m=float(ctx.get("height_m", 0)),
            area_m2=float(ctx.get("area_m2", 0)),
            pre_available=bool(ctx.get("pre_available", False)),
            is_dark=bool(ctx.get("is_dark", False)),
        )
        active_system_prompt = ORTHOPHOTO_SYSTEM_PROMPT
    else:
        user_prompt = build_user_prompt(
            lat=lat, lon=lon, input_type=input_type, image_count=len(images_b64),
            building_type=building_type, building_floors=building_floors,
            field_note=field_note, pre_image_available=pre_image_path is not None,
        )
        active_system_prompt = SYSTEM_PROMPT

    messages = [
        {"role": "system", "content": active_system_prompt},
        {"role": "user", "content": user_prompt, "images": images_b64}
    ]

    _log_agent_event(
        "assessment_prompt_built",
        {
            "user_prompt_preview": user_prompt[:500],
            "message_count": len(messages),
        },
    )

    iteration = 0
    max_iterations = 10
    
    while iteration < max_iterations:
        iteration += 1
        _log_agent_event(
            "assessment_iteration_started",
            {
                "iteration": iteration,
                "max_iterations": max_iterations,
                "message_count": len(messages),
            },
        )

        # This variable stores monotonic iteration start time for debugging slow turns.
        iteration_started_at = time.perf_counter()

        if progress_callback:
            await progress_callback(
                {
                    "stage": "ai_reasoning",
                    "progress_percent": min(78, 30 + (iteration * 7)),
                    "thought": "Gemma is reviewing visual evidence and context.",
                }
            )

        try:
            response_stream = ollama.chat(
                model=model,
                messages=messages,
                tools=TOOLS,
                options={"temperature": 0.1, "num_ctx": get_num_ctx(model)},
                stream=True,
            )
        except Exception as exc:
            _log_agent_event(
                "assessment_chat_failed",
                {
                    "iteration": iteration,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "elapsed_ms": round((time.perf_counter() - iteration_started_at) * 1000, 2),
                },
            )
            raise

        assistant_thinking_parts: list[str] = []
        assistant_content_parts: list[str] = []
        assistant_tool_calls: list[dict[str, Any]] = []
        emitted_thinking_chars = 0
        emitted_response_chars = 0
        # Token counts — populated from the final stream chunk.
        prompt_tokens: int = 0
        completion_tokens: int = 0
        # "length" means Ollama stopped because context window was exhausted.
        # "stop" means the model finished naturally.
        done_reason: str = ""

        for chunk in response_stream:
            if isinstance(chunk, dict):
                message_block = chunk.get("message")
                # Final stream chunk carries usage stats — accumulate across iterations.
                prompt_tokens += chunk.get("prompt_eval_count", 0) or 0
                completion_tokens += chunk.get("eval_count", 0) or 0
                if chunk.get("done"):
                    done_reason = chunk.get("done_reason", "") or ""
            else:
                message_block = getattr(chunk, "message", None)
                prompt_tokens += getattr(chunk, "prompt_eval_count", 0) or 0
                completion_tokens += getattr(chunk, "eval_count", 0) or 0
                if getattr(chunk, "done", False):
                    done_reason = getattr(chunk, "done_reason", "") or ""

            if message_block is None:
                continue

            if isinstance(message_block, dict):
                thinking_chunk = message_block.get("thinking")
                content_chunk = message_block.get("content")
                tool_calls_chunk = message_block.get("tool_calls")
            else:
                thinking_chunk = getattr(message_block, "thinking", None)
                content_chunk = getattr(message_block, "content", None)
                tool_calls_chunk = getattr(message_block, "tool_calls", None)

            if isinstance(thinking_chunk, str) and thinking_chunk:
                assistant_thinking_parts.append(thinking_chunk)
                if progress_callback:
                    full_thinking = "".join(assistant_thinking_parts).strip()
                    if len(full_thinking) - emitted_thinking_chars >= 24:
                        emitted_thinking_chars = len(full_thinking)
                        await progress_callback(
                            {
                                "stage": "ai_reasoning_stream",
                                "progress_percent": min(82, 32 + (iteration * 7)),
                                "thought": full_thinking[-480:],
                                "thinking_text": full_thinking,
                            }
                        )

            if isinstance(content_chunk, str) and content_chunk:
                assistant_content_parts.append(content_chunk)
                if progress_callback:
                    full_response = "".join(assistant_content_parts).strip()
                    if len(full_response) - emitted_response_chars >= 24:
                        emitted_response_chars = len(full_response)
                        await progress_callback(
                            {
                                "stage": "ai_response_stream",
                                "progress_percent": min(86, 36 + (iteration * 7)),
                                "thought": full_response[-480:],
                                "response_text": full_response,
                            }
                        )

            if isinstance(tool_calls_chunk, list) and tool_calls_chunk:
                normalized_tool_calls: list[dict[str, Any]] = []
                for raw_call in tool_calls_chunk:
                    if isinstance(raw_call, dict):
                        function_block = raw_call.get("function", {})
                    else:
                        function_block = getattr(raw_call, "function", None)

                    if isinstance(function_block, dict):
                        tool_name = function_block.get("name")
                        raw_args = function_block.get("arguments")
                    elif function_block is not None:
                        tool_name = getattr(function_block, "name", None)
                        raw_args = getattr(function_block, "arguments", None)
                    else:
                        tool_name = None
                        raw_args = None

                    if not isinstance(tool_name, str) or not tool_name:
                        continue

                    parsed_args: dict[str, Any]
                    if isinstance(raw_args, dict):
                        parsed_args = raw_args
                    elif isinstance(raw_args, str):
                        try:
                            loaded_args = json.loads(raw_args)
                            parsed_args = loaded_args if isinstance(loaded_args, dict) else {}
                        except json.JSONDecodeError:
                            parsed_args = {}
                    else:
                        parsed_args = {}

                    normalized_tool_calls.append(
                        {
                            "function": {
                                "name": tool_name,
                                "arguments": parsed_args,
                            }
                        }
                    )

                if normalized_tool_calls:
                    assistant_tool_calls = normalized_tool_calls

        assistant_content = "".join(assistant_content_parts)
        assistant_thinking = "".join(assistant_thinking_parts).strip()

        if progress_callback and assistant_thinking and len(assistant_thinking) > emitted_thinking_chars:
            await progress_callback(
                {
                    "stage": "ai_reasoning_stream",
                    "progress_percent": min(82, 32 + (iteration * 7)),
                    "thought": assistant_thinking[-480:],
                    "thinking_text": assistant_thinking,
                }
            )
        if progress_callback and assistant_content and len(assistant_content) > emitted_response_chars:
            await progress_callback(
                {
                    "stage": "ai_response_stream",
                    "progress_percent": min(86, 36 + (iteration * 7)),
                    "thought": assistant_content[-480:],
                    "response_text": assistant_content,
                }
            )

        _log_agent_event(
            "assessment_chat_received",
            {
                "iteration": iteration,
                "assistant_content_preview": (assistant_content or "")[:500],
                "assistant_thinking_preview": assistant_thinking[:500],
                "tool_call_count": len(assistant_tool_calls),
                "elapsed_ms": round((time.perf_counter() - iteration_started_at) * 1000, 2),
            },
        )
        
        messages.append({
            "role": "assistant",
            "content": assistant_content or "",
            "tool_calls": assistant_tool_calls or []
        })

        if assistant_tool_calls:
            for tool_call in assistant_tool_calls:
                function_block = tool_call.get("function", {})
                tool_name = function_block.get("name")
                tool_args = function_block.get("arguments", {})
                if not isinstance(tool_name, str):
                    continue
                if not isinstance(tool_args, dict):
                    tool_args = {}
                _log_agent_event(
                    "assessment_tool_call",
                    {
                        "iteration": iteration,
                        "tool_name": tool_name,
                        "tool_args": tool_args,
                    },
                )

                if progress_callback:
                    live_stream_text = (assistant_content or assistant_thinking or "").strip()
                    await progress_callback(
                        {
                            "stage": "tool_call",
                            "progress_percent": min(82, 35 + (iteration * 6)),
                            "thought": (live_stream_text[-480:] if live_stream_text else f"Calling tool: {tool_name}"),
                            "thinking_text": assistant_thinking or None,
                            "response_text": assistant_content or None,
                        }
                    )

                tool_result = await dispatch_tool(tool_name, tool_args, db)
                _log_agent_event(
                    "assessment_tool_result",
                    {
                        "iteration": iteration,
                        "tool_name": tool_name,
                        "tool_result": tool_result,
                    },
                )
                messages.append({
                    "role": "tool",
                    "content": json.dumps(_strip_nulls(tool_result), ensure_ascii=False)
                })

            _log_agent_event(
                "assessment_iteration_continue",
                {
                    "iteration": iteration,
                    "reason": "tool_calls_present",
                    "elapsed_ms": round((time.perf_counter() - iteration_started_at) * 1000, 2),
                },
            )
            continue

        if progress_callback:
            await progress_callback(
                {
                    "stage": "finalize_output",
                    "progress_percent": 84,
                    "thought": (assistant_content or assistant_thinking or "AI is finalizing structured assessment output.")[-480:],
                }
            )

        # Detect context window exhaustion — Ollama reports "length" when the
        # response was cut short because num_ctx was reached.
        context_window_full = done_reason == "length"
        if context_window_full:
            _log_agent_event(
                "assessment_context_window_full",
                {
                    "iteration": iteration,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "context_window": get_num_ctx(model),
                    "done_reason": done_reason,
                },
            )
            if progress_callback:
                await progress_callback(
                    {
                        "stage": "context_window_full",
                        "progress_percent": 0,
                        "thought": (
                            f"Context window full ({get_num_ctx(model) // 1024}k tokens). "
                            "The model ran out of space before finishing its response. "
                            "Try a model with a larger context window."
                        ),
                    }
                )

        raw_assistant_content = assistant_content or ""
        try:
            parsed_assessment = parse_assessment_json(raw_assistant_content)
        except ValueError as parse_exc:
            _log_agent_event(
                "assessment_parse_retry_started",
                {
                    "iteration": iteration,
                    "error": str(parse_exc),
                    "assistant_content_preview": raw_assistant_content[:500],
                },
            )

            if progress_callback:
                await progress_callback(
                    {
                        "stage": "finalize_output",
                        "progress_percent": 86,
                        "thought": "AI is converting response into strict JSON output.",
                    }
                )

            try:
                repaired_output = request_assessment_json_repair(
                    model=model,
                    system_prompt=active_system_prompt,
                    raw_assistant_output=raw_assistant_content,
                )
                parsed_assessment = parse_assessment_json(repaired_output)
                _log_agent_event(
                    "assessment_parse_retry_succeeded",
                    {
                        "iteration": iteration,
                        "repaired_output_preview": repaired_output[:500],
                    },
                )
            except Exception as repair_exc:
                _log_agent_event(
                    "assessment_parse_retry_failed",
                    {
                        "iteration": iteration,
                        "parse_error": str(parse_exc),
                        "repair_error": str(repair_exc),
                    },
                )
                raise parse_exc

        elapsed_s = round(time.perf_counter() - assessment_started_at, 2)
        parsed_assessment["inference_seconds"] = elapsed_s
        parsed_assessment["model_used"] = model
        parsed_assessment["prompt_tokens"] = prompt_tokens
        parsed_assessment["completion_tokens"] = completion_tokens
        parsed_assessment["total_tokens"] = prompt_tokens + completion_tokens
        parsed_assessment["context_window"] = get_num_ctx(model)

        if context_window_full:
            existing_warnings = parsed_assessment.get("warnings") or []
            if isinstance(existing_warnings, list) and "context_window_full" not in existing_warnings:
                existing_warnings.append("context_window_full")
            parsed_assessment["warnings"] = existing_warnings

        _log_agent_event(
            "assessment_completed",
            {
                "iteration": iteration,
                "severity": parsed_assessment.get("severity"),
                "damage_type": parsed_assessment.get("damage_type"),
                "recommended_action": parsed_assessment.get("recommended_action"),
                "confidence": parsed_assessment.get("confidence"),
                "inference_seconds": elapsed_s,
                "model_used": model,
                "elapsed_ms_total": round(elapsed_s * 1000, 2),
            },
        )
        return parsed_assessment
    
    _log_agent_event(
        "assessment_failed_max_iterations",
        {
            "max_iterations": max_iterations,
            "elapsed_ms_total": round((time.perf_counter() - assessment_started_at) * 1000, 2),
        },
    )
    raise ValueError(f"Agent loop exceeded {max_iterations} iterations without final response")


def parse_assessment_json(raw: str) -> dict:
    import re
    _log_agent_event(
        "parse_assessment_started",
        {
            "raw_preview": raw[:500],
            "raw_length": len(raw),
        },
    )
    text = raw.strip()
    text = re.sub(r"```json\s*", "", text)
    text = re.sub(r"```\s*", "", text)
    
    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end == 0:
        _log_agent_event(
            "parse_assessment_failed_no_json",
            {
                "text_preview": text[:500],
            },
        )
        raise ValueError(f"No JSON object found in response: {text[:200]}")
    
    json_str = text[start:end]
    json_str = re.sub(r",\s*}", "}", json_str)
    json_str = re.sub(r",\s*]", "]", json_str)
    
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        _log_agent_event(
            "parse_assessment_failed_invalid_json",
            {
                "error": str(e),
                "json_preview": json_str[:500],
            },
        )
        raise ValueError(f"JSON parse error: {e}\\nRaw: {json_str[:500]}")

    if not isinstance(data, dict):
        _log_agent_event(
            "parse_assessment_failed_not_object",
            {
                "parsed_type": type(data).__name__,
            },
        )
        raise ValueError("Parsed assessment must be a JSON object")
    
    defaults = {
        "severity": 3, "damage_type": "unknown", "damage_description": "Assessment incomplete",
        "structural_risk": "unknown", "building_type": None, "building_floors": None,
        "building_material": "unknown", "estimated_occupants": "unknown", "occupant_status": "unknown",
        "recommended_action": "structural_assessment", "action_priority": 2, "road_access": "unknown",
        "reasoning": "Automated assessment", "warnings": [], "confidence": 0.5, "turkish_summary": ""
    }
    # This variable tracks fields auto-filled from defaults for debugging weak model outputs.
    defaulted_fields: list[str] = []
    for key, default in defaults.items():
        if key not in data or data[key] is None:
            data[key] = default
            defaulted_fields.append(key)

    data["severity"] = max(1, min(5, int(data["severity"])))
    data["action_priority"] = max(1, min(5, int(data["action_priority"])))
    data["confidence"] = max(0.0, min(1.0, float(data["confidence"])))

    _log_agent_event(
        "parse_assessment_completed",
        {
            "defaulted_fields": defaulted_fields,
            "severity": data.get("severity"),
            "damage_type": data.get("damage_type"),
            "recommended_action": data.get("recommended_action"),
            "confidence": data.get("confidence"),
            "keys": sorted(list(data.keys())),
        },
    )
    return data
