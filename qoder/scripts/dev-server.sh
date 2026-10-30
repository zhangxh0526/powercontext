#!/usr/bin/env bash
# 科研实验续作助手：本机开发服务管理（start | stop | status | restart | logs）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT/runtime/dev-server.pid"
LOG_FILE="$ROOT/runtime/dev-server.log"
# When POWERCONTEXT_URL is exported, the caller supplies the complete environment.
# Otherwise load .env before resolving the address used by every script command.
if [[ -z "${POWERCONTEXT_URL:-}" && -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi
HOST="${ASSISTANT_HOST:-127.0.0.1}"
PORT="${ASSISTANT_PORT:-8710}"
URL="http://${HOST}:${PORT}"

running() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid
  pid="$(cat "$PID_FILE")"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

start() {
  if running; then
    echo "服务已在运行：pid=$(cat "$PID_FILE") $URL"
    return 0
  fi
  mkdir -p "$ROOT/runtime"
  cd "$ROOT"
  export ASSISTANT_HOST="$HOST" ASSISTANT_PORT="$PORT"
  nohup python3 -m backend.server >>"$LOG_FILE" 2>&1 &
  echo $! >"$PID_FILE"
  for _ in $(seq 1 40); do
    if curl -fsS --max-time 2 "$URL/api/health" >/dev/null 2>&1; then
      echo "已启动：pid=$(cat "$PID_FILE") $URL（日志 $LOG_FILE）"
      return 0
    fi
    sleep 0.25
  done
  echo "启动失败：$URL/api/health 在 10 秒内不可达，请查看 $LOG_FILE" >&2
  tail -n 20 "$LOG_FILE" >&2
  return 1
}

stop() {
  if ! running; then
    echo "服务未在运行"
    rm -f "$PID_FILE"
    return 0
  fi
  local pid
  pid="$(cat "$PID_FILE")"
  kill "$pid"
  for _ in $(seq 1 40); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.25
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo "进程 $pid 未退出，发送 SIGKILL" >&2
    kill -9 "$pid"
  fi
  rm -f "$PID_FILE"
  echo "已停止：pid=$pid"
}

status() {
  if running; then
    echo "运行中：pid=$(cat "$PID_FILE") $URL"
    curl -fsS --max-time 5 "$URL/api/health" || return 1
  else
    echo "未运行"
    return 1
  fi
}

case "${1:-start}" in
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  status) status ;;
  logs) tail -n "${2:-40}" "$LOG_FILE" ;;
  *) echo "用法: $0 {start|stop|restart|status|logs [n]}" >&2; exit 2 ;;
esac
