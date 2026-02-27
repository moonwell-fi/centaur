#!/usr/bin/env bash
# Smoke test all plugin CLIs with real API keys.
# Usage: ./scripts/smoke-test-clis.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# Load .env
set -a
source .env
set +a

export AI_V2_LOG_LEVEL=critical

exec .venv/bin/python scripts/smoke_test_integrations.py "$@"
