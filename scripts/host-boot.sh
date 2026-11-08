#!/usr/bin/env bash
# 开机检测家庭网关硬件并自动启用（systemd 调用，代理默认关闭）
#   scripts/host-boot.sh --dry-run   # 只检测，不修改网络
set -euo pipefail

SCRIPT=$(readlink -f "${BASH_SOURCE[0]}")
ROOT=$(cd "$(dirname "$SCRIPT")/.." && pwd)
HOST_DIR="$ROOT/host"
CONFIG="$HOST_DIR/config/host.yaml"

if [[ $(id -u) -ne 0 ]]; then
  if command -v pkexec >/dev/null 2>&1; then
    exec pkexec "$SCRIPT" "$@"
  fi
  printf '需要 root 运行，请用: sudo %s %s\n' "$SCRIPT" "$*" >&2
  exit 1
fi

cd "$HOST_DIR"
exec python3 -m usbnic_host.boot --config "$CONFIG" "$@"
