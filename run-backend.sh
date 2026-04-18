#!/bin/bash
# BhumiDrishti - Backend Only Runner
# This script starts the FastAPI backend and its required Ollama dependency via Docker.
# The Next.js frontend container will be skipped.

set -e

echo "🟢 Step 1: Starting Ollama and checking only missing models..."
# ollama-init now pulls only missing models and skips already-downloaded ones.
docker compose up ollama-init

echo "🟢 Step 2: Model check done. Starting backend in detached mode..."
# Now we start the backend detached (-d) so you get your terminal back.
docker compose up -d --build backend

echo "✅ Ready! The API is available at http://localhost:8000"
