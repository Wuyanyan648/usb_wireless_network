#!/usr/bin/env bash
# usbnic 公共函数库：输出、权限、执行封装、设备发现、sysfs 读取

# ---------- 颜色 ----------
if [[ -t 1 && -z ${NO_COLOR:-} ]]; then
  C_RST=$'\e[0m'
  C_B=$'\e[1m'
  C_DIM=$'\e[2m'
  C_RED=$'\e[31m'
  C_GRN=$'\e[32m'
  C_YEL=$'\e[33m'
  C_CYN=$'\e[36m'
else
  C_RST=''
  C_B=''
  C_DIM=''
  C_RED=''
  C_GRN=''
  C_YEL=''
  C_CYN=''
fi

QUIET=${QUIET:-0}
DRY_RUN=${DRY_RUN:-0}
DEVIP=${DEVIP:-}

# ---------- 输出 ----------
log() {
  if (( QUIET )); then return 0; fi
  printf '%s\n' "$*"
}

info() {
  if (( QUIET )); then return 0; fi
  printf '%s[*]%s %s\n' "$C_CYN" "$C_RST" "$*"
}

ok() {
  if (( QUIET )); then return 0; fi
  printf '%s[+]%s %s\n' "$C_GRN" "$C_RST" "$*"
}

warn() {
  printf '%s[!]%s %s\n' "$C_YEL" "$C_RST" "$*" >&2
}

err() {
  printf '%s[x]%s %s\n' "$C_RED" "$C_RST" "$*" >&2
}

die() {
  err "$*"
  exit 1
}

title() {
  if (( QUIET )); then return 0; fi
  printf '\n%s%s== %s ==%s\n' "$C_B" "$C_CYN" "$*" "$C_RST"
}

kv() {
  if (( QUIET )); then return 0; fi
  local key=$1 val=$2 pad
  pad=$(( 24 - ${#key} ))
  if (( pad < 1 )); then pad=1; fi
  printf '  %s%*s %s\n' "$key" "$pad" '' "$val"
}

# 一行三列输出：颜色/标签/名称/说明，按字符数对齐（中文按 1 个宽度计）
row() {
  local color=$1 tag=$2 name=$3 detail=$4 width=${5:-24} pad
  pad=$(( width - ${#name} ))
  if (( pad < 1 )); then pad=1; fi
  printf '  %s%-6s%s %s%*s %s\n' "$color" "$tag" "$C_RST" "$name" "$pad" '' "$detail"
}

have() {
  command -v "$1" >/dev/null 2>&1
}

require_root() {
  if (( DRY_RUN )); then return 0; fi
  if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
    die "该操作需要 root 权限，请使用 sudo 运行"
  fi
}

# ---------- 命令执行（支持 --dry-run） ----------
run() {
  if (( DRY_RUN )); then
    printf '%s[DRY]%s %s\n' "$C_YEL" "$C_RST" "$*"
    return 0
  fi
  "$@"
}

# 执行但不因失败退出，失败时给出告警（用于批量调优）
try_do() {
  if (( DRY_RUN )); then
    printf '%s[DRY]%s %s\n' "$C_YEL" "$C_RST" "$*"
    return 0
  fi
  if ! "$@" >/dev/null 2>&1; then
    warn "未生效: $*"
    return 1
  fi
  return 0
}

# ---------- 设备与 sysfs ----------
netdev_exists() {
  [[ -e /sys/class/net/$1 ]]
}

netdev_driver() {
  local p
  p=$(readlink -f "/sys/class/net/$1/device/driver" 2>/dev/null) || return 1
  printf '%s\n' "${p##*/}"
}

usb_iface_dir() {
  local d
  d=$(readlink -f "/sys/class/net/$1/device" 2>/dev/null) || return 1
  [[ -n $d ]] || return 1
  printf '%s\n' "$d"
}

usb_dev_dir() {
  local d
  d=$(usb_iface_dir "$1") || return 1
  dirname "$d"
}

usb_attr() {
  local d
  d=$(usb_dev_dir "$1") || return 1
  cat "$d/$2" 2>/dev/null | sed 's/^[[:space:]]*//; s/[[:space:]]*$//'
}

stat_val() {
  cat "/sys/class/net/$1/statistics/$2" 2>/dev/null || printf '0'
}

# 自动识别：优先 0bda:8156，其次任意 r8152 设备
detect_dev() {
  local d name drv vid pid fallback=''
  for d in /sys/class/net/*; do
    name=${d##*/}
    [[ -e $d/device ]] || continue
    drv=$(netdev_driver "$name") || continue
    [[ $drv == r8152 ]] || continue
    vid=$(cat "$d/device/../idVendor" 2>/dev/null) || continue
    pid=$(cat "$d/device/../idProduct" 2>/dev/null) || continue
    if [[ ${vid,,} == 0bda && ${pid,,} == 8156 ]]; then
      printf '%s\n' "$name"
      return 0
    fi
    if [[ -z $fallback ]]; then fallback=$name; fi
  done
  if [[ -n $fallback ]]; then
    printf '%s\n' "$fallback"
    return 0
  fi
  return 1
}

resolve_dev() {
  if [[ -n $DEVIP ]]; then
    netdev_exists "$DEVIP" || die "网卡不存在: $DEVIP"
    printf '%s\n' "$DEVIP"
    return 0
  fi
  local d
  d=$(detect_dev) || die "未检测到 r8152 USB 网卡（0bda:8156），请确认设备已插入或使用 -d <网卡名> 指定"
  printf '%s\n' "$d"
}

# 从 ethtool 输出中取单行字段，如 "Speed" / "Duplex" / "Wake-on"
eth_field() {
  ethtool "$1" 2>/dev/null | awk -v key="$2" '
    {
      line = $0
      sub(/^[ \t]+/, "", line)
      if (index(line, key ":") == 1) {
        sub(/^[^:]*:[ \t]*/, "", line)
        print line
        exit
      }
    }'
}

permanent_mac() {
  ethtool -P "$1" 2>/dev/null | awk -F': ' '/Permanent address/{print $2; exit}'
}

link_text() {
  local dev=$1 carrier spd dup
  carrier=$(cat "/sys/class/net/$dev/carrier" 2>/dev/null || printf '0')
  if [[ $carrier != 1 ]]; then
    printf '无连接'
    return 0
  fi
  spd=$(cat "/sys/class/net/$dev/speed" 2>/dev/null || printf '%s' '-1')
  dup=$(eth_field "$dev" "Duplex")
  if [[ $spd == -1 || -z $spd ]]; then
    printf '已连接（速率未知）'
    return 0
  fi
  printf '%s Mb/s %s' "$spd" "${dup:-?}"
}

usb_speed_text() {
  case ${1:-0} in
    10000) printf '10000 Mb/s (USB 3.2 Gen2)' ;;
    5000) printf '5000 Mb/s (USB 3.2 Gen1)' ;;
    480) printf '480 Mb/s (USB 2.0)' ;;
    12) printf '12 Mb/s (USB 1.1)' ;;
    1 | 1.5) printf '1.5 Mb/s (USB 1.0)' ;;
    *) printf '%s Mb/s' "${1:-?}" ;;
  esac
}

bytes_human() {
  awk -v b="${1:-0}" 'BEGIN{
    split("B KiB MiB GiB TiB", u, " ")
    i = 1
    while (b >= 1024 && i < 5) { b /= 1024; i++ }
    if (i == 1) printf "%.0f %s", b, u[i]
    else printf "%.2f %s", b, u[i]
  }'
}

# 读取 / 设置 USB 自动挂起（on=禁用挂起，auto=允许挂起）
usb_power_state() {
  local d
  d=$(usb_dev_dir "$1") || return 1
  cat "$d/power/control" 2>/dev/null
}

set_usb_power() {
  local dev=$1 mode=$2 d path
  d=$(usb_dev_dir "$dev") || {
    warn "无法定位 USB 设备目录"
    return 1
  }
  path="$d/power/control"
  if (( DRY_RUN )); then
    printf '%s[DRY]%s write %s <- %s\n' "$C_YEL" "$C_RST" "$path" "$mode"
    return 0
  fi
  if [[ ! -w $path ]]; then
    warn "无法写入 $path"
    return 1
  fi
  printf '%s\n' "$mode" >"$path" || {
    warn "写入 $path 失败"
    return 1
  }
}
