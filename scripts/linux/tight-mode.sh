#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RAW_DIR="${RAW_DIR:-/var/lib/docker/volumes/hfd_hfd_raw_payloads/_data}"
KEEP_DAYS="${KEEP_DAYS:-3}"
APPLY="false"

usage() {
  cat <<EOF
Usage: $0 [--apply] [--keep-days N]

Stops non-core HFD workers, truncates Docker container logs, and prunes raw
payload files older than N days. By default this is a dry run.

Environment:
  RAW_DIR      Raw payload volume path. Default: $RAW_DIR
  KEEP_DAYS   Days of raw payloads to keep. Default: $KEEP_DAYS
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply)
      APPLY="true"
      shift
      ;;
    --keep-days)
      KEEP_DAYS="${2:?missing keep day count}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if ! [[ "$KEEP_DAYS" =~ ^[0-9]+$ ]]; then
  echo "KEEP_DAYS must be a non-negative integer" >&2
  exit 2
fi

cd "$ROOT_DIR"

echo "== HFD tight mode =="
echo "mode=$([[ "$APPLY" == "true" ]] && echo apply || echo dry-run)"
echo "keep_days=$KEEP_DAYS"
echo "raw_dir=$RAW_DIR"
echo

echo "== stop non-core workers =="
if [[ "$APPLY" == "true" ]]; then
  docker compose stop collector-worker paper-worker darkflow-worker waiting-worker task-worker experiment-worker || true
  docker compose rm -f collector-worker paper-worker darkflow-worker waiting-worker task-worker experiment-worker || true
else
  echo "dry-run: docker compose stop collector-worker paper-worker darkflow-worker waiting-worker task-worker experiment-worker"
  echo "dry-run: docker compose rm -f collector-worker paper-worker darkflow-worker waiting-worker task-worker experiment-worker"
fi
echo

echo "== truncate Docker JSON logs =="
if [[ "$APPLY" == "true" ]]; then
  find /var/lib/docker/containers -name '*-json.log' -type f -print -exec sh -c ': > "$1"' _ {} \;
else
  find /var/lib/docker/containers -name '*-json.log' -type f -printf '%s %p\n' 2>/dev/null | sort -n | tail -20 || true
fi
echo

echo "== raw payload size by day before prune =="
if [[ -d "$RAW_DIR" ]]; then
  find "$RAW_DIR" -mindepth 3 -maxdepth 3 -type d -print0 2>/dev/null | xargs -0 du -sh 2>/dev/null | sort -h || true
else
  echo "raw payload directory not found"
fi
echo

echo "== raw payload prune =="
if [[ -d "$RAW_DIR" ]]; then
  if [[ "$APPLY" == "true" ]]; then
    find "$RAW_DIR" -type f -mtime +"$KEEP_DAYS" -print -delete
    find "$RAW_DIR" -type d -empty -delete
  else
    echo "dry-run: files older than $KEEP_DAYS days"
    find "$RAW_DIR" -type f -mtime +"$KEEP_DAYS" -printf '%TY-%Tm-%Td %s %p\n' 2>/dev/null | sort | head -100 || true
  fi
fi
echo

echo "== final status =="
df -h /
free -h
docker compose ps
