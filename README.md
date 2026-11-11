# usbnic - RTL8156 2.5G USB 网卡管理工具

针对 `0bda:8156`（Realtek RTL8156B，2.5G USB 有线网卡，驱动 `r8152`）的完整命令行工具：
设备识别、链路管理、硬件卸载调优、吞吐/稳定性测试、NetworkManager 配置、开机与热插拔持久化，以及用 Wi-Fi 网卡开热点。

`host/` 还提供一个本机 Web 控制台：实时监控上行与设备，并在「默认客户端 / USB WiFi AP / 家庭网关」三种网络模式之间切换；透明代理支持 TPROXY，保留下游设备真实源 IP，便于按设备查看代理链路。

> 注意：`0bda:8156` 是**有线**网卡，没有射频模块，本身不能开 Wi-Fi 热点。
> 热点功能使用机器上的 Wi-Fi 网卡（例如同 USB 集线器上的 `0bda:c811`，属 RTL8811CU/8821CU 家族，驱动 `rtw88_8821cu`），8156 作为有线上行。

## 目录结构

```
usbnic                  主程序（CLI 入口）
lib/common.sh           输出、权限、设备自动识别、sysfs 工具
lib/info.sh             list/info/status/caps/diag/stats/watch/link
lib/tune.sh             速率/EEE/WOL/MTU/MAC/ring/irq/txq/offload/power/tune
lib/bench.sh            iperf3 吞吐测试、稳定性压测
lib/persist.sh          NetworkManager 配置、USB 复位、持久化
lib/hotspot.sh          Wi-Fi 热点（NetworkManager shared 模式）
config/usbnic.conf      /etc/usbnic/usbnic.conf 模板
host/                   上位机 Web 控制台（FastAPI + 仪表盘）
scripts/host-start.sh   启动上位机
scripts/host-stop.sh    停止上位机
systemd/                开机自动调优服务
udev/                   热插拔触发规则
scripts/install.sh      安装 / 卸载
```

## 环境要求

- Linux（已在 Ubuntu 24.04 / 内核 7.0 验证），需要 `iproute2`、`ethtool`
- 热点与 DHCP/静态地址需要 NetworkManager（`nmcli`）
- 吞吐测试可选安装 `iperf3`

```bash
sudo apt install ethtool iperf3
```

读类命令不需要 root；改配置、启停网卡、热点等写操作需要 `sudo`。
所有写操作都支持 `-n/--dry-run` 预览，不会改动系统。

## 安装

```bash
sudo scripts/install.sh
```

安装内容：

- 程序复制到 `/usr/local/lib/usbnic`，命令软链到 `/usr/local/sbin/usbnic`
- 默认配置写入 `/etc/usbnic/usbnic.conf`
- 安装 `usbnic-tune.service` 与 `99-usbnic.rules`，开机和热插拔时自动应用配置

卸载：

```bash
sudo scripts/install.sh --uninstall          # 保留 /etc/usbnic 配置
sudo scripts/install.sh --uninstall --purge  # 同时删除配置
```

未安装也可直接运行：

```bash
./usbnic help
```

## 常用命令速查

### 查看

| 命令 | 说明 |
| --- | --- |
| `usbnic list` | 列出所有 USB 网络设备（含 Wi-Fi） |
| `usbnic info` | 设备、USB 链路、驱动/固件、MAC、地址、调优详情 |
| `usbnic status` | 简要状态 |
| `usbnic caps` | 硬件能力矩阵（支持/不支持） |
| `usbnic diag` | 健康检查与优化建议，有 FAIL 时返回 1 |
| `usbnic stats` | 完整计数器 |
| `usbnic stats --delta 2` | 2 秒区间速率/包速率/错误增量 |
| `usbnic watch 1` | 每秒刷新 RX/TX 速率与错误 |
| `usbnic watch 1 10` | 刷新 10 次后退出 |
| `usbnic link` | 链路协商详情 |

指定网卡（默认自动识别 `0bda:8156`）：

```bash
usbnic -d enx6c1ff7d02c4a info
```

### 链路与调优

| 命令 | 说明 |
| --- | --- |
| `sudo usbnic up` / `down` | 启用 / 关闭网卡 |
| `sudo usbnic speed auto` | 恢复全速率自协商 |
| `sudo usbnic speed 2500` | 仅协商 2500baseT/Full |
| `sudo usbnic speed 1000` | 仅协商 1G |
| `sudo usbnic speed 2500 --force` | 关闭自协商强制 2.5G（可能被驱动拒绝） |
| `sudo usbnic restart` | 重新触发自协商 |
| `sudo usbnic eee off` | 关闭 EEE（掉速/抖动时优先尝试） |
| `sudo usbnic eee on` | 开启 EEE |
| `usbnic eee show` | 查看 EEE 状态 |
| `sudo usbnic wol g` | 开启魔术包唤醒（驱动不支持时会告警） |
| `sudo usbnic wol off` | 关闭 WOL |
| `sudo usbnic mtu 9000` | 设置巨型帧（链路两端需一致） |
| `sudo usbnic mac random` | 随机本地管理 MAC |
| `sudo usbnic mac reset` | 恢复永久 MAC |
| `sudo usbnic ring 4096` | RX 环形缓冲设为最大值 |
| `sudo usbnic irq 0` | 关闭中断合并，最低延迟 |
| `usbnic irq` | 查看当前 `rx-usecs` |
| `usbnic txq` | 查看 TX 队列长度 |
| `sudo usbnic txq 2000` | 设置 `txqueuelen` |
| `sudo usbnic offload on` | 开启校验和/TSO/GSO/GRO/VLAN 卸载 |
| `sudo usbnic offload off` | 关闭全部卸载（排障用） |
| `sudo usbnic power on` | 禁用 USB 自动挂起（保持链路稳定） |
| `sudo usbnic power auto` | 允许 USB 自动挂起（省电） |
| `sudo usbnic flags promisc on` | 混杂模式 |
| `sudo usbnic flags allmulti on` | 接收全部多播 |

### 调优档位

```bash
sudo usbnic tune balanced     # 推荐：ring 2048, rx-usecs 100
sudo usbnic tune throughput   # 吞吐优先：ring 4096, rx-usecs 50
sudo usbnic tune latency      # 低延迟：ring 4096, rx-usecs 0
sudo usbnic tune lowpower     # 省电：ring 100, rx-usecs 15000, EEE on, 允许挂起
usbnic tune show              # 查看当前参数
```

所有档位都会开启硬件卸载；不支持的单项会给出告警但不会中断。

### 吞吐与稳定性测试

```bash
# 服务端（对端机器）
sudo usbnic bench --server --port 5201

# 客户端：4 流 TCP 10 秒
usbnic bench --client 192.168.1.10 --time 10 --streams 4

# UDP / 反向 / 双向
usbnic bench --client 192.168.1.10 --udp
usbnic bench --client 192.168.1.10 --reverse
usbnic bench --client 192.168.1.10 --bidir

# 稳定性压测：10 分钟，每 5 秒采样
usbnic soak 10 5
```

`bench` 需要系统安装 `iperf3`；`soak` 会统计掉链路次数、错误增量与平均速率。

### 地址配置（NetworkManager）

```bash
usbnic net show
sudo usbnic net dhcp
sudo usbnic net static 192.168.50.2/24 192.168.50.1 223.5.5.5
```

会创建名为 `usbnic-<网卡名>` 的连接并设为自动连接；未插网线时提示"插线后自动生效"。

### 复位

```bash
sudo usbnic reset
```

通过 `r8152` 驱动 unbind/bind 复位 USB 网卡；失败时自动回退到 USB `authorized` 复位。

### 热点（Wi-Fi AP）

8156 无射频，热点必须由 Wi-Fi 网卡提供；8156 插网线时作为上行。

```bash
# 默认：自动选 USB Wi-Fi 网卡，2.4GHz，随机密码
sudo usbnic hotspot start

# 指定 SSID / 密码
sudo usbnic hotspot start --ssid MyAP --pass 12345678

# 5GHz + 信道 149
sudo usbnic hotspot start --ssid MyAP --pass 12345678 --band a --channel 149

# 开放网络 / 开机自启
sudo usbnic hotspot start --open --ssid FreeWiFi
sudo usbnic hotspot start --autostart

# 自定义热点网关与网段（默认 10.42.0.1/24）
sudo usbnic hotspot start --ssid MyAP --pass 12345678 --gateway 192.168.50.1/24

# 指定无线网卡（例如内置 Intel 网卡）
sudo usbnic hotspot start --wifi wlp0s20f3 --ssid MyAP --pass 12345678

usbnic hotspot status     # 查看 SSID/密码/状态/客户端数
sudo usbnic hotspot clients   # 查看已连接设备的 MAC/IP/信号/速率/连接时长
sudo usbnic hotspot stop  # 停止，保留配置
```

`hotspot clients` 的数据来源按优先级：

1. `iw dev <网卡> station dump`（需安装 `iw`，信息最全：信号、收发射率、字节数、连接时长）
2. mac80211 debugfs（需 root，可给出信号/字节数/连接时长）
3. `ip neigh`（无需 root，只显示有 IP 流量的客户端 MAC/IP）

想看完整信息可执行 `sudo apt install iw`，之后普通用户即可运行 `usbnic hotspot clients`。

基于 NetworkManager 的 `ipv4.method shared`：自动 DHCP + NAT，客户端网关默认 `10.42.0.1/24`。
用 `--gateway` 指定地址后，DHCP 范围会从该网段自动推导，无需单独配置。

注意：

- 启动热点会断开所选 Wi-Fi 网卡当前的 STA 连接
- 若所选 Wi-Fi 正是当前默认路由，客户端可能无法上网，建议用 8156 有线或另一块无线网卡作上行
- USB 2.0 的 Wi-Fi 网卡（480 Mb/s）热点吞吐上限约 100–200 Mbps
- 热点网段不能和上行网段重叠。例如上行是 `192.168.3.4/24` 时不要用 `192.168.3.1/24`；脚本会自动检测并拒绝。`192.168.1.1/24` 虽然可用，但它是最常见的家用路由网段，容易与将来的上行冲突，推荐 `192.168.50.1/24` 这类冷门网段。

### 热点拓扑：内置 Wi-Fi 上网 + USB 网卡开热点

这是最常用的组合：内置无线网卡保持连接路由器（上行），USB 无线网卡单独开热点（内网 AP），笔记本做 NAT 路由。

```
公网
  │
路由器 / 光猫（如 HUAWEI-R1C9VU，192.168.3.1）
  │  STA
wlp0s20f3  Intel AX211（192.168.3.4）   ← 默认路由
  │
笔记本：ip_forward=1 + NetworkManager shared（NAT / DHCP / DNS）
  │  AP
wlx6c1ff7f46d6d  USB RTL8811CU（10.42.0.1/24）
  │
手机 / 平板 / 其他电脑（10.42.0.x）
```

两块网卡是独立射频，内置网卡的 STA 连接和 USB 网卡的 AP 互不干扰。`usbnic hotspot start` 默认就是选 USB 无线网卡开 AP、沿用当前默认路由作上行：

```bash
# 1. 确认内置网卡已联网，默认路由在 wlp0s20f3
ip route show default

# 2. 用 USB 网卡开热点（默认自动识别为 wlx6c1ff7f46d6d）
sudo usbnic hotspot start --ssid Laptop-AP --pass 12345678

# 3. 查看状态（会显示上行网卡、网关、客户端数）
usbnic hotspot status
```

启动时会自动检查：

- 默认路由是否存在，以及是否恰好是即将用作 AP 的网卡
- `net.ipv4.ip_forward` 是否为 1，不是则临时开启
- ufw 是否在运行（可能拦截转发/NAT）

客户端连上后，先 `ping 10.42.0.1`，再 `ping 223.5.5.5` 验证 DNS 与公网连通性。
若客户端能连热点但不能上网，优先检查 `usbnic hotspot status` 中的上行网卡与 IPv4 转发，以及本机防火墙。

### 持久化

```bash
sudo usbnic persist install    # 安装并启用开机/热插拔自动调优
sudo usbnic persist show       # 查看配置、systemd、udev
sudo usbnic persist uninstall  # 移除自动调优
sudo usbnic persist apply      # 立即按配置应用一次
```

配置文件 `/etc/usbnic/usbnic.conf`：

```ini
PROFILE=balanced

# 可选覆盖项
# SPEED=auto                  # auto|10|100|1000|2500
# MTU=1500
# RING=2048
# RX_USECS=100
# OFFLOAD=on                  # on|off
# EEE=off                     # on|off
# AUTOSUSPEND=on              # on=禁用挂起，auto=允许
# WOL=off                     # g|d|off
```

修改后：

```bash
sudo systemctl restart usbnic-tune.service
```

## 上位机（Web 控制台）

`host/` 是运行在本机的 FastAPI 控制台，默认监听 `http://127.0.0.1:8787`，用于查看上行健康、接口吞吐、在线设备，并在四种网络模式之间切换。

```bash
# 一键启动（非 root 会自动用 pkexec 提权）
scripts/host-start.sh              # 只启动控制台
scripts/host-start.sh gateway      # 启动并切到家庭网关
scripts/host-start.sh hotspot      # 启动并切到 USB WiFi AP
scripts/host-start.sh client       # 启动并切到默认客户端
scripts/host-start.sh router       # 启动并切到移动路由器（需手动指定）

# 一键停止：停进程并清理 nftables / 策略路由 / 由上位机开启的代理
scripts/host-stop.sh

# 运行测试
cd host && python3 -m pytest -q
```

启动前会自动清理上次残留的 nftables 表和策略路由，避免规则叠加导致启动失败；停止时如果代理是上位机开启的，会同时关闭 TUN / TPROXY。网关或热点连接本身保持现状，需要断开时在页面切到「默认客户端」。

### 第四种模式：移动路由器

把本机当成一台"带代理的移动路由器"，**默认不启用，只在你点击「切换」时生效**（三个 NM profile 的 `autoconnect` 固定为 `no`，开机不会自动进入）。

| 接口 | NM 连接 | 网段 / 地址池 | 用途 |
|---|---|---|---|
| `enx6c1ff7d02c4a` | `usbnic-router-lan` | 10.10.10.1/24，DHCP .100–.199 | 有线扩展口，直连 Switch 2 底座；接交换机可 1 分 2 同时带 Switch 1/2 |
| `router_5g_iface`（默认留空＝自动探测） | `usbnic-router-5g` | 10.20.0.1/24，.100–.199 | SSID `Laptop-5G`，ch36 / 80MHz。AX211 实测**无法**起 5G AP，会被判定不可用并跳过 |
| `wlx6c1ff7f46d6d`（RTL8821CU） | `usbnic-router-24g` | 10.30.0.1/24，.100–.199 | SSID `Laptop-2.4G`，ch6；**默认关闭** |

华为 AX2 Pro 保持原样，只当"有线交换机 + 2.4G 覆盖"：它下游的 LAN 口设备与 2.4G 客户端都落在 `10.10.10.0/24`，随有线整段直连。USB 卡的 2.4G AP 默认关闭（`router_24g_enabled: false`），避免与上游 AP 同频互相压制；profile 与 `10.30.0.0/24` 网段保留，需要时改配置即可启用。

- **密码**复用 `/etc/usbnic/hotspot.pass`；文件缺失时自动生成 24 位强密码并以 0600 落盘。两个 AP 都是 WPA3-SAE + PMF required + 仅 CCMP。
- **代理分流**：`router_wired_direct: true` 时有线段整段直连（游戏机大流量不被节点限速），两个无线段走代理；`router_direct_cidrs` 可追加直连的单 IP/网段（如 `10.20.0.50/32`）。分流在 nftables 层完成，不依赖 Clash 规则。
- **多接口 TPROXY**：一条 nft 规则同时覆盖三个下游接口，并用 `counter` 统计命中；面板的 `tproxy_hits` 能区分"规则装上"与"真的命中"，不再出现源地址被改写成 `198.18.0.1` 却显示正常的情况。
- **降级与回退**：AX211 起不来时只把 5G 标记为降级，有线扩展口和 2.4G 继续可用；有线扩展口启动失败则整体逆序回退（断 AP → 断有线 → 恢复 rfkill 软锁）并自动切回切换前的模式，页面会显示卡在哪一步。
- **RF 开关**：进入模式时只解锁被软锁的 WLAN 设备并记录 ID，退出时按记录恢复，不影响蓝牙等其它射频。

切换会短暂断开：有线口（Switch 2 会掉线约 10–20 秒，重新 DHCP）、两个 AP 上的设备（需重新关联）。`usbnic-gateway` 与 `usbnic-uplink` 的配置不会被修改。

### 换一张 5G 网卡（新卡到货后）

5G AP 绑哪块网卡由 `router_5g_iface` 决定，留空表示自动挑一块**通过能力探测**的网卡：

1. 先只读确认能力：`GET /api/router/status` 里看 `ap_5g.capability`，或直接看面板模式卡片上的「5G 不可用：原因」。
2. 插上新卡后取其接口名（如 `wlx0c8268xxxxxx`），改 `host/config/host.yaml`：
   ```yaml
   router_5g_iface: wlx0c8268xxxxxx
   router_5g_channel: 36
   ```
3. **先验证再切换**：`curl -s -X POST http://127.0.0.1:8787/api/router/probe`，只起 30 秒 AP，不动当前模式。
4. 探针 `ap_active: true` 后才切到 router 模式；失败会给出原因（no IR / 驱动不支持 / 固件限制），此时保持 `router_5g_iface: ""` 即回到自动探测，不影响有线扩展口。
5. 回退：把 `router_5g_iface` 改回 `""`，或直接在页面切回 `gateway`，`teardown_router` 会按逆序断开 AP、恢复 rfkill。

### Switch 2 走有线（本版结论）

**Switch 2（10.10.10.110）底座网线接华为 AX2 Pro 的 LAN 口**，落在 `10.10.10.0/24`，随有线整段直连，不再尝试无线。

原因（本版记录）：

- 家用 AP 的 5G 在 **ch157**（实测 `WuYanWIFI_5G`，5785 MHz，−33 dBm）；
- 国标（CN 监管域）下，client 类无线芯片（如 RTL8821CU / rtw88）做 AP 只能落在 **149–165**，36–48 属 DFS 且驱动无雷达检测，起不来；
- 而 **Switch 不认 149–165 这段信道**，所以国标环境下 Switch 的 5G 直连无解。

因此 5G AP 的代码与配置全部保留、默认不启用，等有真正的双频 AP 设备（自带 5G 且信道可控）时再启用；在那之前游戏机一律走有线。

### 移动路由器（router）模式：本版终点状态

| 项目 | 状态 |
|---|---|
| 默认启用 | **否**，必须手动在页面切换；三个 profile 均 `autoconnect=no` |
| 有线扩展口 | **可用**：`enx` → 10.10.10.1/24，NAT/DHCP/DNS，整段直连，可接 Switch 2 底座或交换机 1 分 2 |
| 代理分流 | **可用**：nftables 层 `bypass_ips` 按 IP 直连，两个无线段走代理；TPROXY 覆盖多接口并用 counter 校验命中 |
| 5G AP | **不可用**：AX211 无 5G AP 能力（no IR + iwlwifi 固件限制），探测后直接标注原因并跳过 |
| 2.4G AP | **默认关闭**（避免与上游 AP 同频），`router_24g_enabled` 可开 |
| 设备速率统计 | 可用：`GET /api/devices/traffic`，页面「设备」表格（在线状态、速率、流量与代理策略合并展示） |

### 开机自启

```bash
scripts/install-host-boot.sh              # 安装并启用开机自启
scripts/install-host-boot.sh --uninstall  # 移除开机自启
```

安装后：

- `usbnicd.service`：开机启动上位机后台
- `usbnic-boot.service`：检测到家庭网关硬件时自动启用家庭网关，**代理保持关闭**
- 登录桌面时若处于家庭网关模式，自动打开 `http://127.0.0.1:8787`

家庭网关硬件判定条件：下游 USB 网卡有载波、存在已连接网线的有线上行，并且上游默认网关等于 `home_gateway`（默认 `192.168.1.1`）。离开家时通常不满足条件，不会自动启用网关。

依赖：`python3-fastapi`、`python3-uvicorn`、`python3-psutil`、`python3-yaml`，以及 `NetworkManager`、`nftables`。

### 三种网络模式

面板上的「网络模式」卡片可直接切换，对应接口 `POST /api/mode`。

| 模式 | 本机 | 下游设备 | 代理 |
| --- | --- | --- | --- |
| 默认客户端 | 有线优先、WiFi 兜底，不做 NAT | 无 | 默认关闭，可手动开启 TUN |
| USB WiFi AP | 板载 WiFi / 有线上网 | USB 无线网卡热点 `192.168.50.0/24` | 本机 TUN + 下游 TPROXY |
| 家庭网关 | 有线接光猫 | USB 有线网卡共享 `10.10.10.0/24` | 本机 TUN + 下游 TPROXY |

模式底层操作以下 NetworkManager 连接：

- `usbnic-uplink`：有线上行，默认客户端模式优先使用
- `usbnic-hotspot`：USB 无线网卡热点
- `usbnic-gateway`：USB 有线网卡共享网关

切换时会同步 `connection.autoconnect`，避免离开家后网关或热点被自动拉起。

### 重启后重放上次模式

在页面里切换模式成功后，选择会原子写入 `host/data/last_mode.json`（`{"mode","proxy","proxy_explicit","saved_at"}`）。`usbnicd` 启动后 3 秒在后台线程读它并重放，所以 stop → start（页面按钮 / `systemctl restart` / 崩溃自动重启）之后不需要再人工点一次。

规则：

- **没有记录**（首次安装）或记录是 `client`（默认态）：不动网络，只写日志。
- 记录是 `gateway` / `hotspot`：先看 NetworkManager 当前连接。**已经处于该模式**时只补守护进程侧状态（`ip_forward`、代理），不执行 `nmcli down/up`，避免把正在上网的网关短暂掐断；**不一致**时才完整应用一次。
- 记录的 `gateway` 但当前硬件不满足家庭网关拓扑（下游没载波 / 没有有线载波的上行 / 默认网关不是 `home_gateway`）：跳过重放，不强行拉起。
- 代理只有被用户**手动拨过**（`proxy_explicit=true`）才在重启后恢复；按模式默认值带出来的代理一律按开机约定保持关闭，不会因为重放把你手动关掉的代理打开。
- `usbnic-boot.service`（开机检测）在记录里是 `gateway` 以外的模式时直接跳过并交给 `usbnicd` 重放，避免开机把用户的选择覆盖回家庭网关。

支持在页面之外查看/重置：直接编辑或删除 `host/data/last_mode.json`（删除后下次启动当作首次安装处理）。

### 透明代理

代理开关在「代理状态」卡片，对应接口 `POST /api/proxy/toggle`。

- **家庭网关 / USB WiFi AP**：用 `TPROXY`（nftables + 策略路由）接管下游转发流量，Clash 能看到设备真实源 IP（`10.10.10.x` / `192.168.50.x`），因此可以按设备查看代理链路；同时开启 TUN 代理本机流量。
- **默认客户端**：只开 TUN 代理本机。

TPROXY 只匹配配置中的下游接口与网段，LAN 互访和上游光猫网段保持直连。关闭代理时会同时清理 nftables 规则、策略路由和 TUN。由于 TPROXY 在 `prerouting/mangle` 阶段就截获下游包，早于 TUN 的路由决策，两者可以同时开启而不会互相抢流量。

开启代理时会先检查 Clash 内核：已运行就直接复用；未运行则通过 Clash Verge 的 root 服务自动拉起（`clash_service_api`），所以开机后不需要手动打开 Clash。服务不可用时会尝试直接启动 `verge-mihomo`。

#### 设备级代理管控（谁走代理、谁直连）

设备列表每行新增「代理策略」开关，配合表头的「默认直连 / 默认代理」，按 **MAC** 决定单台设备走代理还是直连，IP 只是 ARP / DHCP 租约给出的瞬时映射：

| 功能 | 接口 |
| --- | --- |
| 设备策略列表（含当前生效状态） | `GET /api/devices/policies` |
| 设置单台设备 | `PUT /api/devices/policy`，body `{"mac": "aa:bb:cc:dd:ee:ff", "policy": "proxy"}` |
| 设置全局默认 | `PUT /api/devices/policy/default`，body `{"policy": "direct"}` |

- 策略只有 `proxy` / `direct` 两种，全局默认是 `direct`（不改变现有行为）。页面上的修改写入 `data_dir/device_policies.json`，优先于 `host.yaml` 的初始值。
- 底层是 nft 集合：`proxy_nets` 放"走代理"设备的当前 IP，`direct_nets` 放显式直连的 IP。规则顺序固定为 `直连集合 → 白名单 → tproxy`，直连设备永不被劫持。
- ARP / DHCP 租约变化时，吞吐采样线程每 10 秒刷新一次集合，DHCP 换 IP 不会把策略带错。
- 没有任何设备选择走代理时**不装表**，只开 TUN，下游保持普通 NAT。
- 走代理的下游设备会**强制全走代理**：usbnicd 把它们的当前 IP 写进 mihomo 的 `usbnic-proxy` rule-provider（classical，内容是 `SRC-IP-CIDR,<ip>/32`），规则链最前面的 `RULE-SET,usbnic-proxy,PROXY` 排在 `GEOIP,CN,DIRECT` 之前，因此不会被国内规则拦成直连。provider 目录由 `clash_ruleset_dir` 指定，刷新用 `PUT /providers/rules/usbnic-proxy`，不重载 mihomo；撤表或总开关关闭时 provider 会被清空。
- 设备改成「走代理」时同步装表；装表失败会把策略回滚成 `direct`，接口返回 409 并指出失败的那一步（`install.steps`）。
- 撤销：页面点回「直连」并把默认改回「直连」，程序会自动撤表；或 `sudo python3 -m usbnic_host.cleanup`。

**两处已知根因的处理**：

1. `net.ipv4.conf.all.src_valid_mark` 被 Tailscale 写成 1 时，TPROXY 标记过的转发包会在反向路径校验里被判 martian 静默丢弃（nft 命中数上涨、mihomo 没有任何下游连接）。现在装表前临时置 0 并复核，撤表后恢复原值；若装表瞬间又被改回 1，会直接拒绝装表并报错，不再硬装。
2. 透明套接字必须是 IPv4。mihomo 默认 `bind-address: *` 会起双栈监听，IPv4 的 tproxy 查找找不到带 `IP_TRANSPARENT` 的 v4 套接字（mihomo#3098）。现在装表时把 `bind-address` 临时改成 `0.0.0.0`，并用 `ss` 校验 7896 确实绑在 `0.0.0.0`；校验不过就放弃装表并恢复原配置，避免再次把下游打成黑洞。

TPROXY 的 `fwmark 1` 规则显式使用 `pref 100`，排在 mihomo TUN 的 `9000/9001/9002` 之前，防止标记包被 TUN 的 table 2022 先一步抢走。

装表期间 `allow-lan=true` 且 `bind-address=0.0.0.0`，7896/7897/9097 会对下游可见（TPROXY 需要）。关代理或撤表时会恢复 `bind-address` 原值并把 `allow-lan` 关掉；上游光猫网段与 LAN 互访始终直连。

**隔离验证**：在 `unshare -Urn` 的 netns 里用 veth + 透明套接字验证了「选中 → 投递 → 建连 → 回程」四层（`src_valid_mark=0` 时客户端收到回程数据；置 1 时命中计数照涨但连接超时，复现线上症状）。真机验证仍建议在你在场时进行：装表后 25 秒观察窗口内若下游有流量但 mihomo 没有下游连接，会自动撤表回滚。

**部署 / 撤销**：本次没有新增 systemd 单元，也没有改 `scripts/host-start.sh`；改完代码后 `sudo systemctl restart usbnicd`（或 `sudo scripts/host-start.sh`）即可加载，配置项全部有默认值，旧 `host.yaml` 不用改也能启动。重启会按上次模式重放：代理是否恢复取决于你之前是否手动拨过（见「重启后重放上次模式」）。想彻底撤销设备管控，把默认策略改回「直连」并清空各设备开关，程序会自动撤表；或执行 `sudo python3 -m usbnic_host.cleanup --proxy`。

### 宽带测速与实时吞吐

面板新增两张卡片，对应接口如下：

| 功能 | 接口 |
| --- | --- |
| 启动测速（后台任务，单任务互斥） | `POST /api/speedtest/start`（已有任务时返回 409） |
| 测速状态 / 实时采样点 / 最近结果 | `GET /api/speedtest/status` |
| 历史记录（最多 50 条，落盘 `host/data/speedtest_history.json`） | `GET /api/speedtest/history?limit=20` |
| 网口实时吞吐快照 | `GET /api/throughput?iface=enp49s0` |
| 保持后台记录（默认关） | `POST /api/throughput/keep` |

**主动测速**：点「重新测速」后依次跑延迟、下行、上行三段，测试中每 0.25 秒采一个点画曲线，结束后给出下行 / 上行 Mbps、延迟、抖动、耗时、实际出口接口，并标注「直连」或「经代理」。下行用阿里云、清华、腾讯三个国内镜像多线程 Range 下载，默认 6 秒 / 350 MB 上限；上行 POST 到配置的上传端点，默认 5 秒 / 150 MB 上限。测速在后台线程执行，`/api` 不会被阻塞；失败会给出具体原因（端点不可达、超时、接口无流量），不会静默返回 0。

**直连口径**：测速流量必须绕开 Clash，否则测到的是代理节点速度。实现上对所有测速 socket 使用 `SO_BINDTODEVICE` 绑定出口网卡（默认 `enp49s0`，失败时回退绑定该网卡的源 IP），并直接用 UDP 向 `223.5.5.5` / `119.29.29.29` 查询 A 记录，避开 Clash 的 fake-IP 网段（`198.18.0.0/16`），再用真实 IP 建 TLS 连接（SNI 仍是域名）。结果里的 `bind_mode` / `direct` / `proxy_active` 字段记录本次实际绑定方式与当时代理状态，代理开着也会显示「直连 · 本次已绕过」。

**实时吞吐**：后端进程内一个采样线程，每 1 秒读一次 `psutil.net_io_counters(pernic=True)`，保留最近 60 个点，前端只做渲染，不会每个标签页各采一份。页面可切换接口（默认优先当前上行出口，其次 `enp49s0`），显示当前上下行、60 秒均值、60 秒峰值和滚动曲线。默认只在有页面打开时采样，打开「保持后台记录」后页面关闭仍继续采。

**统计口径**：实时吞吐读的是**网口层计数**，包含所有经过这台机器的流量——NAT 转发、代理转发、本机与下游设备、本地互传都会算进去，**不是某一台设备的速度**。主动测速时曲线照常显示，因此能在图上直接看到测速流量本身。

**已知短板 / 待办**：

- 上行端点目前用 `https://speed.cloudflare.com/__up`，属国际线路，直连时实测上传只有 12–18 Mbps，偏低，不能代表宽带上行真实能力。
- **待办：上行测速端点待替换为国内可写接口**（需找到稳定、免鉴权、可接收大体积 POST 的国内端点），替换前上行数值仅作参考。
- 下行镜像与上传端点均在 `host/config/host.yaml` 的 `speedtest_downloads` / `speedtest_upload_url` 中配置，可自行调整。

### 使用时长与健康监测

面板新增「使用时长与健康」卡片：今日前台应用排行（时长 + 占比）、近 7 天趋势，以及 CPU / 内存 / swap / 温度曲线。

**并入现有服务**：该功能直接跑在 `usbnicd` 进程里（`Monitor` 启停时一起启动 / 停止），**不新增 systemd 单元**，也不需要单独的安装步骤。生产环境改完代码后重启即可：

```bash
sudo systemctl restart usbnicd
```

**采集方式**：

- GNOME on **X11**：每 `usage_sample_interval`（默认 4 秒）调用一次 `xprop -root _NET_ACTIVE_WINDOW`；只有窗口 ID 变化时才补一次 `xprop -id ... _NET_WM_PID WM_CLASS`，并按窗口 ID 缓存。PID → `/proc/<pid>/comm` → 易读应用名。
- 两次采样之间的时间差累加到上一个应用；超过 60 秒的间隔（休眠 / 挂起 / 服务停过）直接丢弃，跨午夜按本地日期拆分。
- **空闲不计时**：无活动窗口、logind `LockedHint=yes`（锁屏，15 秒检查一次）、XScreenSaver 空闲超过 `usage_idle_seconds`（默认 300 秒），或活动窗口属于 `gnome-shell` / `Xorg` / gdm 等桌面壳层。
- 系统健康每 `system_health_interval`（默认 30 秒）用 `psutil` 采样一次；温度优先 `coretemp` 的 `Package id 0`，没有 CPU 传感器时退回 nvme 或可用传感器最大值。

**依赖与限制**：

- 需要 `xprop`（Xorg 自带）。不依赖 `xdotool`、`xprintidle` 或 `lm-sensors`；空闲时间通过 `libXss` 的 XScreenSaver 扩展读取。
- 只支持 **X11** 会话。Wayland 下 `_NET_ACTIVE_WINDOW` 不可用，接口会返回 `x11.available: false` 和具体原因，页面显示「X11 不可用」，不会静默计 0。
- 以 root 运行的 `usbnicd` 会自动从 `who` 和 `/run/user/<uid>/gdm/Xauthority` 发现登录会话的 X11 凭据；自动发现失败时可手动配置 `usage_x11_display` / `usage_x11_authority`。

**接口**：

| 功能 | 接口 |
| --- | --- |
| 今日应用使用排行 + 当前前台应用 + 空闲原因 | `GET /api/usage` |
| 近 N 天每日总时长与应用汇总 | `GET /api/usage/trend?days=7` |
| 系统健康最新值与曲线点 | `GET /api/system/health?hours=6&points=240` |

**数据存储与隐私**：

- 全部写入 `data_dir` 下的 `host.db`（开发环境默认 `host/data/host.db`；生产可在 `host.yaml` 把 `data_dir` 指到 `/var/lib/usbnic`）。
- `app_usage(day, app, seconds)`：按日期 + 应用分桶，默认保留 90 天。
- `system_health_samples(ts, cpu, mem, swap, disk, temp)`：默认保留 7 天。
- **默认只记录应用名，不记录窗口标题**；只有把 `usage_record_window_title` 设为 `true` 才会写 `app_titles`。
- 运行时数据（`host/data/`、`*.db`、`*.log`）已在 `.gitignore` 中排除，不会进入 public 仓库；仓库里只有配置默认值，没有 token / 密码 / 私钥。

### 配置

配置文件 `host/config/host.yaml`，主要项：

```yaml
bind: 127.0.0.1
port: 8787
clash_api: http://127.0.0.1:9097
clash_service_api: http://127.0.0.1:33211

uplink_connection: usbnic-uplink
gateway_connection: usbnic-gateway
hotspot_connection: usbnic-hotspot

gateway_iface: enx6c1ff7d02c4a
lan_cidr: 10.10.10.0/24
hotspot_iface: wlx6c1ff7f46d6d
hotspot_cidr: 192.168.50.0/24

# 开机自动进入家庭网关的判定条件
home_gateway: 192.168.1.1

clash_tproxy_port: 7896

# TPROXY 永不接管的下游源地址（按 IP 兜底，设备级策略已能按 MAC 精确控制）
tproxy_exclude_ips: []
# 装表后的观察窗口：窗口内下游有流量但 mihomo 侧没有下游连接，自动撤表回滚
tproxy_verify_seconds: 5
# mihomo rule-provider 目录：usbnicd 维护 usbnic-proxy.yaml，留空表示不启用
clash_ruleset_dir: /path/to/clash-verge/ruleset

# 设备级代理策略：默认直连；运行期设置写入 data_dir/device_policies.json
tproxy_default_policy: direct
device_policies:                     # 首次生成的初始值（MAC -> proxy/direct）
  "40:44:f7:d8:99:c5": direct        # Switch 2：代理会掉 NAT 类型，默认直连
device_policies_file: device_policies.json

# 使用时长 / 系统健康（并入 usbnicd）
usage_enabled: true
usage_sample_interval: 4.0        # 活动窗口查询间隔（秒）
usage_idle_seconds: 300           # 无输入超过 5 分钟不计时
usage_record_window_title: false  # 默认只记应用名，不记窗口标题
usage_retention_days: 90
system_health_interval: 30.0
system_health_retention_days: 7
usage_x11_display: ""             # 留空自动发现
usage_x11_authority: ""           # 留空自动发现
```

设备别名通过 `device_aliases`（MAC → 名称）配置；`device_aliases` 里的 MAC 必须是设备当前实际地址，开启「私有 Wi-Fi 地址」后会变化。

本机接口地址（`10.10.10.1`、`198.18.0.1`、`fdfe:…` 等）默认保留在设备表里并标注「本机」，用来对照接口状态。若只想看真实下游设备，设置：

```yaml
hide_local_addresses: true
```

重启 `usbnicd` 后生效，本机地址会从设备列表和流量统计中一并过滤。

## 设备能力边界

| 能力 | 状态 |
| --- | --- |
| 10/100/1000/2500baseT 自协商 | 支持 |
| 校验和 / TSO / GSO / GRO / SG / VLAN 卸载 | 支持 |
| RX 环形 100–4096 | 支持 |
| 中断合并 `rx-usecs` | 支持 |
| EEE | 支持 |
| MTU 最大 9000（以驱动上限为准） | 支持 |
| USB 自动挂起控制 | 支持 |
| MAC 地址修改 | 支持 |
| Wake-on-LAN | 驱动未暴露（本机实测） |
| RSS / 多队列 | 不支持（单队列） |
| 硬件 PTP 时间戳 | 不支持（仅软件时间戳） |
| FEC | 不支持 |
| 硬件环回 | 不支持 |
| Wi-Fi AP | 不支持（无射频，需用 Wi-Fi 网卡） |

## 典型场景

### 1. 插上就用，日常稳定优先

```bash
sudo usbnic tune balanced
sudo usbnic power on
usbnic diag
```

### 2. 追求 2.5G 满速

```bash
sudo usbnic speed 2500
sudo usbnic tune throughput
sudo usbnic ring 4096
usbnic bench --client <对端IP> --streams 4
```

确认 `diag` 中 USB 链路为 5000 Mb/s；若显示 480 Mb/s，说明插在 USB 2.0 口或用了 USB 2.0 线。

### 3. 链路反复掉线

```bash
sudo usbnic eee off
sudo usbnic power on
sudo usbnic offload on
usbnic soak 10 5
```

### 4. 让 8156 作为热点上行

```bash
# 8156 插网线，USB Wi-Fi 网卡开热点
sudo usbnic hotspot start --ssid MyAP --pass 12345678
usbnic hotspot status
```

### 5. 排障

```bash
usbnic diag
sudo usbnic reset
sudo usbnic offload off
usbnic stats --delta 2
```

## 排障提示

- `ethtool: Operation not permitted`：读写类操作需要 `sudo`
- `speed`/`ring` 设置失败：先 `sudo usbnic down`，设置后再 `sudo usbnic up`
- `diag` 返回 1 表示存在 FAIL 项，按末尾"建议"逐条处理
- WOL 显示"不支持"是该驱动未暴露能力，不是配置错误
- 热点启动失败：确认所选网卡 `WIFI-PROPERTIES.AP: yes`，5GHz 需网卡与监管域支持
- 修改配置后未生效：`sudo systemctl restart usbnic-tune.service`，或 `sudo usbnic persist apply`
