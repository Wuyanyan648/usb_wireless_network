#!/usr/bin/env bash
# 链路、卸载、环形缓冲、中断合并、节能、MAC、MTU、档位

# ---------- 接口启停 ----------
set_link_state() {
  local dev action=$1
  dev=$(resolve_dev)
  require_root
  if ! run ip link set dev "$dev" "$action"; then
    warn "设置接口状态失败"
    return 1
  fi
  if [[ $action == up ]]; then
    ok "已启用 $dev"
  else
    ok "已关闭 $dev"
  fi
}

cmd_up() { set_link_state up; }
cmd_down() { set_link_state down; }

# ---------- TX 队列 ----------
cmd_txq() {
  local dev n=${1:-}
  dev=$(resolve_dev)
  if [[ -z $n ]]; then
    kv "txqueuelen" "$(cat "/sys/class/net/$dev/tx_queue_len" 2>/dev/null)"
    return 0
  fi
  if [[ ! $n =~ ^[0-9]+$ ]] || (( n < 1 || n > 100000 )); then
    die "txqueuelen 需为 1-100000 的整数"
  fi
  require_root
  if ! run ip link set dev "$dev" txqueuelen "$n"; then
    warn "设置 txqueuelen 失败"
    return 1
  fi
  ok "txqueuelen 已设置为 $n"
}

# ---------- 速率 ----------
apply_speed() {
  local dev=$1 mode=$2 force=${3:-0}
  local -a adv=() cmd=()

  case $mode in
    auto)
      adv=(10baseT/Half 10baseT/Full 100baseT/Half 100baseT/Full 1000baseT/Full 2500baseT/Full)
      ;;
    10 | 100)
      adv=("${mode}baseT/Half" "${mode}baseT/Full")
      ;;
    1000 | 2500)
      adv=("${mode}baseT/Full")
      ;;
    *)
      die "不支持的速率: $mode（可选 auto|10|100|1000|2500）"
      ;;
  esac

  if (( force )); then
    if [[ $mode == auto ]]; then
      die "--force 不能与 auto 一起使用"
    fi
    if [[ $mode == 2500 ]]; then
      warn "2500baseT 规范要求自协商，强制模式可能被驱动拒绝"
    fi
    cmd=(ethtool -s "$dev" speed "$mode" duplex full autoneg off)
  else
    cmd=(ethtool -s "$dev" autoneg on advertise "${adv[@]}")
  fi

  if ! run "${cmd[@]}"; then
    warn "速率设置失败；若提示设备忙，可先执行: ip link set $dev down"
    return 1
  fi
  return 0
}

cmd_speed() {
  local mode=${1:-}
  if [[ -z $mode ]]; then
    die "用法: usbnic speed <auto|10|100|1000|2500> [--force]"
  fi
  shift || true
  local force=0
  while (( $# )); do
    case $1 in
      --force | -f) force=1 ;;
      *) die "未知参数: $1" ;;
    esac
    shift
  done
  local dev
  dev=$(resolve_dev)
  require_root
  apply_speed "$dev" "$mode" "$force"
  ok "速率模式已设置为: $mode"
}

cmd_restart() {
  local dev
  dev=$(resolve_dev)
  require_root
  if ! run ethtool -r "$dev"; then
    warn "重新自协商失败"
    return 1
  fi
  ok "已重新触发自协商"
}

# ---------- EEE ----------
cmd_eee() {
  local dev action=${1:-show}
  dev=$(resolve_dev)
  case $action in
    show | '')
      title "EEE: $dev"
      ethtool --show-eee "$dev" 2>/dev/null | sed 's/^/  /' || warn "该设备不支持 EEE 查询"
      ;;
    on | off)
      require_root
      if ! run ethtool --set-eee "$dev" eee "$action"; then
        warn "设置 EEE 失败（可能链路未建立或对端不支持）"
        return 1
      fi
      ok "EEE 已设置为 $action"
      ;;
    *)
      die "用法: usbnic eee <on|off|show>"
      ;;
  esac
}

# ---------- WOL ----------
cmd_wol() {
  local dev action=${1:-show}
  dev=$(resolve_dev)
  case $action in
    show | '')
      local st
      st=$(wol_state "$dev")
      if [[ -z $st ]]; then
        warn "该驱动未暴露 Wake-on-LAN 能力"
      else
        kv "Wake-on" "$st"
      fi
      ;;
    g | d | off)
      require_root
      local v=$action
      if [[ $action == off ]]; then v=d; fi
      if ! run ethtool -s "$dev" wol "$v"; then
        warn "设置 WOL 失败（驱动可能不支持）"
        return 1
      fi
      ok "WOL 已设置为 $v（g=魔术包唤醒，d=关闭）"
      ;;
    *)
      die "用法: usbnic wol <g|d|off|show>"
      ;;
  esac
}

# ---------- MTU ----------
cmd_mtu() {
  local dev n=${1:-}
  dev=$(resolve_dev)
  if [[ -z $n ]]; then
    die "用法: usbnic mtu <68-9000>"
  fi
  if [[ ! $n =~ ^[0-9]+$ ]]; then
    die "MTU 必须为整数: $n"
  fi
  if (( n < 68 || n > 9000 )); then
    die "MTU 超出范围 68-9000"
  fi
  require_root
  if ! run ip link set dev "$dev" mtu "$n"; then
    warn "设置失败，驱动可能不支持该 MTU"
    return 1
  fi
  ok "MTU 已设置为 $n（巨型帧要求链路两端一致）"
}

# ---------- MAC ----------
cmd_mac() {
  local dev val=${1:-}
  dev=$(resolve_dev)
  case $val in
    '' | reset)
      val=$(permanent_mac "$dev")
      if [[ -z $val ]]; then
        die "无法读取永久 MAC 地址"
      fi
      ;;
    random)
      val=$(printf '02:%02x:%02x:%02x:%02x:%02x' \
        $((RANDOM % 256)) $((RANDOM % 256)) $((RANDOM % 256)) $((RANDOM % 256)) $((RANDOM % 256)))
      ;;
    *)
      if [[ ! $val =~ ^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$ ]]; then
        die "MAC 格式错误，应形如 02:11:22:33:44:55"
      fi
      ;;
  esac
  require_root
  if ! run ip link set dev "$dev" address "$val"; then
    warn "设置 MAC 失败"
    return 1
  fi
  ok "MAC 已设置为 $val"
}

# ---------- 环形缓冲 ----------
cmd_ring() {
  local dev n=${1:-}
  dev=$(resolve_dev)
  if [[ -z $n ]]; then
    title "RX 环形: $dev"
    ethtool -g "$dev" 2>/dev/null | sed 's/^/  /' || warn "无法读取环形参数"
    return 0
  fi
  if [[ ! $n =~ ^[0-9]+$ ]]; then
    die "环形大小必须为整数: $n"
  fi
  local max
  max=$(ring_max "$dev")
  if [[ -n $max ]] && (( n > max )); then
    die "超过上限 $max"
  fi
  if (( n < 100 )); then
    die "环形大小不能小于 100"
  fi
  require_root
  if ! run ethtool -G "$dev" rx "$n"; then
    warn "设置失败；可先执行: ip link set $dev down"
    return 1
  fi
  ok "RX 环形已设置为 $n"
}

# ---------- 中断合并 ----------
cmd_irq() {
  local dev usecs=${1:-}
  dev=$(resolve_dev)
  if [[ -z $usecs ]]; then
    title "中断合并: $dev"
    ethtool -c "$dev" 2>/dev/null | sed 's/^/  /' || warn "无法读取合并参数"
    return 0
  fi
  if [[ ! $usecs =~ ^[0-9]+$ ]] || (( usecs > 1000000 )); then
    die "rx-usecs 需为 0-1000000 的整数"
  fi
  require_root
  if ! run ethtool -C "$dev" rx-usecs "$usecs"; then
    warn "设置失败（驱动可能不支持该值）"
    return 1
  fi
  ok "rx-usecs 已设置为 $usecs"
}

# ---------- 硬件卸载 ----------
apply_offloads() {
  local dev=$1 mode=$2 feat
  for feat in rx tx sg tso gso gro rxvlan txvlan; do
    if [[ $mode == on ]]; then
      try_do ethtool -K "$dev" "$feat" on
    else
      try_do ethtool -K "$dev" "$feat" off
    fi
  done
}

cmd_offload() {
  local dev action=${1:-show}
  dev=$(resolve_dev)
  case $action in
    show | '')
      title "硬件卸载: $dev"
      ethtool -k "$dev" 2>/dev/null |
        grep -E '^(rx-checksumming|tx-checksumming|scatter-gather|tcp-segmentation-offload|generic-segmentation-offload|generic-receive-offload|rx-vlan-offload|tx-vlan-offload|rx-vlan-filter|large-receive-offload):' |
        sed 's/^/  /' || true
      ;;
    on | off)
      require_root
      apply_offloads "$dev" "$action"
      ok "硬件卸载已设置为 $action"
      ;;
    *)
      die "用法: usbnic offload <show|on|off>"
      ;;
  esac
}

# ---------- USB 电源 ----------
cmd_power() {
  local dev action=${1:-show}
  dev=$(resolve_dev)
  local d delay
  d=$(usb_dev_dir "$dev")
  case $action in
    show | '')
      title "USB 电源: $dev"
      kv "power/control" "$(cat "$d/power/control" 2>/dev/null)"
      kv "autosuspend_delay_ms" "$(cat "$d/power/autosuspend_delay_ms" 2>/dev/null)"
      ;;
    on | auto)
      require_root
      set_usb_power "$dev" "$action"
      ok "USB power/control 已设置为 $action（on=禁用自动挂起，auto=允许）"
      ;;
    off)
      require_root
      set_usb_power "$dev" auto
      ok "USB power/control 已设置为 auto（允许自动挂起）"
      ;;
    *)
      die "用法: usbnic power <on|auto|off|show>"
      ;;
  esac
}

# ---------- 混杂/多播 ----------
cmd_flags() {
  local dev what=${1:-} action=${2:-show}
  dev=$(resolve_dev)
  case $what in
    promisc | allmulti) ;;
    *) die "用法: usbnic flags <promisc|allmulti> <on|off|show>" ;;
  esac

  if [[ $action == show ]]; then
    title "接口 flags: $dev"
    ip -d link show dev "$dev" 2>/dev/null | grep -E 'promiscuity|allmulti' | sed 's/^/  /' || true
    return 0
  fi
  case $action in
    on | off) ;;
    *) die "用法: usbnic flags <promisc|allmulti> <on|off|show>" ;;
  esac
  require_root
  if ! run ip link set dev "$dev" "$what" "$action"; then
    warn "设置失败"
    return 1
  fi
  ok "$what 已设置为 $action"
}

# ---------- 档位 ----------
apply_profile() {
  local dev=$1 prof=$2 ring irq eee power
  case $prof in
    throughput)
      ring=4096; irq=50; eee=off; power=on
      ;;
    latency)
      ring=4096; irq=0; eee=off; power=on
      ;;
    balanced)
      ring=2048; irq=100; eee=off; power=on
      ;;
    lowpower)
      ring=100; irq=15000; eee=on; power=auto
      ;;
    *)
      return 1
      ;;
  esac

  apply_offloads "$dev" on
  try_do ethtool -G "$dev" rx "$ring"
  try_do ethtool -C "$dev" rx-usecs "$irq"
  try_do ethtool --set-eee "$dev" eee "$eee"
  set_usb_power "$dev" "$power"
  return 0
}

cmd_tune() {
  local dev sub=${1:-show}
  dev=$(resolve_dev)
  case $sub in
    show | '')
      title "当前调优参数: $dev"
      kv "RX 环形" "$(ring_current "$dev") / 最大 $(ring_max "$dev")"
      kv "rx-usecs" "$(coal_rx_usecs "$dev")"
      kv "硬件卸载" "$(offload_summary "$dev")"
      kv "EEE" "$(eee_state "$dev")"
      kv "USB 自动挂起" "$(usb_power_state "$dev")"
      kv "MTU" "$(cat "/sys/class/net/$dev/mtu" 2>/dev/null)"
      ;;
    throughput | latency | balanced | lowpower)
      require_root
      info "应用档位: $sub"
      apply_profile "$dev" "$sub"
      ok "档位 $sub 已应用（个别不支持的项会给出告警）"
      ;;
    *)
      die "用法: usbnic tune <throughput|latency|balanced|lowpower|show>"
      ;;
  esac
}
