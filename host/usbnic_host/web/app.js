const POLL_MS = 2000;
const MAX_POINTS = 90;

const state = {
  status: null,
  deviceFilter: "all",
  search: "",
  chart: null,
  historyIface: "",
  busy: false,
  proxyBusy: false,
  lastStatusAt: 0,
};

const $ = (id) => document.getElementById(id);

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function setText(id, value) {
  const node = $(id);
  if (node) node.textContent = value === undefined || value === null ? "--" : String(value);
}

function fmtBytes(value) {
  const n = Number(value) || 0;
  if (n < 1024) return `${Math.round(n)} B`;
  const units = ["KB", "MB", "GB", "TB", "PB"];
  let size = n;
  let index = -1;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size >= 100 ? size.toFixed(0) : size >= 10 ? size.toFixed(1) : size.toFixed(2)} ${units[index]}`;
}

function fmtRate(value) {
  return `${fmtBytes(value)}/s`;
}

function fmtMbps(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  const n = Number(value);
  return `${n >= 100 ? n.toFixed(0) : n.toFixed(1)} Mbps`;
}

function fmtDuration(seconds) {
  const n = Number(seconds);
  if (!Number.isFinite(n) || n < 0) return "—";
  if (n < 60) return `${Math.round(n)}s`;
  if (n < 3600) return `${Math.floor(n / 60)}m ${Math.floor(n % 60)}s`;
  return `${Math.floor(n / 3600)}h ${Math.floor((n % 3600) / 60)}m`;
}

function fmtAgo(ts) {
  const n = Number(ts);
  if (!Number.isFinite(n) || n <= 0) return "仅租约";
  const seconds = Math.max(0, Date.now() / 1000 - n);
  if (seconds < 5) return "刚刚";
  if (seconds < 60) return `${Math.floor(seconds)}s 前`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m 前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h 前`;
  return `${Math.floor(seconds / 86400)}d 前`;
}

function fmtTime(ts) {
  const n = Number(ts);
  if (!Number.isFinite(n) || n <= 0) return "--";
  return new Date(n * 1000).toLocaleTimeString("zh-CN", { hour12: false });
}

function fmtTimeShort(ts) {
  const n = Number(ts);
  if (!Number.isFinite(n) || n <= 0) return "";
  return new Date(n * 1000).toLocaleTimeString("zh-CN", {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function typeLabel(type) {
  return (
    {
      "wifi-ap": "Wi-Fi AP",
      wired: "有线",
      upstream: "上行",
      vpn: "VPN",
      unknown: "未知",
    }[type] || type || "未知"
  );
}

function typeClass(type) {
  if (type === "wired") return "type-badge wired";
  if (type === "wifi-ap" || type === "upstream") return "type-badge wifi";
  return "type-badge";
}

function signalClass(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "";
  if (n >= -60) return "signal-strong";
  if (n >= -72) return "signal-mid";
  return "signal-weak";
}

function setService(ok) {
  const dot = $("serviceDot");
  const text = $("serviceText");
  if (!dot || !text) return;
  dot.className = `status-dot ${ok ? "on" : "off"}`;
  text.textContent = ok ? "服务在线" : "服务离线";
}

async function fetchJSON(url) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) throw new Error(`${url}: HTTP ${response.status}`);
  return response.json();
}

async function refresh() {
  if (state.busy) return;
  state.busy = true;
  try {
    const [status, events] = await Promise.all([
      fetchJSON("/api/status"),
      fetchJSON("/api/events?limit=40"),
    ]);
    state.status = status;
    state.lastStatusAt = Date.now();
    setService(true);
    renderAll(status, events.items || []);
    setText("lastUpdate", `更新于 ${fmtTime(status.ts)}`);
  } catch (error) {
    setService(false);
    setText("lastUpdate", "无法连接服务");
    console.warn(error);
  } finally {
    state.busy = false;
  }
}

function renderAll(status, events) {
  renderSummary(status);
  renderUplink(status);
  renderWarnings(status);
  renderInterfaces(status);
  renderDevices(status);
  renderEvents(events);
  setText("version", status.version || "--");
  renderChart(status);
}

function renderSummary(status) {
  const uplink = status.uplink || {};
  const proxy = status.proxy || {};
  const summary = (status.devices || {}).summary || {};
  setText("statMode", "只读监控");
  setText("statModeSub", "Phase 1 不修改网络配置");
  setText("statUplink", uplink.dev || "无");
  setText(
    "statUplinkSub",
    `${uplink.ipv4 || "无地址"} · ${uplink.gateway || "无网关"}${
      uplink.speed_mbps ? ` · ${uplink.speed_mbps} Mbps` : ""
    }`
  );

  const proxyText = {
    active: "TUN 已接管",
    clash_only: "Clash 在线",
    tun_only: "TUN 存在",
    off: "未开启",
  }[proxy.status] || "未知";
  setText("statProxy", proxyText);
  setText(
    "statProxySub",
    proxy.clash_ok
      ? `${proxy.active_connections || 0} 活跃连接 · ↓${fmtBytes(
          proxy.download_total
        )} ↑${fmtBytes(proxy.upload_total)}`
      : proxy.error || "Clash API 不可用"
  );

  const proxyButton = $("proxyToggle");
  if (proxyButton && !state.proxyBusy) {
    const active = proxy.status === "active";
    proxyButton.textContent = active ? "关闭代理" : "开启代理";
    proxyButton.dataset.enable = active ? "false" : "true";
    proxyButton.classList.toggle("on", active);
    proxyButton.classList.toggle("off", !active);
    proxyButton.disabled = false;
  }

  setText("statDevices", `${summary.online || 0}/${summary.total || 0}`);
  setText(
    "statDevicesSub",
    `AP ${summary.wifi_ap || 0} · 有线 ${summary.wired || 0} · 走代理 ${
      summary.proxied || 0
    }`
  );

  setText("statThroughput", `↓ ${fmtRate(uplink.rx_rate)}`);
  setText("statThroughputSub", `↑ ${fmtRate(uplink.tx_rate)}`);
}

function renderUplink(status) {
  const uplink = status.uplink || {};
  const iface = uplink.interface || {};
  setText("uplinkDev", uplink.dev || "无");
  setText("uplinkIp", uplink.ipv4 || "—");
  setText("uplinkGw", uplink.gateway || "—");
  const link = [];
  if (uplink.speed_mbps) link.push(`${uplink.speed_mbps} Mbps`);
  if (iface.duplex) link.push(iface.duplex);
  if (iface.connection) link.push(iface.connection);
  setText("uplinkLink", link.join(" · ") || "—");

  const health = status.health || {};
  setText("healthChecked", health.checked_at ? `探测于 ${fmtTime(health.checked_at)}` : "等待探测");
  const grid = $("healthGrid");
  if (!grid) return;
  grid.replaceChildren();

  const entries = Object.values(health.interfaces || {});
  if (!entries.length) {
    grid.appendChild(el("div", "health-empty", health.error || "暂无可探测的上行"));
    return;
  }

  entries.sort((a, b) => (b.score || 0) - (a.score || 0));
  for (const item of entries) {
    const card = el("div", "health-card");
    if (item.iface === uplink.dev) card.classList.add("active");

    const head = el("div", "health-head");
    head.appendChild(el("span", "health-name", item.iface));
    head.appendChild(el("span", "health-score", `${item.score || 0} 分`));
    card.appendChild(head);

    card.appendChild(
      healthLine(
        "网关",
        item.gateway_ok,
        item.gateway_rtt_ms,
        item.gateway ? `${item.gateway}` : "未获取"
      )
    );
    card.appendChild(
      healthLine(
        "公网",
        item.internet_ok,
        item.internet_rtt_ms,
        item.internet_ok === null ? "非默认路由" : item.internet_target || "未探测"
      )
    );
    card.appendChild(
      healthLine("DNS", item.dns_ok, item.dns_ms, item.dns_ok === null ? "非默认路由" : "")
    );

    grid.appendChild(card);
  }
}

function healthLine(label, ok, rtt, detail) {
  const line = el("div", "health-line");
  const dot = el("span", "health-dot");
  if (ok === true) dot.classList.add("ok");
  else if (ok === false) dot.classList.add("bad");
  else dot.classList.add("unknown");
  line.appendChild(dot);
  const text = el("span", "", label);
  line.appendChild(text);
  const value = el("span", "", rtt !== null && rtt !== undefined ? `${rtt} ms` : detail || "—");
  value.style.marginLeft = "auto";
  value.style.color = "var(--faint)";
  line.appendChild(value);
  return line;
}

function renderWarnings(status) {
  const bar = $("warningBar");
  if (!bar) return;
  const warnings = status.warnings || [];
  bar.replaceChildren();
  if (!warnings.length) {
    bar.hidden = true;
    return;
  }
  bar.hidden = false;
  for (const warning of warnings) {
    bar.appendChild(el("span", "warning-chip", warning));
  }
}

function renderInterfaces(status) {
  const tbody = $("ifaceTable")?.querySelector("tbody");
  if (!tbody) return;
  tbody.replaceChildren();
  const interfaces = status.interfaces || [];
  if (!interfaces.length) {
    const row = el("tr");
    const cell = el("td", "empty", "未发现网络接口");
    cell.colSpan = 9;
    row.appendChild(cell);
    tbody.appendChild(row);
    return;
  }

  for (const iface of interfaces) {
    const row = el("tr");
    if (!iface.up) row.style.opacity = "0.62";

    const nameCell = el("td");
    const nameLine = el("div", "cell-main");
    const dot = el("span", `state-dot ${iface.up ? "on" : "off"}`);
    nameLine.appendChild(dot);
    nameLine.appendChild(el("span", "cell-name", iface.name));
    nameCell.appendChild(nameLine);
    const sub = [iface.connection, iface.driver, iface.usb_id].filter(Boolean).join(" · ");
    if (sub) nameCell.appendChild(el("div", "cell-sub", sub));
    row.appendChild(nameCell);

    const typeCell = el("td");
    typeCell.appendChild(el("span", typeClass(iface.type === "ethernet" ? "wired" : iface.type), typeLabel(iface.type)));
    row.appendChild(typeCell);

    const stateCell = el("td");
    const stateLine = el("div", "cell-main");
    stateLine.appendChild(el("span", `state-dot ${iface.up ? "on" : "off"}`));
    stateLine.appendChild(el("span", "", iface.up ? "up" : iface.state || "down"));
    stateCell.appendChild(stateLine);
    row.appendChild(stateCell);

    const link = [];
    if (iface.speed_mbps) link.push(`${iface.speed_mbps} Mbps`);
    if (iface.duplex) link.push(iface.duplex);
    row.appendChild(el("td", "", link.join(" · ") || "—"));

    const ipv4 = iface.ipv4 || [];
    row.appendChild(el("td", "mono", ipv4.length ? ipv4[0] : "—"));
    row.appendChild(el("td", "muted", iface.connection || "—"));
    row.appendChild(el("td", "num", fmtRate(iface.rx_rate)));
    row.appendChild(el("td", "num", fmtRate(iface.tx_rate)));
    row.appendChild(
      el(
        "td",
        "num",
        `${(iface.rx_errors || 0) + (iface.tx_errors || 0)} / ${
          (iface.rx_dropped || 0) + (iface.tx_dropped || 0)
        }`
      )
    );
    tbody.appendChild(row);
  }
}

function renderDevices(status) {
  const tbody = $("deviceTable")?.querySelector("tbody");
  const empty = $("deviceEmpty");
  if (!tbody) return;
  tbody.replaceChildren();
  const items = ((status.devices || {}).items || []).filter(deviceMatches);
  if (!items.length) {
    if (empty) empty.hidden = false;
    return;
  }
  if (empty) empty.hidden = true;

  for (const item of items) {
    const row = el("tr");
    const online = Boolean(item.online);
    if (!online) row.style.opacity = "0.68";

    const statusCell = el("td");
    const statusLine = el("div", "cell-main");
    const dotClass = online ? "on" : item.lease_only ? "warn" : "off";
    statusLine.appendChild(el("span", `state-dot ${dotClass}`));
    statusLine.appendChild(el("span", "", online ? "在线" : item.lease_only ? "仅租约" : "离线"));
    statusCell.appendChild(statusLine);
    row.appendChild(statusCell);

    const nameCell = el("td");
    const nameLine = el("div", "cell-main");
    nameLine.appendChild(el("span", "cell-name", item.name || item.hostname || item.ip || "未知设备"));
    if ((item.vendor || "").toLowerCase().includes("nintendo")) {
      nameLine.appendChild(el("span", "type-badge nintendo", "Nintendo"));
    } else if (item.vendor) {
      nameLine.appendChild(el("span", "type-badge", item.vendor));
    }
    nameCell.appendChild(nameLine);
    if (item.connected_seconds !== null && item.connected_seconds !== undefined) {
      nameCell.appendChild(el("div", "cell-sub", `在线 ${fmtDuration(item.connected_seconds)}`));
    }
    row.appendChild(nameCell);

    const typeCell = el("td");
    typeCell.appendChild(el("span", typeClass(item.type), typeLabel(item.type)));
    row.appendChild(typeCell);

    row.appendChild(el("td", "mono", item.ip || "—"));
    row.appendChild(el("td", "mono", item.mac || "—"));
    row.appendChild(el("td", "muted", item.interface || "—"));

    const signal = el("td", "num");
    if (item.signal_dbm !== null && item.signal_dbm !== undefined) {
      signal.textContent = `${item.signal_dbm} dBm`;
      signal.classList.add(signalClass(item.signal_dbm));
    } else {
      signal.textContent = "—";
    }
    row.appendChild(signal);

    const rate = el("td", "num");
    if (item.rx_rate_mbps !== null || item.tx_rate_mbps !== null) {
      rate.textContent = `↓${fmtMbps(item.rx_rate_mbps)} / ↑${fmtMbps(item.tx_rate_mbps)}`;
    } else {
      rate.textContent = "—";
    }
    row.appendChild(rate);

    const traffic = el("td", "num");
    if (item.byte_source) {
      traffic.appendChild(
        el("div", "", `↓${fmtBytes(item.rx_bytes)} / ↑${fmtBytes(item.tx_bytes)}`)
      );
      traffic.appendChild(el("div", "cell-sub", item.byte_source === "iw" ? "Wi-Fi 关联" : "代理连接"));
    } else if (item.proxy_connections) {
      traffic.textContent = `${item.proxy_connections} 条代理连接`;
    } else {
      traffic.textContent = "—";
    }
    row.appendChild(traffic);

    const proxyCell = el("td");
    const chains = item.proxy_chains || [];
    if (chains.length) {
      const list = el("div", "chain-list");
      for (const chain of chains.slice(0, 3)) {
        list.appendChild(el("span", "chain", chain));
      }
      proxyCell.appendChild(list);
    } else if (item.proxy_connections) {
      proxyCell.appendChild(el("span", "type-badge proxy", `${item.proxy_connections} 条`));
    } else {
      proxyCell.textContent = "—";
    }
    row.appendChild(proxyCell);

    row.appendChild(el("td", "num muted", online ? "刚刚" : fmtAgo(item.last_seen)));
    tbody.appendChild(row);
  }
}

function deviceMatches(item) {
  if (state.deviceFilter === "online" && !item.online) return false;
  if (state.deviceFilter === "wifi" && !["wifi-ap", "upstream"].includes(item.type)) return false;
  if (state.deviceFilter === "wired" && item.type !== "wired") return false;
  if (state.deviceFilter === "proxied" && !(item.proxy_connections > 0)) return false;
  if (!state.search) return true;
  const haystack = [
    item.name,
    item.hostname,
    item.ip,
    item.mac,
    item.vendor,
    item.interface,
    item.type,
    ...(item.proxy_chains || []),
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  return haystack.includes(state.search);
}

function renderEvents(events) {
  const list = $("eventList");
  if (!list) return;
  list.replaceChildren();
  if (!events.length) {
    list.appendChild(el("div", "empty-small", "暂无事件"));
    return;
  }
  for (const event of events) {
    const item = el("div", "event-item");
    item.appendChild(el("span", "event-time", fmtTime(event.ts)));
    item.appendChild(el("span", `event-level ${event.level || "info"}`));
    item.appendChild(el("span", "event-msg", event.message));
    list.appendChild(item);
  }
}

function renderChart(status) {
  if (!state.chart) return;
  const uplink = status.uplink || {};
  const iface = uplink.dev || "";
  setText("chartIface", iface || "无默认上行");
  if (state.historyIface !== iface) {
    state.historyIface = iface;
    loadHistory(iface);
    return;
  }
  if (uplink.dev) {
    state.chart.push(status.ts, uplink.rx_rate || 0, uplink.tx_rate || 0);
  }
}

async function loadHistory(iface) {
  if (!state.chart) return;
  if (!iface) {
    state.chart.setPoints([]);
    return;
  }
  try {
    const data = await fetchJSON(
      `/api/history?iface=${encodeURIComponent(iface)}&minutes=10`
    );
    state.chart.setPoints(data.points || []);
  } catch (error) {
    console.warn(error);
  }
}

class TrafficChart {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.points = [];
    this.width = 0;
    this.height = 0;
    this.resize = this.resize.bind(this);
    window.addEventListener("resize", this.resize);
    if (window.ResizeObserver) {
      new ResizeObserver(this.resize).observe(canvas.parentElement);
    }
    this.resize();
  }

  setPoints(points) {
    this.points = (points || []).slice(-MAX_POINTS);
    this.draw();
  }

  push(ts, rx, tx) {
    const last = this.points[this.points.length - 1];
    if (last && Math.abs(last.ts - ts) < 0.75) {
      last.rx = rx;
      last.tx = tx;
    } else {
      this.points.push({ ts, rx, tx });
    }
    if (this.points.length > MAX_POINTS) this.points.shift();
    this.draw();
  }

  resize() {
    const parent = this.canvas.parentElement;
    if (!parent) return;
    const rect = parent.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    this.canvas.width = Math.max(1, Math.floor(rect.width * dpr));
    this.canvas.height = Math.max(1, Math.floor(rect.height * dpr));
    this.canvas.style.width = `${rect.width}px`;
    this.canvas.style.height = `${rect.height}px`;
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.width = rect.width;
    this.height = rect.height;
    this.draw();
  }

  draw() {
    const ctx = this.ctx;
    if (!ctx || !this.width || !this.height) return;
    ctx.clearRect(0, 0, this.width, this.height);

    const styles = getComputedStyle(document.documentElement);
    const rxColor = styles.getPropertyValue("--rx").trim() || "#41d1c4";
    const txColor = styles.getPropertyValue("--tx").trim() || "#8a6cff";
    const gridColor = styles.getPropertyValue("--border-soft").trim() || "#1a2332";
    const faint = styles.getPropertyValue("--faint").trim() || "#5d687b";
    const muted = styles.getPropertyValue("--muted").trim() || "#8e99ad";

    const padding = { left: 54, right: 12, top: 14, bottom: 24 };
    const plotWidth = Math.max(10, this.width - padding.left - padding.right);
    const plotHeight = Math.max(10, this.height - padding.top - padding.bottom);

    if (!this.points.length) {
      ctx.fillStyle = faint;
      ctx.font = '12px "PingFang SC", system-ui, sans-serif';
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText("等待吞吐数据", this.width / 2, this.height / 2);
      return;
    }

    const maxValue = Math.max(
      1,
      ...this.points.map((point) => Math.max(point.rx || 0, point.tx || 0))
    );
    const niceMax = niceNumber(maxValue);
    const yFor = (value) => padding.top + plotHeight * (1 - value / niceMax);

    ctx.lineWidth = 1;
    ctx.strokeStyle = gridColor;
    ctx.fillStyle = muted;
    ctx.font = '11px "JetBrains Mono", ui-monospace, monospace';
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (let step = 0; step <= 3; step += 1) {
      const value = (niceMax / 3) * step;
      const y = yFor(value);
      ctx.beginPath();
      ctx.moveTo(padding.left, y);
      ctx.lineTo(this.width - padding.right, y);
      ctx.stroke();
      ctx.fillText(fmtRate(value), padding.left - 8, y);
    }

    const xFor = (index) => {
      if (this.points.length === 1) return padding.left + plotWidth / 2;
      return padding.left + (index / (this.points.length - 1)) * plotWidth;
    };

    drawSeries(ctx, this.points, "rx", rxColor, xFor, yFor, padding, plotHeight);
    drawSeries(ctx, this.points, "tx", txColor, xFor, yFor, padding, plotHeight);

    ctx.fillStyle = muted;
    ctx.font = '11px "JetBrains Mono", ui-monospace, monospace';
    ctx.textBaseline = "top";
    ctx.textAlign = "left";
    ctx.fillText(fmtTimeShort(this.points[0].ts), padding.left, this.height - 18);
    ctx.textAlign = "right";
    ctx.fillText(
      fmtTimeShort(this.points[this.points.length - 1].ts),
      this.width - padding.right,
      this.height - 18
    );
  }
}

function drawSeries(ctx, points, key, color, xFor, yFor, padding, plotHeight) {
  if (!points.length) return;
  ctx.beginPath();
  points.forEach((point, index) => {
    const x = xFor(index);
    const y = yFor(point[key] || 0);
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  ctx.stroke();

  const lastIndex = points.length - 1;
  ctx.beginPath();
  ctx.arc(xFor(lastIndex), yFor(points[lastIndex][key] || 0), 2.8, 0, Math.PI * 2);
  ctx.fillStyle = color;
  ctx.fill();

  if (key === "rx") {
    ctx.save();
    ctx.globalAlpha = 0.08;
    ctx.beginPath();
    points.forEach((point, index) => {
      const x = xFor(index);
      const y = yFor(point[key] || 0);
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.lineTo(xFor(lastIndex), padding.top + plotHeight);
    ctx.lineTo(xFor(0), padding.top + plotHeight);
    ctx.closePath();
    ctx.fillStyle = color;
    ctx.fill();
    ctx.restore();
  }
}

function niceNumber(value) {
  if (!Number.isFinite(value) || value <= 0) return 1;
  const exponent = Math.floor(Math.log10(value));
  const fraction = value / 10 ** exponent;
  let niceFraction;
  if (fraction <= 1) niceFraction = 1;
  else if (fraction <= 2) niceFraction = 2;
  else if (fraction <= 5) niceFraction = 5;
  else niceFraction = 10;
  return niceFraction * 10 ** exponent;
}

async function toggleProxy() {
  const button = $("proxyToggle");
  if (!button || state.proxyBusy) return;
  const enable = button.dataset.enable !== "false";
  state.proxyBusy = true;
  button.disabled = true;
  button.textContent = enable ? "开启中..." : "关闭中...";
  try {
    const response = await fetch("/api/proxy/toggle", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enable }),
    });
    const data = await response.json();
    if (!data.ok) throw new Error(data.error || "切换失败");
    await refresh();
  } catch (error) {
    alert(`代理切换失败: ${error.message}`);
  } finally {
    state.proxyBusy = false;
    if (state.status) renderSummary(state.status);
  }
}

function bindFilters() {
  const filter = $("deviceFilter");
  if (filter) {
    filter.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-filter]");
      if (!button) return;
      state.deviceFilter = button.dataset.filter;
      filter.querySelectorAll("button").forEach((item) => {
        item.classList.toggle("active", item === button);
      });
      if (state.status) renderDevices(state.status);
    });
  }

  const search = $("deviceSearch");
  if (search) {
    search.addEventListener("input", () => {
      state.search = search.value.trim().toLowerCase();
      if (state.status) renderDevices(state.status);
    });
  }
}

function init() {
  state.chart = new TrafficChart($("trafficChart"));
  bindFilters();
  const proxyButton = $("proxyToggle");
  if (proxyButton) proxyButton.addEventListener("click", toggleProxy);
  refresh();
  setInterval(refresh, POLL_MS);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refresh();
  });
}

document.addEventListener("DOMContentLoaded", init);
