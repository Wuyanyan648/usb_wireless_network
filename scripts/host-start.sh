#!/usr/bin/env bash
# 一键启动 usbnic 上位机
#   scripts/host-start.sh                 # 只启动控制台
#   scripts/host-start.sh gateway         # 启动并切到家庭网关
#   scripts/host-start.sh hotspot         # 启动并切到 USB WiFi AP
#   scripts/host-start.sh client          # 启动并切到默认客户端
set -euo pipefail

SCRIPT=$(readlink -f "${BASH_SOURCE[0]}")
ROOT=$(cd "$(dirname "$SCRIPT")/.." && pwd)
HOST_DIR="$ROOT/host"
CONFIG="$HOST_DIR/config/host.yaml"
DATA_DIR="$HOST_DIR/data"
PID_FILE="$DATA_DIR/usbnicd.pid"
LOG_FILE="$DATA_DIR/usbnicd.log"
URL="http://127.0.0.1:8787"
PORT=8787

# 需要 root：读取 DHCP 租约、iw 关联并修改网络。非 root 时自动提权。
if [[ $(id -u) -ne 0 ]]; then
  if command -v pkexec >/dev/null 2>&1; then
    exec pkexec env DISPLAY="${DISPLAY:-}" WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-}" \
      "$SCRIPT" "$@"
  fi
  printf '需要 root 运行，请用: sudo %s %s\n' "$SCRIPT" "$*" >&2
  exit 1
fi

usage() {
  cat <<EOF
用法: $(basename "$0") [client|hotspot|gateway] [daemon 参数]

  client    只作为普通客户端上网，有线优先、WiFi 兜底
  hotspot   USB 无线网卡开热点，本机与下游都走代理
  gateway   USB 有线网卡做家庭网关，本机与下游都走代理

不带模式参数时只启动控制台，之后可在页面里切换模式。
EOF
}

MODE=""
DAEMON_ARGS=()
while (($#)); do
  case "$1" in
    --mode)
      MODE="${2:-}"
      shift 2 || true
      ;;
    --mode=*)
      MODE="${1#--mode=}"
      shift
      ;;
    client | hotspot | gateway)
      MODE="$1"
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      DAEMON_ARGS+=("$1")
      shift
      ;;
  esac
done

if [[ -n $MODE && ! $MODE =~ ^(client|hotspot|gateway)$ ]]; then
  printf '未知模式: %s\n' "$MODE" >&2
  usage >&2
  exit 1
fi

apply_mode() {
  python3 - "$URL" "$1" <<'PY'
import json
import sys
import urllib.request

url, mode = sys.argv[1].rstrip("/"), sys.argv[2]
request = urllib.request.Request(
    url + "/api/mode",
    data=json.dumps({"mode": mode}).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(request, timeout=60) as response:
        data = json.load(response)
except Exception as exc:  # noqa: BLE001
    print(f"模式切换请求失败: {exc}", file=sys.stderr)
    sys.exit(1)

if not data.get("ok"):
    failed = [
        f"{step.get('name')}: {step.get('detail') or '失败'}"
        for step in data.get("steps", [])
        if not step.get("ok")
    ]
    print("模式部分步骤失败:\n  " + "\n  ".join(failed), file=sys.stderr)
    sys.exit(1)

print(f"当前模式: {data.get('mode_label') or mode}")
PY
}

mkdir -p "$DATA_DIR"

# 已由 systemd 管理：不再重复启动，只按需应用模式
if systemctl is-active --quiet usbnicd 2>/dev/null; then
  printf 'usbnicd systemd 服务已在运行: %s\n' "$URL"
  if [[ -n $MODE ]]; then
    printf '应用网络模式: %s\n' "$MODE"
    apply_mode "$MODE"
  fi
  if command -v xdg-open >/dev/null 2>&1 && [[ -n ${DISPLAY:-}${WAYLAND_DISPLAY:-} ]]; then
    xdg-open "$URL" >/dev/null 2>&1 || true
  fi
  exit 0
fi

# 已有实例：直接复用；带模式参数时补一次模式切换
if [[ -f $PID_FILE ]]; then
  old_pid=$(cat "$PID_FILE" 2>/dev/null || true)
  if [[ -n ${old_pid:-} ]] && ps -p "$old_pid" >/dev/null 2>&1 &&
    ps -p "$old_pid" -o args= | grep -q 'usbnic_host.daemon'; then
    printf 'usbnicd 已在运行 (PID %s): %s\n' "$old_pid" "$URL"
    if [[ -n $MODE ]]; then
      printf '应用网络模式: %s\n' "$MODE"
      apply_mode "$MODE"
    fi
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

# 启动前清理上次残留的 nftables / 策略路由，避免劫持规则叠加导致失败。
# 不关闭代理：TUN 保持原状，规则会在重新开启代理时重建。
if ! (cd "$HOST_DIR" && python3 -m usbnic_host.cleanup --config "$CONFIG"); then
  printf '警告: 启动前清理网络规则失败，继续尝试启动\n' >&2
fi

if command -v ss >/dev/null 2>&1 && ss -ltnH "sport = :$PORT" | grep -q .; then
  printf '端口 %s 已被占用，无法启动:\n' "$PORT" >&2
  ss -ltnp "sport = :$PORT" >&2 || true
  exit 1
fi

cd "$HOST_DIR"
setsid nohup python3 -m usbnic_host.daemon --config "$CONFIG" "${DAEMON_ARGS[@]}" \
  >"$LOG_FILE" 2>&1 </dev/null &
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

mode_status=0
if [[ -n $MODE ]]; then
  printf '应用网络模式: %s\n' "$MODE"
  apply_mode "$MODE" || mode_status=$?
fi

printf '停止: %s\n' "$ROOT/scripts/host-stop.sh"

if command -v xdg-open >/dev/null 2>&1 && [[ -n ${DISPLAY:-}${WAYLAND_DISPLAY:-} ]]; then
  xdg-open "$URL" >/dev/null 2>&1 || true
fi

exit "$mode_status"
