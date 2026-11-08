#!/usr/bin/env bash
# 启动 usbnic 上位机（Phase 1 只读监控）
set -euo pipefail

ROOT=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)
HOST_DIR="$ROOT/host"
DATA_DIR="$HOST_DIR/data"
PID_FILE="$DATA_DIR/usbnicd.pid"
LOG_FILE="$DATA_DIR/usbnicd.log"
URL="http://127.0.0.1:8787"

mkdir -p "$DATA_DIR"

if [[ -f $PID_FILE ]]; then
  old_pid=$(cat "$PID_FILE" 2>/dev/null || true)
  if [[ -n ${old_pid:-} ]] && ps -p "$old_pid" >/dev/null 2>&1; then
    printf 'usbnicd 已在运行 (PID %s): %s\n' "$old_pid" "$URL"
    exit 0
  fi
  rm -f "$PID_FILE"
fi

if ! python3 -c 'import fastapi, uvicorn, psutil, yaml' >/dev/null 2>&1; then
  printf '缺少 Python 依赖，请先安装:\n' >&2
  printf '  sudo apt install python3-fastapi python3-uvicorn python3-psutil python3-yaml\n' >&2
  printf '或:\n  python3 -m pip install fastapi uvicorn psutil PyYAML\n' >&2
  exit 1
fi

if [[ $(id -u) -ne 0 ]]; then
  printf '提示: 当前不是 root，DHCP 租约和部分 iw 数据可能不可见。\n' >&2
  printf '      需要完整数据时可用: sudo %s\n' "$0" >&2
fi

cd "$HOST_DIR"
setsid nohup python3 -m usbnic_host.daemon --config config/host.yaml "$@" >"$LOG_FILE" 2>&1 </dev/null &
echo $! >"$PID_FILE"

python3 - "$URL" <<'PY'
import sys
import time
import urllib.request

url = sys.argv[1].rstrip("/") + "/api/health"
for _ in range(50):
    try:
        with urllib.request.urlopen(url, timeout=0.5) as response:
            if response.status == 200:
                sys.exit(0)
    except Exception:
        pass
    time.sleep(0.2)
sys.exit(1)
PY
status=$?

if [[ $status -ne 0 ]]; then
  printf 'usbnicd 启动失败，最近日志:\n' >&2
  tail -n 30 "$LOG_FILE" >&2 || true
  rm -f "$PID_FILE"
  exit 1
fi

printf 'usbnicd 已启动: %s\n' "$URL"
printf '日志: %s\n' "$LOG_FILE"
printf '停止: %s\n' "$ROOT/scripts/host-stop.sh"

if command -v xdg-open >/dev/null 2>&1 && [[ -n ${DISPLAY:-}${WAYLAND_DISPLAY:-} ]]; then
  xdg-open "$URL" >/dev/null 2>&1 || true
fi
