#!/usr/bin/env bash
# Run the complete local daily pipeline without sending Feishu messages or deploying.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON_BIN="${PYTHON_BIN:-$ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python runtime not found: $PYTHON_BIN" >&2
  echo "Create .venv first, or set PYTHON_BIN=/path/to/python." >&2
  exit 1
fi
if [[ ! -f .env ]]; then
  echo "Missing $ROOT/.env" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

REPORT_DATE="${1:-$(TZ=Asia/Shanghai date +%F)}"
LOG_DIR="$ROOT/output/logs"
LOG_FILE="$LOG_DIR/daily-local-${REPORT_DATE}.log"
BRIEF_FILE="$ROOT/output/daily-brief-${REPORT_DATE}.json"
mkdir -p "$LOG_DIR"

run_required() {
  local label="$1"
  shift
  printf '\n[%s] %s\n' "$(date '+%F %T')" "$label" | tee -a "$LOG_FILE"
  "$@" 2>&1 | tee -a "$LOG_FILE"
}

run_optional() {
  local label="$1"
  shift
  printf '\n[%s] %s\n' "$(date '+%F %T')" "$label" | tee -a "$LOG_FILE"
  if ! "$@" 2>&1 | tee -a "$LOG_FILE"; then
    printf '[%s] %s failed; continuing.\n' "$(date '+%F %T')" "$label" | tee -a "$LOG_FILE"
  fi
}

# This script deliberately does not invoke src.notify, any deployment command, or git push.
run_required "Ingest RSS, Scrape, Media, and Social" \
  "$PYTHON_BIN" -m src.main
run_optional "Check source capture specifications" \
  "$PYTHON_BIN" -m src.spec_check --exit-code
run_required "Generate daily brief for ${REPORT_DATE}" \
  "$PYTHON_BIN" -m src.daily --date "$REPORT_DATE" --output "$BRIEF_FILE"
run_required "Build local static site" \
  "$PYTHON_BIN" -m src.publish --input "$BRIEF_FILE"
run_optional "Refresh tracked entity timeline" \
  "$PYTHON_BIN" -m src.timeline --output site/data/timeline-latest.json
run_optional "Refresh topic trends" \
  "$PYTHON_BIN" -m src.trends --output site/data/heatmap-trends.json
run_optional "Tag today\047s brief and refresh heatmap" \
  "$PYTHON_BIN" -m src.tag_topics --ingest-brief site/data/brief-latest.json
run_optional "Aggregate topic heatmap" \
  "$PYTHON_BIN" -m src.aggregate

printf '\nCompleted local daily pipeline.\nBrief: %s\nLog: %s\n' "$BRIEF_FILE" "$LOG_FILE"
