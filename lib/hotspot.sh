#!/usr/bin/env bash
# 热点：用 Wi-Fi 网卡开 AP，共享本机网络（8156 有线可作为上行）
# 说明：0bda:8156 是无射频的有线网卡，不能自身发 Wi-Fi。

HOTSPOT_CONN=usbnic-hotspot

hotspot_usage() {
  cat <<'EOF'
用法:
  usbnic hotspot start [选项]      启动热点（不带选项则复用上次配置）
  usbnic hotspot stop              停止热点（保留配置）
  usbnic hotspot status            查看热点状态与密码
  usbnic hotspot clients [IFACE]   查看已关联设备的 MAC/IP/信号/速率

选项:
  --wifi IFACE      指定 Wi-Fi 网卡（默认自动选 USB 无线网卡）
  --ssid NAME       热点名称（默认 USB-NIC-xxxx）
  --pass PASS       密码，8-63 位（不指定则随机生成）
  --open            开放网络（无密码）
  --band bg|a       2.4GHz(bg) 或 5GHz(a)，默认 bg
  --channel N       信道（36 及以上会自动切到 5GHz）
  --gateway IP/PFX  热点网关与网段，默认 10.42.0.1/24
                    例如 --gateway 192.168.50.1/24
  --autostart       开机自动启动热点

注意:
  热点是独立内网（默认 10.42.0.0/24，可用 --gateway 修改），
  客户端经本机 NAT 与默认路由上网。
  热点必须由 Wi-Fi 网卡提供；RTL8156 只能作为上行有线口。
  启动热点会断开所选 Wi-Fi 网卡当前的 STA 连接。
EOF
}

hotspot_uplink_dev() {
  ip route show default 2>/dev/null | awk '{print $5; exit}'
}

# ---------- IPv4 校验与网段重叠判断 ----------
ipv4_valid_cidr() {
  local s=$1 ip p o o1 o2 o3 o4
  [[ $s == */* ]] || return 1
  ip=${s%/*}
  p=${s#*/}
  [[ $ip =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || return 1
  [[ $p =~ ^[0-9]+$ ]] || return 1
  if (( p < 8 || p > 30 )); then return 1; fi
  IFS=. read -r o1 o2 o3 o4 <<<"$ip"
  for o in "$o1" "$o2" "$o3" "$o4"; do
    if (( o < 0 || o > 255 )); then return 1; fi
  done
  return 0
}

ipv4_int() {
  awk -F. '{printf "%.0f", (($1 * 256 + $2) * 256 + $3) * 256 + $4}' <<<"$1"
}

ipv4_mask_int() {
  awk -v p="$1" 'BEGIN{ if (p == 0) print 0; else printf "%.0f", 2 ^ 32 - 2 ^ (32 - p) }'
}

# 两个 CIDR 是否落在同一地址段（按较宽的前缀判断）
ipv4_overlap() {
  local a=${1%/*} pa=${1#*/} b=${2%/*} pb=${2#*/}
  local ia ib p m
  ia=$(ipv4_int "$a")
  ib=$(ipv4_int "$b")
  p=$pa
  if (( pb < p )); then p=$pb; fi
  m=$(ipv4_mask_int "$p")
  (( (ia & m) == (ib & m) ))
}

hotspot_conn_exists() {
  nmcli -t -f NAME connection show 2>/dev/null | grep -qx "$HOTSPOT_CONN"
}

band_label() {
  if [[ $1 == a ]]; then printf '5GHz'; else printf '2.4GHz'; fi
}

hotspot_ap_supported() {
  nmcli -f WIFI-PROPERTIES.AP device show "$1" 2>/dev/null | grep -qE 'AP:[[:space:]]+yes'
}

# 优先选择 USB Wi-Fi 网卡，其次任意支持 AP 的无线网卡
hotspot_default_wifi() {
  local d name drv
  for d in /sys/class/net/*; do
    name=${d##*/}
    [[ -e $d/phy80211 ]] || continue
    [[ -e $d/device/../idVendor ]] || continue
    drv=$(netdev_driver "$name" 2>/dev/null || printf '')
    case $drv in
      rtw88* | rtl8* | mt76* | ath9k* | ath10k* | brcmfmac)
        if hotspot_ap_supported "$name"; then
          printf '%s\n' "$name"
          return 0
        fi
        ;;
    esac
  done
  for d in /sys/class/net/*; do
    name=${d##*/}
    [[ -e $d/phy80211 ]] || continue
    [[ $name == p2p-* ]] && continue
    if hotspot_ap_supported "$name"; then
      printf '%s\n' "$name"
      return 0
    fi
  done
  return 1
}

gen_pass() {
  od -An -N6 -tx1 /dev/urandom | tr -d ' \n'
}

hotspot_client_count() {
  local wifi=$1 phy dir n out
  if have iw; then
    if out=$(iw dev "$wifi" station dump 2>/dev/null); then
      n=$(grep -c '^Station' <<<"$out" || true)
      printf '%s' "$n"
      return 0
    fi
  fi
  phy=$(basename "$(readlink -f "/sys/class/net/$wifi/phy80211" 2>/dev/null || printf '')")
  dir="/sys/kernel/debug/ieee80211/$phy/netdev:$wifi/stations"
  if [[ -d $dir ]]; then
    n=$(find "$dir" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l)
    printf '%s' "$n"
  else
    printf '%s' '-'
  fi
}

# 输出 "MAC|RSSI|RX|TX|速率|连接秒数"；优先 iw，其次 mac80211 debugfs
hotspot_station_dump() {
  local wifi=$1 phy dir s mac
  if have iw; then
    if iw dev "$wifi" station dump >/dev/null 2>&1; then
      iw dev "$wifi" station dump 2>/dev/null | awk '
        /^Station /{
          if (mac != "") print mac "|" sig "|" rx "|" tx "|" rate "|" ct
          mac = $2; sig = "-"; rx = "-"; tx = "-"; rate = "-"; ct = "-"
          next
        }
        /signal:/{sig = $2}
        /rx bytes:/{rx = $3}
        /tx bytes:/{tx = $3}
        /rx bitrate:/{rate = $3}
        /connected time:/{ct = $3}
        END{ if (mac != "") print mac "|" sig "|" rx "|" tx "|" rate "|" ct }'
      return 0
    fi
  fi
  phy=$(basename "$(readlink -f "/sys/class/net/$wifi/phy80211" 2>/dev/null || printf '')")
  dir="/sys/kernel/debug/ieee80211/$phy/netdev:$wifi/stations"
  if [[ -d $dir ]]; then
    for s in "$dir"/*/; do
      [[ -d $s ]] || continue
      mac=$(basename "$s")
      printf '%s|%s|%s|%s|%s|%s\n' "$mac" \
        "$(cat "$s/signal" 2>/dev/null || printf '%s' '-')" \
        "$(cat "$s/rx_bytes" 2>/dev/null || printf '%s' '-')" \
        "$(cat "$s/tx_bytes" 2>/dev/null || printf '%s' '-')" \
        "-" \
        "$(cat "$s/connected_time" 2>/dev/null || printf '%s' '-')"
    done
    return 0
  fi
  return 1
}

# 从 NetworkManager 的 dnsmasq 租约中查主机名（需要 root）
hotspot_lease_host() {
  local ip=$1 f host
  for f in /var/lib/NetworkManager/dnsmasq-*.leases /var/lib/misc/dnsmasq.leases; do
    [[ -r $f ]] || continue
    host=$(awk -v ip="$ip" '$3 == ip {print $4; exit}' "$f" 2>/dev/null)
    if [[ -n $host && $host != '*' ]]; then
      printf '%s' "$host"
      return 0
    fi
  done
  return 1
}

fmt_duration() {
  local s=${1:-}
  if [[ ! $s =~ ^[0-9]+$ ]]; then
    printf '%s' '-'
    return 0
  fi
  if (( s >= 3600 )); then
    printf '%dh%dm' "$((s / 3600))" "$((s % 3600 / 60))"
  elif (( s >= 60 )); then
    printf '%dm%ds' "$((s / 60))" "$((s % 60))"
  else
    printf '%ds' "$s"
  fi
}

hotspot_clients() {
  local wifi=${1:-}
  if [[ -z $wifi ]]; then
    if hotspot_conn_exists; then
      wifi=$(nmcli -g connection.interface-name connection show "$HOTSPOT_CONN" 2>/dev/null || true)
    fi
  fi
  if [[ -z $wifi ]]; then
    die "未配置热点；可执行 sudo usbnic hotspot start，或指定网卡: usbnic hotspot clients <IFACE>"
  fi
  netdev_exists "$wifi" || die "网卡不存在: $wifi"

  local -A STA=()
  local -A NEIGH=()
  local mac ip line has_station=0

  while IFS= read -r line; do
    [[ -n $line ]] || continue
    mac=${line%%|*}
    STA[${mac,,}]=${line#*|}
    has_station=1
  done < <(hotspot_station_dump "$wifi" 2>/dev/null || true)

  while read -r ip mac; do
    [[ -n $ip && -n $mac ]] || continue
    NEIGH[${mac,,}]=$ip
  done < <(ip neigh show dev "$wifi" 2>/dev/null | awk '$2 == "lladdr" {print $1, $3}')

  local -A ALL=()
  for mac in "${!STA[@]}"; do ALL[$mac]=1; done
  for mac in "${!NEIGH[@]}"; do ALL[$mac]=1; done

  title "热点客户端: $wifi"
  if [[ ${#ALL[@]} -eq 0 ]]; then
    if [[ $(cat "/sys/class/net/$wifi/operstate" 2>/dev/null) != up ]]; then
      info "热点未启动（$wifi 状态: $(cat "/sys/class/net/$wifi/operstate" 2>/dev/null)）"
    else
      info "当前没有设备关联"
    fi
    if (( ! has_station )); then
      info "查看完整关联信息需要 root 或安装 iw: sudo apt install iw"
    fi
    return 0
  fi

  printf '%-18s %-16s %-22s %-9s %-10s %-10s %-10s %s\n' \
    "MAC" "IP" "HOST" "RSSI" "RX" "TX" "RATE" "TIME"

  local macs=()
  mapfile -t macs < <(printf '%s\n' "${!ALL[@]}" | sort)
  local sig rx tx rate ct host rxs txs sigs rates
  for mac in "${macs[@]}"; do
    ip=${NEIGH[$mac]:--}
    local stainfo=${STA[$mac]:--|-|-|-|-}
    IFS='|' read -r sig rx tx rate ct <<<"$stainfo"

    host='-'
    if [[ $ip != '-' ]]; then
      host=$(getent hosts "$ip" 2>/dev/null | awk '{print $2; exit}' || true)
      if [[ -z $host ]]; then
        host=$(hotspot_lease_host "$ip" 2>/dev/null || true)
      fi
      if [[ -z $host ]]; then host='-'; fi
    fi

    if [[ $rx =~ ^[0-9]+$ ]]; then rxs=$(bytes_human "$rx"); else rxs='-'; fi
    if [[ $tx =~ ^[0-9]+$ ]]; then txs=$(bytes_human "$tx"); else txs='-'; fi
    if [[ $sig =~ ^-?[0-9]+$ ]]; then sigs="${sig} dBm"; else sigs='-'; fi
    if [[ $rate =~ ^[0-9.]+$ ]]; then rates="${rate} Mbps"; else rates='-'; fi

    printf '%-18s %-16s %-22s %-9s %-10s %-10s %-10s %s\n' \
      "$mac" "$ip" "$host" "$sigs" "$rxs" "$txs" "$rates" "$(fmt_duration "$ct")"
  done

  printf '\n'
  ok "共 ${#ALL[@]} 个设备"
  if (( ! has_station )); then
    info "当前仅显示 ARP/NDP 邻居；信号/速率/时长需要 root 或安装 iw: sudo apt install iw"
  fi
}

hotspot_status() {
  if ! hotspot_conn_exists; then
    info "尚未配置热点（执行: sudo usbnic hotspot start）"
    return 0
  fi

  local wifi ssid psk band ch auto state ip clients uplink fwd
  wifi=$(nmcli -g connection.interface-name connection show "$HOTSPOT_CONN" 2>/dev/null || true)
  ssid=$(nmcli -g 802-11-wireless.ssid connection show "$HOTSPOT_CONN" 2>/dev/null || true)
  psk=$(nmcli -s -g 802-11-wireless-security.psk connection show "$HOTSPOT_CONN" 2>/dev/null || true)
  band=$(nmcli -g 802-11-wireless.band connection show "$HOTSPOT_CONN" 2>/dev/null || true)
  ch=$(nmcli -g 802-11-wireless.channel connection show "$HOTSPOT_CONN" 2>/dev/null || true)
  if [[ $ch == 0 ]]; then ch=''; fi
  auto=$(nmcli -g connection.autoconnect connection show "$HOTSPOT_CONN" 2>/dev/null || true)
  state=$(nmcli -g GENERAL.STATE device show "$wifi" 2>/dev/null || true)
  ip=$(nmcli -g IP4.ADDRESS device show "$wifi" 2>/dev/null || true)
  if [[ -z $ip ]]; then
    ip=$(nmcli -g ipv4.addresses connection show "$HOTSPOT_CONN" 2>/dev/null || true)
  fi
  clients=$(hotspot_client_count "$wifi")
  uplink=$(hotspot_uplink_dev)
  fwd=$(cat /proc/sys/net/ipv4/ip_forward 2>/dev/null || printf '0')
  local clients_text=$clients
  if [[ $clients == '-' ]]; then
    clients_text='未知（需 root 或 iw）'
  fi

  title "热点状态"
  kv "Wi-Fi 网卡" "$wifi"
  kv "SSID" "$ssid"
  kv "密码" "${psk:-（开放网络）}"
  kv "频段/信道" "$(band_label "${band:-bg}")${ch:+/$ch}"
  kv "状态" "${state:-未知}"
  kv "网关地址" "${ip:--}"
  kv "上行网卡" "${uplink:-无（客户端无法上网）}"
  kv "IPv4 转发" "$fwd"
  kv "已连客户端" "$clients_text"
  kv "开机自启" "$auto"

  if [[ $state == *connected* ]]; then
    ok "热点运行中：$ssid"
  else
    info "热点未激活（sudo usbnic hotspot start 可重新启动）"
  fi
}

hotspot_stop() {
  require_root
  if ! hotspot_conn_exists; then
    info "未配置热点"
    return 0
  fi
  if ! run nmcli connection down "$HOTSPOT_CONN"; then
    warn "热点当前未激活或停止失败"
  fi
  ok "热点已停止（配置已保留）"
}

hotspot_start() {
  local wifi='' ssid='' pass='' band='bg' channel='' gateway='' open=0 autostart=0 band_set=0
  while (( $# )); do
    case $1 in
      --wifi)
        if [[ -z ${2:-} ]]; then die "--wifi 需要网卡名"; fi
        wifi=$2
        shift
        ;;
      --ssid)
        if [[ -z ${2:-} ]]; then die "--ssid 需要名称"; fi
        ssid=$2
        shift
        ;;
      --pass | --password)
        if [[ -z ${2:-} ]]; then die "--pass 需要密码"; fi
        pass=$2
        shift
        ;;
      --band)
        if [[ -z ${2:-} ]]; then die "--band 需要 bg 或 a"; fi
        band=$2
        band_set=1
        shift
        ;;
      --channel)
        if [[ -z ${2:-} ]]; then die "--channel 需要信道号"; fi
        channel=$2
        shift
        ;;
      --gateway | --ip)
        if [[ -z ${2:-} ]]; then die "--gateway 需要 IP/前缀，例如 192.168.50.1/24"; fi
        gateway=$2
        shift
        ;;
      --open) open=1 ;;
      --autostart) autostart=1 ;;
      -h | --help)
        hotspot_usage
        return 0
        ;;
      *) die "未知参数: $1（运行 usbnic hotspot start --help）" ;;
    esac
    shift
  done

  require_root
  if ! have nmcli; then
    die "未找到 nmcli，请安装 NetworkManager"
  fi

  local has_opts=0
  if [[ -n $wifi$ssid$pass$channel$gateway ]]; then has_opts=1; fi
  if (( open || autostart || band_set )); then has_opts=1; fi

  # 无参数且已有配置：直接复用
  if (( ! has_opts )) && hotspot_conn_exists; then
    if ! run nmcli connection up "$HOTSPOT_CONN"; then
      warn "启动失败，可用 sudo usbnic hotspot start --ssid 新名称 --pass 新密码 重建"
      return 1
    fi
    ok "热点已启动（复用已保存配置）"
    hotspot_status
    return 0
  fi

  if [[ -z $wifi ]]; then
    if ! wifi=$(hotspot_default_wifi); then
      die "未找到支持 AP 模式的 Wi-Fi 网卡；8156 是有线网卡，无法发 Wi-Fi"
    fi
  fi
  netdev_exists "$wifi" || die "Wi-Fi 网卡不存在: $wifi"
  if ! hotspot_ap_supported "$wifi"; then
    die "$wifi 不支持 AP 模式（检查驱动/固件）"
  fi

  # 网关/网段
  if [[ -n $gateway ]]; then
    if [[ $gateway != */* ]]; then
      gateway="$gateway/24"
    fi
    if ! ipv4_valid_cidr "$gateway"; then
      die "网关格式错误: $gateway（示例 192.168.50.1/24）"
    fi
  fi

  # 频段与信道
  if [[ $band != bg && $band != a ]]; then
    die "--band 只能是 bg 或 a"
  fi
  if [[ -n $channel ]]; then
    if [[ ! $channel =~ ^[0-9]+$ ]] || (( channel < 1 || channel > 196 )); then
      die "信道需为 1-196 的整数"
    fi
    if (( channel >= 36 )); then
      if (( band_set )) && [[ $band == bg ]]; then
        die "信道 $channel 属于 5GHz，与 --band bg 冲突"
      fi
      band=a
    else
      if (( band_set )) && [[ $band == a ]]; then
        die "信道 $channel 属于 2.4GHz，与 --band a 冲突"
      fi
      band=bg
    fi
  fi

  # 密码与 SSID
  if (( open )) && [[ -n $pass ]]; then
    die "--open 与 --pass 不能同时使用"
  fi
  if (( ! open )); then
    if [[ -z $pass ]]; then
      pass=$(gen_pass)
    fi
    if (( ${#pass} < 8 || ${#pass} > 63 )); then
      die "WPA2 密码长度需为 8-63 位"
    fi
  fi
  if [[ -z $ssid ]]; then
    local hex
    hex=$(cat "/sys/class/net/$wifi/address" 2>/dev/null | tr -d ':')
    ssid="USB-NIC-${hex: -4}"
  fi
  if (( ${#ssid} < 1 || ${#ssid} > 32 )); then
    die "SSID 长度需为 1-32 个字符"
  fi

  # 风险提示与上行检查
  local cur uplink wired fwd upaddr
  cur=$(nmcli -g GENERAL.CONNECTION device show "$wifi" 2>/dev/null || true)
  if [[ -n $cur && $cur != '--' ]]; then
    warn "$wifi 当前连接 $cur，启动热点会断开该连接"
  fi

  uplink=$(hotspot_uplink_dev)
  if [[ -z $uplink ]]; then
    warn "当前没有默认路由，热点客户端只能访问本机内网，无法上网"
  elif [[ $uplink == "$wifi" ]]; then
    warn "默认路由走 $wifi，而它即将改作 AP；请确认 8156 有线或另一块 Wi-Fi 能提供上行"
  else
    info "上行链路: $uplink ($(ip -4 -o addr show dev "$uplink" 2>/dev/null | awk '{print $4; exit}'))，客户端经本机 NAT 上网"
  fi

  if [[ -n $gateway && -n $uplink ]]; then
    upaddr=$(ip -4 -o addr show dev "$uplink" 2>/dev/null | awk '{print $4; exit}')
    if [[ -n $upaddr ]] && ipv4_overlap "$gateway" "$upaddr"; then
      die "热点网段 $gateway 与上行 $uplink ($upaddr) 重叠，客户端将无法上网；请换网段，如 192.168.50.1/24"
    fi
  fi

  if wired=$(detect_dev); then
    if [[ $(cat "/sys/class/net/$wired/carrier" 2>/dev/null || printf '0') == 1 ]]; then
      info "有线口 $wired 已连接（$(link_text "$wired")）"
    fi
  fi

  fwd=$(cat /proc/sys/net/ipv4/ip_forward 2>/dev/null || printf '0')
  if [[ $fwd != 1 ]]; then
    info "开启 IPv4 转发（NAT 路由需要）"
    if ! run sysctl -w net.ipv4.ip_forward=1; then
      warn "IPv4 转发开启失败，热点客户端可能无法访问公网"
    fi
  fi

  if have systemctl && systemctl is-active --quiet ufw; then
    warn "ufw 正在运行；若客户端能连上热点但无法上网，请放行 ${gateway:-10.42.0.0/24} 的转发与 NAT"
  fi

  info "配置热点: SSID=$ssid 网卡=$wifi 频段=$band${channel:+ 信道=$channel}${gateway:+ 网关=$gateway}"
  run nmcli device set "$wifi" managed yes || warn "设置 managed 失败"
  run nmcli radio wifi on || true

  if hotspot_conn_exists; then
    run nmcli connection down "$HOTSPOT_CONN" || true
    run nmcli connection delete "$HOTSPOT_CONN" || warn "删除旧配置失败"
  fi

  local -a args=(connection add type wifi ifname "$wifi" con-name "$HOTSPOT_CONN"
    ssid "$ssid" 802-11-wireless.mode ap 802-11-wireless.band "$band")
  if (( autostart )); then
    args+=(connection.autoconnect yes)
  else
    args+=(connection.autoconnect no)
  fi
  if [[ -n $channel ]]; then
    args+=(802-11-wireless.channel "$channel")
  fi
  if (( ! open )); then
    args+=(802-11-wireless-security.key-mgmt wpa-psk 802-11-wireless-security.psk "$pass")
  fi
  args+=(ipv4.method shared ipv6.method ignore)
  if [[ -n $gateway ]]; then
    args+=(ipv4.addresses "$gateway")
  fi

  if ! run nmcli "${args[@]}"; then
    warn "创建热点配置失败"
    return 1
  fi
  if ! run nmcli connection up "$HOTSPOT_CONN"; then
    warn "热点启动失败。可尝试: 升级 wpa_supplicant/NetworkManager，或换 --wifi 指定另一块无线网卡"
    return 1
  fi

  printf '\n'
  ok "热点已启动"
  local gw
  gw=$(nmcli -g IP4.ADDRESS device show "$wifi" 2>/dev/null || true)
  kv "SSID" "$ssid"
  kv "密码" "$( (( open )) && printf '（开放网络）' || printf '%s' "$pass" )"
  kv "网卡/频段" "$wifi / $(band_label "$band")${channel:+/$channel}"
  kv "客户端网关" "${gw:-${gateway:-10.42.0.1/24}}"
  kv "上行网卡" "${uplink:-无}"
  printf '\n'
  info "查看状态: usbnic hotspot status   停止: sudo usbnic hotspot stop"
}

cmd_hotspot() {
  local sub=${1:-status}
  if (( $# )); then shift; fi
  case $sub in
    start) hotspot_start "$@" ;;
    stop) hotspot_stop ;;
    status | show) hotspot_status ;;
    clients | stations) hotspot_clients "$@" ;;
    -h | --help) hotspot_usage ;;
    *)
      hotspot_usage
      die "未知子命令: $sub"
      ;;
  esac
}
