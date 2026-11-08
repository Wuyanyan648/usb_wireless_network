#!/usr/bin/env bash
# 安装 / 卸载开机自启：
#   - usbnicd.service  开机启动上位机后台
#   - usbnic-boot.service  检测到家庭网关硬件时自动启用（代理默认关闭）
#   - 登录时若为家庭网关模式，自动打开上位机界面
set -euo pipefail

SCRIPT=$(readlink -f "${BASH_SOURCE[0]}")
ROOT=$(cd "$(dirname "$SCRIPT")/.." && pwd)

if [[ $(id -u) -ne 0 ]]; then
  if command -v pkexec >/dev/null 2>&1; then
    exec pkexec "$SCRIPT" "$@"
  fi
  printf '需要 root 运行，请用: sudo %s\n' "$SCRIPT" >&2
  exit 1
fi

TARGET_UID="${PKEXEC_UID:-${SUDO_UID:-}}"
if [[ -z ${TARGET_UID:-} ]]; then
  TARGET_UID=$(logname 2>/dev/null | xargs -r id -u 2>/dev/null || true)
fi
[[ -n ${TARGET_UID:-} ]] || TARGET_UID=1000
TARGET_USER=$(getent passwd "$TARGET_UID" | cut -d: -f1)
TARGET_HOME=$(getent passwd "$TARGET_UID" | cut -d: -f6)
TARGET_GROUP=$(id -gn "$TARGET_USER" 2>/dev/null || true)

UNIT_DIR=/etc/systemd/system
DAEMON_UNIT="$UNIT_DIR/usbnicd.service"
BOOT_UNIT="$UNIT_DIR/usbnic-boot.service"
AUTOSTART_DIR="$TARGET_HOME/.config/autostart"
AUTOSTART_FILE="$AUTOSTART_DIR/usbnic-host.desktop"

if [[ ${1:-} == --uninstall ]]; then
  systemctl disable --now usbnic-boot.service 2>/dev/null || true
  systemctl disable --now usbnicd.service 2>/dev/null || true
  rm -f "$DAEMON_UNIT" "$BOOT_UNIT" "$AUTOSTART_FILE"
  systemctl daemon-reload
  printf '已移除开机自启和登录自动打开\n'
  exit 0
fi

chmod +x "$ROOT/scripts/host-boot.sh" "$ROOT/scripts/host-open.sh"

cat >"$DAEMON_UNIT" <<EOF
[Unit]
Description=usbnic host dashboard daemon
After=network-online.target NetworkManager.service
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$ROOT/host
ExecStart=/usr/bin/python3 -m usbnic_host.daemon --config $ROOT/host/config/host.yaml
Restart=on-failure
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

cat >"$BOOT_UNIT" <<EOF
[Unit]
Description=usbnic home gateway boot setup
After=network-online.target NetworkManager.service usbnicd.service
Wants=network-online.target usbnicd.service

[Service]
Type=oneshot
ExecStart=$ROOT/scripts/host-boot.sh --wait 90
RemainAfterExit=yes
TimeoutStartSec=180

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable usbnicd.service usbnic-boot.service

if [[ -n ${TARGET_HOME:-} ]]; then
  install -d -m 755 "$AUTOSTART_DIR"
  cat >"$AUTOSTART_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=usbnic 上位机
Comment=家庭网关模式下自动打开上位机界面
Exec=$ROOT/scripts/host-open.sh
Terminal=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
EOF
  if [[ -n ${TARGET_GROUP:-} ]]; then
    chown "$TARGET_UID:$TARGET_GROUP" "$AUTOSTART_DIR" "$AUTOSTART_FILE" 2>/dev/null || true
  fi
  chmod 644 "$AUTOSTART_FILE"
fi

printf '已安装开机自启：\n'
printf '  - usbnicd.service：开机启动上位机后台\n'
printf '  - usbnic-boot.service：检测到家庭网关硬件时自动启用（代理默认关闭）\n'
printf '  - 登录时若为家庭网关模式，自动打开 http://127.0.0.1:8787\n'
printf '\n当前手动启动的实例不受影响；下次开机会由 systemd 启动。\n'
