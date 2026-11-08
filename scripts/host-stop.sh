#!/usr/bin/env bash
# 一键停止 usbnic 上位机，并清理它创建的 nftables / 策略路由 / 透明代理
set -euo pipefail

SCRIPT=$(readlink -f "${BASH_SOURCE[0]}")
ROOT=$(cd "$(dirname "$SCRIPT")/.." && pwd)
HOST_DIR="$ROOT/host"
CONFIG="$HOST_DIR/config/host.yaml"
PID_FILE="$HOST_DIR/data/usbnicd.pid"

# 需要 root 才能清理 nftables 并关闭透明代理
if [[ $(id -u) -ne 0 ]]; then
  if command -v pkexec >/dev/null 2>&1; then
    exec pkexec env DISPLAY="${DISPLAY:-}" WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-}" \
      "$SCRIPT" "$@"
  fi
  printf '需要 root 运行，请用: sudo %s\n' "$SCRIPT" >&2
  exit 1
fi

stopped=0

if systemctl is-active --quiet usbnicd 2>/dev/null; then
  systemctl stop usbnicd || true
  printf 'usbnicd systemd 服务已停止\n'
  stopped=1
fi

if [[ -f $PID_FILE ]]; then
  pid=$(cat "$PID_FILE" 2>/dev/null || true)
  if [[ -n ${pid:-} ]] && ps -p "$pid" >/dev/null 2>&1 &&
    ps -p "$pid" -o args= | grep -q 'usbnic_host.daemon'; then
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 25); do
      ps -p "$pid" >/dev/null 2>&1 || break
      sleep 0.2
    done
    if ps -p "$pid" >/dev/null 2>&1; then
      kill -9 "$pid" 2>/dev/null || true
    fi
    stopped=1
  fi
  rm -f "$PID_FILE"
fi

if [[ $stopped -eq 0 ]]; then
  printf 'usbnicd 未在运行，继续清理残留资源\n'
else
  printf 'usbnicd 已停止\n'
fi

# 清理网络资源：nftables 表、ip rule、策略路由表；
# 如果代理由上位机开启，则同时关闭 TUN / TPROXY。
(cd "$HOST_DIR" && python3 -m usbnic_host.cleanup --config "$CONFIG" --proxy)

if command -v nmcli >/dev/null 2>&1 &&
  nmcli -t -f DEVICE,CONNECTION device status 2>/dev/null |
  grep -qE 'usbnic-(gateway|hotspot)'; then
  printf '提示: 网关/热点连接保持现状；如需断开，重启后在页面切换到「默认客户端」。\n'
fi
