#!/usr/bin/env bash
# 只监听本机。不要改成公网地址，也不要加穿透。
set -euo pipefail
cd "$(dirname "$0")"
HOST="${TRANSCRIBER_HOST:-127.0.0.1}"
PORT="${TRANSCRIBER_PORT:-8900}"
exec uv run uvicorn app.main:create_app --factory --host "$HOST" --port "$PORT"
