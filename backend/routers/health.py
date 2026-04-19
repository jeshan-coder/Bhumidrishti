"""Health check endpoints."""

import os
from typing import Any
from fastapi import APIRouter
from ollama import AsyncClient

router = APIRouter(prefix="", tags=["health"])

MODEL_NAME = "gemma4:26b"
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
ollama_client = AsyncClient(host=OLLAMA_HOST)


def _extract_model_names(models: list[Any]) -> list[str]:
    """Extract model names from Ollama list response payload."""
    names: list[str] = []
    for model in models:
        if isinstance(model, dict):
            model_name = model.get("name") or model.get("model")
        else:
            model_name = getattr(model, "name", None) or getattr(model, "model", None)

        if isinstance(model_name, str) and model_name:
            names.append(model_name)
    return names


@router.get("/")
async def root():
    """Root endpoint health check."""
    return {
        "success": True,
        "data": {"message": "Hello from BhumiDrishti Backend"},
        "error": None
    }


@router.get("/health/model")
async def model_health() -> dict[str, Any]:
    """Check Ollama reachability and gemma4:26b availability."""
    try:
        list_response = await ollama_client.list()

        if isinstance(list_response, dict):
            raw_models = list_response.get("models", [])
        else:
            raw_models = getattr(list_response, "models", [])

        models = raw_models if isinstance(raw_models, list) else []
        model_names = _extract_model_names(models)
        model_available = any(name == MODEL_NAME for name in model_names)

        return {
            "success": True,
            "data": {
                "ollama_host": OLLAMA_HOST,
                "model": MODEL_NAME,
                "model_available": model_available,
                "loaded_models": model_names,
            },
            "error": None,
        }
    except Exception as exc:
        return {
            "success": False,
            "data": {
                "ollama_host": OLLAMA_HOST,
                "model": MODEL_NAME,
                "model_available": False,
                "loaded_models": [],
            },
            "error": f"Model health check failed: {exc}",
        }
