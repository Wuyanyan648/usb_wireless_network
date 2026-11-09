#!/usr/bin/env bash
# 热点：用 Wi-Fi 网卡开 AP，共享本机网络（8156 有线可作为上行）
# 说明：0bda:8156 是无射频的有线网卡，不能自身发 Wi-Fi。

HOTSPOT_CONN=usbnic-hotspot
# 明文密码只存本机（仓库外），权限 600；不会进入 git
HOTSPOT_PASS_FILE=${HOTSPOT_PASS_FILE:-/etc/usbnic/hotspot.pass}
HOTSPOT_PASS_MIN=20

hotspot_usage() {
  cat <<'EOF'
用法:
  usbnic hotspot start [选项]      启动热点（不带选项则复用上次配置）
  usbnic hotspot stop              停止热点（保留配置）
  usbnic hotspot status            查看热点状态、安全参数与密码
  usbnic hotspot pass              仅输出当前热点密码（可 > 文件保存）
  usbnic hotspot clients [IFACE]   查看已关联设备的 MAC/IP/信号/速率

选项:
  --wifi IFACE      指定 Wi-Fi 网卡（默认自动选 USB 无线网卡）
  --ssid NAME       热点名称（默认 USB-NIC-xxxx）
  --pass PASS       自定义密码；至少 20 位且需含大小写/数字/符号中至少三类
                    （不指定则自动生成 24 位强密码，并复用已保存的密码）
  --new-pass        重新生成 24 位强密码并重建热点（密码轮换）
  --open            开放网络（无密码，强烈不推荐）
  --band bg|a       2.4GHz(bg) 或 5GHz(a)，默认 bg
  --channel N       信道（36 及以上会自动切到 5GHz）
  --gateway IP/PFX  热点网关与网段，默认 10.42.0.1/24
                    例如 --gateway 192.168.50.1/24
  --autostart       开机自动启动热点
  --ap-isolation    开启 AP 隔离（客户端之间无法互访）
  --no-ap-isolation 关闭 AP 隔离（默认；投屏/打印机/局域网互访需要）
  --compat          WPA2/WPA3 过渡模式（key-mgmt wpa-psk，pmf optional）
                    默认 WPA3-SAE + pmf required，SAE 协商失败会自动回退

安全默认值:
  key-mgmt=sae、pmf=required(3)、proto=rsn、pairwise=ccmp、group=ccmp、
  WPS 显式关闭。密码保存在 /etc/usbnic/hotspot.pass（600，仅 root），
  可用 usbnic hotspot pass 或 usbnic hotspot status 查看。

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

# 生成 24 位强密码：大小写+数字+符号各至少一个，排除 0O1lI 等易混淆字符
gen_pass() {
  local upper='ABCDEFGHJKLMNPQRSTUVWXYZ'
  local lower='abcdefghijkmnopqrstuvwxyz'
  local digit='23456789'
  local symbol='!@#%^&*_=+:,.?/'
  local all="${upper}${lower}${digit}${symbol}"
  local -a chars=()
  local set idx
  for set in "$upper" "$lower" "$digit" "$symbol"; do
    idx=$(od -An -N1 -tu1 /dev/urandom | tr -d ' \n')
    chars+=("${set:idx%${#set}:1}")
  done
  while (( ${#chars[@]} < 24 )); do
    idx=$(od -An -N1 -tu1 /dev/urandom | tr -d ' \n')
    chars+=("${all:idx%${#all}:1}")
  done
  printf '%s\n' "$(printf '%s\n' "${chars[@]}" | shuf | tr -d '\n')"
}

# 弱口令判定：长度不足 20、字符类别少于 3 类、或全部为同一字符
hotspot_pass_weak() {
  local p=$1 rest first
  (( ${#p} < HOTSPOT_PASS_MIN || ${#p} > 63 )) && return 0
  local classes=0
  [[ $p == *[a-z]* ]] && ((classes++))
  [[ $p == *[A-Z]* ]] && ((classes++))
  [[ $p == *[0-9]* ]] && ((classes++))
  [[ $p == *[^a-zA-Z0-9]* ]] && ((classes++))
  (( classes < 3 )) && return 0
  first=${p:0:1}
  rest=${p:1}
  [[ -z ${rest//"$first"/} ]] && return 0
  return 1
}

hotspot_pass_load() {
  [[ -r $HOTSPOT_PASS_FILE ]] || return 1
  local p
  p=$(head -n1 "$HOTSPOT_PASS_FILE" 2>/dev/null || true)
  [[ ${#p} -ge 8 ]] || return 1
  printf '%s' "$p"
}

hotspot_pass_save() {
  local pass=$1 dir
  dir=$(dirname "$HOTSPOT_PASS_FILE")
  if ! run install -d -m 700 "$dir"; then
    return 1
  fi
  if ! (umask 077 && printf '%s\n' "$pass" >"$HOTSPOT_PASS_FILE"); then
    return 1
  fi
  chmod 600 "$HOTSPOT_PASS_FILE" 2>/dev/null || true
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
  local km proto pair group pmf wps iso
  wifi=$(nmcli -g connection.interface-name connection show "$HOTSPOT_CONN" 2>/dev/null || true)
  ssid=$(nmcli -g 802-11-wireless.ssid connection show "$HOTSPOT_CONN" 2>/dev/null || true)
  psk=$(nmcli -s -g 802-11-wireless-security.psk connection show "$HOTSPOT_CONN" 2>/dev/null || true)
  band=$(nmcli -g 802-11-wireless.band connection show "$HOTSPOT_CONN" 2>/dev/null || true)
  ch=$(nmcli -g 802-11-wireless.channel connection show "$HOTSPOT_CONN" 2>/dev/null || true)
  if [[ $ch == 0 ]]; then ch=''; fi
  auto=$(nmcli -g connection.autoconnect connection show "$HOTSPOT_CONN" 2>/dev/null || true)
  km=$(nmcli -g 802-11-wireless-security.key-mgmt connection show "$HOTSPOT_CONN" 2>/dev/null || true)
  proto=$(nmcli -g 802-11-wireless-security.proto connection show "$HOTSPOT_CONN" 2>/dev/null || true)
  pair=$(nmcli -g 802-11-wireless-security.pairwise connection show "$HOTSPOT_CONN" 2>/dev/null || true)
  group=$(nmcli -g 802-11-wireless-security.group connection show "$HOTSPOT_CONN" 2>/dev/null || true)
  pmf=$(nmcli -g 802-11-wireless-security.pmf connection show "$HOTSPOT_CONN" 2>/dev/null || true)
  wps=$(nmcli -g 802-11-wireless-security.wps-method connection show "$HOTSPOT_CONN" 2>/dev/null || true)
  iso=$(nmcli -g 802-11-wireless.ap-isolation connection show "$HOTSPOT_CONN" 2>/dev/null || true)
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
  local pmf_text=$pmf
  case $pmf in
    0) pmf_text='default(0)' ;;
    1) pmf_text='disable(1)' ;;
    2) pmf_text='optional(2)' ;;
    3) pmf_text='required(3)' ;;
  esac
  local iso_text='关闭'
  case $iso in
    yes | 1 | true) iso_text='开启（客户端互访被阻断）' ;;
    no | 0 | false) iso_text='关闭（允许互访）' ;;
    *) iso_text='默认（视为关闭）' ;;
  esac

  title "热点状态"
  kv "Wi-Fi 网卡" "$wifi"
  kv "SSID" "$ssid"
  kv "密码" "${psk:-（开放网络，或需 root 读取）}"
  kv "认证/密钥管理" "${km:--}"
  kv "PMF (802.11w)" "$pmf_text"
  kv "协议 (proto)" "${proto:--（未限制）}"
  kv "单播/组播加密" "${pair:--} / ${group:--}"
  kv "WPS" "$( case $wps in 1 | 0x1) printf '已关闭' ;; *) printf '%s' "${wps:-默认}" ;; esac )"
  kv "AP 隔离" "$iso_text"
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
  info "查看密码: usbnic hotspot pass（或 usbnic hotspot status）"
}

# 只输出当前热点密码，便于复制或重定向到文件
hotspot_pass_cmd() {
  local p
  if ! p=$(hotspot_pass_load); then
    p=$(nmcli -s -g 802-11-wireless-security.psk connection show "$HOTSPOT_CONN" 2>/dev/null || true)
  fi
  if [[ -z $p ]]; then
    die "未找到热点密码（$HOTSPOT_PASS_FILE）；请先 sudo usbnic hotspot start"
  fi
  printf '%s\n' "$p"
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

# 创建热点连接。use_sae=1 用 WPA3-SAE + pmf required；0 用 WPA2/WPA3 过渡模式
hotspot_build_conn() {
  local wifi=$1 ssid=$2 band=$3 channel=$4 gateway=$5 autostart=$6 pass=$7 use_sae=$8 isolation=$9
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
  if [[ -n $pass ]]; then
    args+=(802-11-wireless-security.proto rsn
      802-11-wireless-security.pairwise ccmp
      802-11-wireless-security.group ccmp
      802-11-wireless-security.wps-method 1)
    if (( use_sae )); then
      args+=(802-11-wireless-security.key-mgmt sae 802-11-wireless-security.pmf 3)
    else
      args+=(802-11-wireless-security.key-mgmt wpa-psk 802-11-wireless-security.pmf 2)
    fi
    args+=(802-11-wireless-security.psk "$pass")
  fi
  if (( isolation )); then
    args+=(802-11-wireless.ap-isolation yes)
  else
    args+=(802-11-wireless.ap-isolation no)
  fi
  args+=(ipv4.method shared ipv6.method ignore)
  if [[ -n $gateway ]]; then
    args+=(ipv4.addresses "$gateway")
  fi
  run nmcli "${args[@]}"
}

# 已保存的热点配置是否达到加固基线（SAE 或过渡模式 + PMF + 仅 CCMP + 关闭 WPS + 强密码）
hotspot_conn_secure() {
  local km proto pair group pmf wps psk
  km=$(nmcli -g 802-11-wireless-security.key-mgmt connection show "$HOTSPOT_CONN" 2>/dev/null || true)
  [[ $km == sae || $km == wpa-psk ]] || return 1
  proto=$(nmcli -g 802-11-wireless-security.proto connection show "$HOTSPOT_CONN" 2>/dev/null || true)
  [[ $proto == *rsn* ]] || return 1
  pair=$(nmcli -g 802-11-wireless-security.pairwise connection show "$HOTSPOT_CONN" 2>/dev/null || true)
  [[ $pair == *ccmp* && $pair != *tkip* ]] || return 1
  group=$(nmcli -g 802-11-wireless-security.group connection show "$HOTSPOT_CONN" 2>/dev/null || true)
  [[ $group == *ccmp* && $group != *tkip* ]] || return 1
  pmf=$(nmcli -g 802-11-wireless-security.pmf connection show "$HOTSPOT_CONN" 2>/dev/null || true)
  [[ $pmf == 2 || $pmf == 3 ]] || return 1
  wps=$(nmcli -g 802-11-wireless-security.wps-method connection show "$HOTSPOT_CONN" 2>/dev/null || true)
  [[ $wps == 1 || $wps == 0x1 ]] || return 1
  psk=$(nmcli -s -g 802-11-wireless-security.psk connection show "$HOTSPOT_CONN" 2>/dev/null || true)
  [[ -n $psk ]] || return 1
  ! hotspot_pass_weak "$psk"
}

# 读取已有连接的参数到调用方变量（已显式指定的值不覆盖）
hotspot_reuse_existing() {
  local v
  [[ -n $wifi ]] || wifi=$(nmcli -g connection.interface-name connection show "$HOTSPOT_CONN" 2>/dev/null || true)
  [[ -n $ssid ]] || ssid=$(nmcli -g 802-11-wireless.ssid connection show "$HOTSPOT_CONN" 2>/dev/null || true)
  if (( ! band_set )); then
    v=$(nmcli -g 802-11-wireless.band connection show "$HOTSPOT_CONN" 2>/dev/null || true)
    if [[ -n $v && $v != -- ]]; then band=$v; fi
  fi
  if [[ -z $channel ]]; then
    v=$(nmcli -g 802-11-wireless.channel connection show "$HOTSPOT_CONN" 2>/dev/null || true)
    if [[ $v != 0 && $v != -- ]]; then channel=$v; fi
  fi
  if [[ -z $gateway ]]; then
    v=$(nmcli -g ipv4.addresses connection show "$HOTSPOT_CONN" 2>/dev/null || true)
    if [[ $v != -- ]]; then gateway=$v; fi
  fi
  if (( ! autostart )); then
    v=$(nmcli -g connection.autoconnect connection show "$HOTSPOT_CONN" 2>/dev/null || true)
    if [[ $v == yes ]]; then autostart=1; fi
  fi
  if (( ! ap_isolation_set )); then
    v=$(nmcli -g 802-11-wireless.ap-isolation connection show "$HOTSPOT_CONN" 2>/dev/null || true)
    if [[ $v == yes ]]; then ap_isolation=1; fi
  fi
}

hotspot_start() {
  local wifi='' ssid='' pass='' band='bg' channel='' gateway='' open=0 autostart=0 band_set=0
  local compat=0 ap_isolation=0 ap_isolation_set=0 pass_given=0 new_pass=0
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
        pass_given=1
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
      --ap-isolation | --isolate) ap_isolation=1; ap_isolation_set=1 ;;
      --no-ap-isolation) ap_isolation=0; ap_isolation_set=1 ;;
      --new-pass | --regen-pass) new_pass=1 ;;
      --compat | --wpa2-transition) compat=1 ;;
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
  if (( open || autostart || band_set || compat || ap_isolation || new_pass )); then has_opts=1; fi

  # 无参数且已有配置：安全基线达标才直接复用，否则按加固默认值重建
  if (( ! has_opts )) && hotspot_conn_exists; then
    if hotspot_conn_secure; then
      if ! run nmcli connection up "$HOTSPOT_CONN"; then
        warn "启动失败，可用 sudo usbnic hotspot start --ssid 新名称 重建"
        return 1
      fi
      ok "热点已启动（复用已保存配置）"
      hotspot_status
      return 0
    fi
    warn "已保存的热点配置未达到安全基线，将按 WPA3-SAE 默认值重建"
    hotspot_reuse_existing
  fi

  if (( new_pass )) && hotspot_conn_exists; then
    hotspot_reuse_existing
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

  # 密码与 SSID：优先复用已保存的强密码，避免每次重建都换密码
  if (( open )) && [[ -n $pass ]]; then
    die "--open 与 --pass 不能同时使用"
  fi
  if (( new_pass )) && (( pass_given )); then
    die "--new-pass 与 --pass 不能同时使用；轮换密码请只加 --new-pass"
  fi
  local pass_source=''
  if (( ! open )); then
    if (( new_pass )); then
      pass=$(gen_pass)
      pass_source='generated'
    elif [[ -z $pass ]]; then
      if pass=$(hotspot_pass_load); then
        pass_source='saved'
      else
        pass=$(gen_pass)
        pass_source='generated'
      fi
    else
      pass_source='user'
    fi
    if hotspot_pass_weak "$pass"; then
      die "密码太弱：至少 ${HOTSPOT_PASS_MIN} 位，且需包含大写/小写/数字/符号中至少三类；建议省略 --pass 自动生成强密码"
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

  local use_sae=1
  if (( compat )); then use_sae=0; fi
  local sec_label='WPA3-SAE (pmf=required)'
  if (( ! use_sae )); then sec_label='WPA2/WPA3 过渡 (wpa-psk, pmf=optional)'; fi
  if (( open )); then sec_label='开放网络（无加密）'; fi
  info "安全配置: $sec_label, proto=rsn, pairwise/group=ccmp, WPS=disabled, AP隔离=$([[ $ap_isolation == 1 ]] && printf 开启 || printf 关闭)"

  if ! hotspot_build_conn "$wifi" "$ssid" "$band" "$channel" "$gateway" "$autostart" "$pass" "$use_sae" "$ap_isolation"; then
    warn "创建热点配置失败"
    return 1
  fi
  if ! run nmcli connection up "$HOTSPOT_CONN"; then
    if (( use_sae && ! open )); then
      warn "WPA3-SAE(pmf=required) 协商失败，回退 WPA2/WPA3 过渡模式 (pmf=optional)"
      run nmcli connection delete "$HOTSPOT_CONN" || true
      use_sae=0
      if hotspot_build_conn "$wifi" "$ssid" "$band" "$channel" "$gateway" "$autostart" "$pass" 0 "$ap_isolation" \
        && run nmcli connection up "$HOTSPOT_CONN"; then
        warn "已回退为 WPA2/WPA3 过渡模式；如需仅 WPA3，请换用支持 SAE 的网卡/驱动"
      else
        warn "热点启动失败。可尝试: 升级 wpa_supplicant/NetworkManager，或换 --wifi 指定另一块无线网卡"
        return 1
      fi
    else
      warn "热点启动失败。可尝试: 升级 wpa_supplicant/NetworkManager，或换 --wifi 指定另一块无线网卡"
      return 1
    fi
  fi

  if (( ! open )); then
    if hotspot_pass_save "$pass"; then
      info "热点密码已保存到 $HOTSPOT_PASS_FILE（600，仅 root 可读）"
      if [[ $pass_source == saved ]]; then
        info "复用已保存的密码；忘记可用: usbnic hotspot pass"
      fi
    else
      warn "密码保存到 $HOTSPOT_PASS_FILE 失败；请用 usbnic hotspot status 查看本次密码"
    fi
  fi

  printf '\n'
  ok "热点已启动"
  local gw
  gw=$(nmcli -g IP4.ADDRESS device show "$wifi" 2>/dev/null || true)
  kv "SSID" "$ssid"
  kv "密码" "$( (( open )) && printf '（开放网络）' || printf '%s' "$pass" )"
  kv "加密" "$sec_label"
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
    pass | password) hotspot_pass_cmd ;;
    clients | stations) hotspot_clients "$@" ;;
    -h | --help) hotspot_usage ;;
    *)
      hotspot_usage
      die "未知子命令: $sub"
      ;;
  esac
}
