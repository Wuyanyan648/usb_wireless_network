#!/usr/bin/env bash
# 信息展示、能力矩阵、诊断、计数器

declare -gA S1=()
declare -gA S2=()

# ---------- 解析辅助 ----------
ring_max() {
  ethtool -g "$1" 2>/dev/null | awk 'tolower($1) == "rx:" {print $2; exit}'
}

ring_current() {
  ethtool -g "$1" 2>/dev/null | awk 'tolower($1) == "rx:" {n++; if (n == 2) {print $2; exit}}'
}

coal_rx_usecs() {
  ethtool -c "$1" 2>/dev/null | awk -F': *' '/^[ \t]*rx-usecs:/{gsub(/[ \t]/, "", $2); print $2; exit}'
}

eee_state() {
  local out
  out=$(ethtool --show-eee "$1" 2>/dev/null) || {
    printf '不支持'
    return 0
  }
  if grep -q 'EEE status: enabled' <<<"$out"; then
    printf 'enabled'
  elif grep -q 'EEE status: disabled' <<<"$out"; then
    printf 'disabled'
  else
    printf '未知'
  fi
}

wol_state() {
  eth_field "$1" "Wake-on"
}

wol_text() {
  local s
  s=$(wol_state "$1")
  printf '%s' "${s:-不支持}"
}

offload_summary() {
  local f out=()
  f=$(ethtool -k "$1" 2>/dev/null)
  if grep -q '^rx-checksumming: on' <<<"$f"; then out+=(rx-csum); fi
  if grep -q '^tx-checksumming: on' <<<"$f"; then out+=(tx-csum); fi
  if grep -q '^scatter-gather: on' <<<"$f"; then out+=(sg); fi
  if grep -q '^tcp-segmentation-offload: on' <<<"$f"; then out+=(tso); fi
  if grep -q '^generic-segmentation-offload: on' <<<"$f"; then out+=(gso); fi
  if grep -q '^generic-receive-offload: on' <<<"$f"; then out+=(gro); fi
  if grep -q '^rx-vlan-offload: on' <<<"$f"; then out+=(rxvlan); fi
  if grep -q '^tx-vlan-offload: on' <<<"$f"; then out+=(txvlan); fi
  if [[ ${#out[@]} -eq 0 ]]; then
    printf '全部关闭'
  else
    printf '%s' "${out[*]}"
  fi
}

err_total() {
  local d=$1
  printf '%s' "$(( $(stat_val "$d" rx_errors) + $(stat_val "$d" tx_errors) + $(stat_val "$d" rx_dropped) + $(stat_val "$d" tx_dropped) ))"
}

# 采集一次快照到指定关联数组（bash 4.3+ 的 nameref）
snap_fill() {
  local -n __arr=$1
  local __dev=$2 __k __v
  __arr=()
  while IFS=: read -r __k __v; do
    __k=${__k//[[:space:]]/}
    __v=${__v//[[:space:]]/}
    if [[ $__k =~ ^[A-Za-z0-9_]+$ && $__v =~ ^[0-9]+$ ]]; then
      __arr[$__k]=$__v
    fi
  done < <(ethtool -S "$__dev" 2>/dev/null)
  __arr[rx_bytes]=$(stat_val "$__dev" rx_bytes)
  __arr[tx_bytes]=$(stat_val "$__dev" tx_bytes)
  __arr[rx_dropped]=$(stat_val "$__dev" rx_dropped)
  __arr[tx_dropped]=$(stat_val "$__dev" tx_dropped)
  __arr[rx_errors]=$(stat_val "$__dev" rx_errors)
  __arr[tx_errors]=$(stat_val "$__dev" tx_errors)
}

rate_mbps() {
  awk -v b="${1:-0}" -v s="${2:-1}" 'BEGIN{ if (s <= 0) s = 1; printf "%.2f", b * 8 / s / 1000000 }'
}

rate_pps() {
  awk -v p="${1:-0}" -v s="${2:-1}" 'BEGIN{ if (s <= 0) s = 1; printf "%.0f", p / s }'
}

# ---------- list ----------
cmd_list() {
  printf '%-16s %-9s %-14s %-11s %-10s %-9s %s\n' \
    "IFACE" "TYPE" "DRIVER" "VID:PID" "USB" "STATE" "LINK"
  local d name drv vid pid spd usbspd state type link
  for d in /sys/class/net/*; do
    name=${d##*/}
    [[ -e $d/device ]] || continue
    [[ -f $d/device/../idVendor ]] || continue
    vid=$(cat "$d/device/../idVendor" 2>/dev/null || printf '?')
    pid=$(cat "$d/device/../idProduct" 2>/dev/null || printf '?')
    drv=$(netdev_driver "$name" 2>/dev/null || printf '?')
    usbspd=$(cat "$d/device/../speed" 2>/dev/null || printf '%s' '-1')
    spd=$(cat "$d/speed" 2>/dev/null || printf '%s' '-1')
    state=$(cat "$d/operstate" 2>/dev/null || printf '?')
    case $drv in
      r8152) type="Ethernet" ;;
      rtw88* | rtl8* | mt76* | ath9k* | ath10k* | brcmfmac) type="Wi-Fi" ;;
      cdc_ether | cdc_ncm | rndis_host | ax88179_178a | aqc111 | cdc_mbim) type="Ethernet" ;;
      *) type="USB-Net" ;;
    esac
    if [[ $spd == -1 || -z $spd ]]; then
      link="-"
    else
      link="${spd} Mb/s"
    fi
    printf '%-16s %-9s %-14s %-11s %-10s %-9s %s\n' \
      "$name" "$type" "$drv" "${vid}:${pid}" "$(usb_speed_text "$usbspd" | cut -d' ' -f1) Mb/s" "$state" "$link"
  done
}

# ---------- info ----------
cmd_info() {
  if [[ -n ${1:-} ]]; then DEVIP=$1; fi
  local dev
  dev=$(resolve_dev)

  local drv ver fw perm mac mtu state usbdir
  drv=$(netdev_driver "$dev" 2>/dev/null || printf '?')
  ver=$(ethtool -i "$dev" 2>/dev/null | awk -F': ' '/^version:/{print $2; exit}')
  fw=$(ethtool -i "$dev" 2>/dev/null | awk -F': ' '/^firmware-version:/{print $2; exit}')
  perm=$(permanent_mac "$dev")
  mac=$(cat "/sys/class/net/$dev/address" 2>/dev/null)
  mtu=$(cat "/sys/class/net/$dev/mtu" 2>/dev/null)
  state=$(cat "/sys/class/net/$dev/operstate" 2>/dev/null)
  usbdir=$(usb_dev_dir "$dev")

  title "设备"
  kv "网卡" "$dev"
  kv "产品" "$(usb_attr "$dev" product)"
  kv "VID:PID" "$(usb_attr "$dev" idVendor):$(usb_attr "$dev" idProduct)"
  kv "序列号" "$(usb_attr "$dev" serial)"
  kv "USB 路径" "${usbdir#/sys/devices/}"
  kv "USB 速率" "$(usb_speed_text "$(usb_attr "$dev" speed)")"
  kv "USB 版本" "USB $(usb_attr "$dev" version)"
  kv "驱动/版本" "$drv $ver"
  kv "固件" "${fw:-未知}"

  title "链路"
  kv "状态" "$state"
  kv "连接" "$(link_text "$dev")"
  kv "自协商" "$(eth_field "$dev" "Auto-negotiation")"
  kv "端口类型" "$(eth_field "$dev" "Port")"
  kv "MAC（当前）" "$mac"
  kv "MAC（永久）" "${perm:-未知}"
  kv "MTU" "$mtu"

  title "地址"
  if have ip; then
    if ip -br addr show dev "$dev" 2>/dev/null | grep -q 'inet'; then
      ip -br addr show dev "$dev" 2>/dev/null | sed 's/^/  /'
    else
      printf '  （未配置 IP 地址）\n'
    fi
  fi

  title "调优"
  kv "RX 环形" "$(ring_current "$dev") / 最大 $(ring_max "$dev")"
  kv "rx-usecs" "$(coal_rx_usecs "$dev")"
  kv "txqueuelen" "$(cat "/sys/class/net/$dev/tx_queue_len" 2>/dev/null)"
  kv "硬件卸载" "$(offload_summary "$dev")"
  kv "EEE" "$(eee_state "$dev")"
  kv "USB 自动挂起" "$(usb_power_state "$dev")"
  kv "WOL" "$(wol_text "$dev")"
}

# ---------- status ----------
cmd_status() {
  if [[ -n ${1:-} ]]; then DEVIP=$1; fi
  local dev
  dev=$(resolve_dev)
  local mac mtu rx tx ip4
  mac=$(cat "/sys/class/net/$dev/address" 2>/dev/null)
  mtu=$(cat "/sys/class/net/$dev/mtu" 2>/dev/null)
  rx=$(stat_val "$dev" rx_bytes)
  tx=$(stat_val "$dev" tx_bytes)
  ip4=$(ip -4 -o addr show dev "$dev" 2>/dev/null | awk '{print $4}' | paste -sd, -)

  title "状态"
  kv "网卡" "$dev"
  kv "链路" "$(link_text "$dev")"
  kv "MAC / MTU" "$mac / $mtu"
  kv "RX / TX" "$(bytes_human "$rx") / $(bytes_human "$tx")"
  kv "错误 / 丢包" "$(( $(stat_val "$dev" rx_errors) + $(stat_val "$dev" tx_errors) )) / $(( $(stat_val "$dev" rx_dropped) + $(stat_val "$dev" tx_dropped) ))"
  kv "IPv4" "${ip4:--}"
  kv "USB 速率" "$(usb_speed_text "$(usb_attr "$dev" speed)")"
}

# ---------- caps ----------
cap_mark() {
  row "$2" "$1" "$3" "$4" 30
}

cmd_caps() {
  local dev
  dev=$(resolve_dev)
  local feat modes ts eee out

  feat=$(ethtool -k "$dev" 2>/dev/null)
  modes=$(ethtool "$dev" 2>/dev/null)
  ts=$(ethtool -T "$dev" 2>/dev/null)

  title "能力矩阵: $dev"

  if grep -q '2500baseT/Full' <<<"$modes"; then
    cap_mark "YES" "$C_GRN" "链路速率" "10/100/1000/2500baseT，自协商"
  else
    cap_mark "NO" "$C_RED" "链路速率" "不支持 2500baseT"
  fi

  if ethtool -a "$dev" >/dev/null 2>&1; then
    cap_mark "YES" "$C_GRN" "流控（暂停帧）" "支持配置（当前 TX/RX 都关闭）"
  else
    cap_mark "NO" "$C_RED" "流控（暂停帧）" "驱动未暴露"
  fi

  if grep -q '^rx-checksumming: on' <<<"$feat"; then
    cap_mark "YES" "$C_GRN" "校验和卸载" "RX/TX checksum"
  else
    cap_mark "NO" "$C_YEL" "校验和卸载" "当前关闭，可 offload on 开启"
  fi

  if grep -q '^tcp-segmentation-offload: on' <<<"$feat"; then
    cap_mark "YES" "$C_GRN" "TSO/GSO/GRO/SG" "已开启"
  else
    cap_mark "NO" "$C_YEL" "TSO/GSO/GRO/SG" "当前关闭，可 offload on 开启"
  fi

  if grep -q '^rx-vlan-offload: on' <<<"$feat"; then
    cap_mark "YES" "$C_GRN" "VLAN 卸载" "RX/TX VLAN tag"
  else
    cap_mark "NO" "$C_YEL" "VLAN 卸载" "当前关闭"
  fi

  cap_mark "YES" "$C_GRN" "RX 环形缓冲" "100 - $(ring_max "$dev")（当前 $(ring_current "$dev")）"
  cap_mark "YES" "$C_GRN" "中断合并" "rx-usecs（当前 $(coal_rx_usecs "$dev")）"

  eee=$(eee_state "$dev")
  if [[ $eee == 不支持 ]]; then
    cap_mark "NO" "$C_RED" "EEE 节能以太网" "驱动未暴露"
  else
    cap_mark "YES" "$C_GRN" "EEE 节能以太网" "当前 $eee"
  fi

  out=$(wol_state "$dev")
  if [[ -n $out ]]; then
    cap_mark "YES" "$C_GRN" "Wake-on-LAN" "当前 $out"
  else
    cap_mark "NO" "$C_YEL" "Wake-on-LAN" "驱动未暴露 WOL 参数"
  fi

  if grep -q 'PTP Hardware Clock: none' <<<"$ts"; then
    cap_mark "INFO" "$C_CYN" "时间戳" "仅软件时间戳，无 PTP 硬件时钟"
  else
    cap_mark "YES" "$C_GRN" "时间戳" "支持硬件时间戳"
  fi

  if ethtool -l "$dev" >/dev/null 2>&1; then
    cap_mark "YES" "$C_GRN" "多队列 / RSS" "支持"
  else
    cap_mark "NO" "$C_RED" "多队列 / RSS" "单队列驱动，内核固定关闭"
  fi

  if ethtool --show-priv-flags "$dev" >/dev/null 2>&1; then
    cap_mark "YES" "$C_GRN" "私有 flags" "支持"
  else
    cap_mark "NO" "$C_RED" "私有 flags" "无"
  fi

  if ethtool --show-fec "$dev" >/dev/null 2>&1; then
    cap_mark "YES" "$C_GRN" "FEC" "支持"
  else
    cap_mark "NO" "$C_RED" "FEC" "USB 网卡不支持"
  fi

  cap_mark "YES" "$C_GRN" "巨型帧" "MTU 68-9000（以驱动上限为准）"
  cap_mark "YES" "$C_GRN" "USB 自动挂起控制" "power/control"
  cap_mark "YES" "$C_GRN" "MAC 地址修改" "ip link set address"

  printf '\n  说明：RTL8156 为单队列 USB 网卡，不支持 RSS/多队列、硬件 PTP、FEC、\n'
  printf '        硬件环回（loopback 固定 off），这些项无法通过脚本启用。\n'
}

# ---------- link ----------
cmd_link() {
  local dev
  dev=$(resolve_dev)
  title "链路协商: $dev"
  ethtool "$dev" 2>/dev/null | sed 's/^/  /' || true
  if have ip; then
    title "接口状态"
    ip -br link show dev "$dev" 2>/dev/null | sed 's/^/  /' || true
  fi
}

# ---------- stats ----------
cmd_stats() {
  local dev
  dev=$(resolve_dev)
  local delta=0
  if [[ ${1:-} == --delta ]]; then
    delta=${2:-2}
  fi
  [[ $delta =~ ^[0-9]+$ ]] || delta=2

  if (( delta > 0 )); then
    snap_fill S1 "$dev"
    sleep "$delta"
    snap_fill S2 "$dev"
    local rx_b tx_b rx_p tx_p err_d
    rx_b=$(( ${S2[rx_bytes]:-0} - ${S1[rx_bytes]:-0} ))
    tx_b=$(( ${S2[tx_bytes]:-0} - ${S1[tx_bytes]:-0} ))
    rx_p=$(( ${S2[rx_packets]:-0} - ${S1[rx_packets]:-0} ))
    tx_p=$(( ${S2[tx_packets]:-0} - ${S1[tx_packets]:-0} ))
    err_d=$(( ( ${S2[rx_errors]:-0} + ${S2[tx_errors]:-0} + ${S2[rx_dropped]:-0} + ${S2[tx_dropped]:-0} ) \
            - ( ${S1[rx_errors]:-0} + ${S1[tx_errors]:-0} + ${S1[rx_dropped]:-0} + ${S1[tx_dropped]:-0} ) ))

    title "区间统计（${delta}s）"
    kv "RX 速率" "$(rate_mbps "$rx_b" "$delta") Mbps"
    kv "TX 速率" "$(rate_mbps "$tx_b" "$delta") Mbps"
    kv "RX 包速率" "$(rate_pps "$rx_p" "$delta") pps"
    kv "TX 包速率" "$(rate_pps "$tx_p" "$delta") pps"
    kv "新增错误/丢包" "$err_d"
    kv "链路" "$(link_text "$dev")"
  else
    title "计数器 (ethtool -S)"
    ethtool -S "$dev" 2>/dev/null | sed 's/^/  /'
    title "汇总"
    kv "RX 字节" "$(bytes_human "$(stat_val "$dev" rx_bytes)")"
    kv "TX 字节" "$(bytes_human "$(stat_val "$dev" tx_bytes)")"
    kv "RX 错误" "$(stat_val "$dev" rx_errors)"
    kv "TX 错误" "$(stat_val "$dev" tx_errors)"
    kv "RX 丢包" "$(stat_val "$dev" rx_dropped)"
    kv "TX 丢包" "$(stat_val "$dev" tx_dropped)"
  fi
}

# ---------- watch ----------
cmd_watch() {
  local dev
  dev=$(resolve_dev)
  local interval=${1:-1} count=${2:-0}
  [[ $interval =~ ^[0-9]+([.][0-9]+)?$ ]] || die "间隔秒数格式错误: $interval"
  [[ $count =~ ^[0-9]+$ ]] || die "次数必须为整数: $count"

  local i=0 rx_b tx_b rx_p tx_p err_d
  trap 'printf "\n"; exit 0' INT
  while :; do
    snap_fill S1 "$dev"
    sleep "$interval"
    snap_fill S2 "$dev"
    rx_b=$(( ${S2[rx_bytes]:-0} - ${S1[rx_bytes]:-0} ))
    tx_b=$(( ${S2[tx_bytes]:-0} - ${S1[tx_bytes]:-0} ))
    rx_p=$(( ${S2[rx_packets]:-0} - ${S1[rx_packets]:-0} ))
    tx_p=$(( ${S2[tx_packets]:-0} - ${S1[tx_packets]:-0} ))
    err_d=$(( ( ${S2[rx_errors]:-0} + ${S2[tx_errors]:-0} + ${S2[rx_dropped]:-0} + ${S2[tx_dropped]:-0} ) \
            - ( ${S1[rx_errors]:-0} + ${S1[tx_errors]:-0} + ${S1[rx_dropped]:-0} + ${S1[tx_dropped]:-0} ) ))

    printf '%s  %-18s RX %9s Mbps  TX %9s Mbps  rx %7s pps  tx %7s pps  err %s\n' \
      "$(date +%H:%M:%S)" "$(link_text "$dev")" \
      "$(rate_mbps "$rx_b" "$interval")" "$(rate_mbps "$tx_b" "$interval")" \
      "$(rate_pps "$rx_p" "$interval")" "$(rate_pps "$tx_p" "$interval")" "$err_d"

    i=$((i + 1))
    if (( count > 0 && i >= count )); then
      break
    fi
  done
}

# ---------- diag ----------
DIAG_WARN=0
DIAG_FAIL=0
DIAG_TIPS=()

d_pass() { row "$C_GRN" "PASS" "$1" "${2:-}" 24; }
d_info() { row "$C_CYN" "INFO" "$1" "${2:-}" 24; }
d_warn() {
  DIAG_WARN=$((DIAG_WARN + 1))
  row "$C_YEL" "WARN" "$1" "${2:-}" 24
  if [[ -n ${2:-} ]]; then DIAG_TIPS+=("$1: $2"); fi
}
d_fail() {
  DIAG_FAIL=$((DIAG_FAIL + 1))
  row "$C_RED" "FAIL" "$1" "${2:-}" 24
  if [[ -n ${2:-} ]]; then DIAG_TIPS+=("$1: $2"); fi
}

cmd_diag() {
  local dev
  dev=$(resolve_dev)
  DIAG_WARN=0
  DIAG_FAIL=0
  DIAG_TIPS=()

  local drv fw usbspeed carrier spd dup mtu ip4 autosuspend eee feat ring rxusecs tip
  drv=$(netdev_driver "$dev" 2>/dev/null || printf '?')
  fw=$(ethtool -i "$dev" 2>/dev/null | awk -F': ' '/^firmware-version:/{print $2; exit}')
  usbspeed=$(usb_attr "$dev" speed)
  carrier=$(cat "/sys/class/net/$dev/carrier" 2>/dev/null || printf '0')
  spd=$(cat "/sys/class/net/$dev/speed" 2>/dev/null || printf '%s' '-1')
  dup=$(eth_field "$dev" "Duplex")
  mtu=$(cat "/sys/class/net/$dev/mtu" 2>/dev/null)
  ip4=$(ip -4 -o addr show dev "$dev" 2>/dev/null | awk '{print $4}' | head -1)
  autosuspend=$(usb_power_state "$dev")
  eee=$(eee_state "$dev")
  feat=$(ethtool -k "$dev" 2>/dev/null)
  ring=$(ring_current "$dev")
  rxusecs=$(coal_rx_usecs "$dev")

  title "健康检查: $dev"

  if [[ $drv == r8152 ]]; then
    d_pass "驱动" "r8152"
  else
    d_fail "驱动" "当前为 $drv，不是 r8152"
  fi

  case ${fw:-} in
    '' | N/A) d_warn "固件" "未读到固件版本" ;;
    *) d_pass "固件" "$fw" ;;
  esac

  if [[ ${usbspeed:-0} -ge 5000 ]]; then
    d_pass "USB 链路" "$(usb_speed_text "$usbspeed")"
  elif [[ ${usbspeed:-0} -eq 480 ]]; then
    d_fail "USB 链路" "仅 USB 2.0，最高约 300 Mbps，需换 USB3 口/线"
  else
    d_warn "USB 链路" "$(usb_speed_text "$usbspeed")"
  fi

  if [[ $carrier == 1 ]]; then
    d_pass "网线连接" "已连接"
    if [[ $spd == 2500 ]]; then
      d_pass "协商速率" "2500 Mb/s $dup"
    elif [[ $spd == 1000 ]]; then
      d_warn "协商速率" "1000 Mb/s，检查网线(需 Cat5e+)、对端 2.5G 能力"
    else
      d_warn "协商速率" "${spd} Mb/s $dup"
    fi
    if [[ $dup == Full ]]; then
      d_pass "双工" "Full"
    else
      d_warn "双工" "${dup:-未知}"
    fi
  else
    d_warn "网线连接" "未检测到载波，速率/丢包测试无法进行"
  fi

  local errs
  errs=$(err_total "$dev")
  if [[ $errs -eq 0 ]]; then
    d_pass "错误/丢包" "0"
  else
    d_warn "错误/丢包" "累计 $errs（若持续增长需排查线缆/供电）"
  fi

  if grep -q '^tcp-segmentation-offload: on' <<<"$feat" &&
    grep -q '^generic-receive-offload: on' <<<"$feat" &&
    grep -q '^rx-checksumming: on' <<<"$feat" &&
    grep -q '^tx-checksumming: on' <<<"$feat"; then
    d_pass "硬件卸载" "TSO/GSO/GRO/校验和已开启"
  else
    d_warn "硬件卸载" "部分关闭，执行: sudo usbnic offload on"
  fi

  if [[ ${ring:-0} -ge 2048 ]]; then
    d_pass "RX 环形" "$ring"
  else
    d_warn "RX 环形" "$ring，建议 2048-4096：sudo usbnic ring 4096"
  fi

  if [[ ${rxusecs:-99999} -le 100 ]]; then
    d_pass "中断合并" "rx-usecs=$rxusecs"
  else
    d_warn "中断合并" "rx-usecs=$rxusecs，低延迟可设 0：sudo usbnic irq 0"
  fi

  if [[ $eee == enabled ]]; then
    d_warn "EEE" "已开启，部分交换机下会引起掉速/抖动：sudo usbnic eee off"
  else
    d_pass "EEE" "$eee"
  fi

  if [[ $autosuspend == on ]]; then
    d_pass "USB 电源" "自动挂起已禁用"
  else
    d_warn "USB 电源" "允许自动挂起，可能掉链路：sudo usbnic power on"
  fi

  if [[ ${mtu:-0} -ge 1500 ]]; then
    d_pass "MTU" "$mtu"
  else
    d_warn "MTU" "$mtu < 1500"
  fi

  if [[ -n $ip4 ]]; then
    d_pass "IPv4" "$ip4"
  else
    d_warn "IPv4" "未配置地址，可执行: sudo usbnic net dhcp"
  fi

  if have nmcli; then
    tip=$(nmcli -t -f GENERAL.STATE device show "$dev" 2>/dev/null | head -1)
    if [[ -n $tip ]]; then
      d_info "NetworkManager" "$tip"
    fi
  fi

  printf '\n'
  if (( DIAG_FAIL > 0 )); then
    err "检查完成：$DIAG_FAIL 项失败，$DIAG_WARN 项警告"
  elif (( DIAG_WARN > 0 )); then
    warn "检查完成：$DIAG_WARN 项警告"
  else
    ok "检查完成：全部通过"
  fi

  if [[ ${#DIAG_TIPS[@]} -gt 0 ]]; then
    printf '\n建议:\n'
    local t
    for t in "${DIAG_TIPS[@]}"; do
      printf '  - %s\n' "$t"
    done
  fi

  if (( DIAG_FAIL > 0 )); then
    return 1
  fi
  return 0
}
