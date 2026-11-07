#!/usr/bin/env bash
# 配置持久化（systemd + udev）、NetworkManager 配置、USB 复位

CONF_PATH=${USBNIC_CONF:-/etc/usbnic/usbnic.conf}

load_config() {
  PROFILE=''
  if [[ -r $CONF_PATH ]]; then
    # shellcheck disable=SC1090
    source "$CONF_PATH"
  fi
  PROFILE=${PROFILE:-balanced}
  case $PROFILE in
    throughput | latency | balanced | lowpower) ;;
    *)
      warn "配置中的档位无效: $PROFILE，回退 balanced"
      PROFILE=balanced
      ;;
  esac
}

# 由 systemd / udev 调用：读取配置后静默应用
cmd_apply() {
  require_root
  load_config
  local dev
  if ! dev=$(detect_dev); then
    if (( QUIET )); then exit 0; fi
    info "未检测到 r8152 设备，跳过"
    return 0
  fi

  info "应用配置到 $dev（档位 $PROFILE）"
  apply_profile "$dev" "$PROFILE"

  if [[ -n ${SPEED:-} ]]; then
    case $SPEED in
      auto | 10 | 100 | 1000 | 2500)
        if ! apply_speed "$dev" "$SPEED" 0; then
          warn "SPEED=$SPEED 未生效"
        fi
        ;;
      *) warn "配置中的 SPEED 无效: $SPEED" ;;
    esac
  fi

  if [[ -n ${MTU:-} ]]; then
    if [[ $MTU =~ ^[0-9]+$ ]] && (( MTU >= 68 && MTU <= 9000 )); then
      try_do ip link set dev "$dev" mtu "$MTU"
    else
      warn "配置中的 MTU 无效: $MTU"
    fi
  fi
  if [[ -n ${RING:-} ]]; then
    if [[ $RING =~ ^[0-9]+$ ]]; then
      try_do ethtool -G "$dev" rx "$RING"
    else
      warn "配置中的 RING 无效: $RING"
    fi
  fi
  if [[ -n ${RX_USECS:-} ]]; then
    if [[ $RX_USECS =~ ^[0-9]+$ ]]; then
      try_do ethtool -C "$dev" rx-usecs "$RX_USECS"
    else
      warn "配置中的 RX_USECS 无效: $RX_USECS"
    fi
  fi
  if [[ -n ${OFFLOAD:-} ]]; then
    case $OFFLOAD in
      on | off) apply_offloads "$dev" "$OFFLOAD" ;;
      *) warn "配置中的 OFFLOAD 无效: $OFFLOAD" ;;
    esac
  fi
  if [[ -n ${EEE:-} ]]; then
    case $EEE in
      on | off) try_do ethtool --set-eee "$dev" eee "$EEE" ;;
      *) warn "配置中的 EEE 无效: $EEE" ;;
    esac
  fi
  if [[ -n ${AUTOSUSPEND:-} ]]; then
    case $AUTOSUSPEND in
      on) set_usb_power "$dev" on ;;
      auto | off) set_usb_power "$dev" auto ;;
      *) warn "配置中的 AUTOSUSPEND 无效: $AUTOSUSPEND" ;;
    esac
  fi
  if [[ -n ${WOL:-} ]]; then
    case $WOL in
      g | d) try_do ethtool -s "$dev" wol "$WOL" ;;
      off) try_do ethtool -s "$dev" wol d ;;
      *) warn "配置中的 WOL 无效: $WOL" ;;
    esac
  fi

  ok "配置应用完成"
}

# ---------- persist ----------
cmd_persist() {
  local sub=${1:-show}
  local unit=/etc/systemd/system/usbnic-tune.service
  local rule=/etc/udev/rules.d/99-usbnic.rules
  local src_unit="$USBNIC_HOME/systemd/usbnic-tune.service"
  local src_rule="$USBNIC_HOME/udev/99-usbnic.rules"

  case $sub in
    install)
      require_root
      if (( ! DRY_RUN )) && [[ ! -x /usr/local/sbin/usbnic ]]; then
        die "请先运行 scripts/install.sh 完成安装，再执行 persist install"
      fi
      [[ -f $src_unit ]] || die "缺少文件: $src_unit"
      [[ -f $src_rule ]] || die "缺少文件: $src_rule"
      run install -Dm644 "$src_unit" "$unit"
      run install -Dm644 "$src_rule" "$rule"
      run systemctl daemon-reload || warn "daemon-reload 失败"
      run systemctl enable usbnic-tune.service || warn "设置开机自启失败"
      run systemctl restart usbnic-tune.service || warn "立即应用配置失败（设备可能未插入）"
      run udevadm control --reload-rules || warn "重载 udev 规则失败"
      ok "已安装开机/热插拔自动调优（配置: $CONF_PATH）"
      ;;
    uninstall)
      require_root
      run systemctl disable --now usbnic-tune.service || true
      run rm -f "$unit" "$rule"
      run systemctl daemon-reload || true
      run udevadm control --reload-rules || true
      ok "已移除开机/热插拔自动调优"
      ;;
    apply)
      cmd_apply
      ;;
    show)
      title "配置文件: $CONF_PATH"
      if [[ -r $CONF_PATH ]]; then
        sed 's/^/  /' "$CONF_PATH"
      else
        info "未安装"
      fi
      title "systemd: $unit"
      if [[ -r $unit ]]; then
        sed 's/^/  /' "$unit"
      elif [[ -r $src_unit ]]; then
        info "未安装，仓库模板如下:"
        sed 's/^/  /' "$src_unit"
      else
        info "未安装"
      fi
      title "udev: $rule"
      if [[ -r $rule ]]; then
        sed 's/^/  /' "$rule"
      elif [[ -r $src_rule ]]; then
        info "未安装，仓库模板如下:"
        sed 's/^/  /' "$src_rule"
      else
        info "未安装"
      fi
      ;;
    *)
      die "用法: usbnic persist <install|uninstall|show|apply>"
      ;;
  esac
}

# ---------- NetworkManager ----------
cmd_net() {
  local dev sub=${1:-show}
  dev=$(resolve_dev)
  shift || true

  if ! have nmcli; then
    die "未找到 nmcli，请安装 NetworkManager"
  fi

  local conn="usbnic-$dev"
  case $sub in
    show)
      title "NetworkManager: $dev"
      nmcli -t -f GENERAL.STATE,GENERAL.CONNECTION,IP4.ADDRESS,IP4.GATEWAY,IP4.DNS \
        device show "$dev" 2>/dev/null | sed 's/^/  /' || true
      ;;
    dhcp)
      require_root
      run nmcli device set "$dev" managed yes || warn "设置 managed 失败"
      if nmcli -t -f NAME connection show 2>/dev/null | grep -qx "$conn"; then
        run nmcli connection delete "$conn" || warn "删除旧连接失败"
      fi
      run nmcli connection add type ethernet ifname "$dev" con-name "$conn" \
        ipv4.method auto ipv6.method auto connection.autoconnect yes || {
        warn "创建连接失败"
        return 1
      }
      if ! run nmcli connection up "$conn"; then
        warn "激活失败（网线未连接时会如此），插线后会自动生效"
      fi
      ok "已配置 DHCP: $conn"
      ;;
    static)
      local cidr=${1:-} gw=${2:-} dns=${3:-}
      if [[ -z $cidr || $cidr != */* ]]; then
        die "用法: usbnic net static <IP/前缀> [网关] [DNS]，例如 192.168.50.2/24 192.168.50.1 223.5.5.5"
      fi
      require_root
      run nmcli device set "$dev" managed yes || warn "设置 managed 失败"
      if nmcli -t -f NAME connection show 2>/dev/null | grep -qx "$conn"; then
        run nmcli connection delete "$conn" || warn "删除旧连接失败"
      fi
      local -a args=(connection add type ethernet ifname "$dev" con-name "$conn"
        ipv4.method manual ipv4.addresses "$cidr")
      if [[ -n $gw ]]; then args+=(ipv4.gateway "$gw"); fi
      if [[ -n $dns ]]; then args+=(ipv4.dns "$dns"); fi
      args+=(ipv6.method auto connection.autoconnect yes)
      run nmcli "${args[@]}" || {
        warn "创建静态地址连接失败"
        return 1
      }
      if ! run nmcli connection up "$conn"; then
        warn "激活失败（网线未连接时会如此），插线后会自动生效"
      fi
      ok "已配置静态地址: $cidr ${gw:+网关 $gw}"
      ;;
    *)
      die "用法: usbnic net <show|dhcp|static>"
      ;;
  esac
}

# ---------- USB 复位 ----------
cmd_reset() {
  require_root
  local dev
  dev=$(resolve_dev)

  local ifdir ifid drvdir
  ifdir=$(usb_iface_dir "$dev")
  ifid=${ifdir##*/}
  drvdir=/sys/bus/usb/drivers/r8152

  if [[ ! -d $drvdir ]]; then
    die "未找到驱动目录: $drvdir（驱动可能未加载）"
  fi

  title "复位 USB 网卡"
  kv "网卡" "$dev"
  kv "USB 接口" "$ifid"

  if (( DRY_RUN )); then
    printf '%s[DRY]%s echo %s > %s/unbind\n' "$C_YEL" "$C_RST" "$ifid" "$drvdir"
    printf '%s[DRY]%s echo %s > %s/bind\n' "$C_YEL" "$C_RST" "$ifid" "$drvdir"
    return 0
  fi

  info "解绑驱动..."
  if ! printf '%s' "$ifid" >"$drvdir/unbind" 2>/dev/null; then
    warn "unbind 失败，尝试 USB authorized 复位"
    local d
    d=$(usb_dev_dir "$dev")
    printf '0' >"$d/authorized"
    sleep 2
    printf '1' >"$d/authorized"
    sleep 3
    if dev=$(detect_dev); then
      ok "USB 复位完成，当前网卡: $dev"
      return 0
    fi
    die "USB 复位失败，请重新插拔设备"
  fi

  sleep 2
  info "重新绑定驱动..."
  if ! printf '%s' "$ifid" >"$drvdir/bind" 2>/dev/null; then
    die "bind 失败，请重新插拔设备"
  fi
  sleep 2

  if dev=$(detect_dev); then
    ok "复位完成，当前网卡: $dev"
  else
    warn "驱动已重绑，但暂未枚举到网卡，请稍后执行 usbnic list 检查"
  fi
}
