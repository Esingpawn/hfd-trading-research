#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

if [ ! -f .env ]; then
  cp .env.production.example .env
  echo "Created .env from .env.production.example. Edit secrets before production use."
fi

docker compose build
docker compose up -d postgres redis
docker compose run --rm api alembic upgrade head
docker compose up -d api collector-worker paper-worker experiment-worker

echo "HFD stack is starting."
echo "Dashboard: http://127.0.0.1:8000/dashboard"
echo "Status: docker compose ps"
