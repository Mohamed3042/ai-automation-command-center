#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"
exec node scripts/capture_screenshots.mjs "${1:-http://127.0.0.1:4173}"
