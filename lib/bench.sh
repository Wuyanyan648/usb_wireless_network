#!/usr/bin/env bash
# 吞吐测试（iperf3）与稳定性压测

bench_usage() {
  cat <<'EOF'
用法:
  usbnic bench --server [--port 5201]
  usbnic bench --client <对端IP> [--time 10] [--streams 4] [--udp] [--reverse] [--bidir] [--port 5201]

说明:
  --server      在本机启动 iperf3 服务端
  --client IP   连接对端 iperf3 服务端
  --time N      测试秒数（默认 10）
  --streams N   并发流数（默认 4）
  --udp         使用 UDP（反之 TCP）
  --reverse     反向测试（对端发、本机收）
  --bidir       双向同时测试
  --port N      端口（默认 5201）
EOF
}

cmd_bench() {
  local mode='' host='' t=10 streams=1 udp=0 rev=0 bidir=0 port=5201

  while (( $# )); do
    case $1 in
      -s | --server) mode=server ;;
      -c | --client)
        if [[ -z ${2:-} ]]; then die "--client 需要对端地址"; fi
        mode=client
        host=$2
        shift
        ;;
      -t | --time)
        if [[ -z ${2:-} ]]; then die "--time 需要秒数"; fi
        t=$2
        shift
        ;;
      -P | --streams)
        if [[ -z ${2:-} ]]; then die "--streams 需要流数"; fi
        streams=$2
        shift
        ;;
      -u | --udp) udp=1 ;;
      -R | --reverse) rev=1 ;;
      --bidir) bidir=1 ;;
      -p | --port)
        if [[ -z ${2:-} ]]; then die "--port 需要端口号"; fi
        port=$2
        shift
        ;;
      -h | --help)
        bench_usage
        return 0
        ;;
      *) die "未知参数: $1" ;;
    esac
    shift
  done

  if [[ -z $mode ]]; then
    bench_usage
    die "请指定 --server 或 --client"
  fi
  if ! have iperf3; then
    die "未安装 iperf3，请执行: sudo apt install iperf3"
  fi

  local dev
  dev=$(resolve_dev)

  if [[ $mode == server ]]; then
    title "iperf3 服务端"
    kv "网卡" "$dev"
    kv "端口" "$port"
    info "在另一台主机执行: usbnic bench --client <本机IP> --port $port"
    run iperf3 -s -p "$port"
    return 0
  fi

  if [[ ! $t =~ ^[0-9]+$ ]] || (( t < 1 )); then die "--time 必须为正整数"; fi
  if [[ ! $streams =~ ^[0-9]+$ ]] || (( streams < 1 )); then die "--streams 必须为正整数"; fi
  if [[ ! $port =~ ^[0-9]+$ ]] || (( port < 1 || port > 65535 )); then die "--port 非法"; fi

  title "吞吐测试"
  kv "网卡" "$dev"
  kv "链路" "$(link_text "$dev")"
  kv "USB 速率" "$(usb_speed_text "$(usb_attr "$dev" speed)")"

  local -a args=(-c "$host" -p "$port" -t "$t" -P "$streams")
  if (( udp )); then args+=(-u -b 0); fi
  if (( rev )); then args+=(-R); fi
  if (( bidir )); then args+=(--bidir); fi

  info "执行: iperf3 ${args[*]}"
  if (( DRY_RUN )); then
    printf '%s[DRY]%s iperf3 %s\n' "$C_YEL" "$C_RST" "${args[*]}"
    return 0
  fi

  local log
  log=$(mktemp)
  if ! iperf3 "${args[@]}" | tee "$log"; then
    warn "iperf3 返回非零，检查对端服务端、防火墙与地址"
  fi

  local sum
  sum=$(awk '/\[SUM\].*(sender|receiver)/{line=$0} END{print line}' "$log")
  if [[ -n $sum ]]; then
    printf '\n'
    ok "汇总: $sum"
  fi
  rm -f "$log"
}

cmd_soak() {
  local dev
  dev=$(resolve_dev)
  local mins=${1:-10} interval=${2:-5}

  if [[ ! $mins =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    die "分钟数格式错误: $mins"
  fi
  if [[ ! $interval =~ ^[0-9]+$ ]] || (( interval < 1 )); then
    die "采样间隔必须为 >=1 的整数秒"
  fi

  local total
  total=$(awk -v m="$mins" 'BEGIN{printf "%d", m * 60}')
  if (( total < 1 )); then total=1; fi

  title "稳定性压测"
  kv "网卡" "$dev"
  kv "时长" "${mins} 分钟（每 ${interval}s 采样）"
  kv "链路" "$(link_text "$dev")"
  printf '\n'

  local start=$SECONDS end=$((SECONDS + total)) samples=0 flaps=0
  local prev_carrier last_err start_err sum_rx=0 sum_tx=0
  snap_fill S1 "$dev"
  prev_carrier=$(cat "/sys/class/net/$dev/carrier" 2>/dev/null || printf '0')
  last_err=$(err_total "$dev")
  start_err=$last_err

  while (( SECONDS < end )); do
    sleep "$interval"
    snap_fill S2 "$dev"

    local rx_d tx_d rx_p tx_p err_now err_d carrier
    rx_d=$(( ${S2[rx_bytes]:-0} - ${S1[rx_bytes]:-0} ))
    tx_d=$(( ${S2[tx_bytes]:-0} - ${S1[tx_bytes]:-0} ))
    rx_p=$(( ${S2[rx_packets]:-0} - ${S1[rx_packets]:-0} ))
    tx_p=$(( ${S2[tx_packets]:-0} - ${S1[tx_packets]:-0} ))
    err_now=$(err_total "$dev")
    err_d=$(( err_now - last_err ))
    last_err=$err_now
    carrier=$(cat "/sys/class/net/$dev/carrier" 2>/dev/null || printf '0')

    if [[ $carrier != "$prev_carrier" ]]; then
      flaps=$((flaps + 1))
      prev_carrier=$carrier
    fi

    sum_rx=$((sum_rx + rx_d))
    sum_tx=$((sum_tx + tx_d))
    samples=$((samples + 1))

    printf '[%s] %-16s RX %9s Mbps  TX %9s Mbps  新增错误 %-4s 链路抖动 %s\n' \
      "$(date +%H:%M:%S)" "$(link_text "$dev")" \
      "$(rate_mbps "$rx_d" "$interval")" "$(rate_mbps "$tx_d" "$interval")" \
      "$err_d" "$flaps"

    local k
    for k in "${!S2[@]}"; do
      S1[$k]=${S2[$k]}
    done
  done

  local elapsed=$((SECONDS - start))
  if (( elapsed < 1 )); then elapsed=1; fi

  title "压测结果"
  kv "采样次数" "$samples"
  kv "链路抖动次数" "$flaps"
  kv "平均 RX" "$(rate_mbps "$sum_rx" "$elapsed") Mbps"
  kv "平均 TX" "$(rate_mbps "$sum_tx" "$elapsed") Mbps"
  kv "累计错误增量" "$((last_err - start_err))"

  if [[ $flaps -gt 0 ]]; then
    warn "检测到链路抖动，建议检查 USB 供电/线缆，并执行: sudo usbnic power on; sudo usbnic eee off"
  else
    ok "链路稳定，无抖动"
  fi
}
