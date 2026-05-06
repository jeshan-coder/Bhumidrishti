"""This file centralizes AI runtime configuration shared across backend modules."""

import os

# This variable stores the default Gemma model used when no environment override is set.
DEFAULT_GEMMA_MODEL = "gemma4:e4b"

# This variable stores the active Gemma model used by chat, health, and assessment pipelines.
ACTIVE_GEMMA_MODEL = (os.getenv("GEMMA_MODEL") or DEFAULT_GEMMA_MODEL).strip() or DEFAULT_GEMMA_MODEL
