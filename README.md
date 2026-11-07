# usbnic - RTL8156 2.5G USB 网卡管理工具

针对 `0bda:8156`（Realtek RTL8156B，2.5G USB 有线网卡，驱动 `r8152`）的完整命令行工具：
设备识别、链路管理、硬件卸载调优、吞吐/稳定性测试、NetworkManager 配置、开机与热插拔持久化，以及用 Wi-Fi 网卡开热点。

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
