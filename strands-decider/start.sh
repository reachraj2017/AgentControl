#!/usr/bin/env bash
# Starts the Strands Decider 2B native host process — a second, independent
# decision-model backend alongside Ollama (which stays as-is). Not managed by
# `make up` / Docker Compose: start/stop this yourself.
set -euo pipefail
cd "$(dirname "$0")"
source venv/bin/activate
exec strands-decider serve StrandsAgents/strands-decider-2B-qwen3.5-v1-2610 --port 8000
