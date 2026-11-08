#!/usr/bin/env bash
# 停止 usbnic 上位机（Phase 1 只读，无网络配置需要回滚）
set -euo pipefail

ROOT=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)
PID_FILE="$ROOT/host/data/usbnicd.pid"

if systemctl is-active --quiet usbnicd 2>/dev/null; then
  if [[ $(id -u) -eq 0 ]]; then
    systemctl stop usbnicd
  else
    sudo systemctl stop usbnicd
  fi
  printf 'usbnicd systemd 服务已停止\n'
  exit 0
fi

if [[ ! -f $PID_FILE ]]; then
  printf 'usbnicd 未在运行\n'
  exit 0
fi

pid=$(cat "$PID_FILE" 2>/dev/null || true)
if [[ -n ${pid:-} ]] && ps -p "$pid" >/dev/null 2>&1; then
  # 进程可能是 root 启动的，普通用户直接 kill 会 EPERM
  if ! kill "$pid" 2>/dev/null; then
    sudo kill "$pid" 2>/dev/null || true
  fi
  for _ in $(seq 1 25); do
    if ! ps -p "$pid" >/dev/null 2>&1; then
      break
    fi
    sleep 0.2
  done
  if ps -p "$pid" >/dev/null 2>&1; then
    if ! kill -9 "$pid" 2>/dev/null; then
      sudo kill -9 "$pid" 2>/dev/null || true
    fi
  fi
fi

rm -f "$PID_FILE"
printf 'usbnicd 已停止\n'
