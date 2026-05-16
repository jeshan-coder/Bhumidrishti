"""This file centralizes AI runtime configuration shared across backend modules."""

import os

# This variable stores the default Gemma model used when no environment override is set.
DEFAULT_GEMMA_MODEL = "gemma4:e4b"

# This variable stores the active Gemma model used by chat, health, and assessment pipelines.
ACTIVE_GEMMA_MODEL = (os.getenv("GEMMA_MODEL") or DEFAULT_GEMMA_MODEL).strip() or DEFAULT_GEMMA_MODEL

# Context window sizes per Gemma 4 model variant (in tokens).
# Used by the assessment pipeline (num_ctx) and the health endpoint (UI display).
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "gemma4:e2b":  131072,   # 128k
    "gemma4:e4b":  131072,   # 128k
    "gemma4:12b":  131072,   # 128k
    "gemma4:26b":  262144,   # 256k
    "gemma4:31b":  262144,   # 256k
}
DEFAULT_CONTEXT_WINDOW = 131072  # safe fallback for unknown Gemma 4 variants


def get_model_context_window(model: str) -> int:
    """Return the context window size in tokens for a given model name."""
    return MODEL_CONTEXT_WINDOWS.get(model, DEFAULT_CONTEXT_WINDOW)
