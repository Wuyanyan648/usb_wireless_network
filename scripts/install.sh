#!/usr/bin/env bash
# 安装 / 卸载 usbnic
set -euo pipefail

PREFIX=/usr/local
DEST="$PREFIX/lib/usbnic"
SRC=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "请使用 sudo 运行: sudo $0 $*" >&2
  exit 1
fi

if [[ ${1:-} == --uninstall ]]; then
  if [[ -x "$PREFIX/sbin/usbnic" ]]; then
    "$PREFIX/sbin/usbnic" persist uninstall || true
  fi
  rm -f "$PREFIX/sbin/usbnic"
  rm -rf "$DEST"
  if [[ " $* " == *" --purge "* ]]; then
    rm -rf /etc/usbnic
    echo "已卸载，并删除 /etc/usbnic 配置"
  else
    echo "已卸载（配置保留在 /etc/usbnic）"
  fi
  exit 0
fi

install -d "$DEST/lib" "$DEST/systemd" "$DEST/udev" "$DEST/config"
install -d "$PREFIX/sbin" /etc/usbnic

install -m755 "$SRC/usbnic" "$DEST/usbnic"
install -m644 "$SRC/lib/"*.sh "$DEST/lib/"
install -m644 "$SRC/systemd/"* "$DEST/systemd/"
install -m644 "$SRC/udev/"* "$DEST/udev/"
install -m644 "$SRC/config/"* "$DEST/config/"

ln -sfn "$DEST/usbnic" "$PREFIX/sbin/usbnic"

if [[ ! -e /etc/usbnic/usbnic.conf ]]; then
  install -m644 "$SRC/config/usbnic.conf" /etc/usbnic/usbnic.conf
  echo "已写入默认配置: /etc/usbnic/usbnic.conf"
fi

"$PREFIX/sbin/usbnic" persist install

echo
echo "安装完成。常用命令:"
echo "  usbnic list"
echo "  sudo usbnic tune balanced"
echo "  sudo usbnic diag"
