const POLL_MS = 2000;
const PROXY_POLL_MS = 10000;
// 路由器只读状态要 fork nmcli/iw，不必跟 2s 主循环同频；服务端另有 15s 缓存
const ROUTER_POLL_MS = 10000;
// 页面在后台时整体降频，避免多个标签页一起空转
const HIDDEN_POLL_MS = 15000;
const MAX_POINTS = 90;

const state = {
  status: null,
  deviceFilter: "all",
  search: "",
  chart: null,
  historyIface: "",
  busy: false,
  proxyBusy: false,
  modeBusy: false,
  lastStatusAt: 0,
  refreshTimer: null,
  proxyOverview: null,
  proxySignature: "",
  proxyFilters: {},
  proxyEls: {},
  proxyGroupBusy: "",
  routerStatus: null,
  routerTimer: null,
  hotspot: null,
  hotspotReveal: false,
  hotspotPassword: "",
  speedtest: null,
  speedtestBusy: false,
  speedtestTimer: null,
  speedtestChart: null,
  speedtestTrend: null,
  speedtestNoticeUntil: 0,
  throughput: null,
  throughputIface: "",
  throughputTimer: null,
  throughputChart: null,
  throughputSignatures: "",
  usage: null,
  usageTrend: null,
  systemHealth: null,
  usageTimer: null,
  usageTrendTimer: null,
  systemHealthTimer: null,
  usageTrendChart: null,
  systemHealthChart: null,
  devicePolicies: null,
  traffic: null,
  policyBusy: false,
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
    const [status, events, rates, policies] = await Promise.all([
      fetchJSON("/api/status"),
      fetchJSON("/api/events?limit=40"),
      fetchJSON("/api/devices/traffic").catch(() => ({ items: [] })),
      fetchJSON("/api/devices/policies").catch(() => null),
    ]);
    state.status = status;
    state.lastStatusAt = Date.now();
    state.traffic = rates;
    if (policies) state.devicePolicies = policies;
    setService(true);
    renderAll(status, events.items || []);
    renderPolicyDefault();
    setText("lastUpdate", `更新于 ${fmtTime(status.ts)}`);
  } catch (error) {
    setService(false);
    setText("lastUpdate", "无法连接服务");
    console.warn(error);
  } finally {
    state.busy = false;
  }
}

// 路由器只读状态单独一个稍慢的节拍：每次请求会 fork nmcli/iw，没必要跟主循环同频
async function refreshRouter() {
  if (!document.hidden) {
    try {
      state.routerStatus = await fetchJSON("/api/router/status");
      if (state.status) renderModes(state.status);
    } catch (error) {
      console.warn(error);
    }
  }
  scheduleRouter();
}

function scheduleRouter() {
  if (state.routerTimer) clearTimeout(state.routerTimer);
  state.routerTimer = setTimeout(
    refreshRouter,
    document.hidden ? HIDDEN_POLL_MS : ROUTER_POLL_MS
  );
}

function scheduleRefresh() {
  if (state.refreshTimer) clearTimeout(state.refreshTimer);
  state.refreshTimer = setTimeout(
    runRefresh,
    document.hidden ? HIDDEN_POLL_MS : POLL_MS
  );
}

async function runRefresh() {
  await refresh();
  scheduleRefresh();
}

function renderAll(status, events) {
  renderSummary(status);
  renderUplink(status);
  renderModes(status);
  renderWarnings(status);
  renderInterfaces(status);
  renderDevices();
  renderEvents(events);
  setText("version", status.version || "--");
  renderChart(status);
}

function renderSummary(status) {
  const uplink = status.uplink || {};
  const proxy = status.proxy || {};
  const summary = (status.devices || {}).summary || {};
  const modes = status.modes || {};
  const activeMode = (modes.items || []).find((item) => item.active) || {};
  setText("statMode", status.mode_label || activeMode.label || "--");
  setText("statModeSub", activeMode.desc || "--");
  setText("statUplink", uplink.dev || "无");
  setText(
    "statUplinkSub",
    `${uplink.ipv4 || "无地址"} · ${uplink.gateway || "无网关"}${
      uplink.speed_mbps ? ` · ${uplink.speed_mbps} Mbps` : ""
    }`
  );

  const proxyText = {
    active: proxy.mode === "tproxy" ? "TPROXY 已接管" : "TUN 已接管",
    clash_only: "Clash 在线",
    tun_only: "TUN 存在",
    off: "未开启",
  }[proxy.status] || "未知";
  setText("statProxy", proxyText);
  const proxyDetail = [];
  if (proxy.clash_ok) {
    proxyDetail.push(`${proxy.active_connections || 0} 活跃连接`);
    if (proxy.tproxy) proxyDetail.push(`下游 ${proxy.downstream_connections || 0} 条`);
    proxyDetail.push(`↓${fmtBytes(proxy.download_total)} ↑${fmtBytes(proxy.upload_total)}`);
  } else {
    proxyDetail.push(proxy.error || "Clash API 不可用");
  }
  setText("statProxySub", proxyDetail.join(" · "));

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

function renderModes(status) {
  const list = $("modeList");
  if (!list) return;
  const items = ((status.modes || {}).items || []);
  list.replaceChildren();
  if (!items.length) {
    list.appendChild(el("div", "empty-small", "暂无模式信息"));
    return;
  }
  for (const item of items) {
    const row = el("div", `mode-item${item.active ? " active" : ""}`);
    const text = el("div", "mode-text");
    text.appendChild(el("strong", "", item.label));
    text.appendChild(el("span", "", item.desc));
    row.appendChild(text);

    if (item.key === "router" && state.routerStatus) {
      const cap = el("div", "mode-cap");
      const ap5 = state.routerStatus.ap_5g || {};
      const ap24 = state.routerStatus.ap_24g || {};
      const line5 = ap5.available
        ? `5G 可用：${ap5.iface || "自动"} ch${ap5.channel}`
        : `5G 不可用：${ap5.reason || "能力未知"}`;
      const line24 = ap24.enabled
        ? ap24.available
          ? `2.4G 可用：${ap24.iface} ch${ap24.channel}`
          : `2.4G 不可用：${ap24.reason || "能力未知"}`
        : "2.4G 默认关闭";
      cap.textContent = `${line5} · ${line24}`;
      text.appendChild(cap);
    }

    const button = el(
      "button",
      `mode-btn${item.active ? " active" : ""}`,
      state.modeBusy ? "切换中…" : item.active ? "当前" : "切换"
    );
    button.type = "button";
    button.disabled = state.modeBusy || item.active;
    button.addEventListener("click", () => toggleMode(item.key));
    row.appendChild(button);
    list.appendChild(row);
  }
}

async function toggleMode(mode) {
  if (state.modeBusy) return;
  const items = ((state.status || {}).modes || {}).items || [];
  const target = items.find((item) => item.key === mode);
  const label = target ? target.label : mode;
  if (!confirm(`切换到「${label}」会修改本机网络配置，继续？`)) return;

  state.modeBusy = true;
  setText("modeHint", "切换中，请稍候…");
  if (state.status) renderModes(state.status);
  try {
    const response = await fetch("/api/mode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode }),
    });
    const data = await response.json();
    if (!data.ok) {
      const failed = (data.steps || [])
        .filter((step) => !step.ok)
        .map((step) => `${step.name}: ${step.detail || "失败"}`)
        .join("\n");
      throw new Error(failed || data.error || "切换失败");
    }
    await refresh();
  } catch (error) {
    alert(`模式切换失败:\n${error.message}`);
  } finally {
    state.modeBusy = false;
    setText("modeHint", "切换后立即生效");
    if (state.status) renderModes(state.status);
  }
}

function proxyTypeLabel(type) {
  return (
    {
      Selector: "手动选择",
      URLTest: "自动测速",
      Fallback: "故障转移",
      LoadBalance: "负载均衡",
      Relay: "链式代理",
    }[type] || type || "策略组"
  );
}

function proxyModeLabel(mode) {
  return (
    {
      rule: "规则模式",
      global: "全局模式",
      direct: "直连模式",
    }[mode] || mode || ""
  );
}

function fmtDelay(delay, alive = true) {
  if (!alive) return "不可用";
  if (delay === null || delay === undefined) return "未测速";
  if (delay === 0) return "超时";
  return `${delay} ms`;
}

function proxySignature(overview) {
  return JSON.stringify({
    ok: Boolean(overview && overview.ok),
    groups: (overview && overview.groups) || [],
  });
}

function updateProxyMeta(overview) {
  if (!overview || !overview.ok) {
    setText("proxyMeta", (overview && overview.error) || "Clash 不可用");
    return;
  }
  const parts = [proxyModeLabel(overview.mode), `更新于 ${fmtTime(overview.updated_at)}`].filter(
    Boolean
  );
  setText("proxyMeta", parts.join(" · "));
}

async function refreshProxies(force = false) {
  if (state.proxyGroupBusy) return;
  // 页面在后台时不再拉 60KB 的节点列表
  if (!force && document.hidden) return;
  // 用户正在筛选节点时不重建列表，避免输入框失焦
  const active = document.activeElement;
  if (!force && active && active.classList && active.classList.contains("proxy-search")) {
    return;
  }
  try {
    const overview = await fetchJSON("/api/proxies");
    state.proxyOverview = overview;
    updateProxyMeta(overview);
    const signature = proxySignature(overview);
    if (!force && signature === state.proxySignature) return;
    state.proxySignature = signature;
    renderProxies(overview);
  } catch (error) {
    setText("proxyMeta", "无法读取 Clash");
    console.warn(error);
  }
}

function applyProxyOverview(overview) {
  if (!overview) return;
  state.proxyOverview = overview;
  state.proxySignature = proxySignature(overview);
  updateProxyMeta(overview);
  renderProxies(overview);
}

function renderProxies(overview) {
  const container = $("proxyGroups");
  if (!container) return;
  state.proxyEls = {};
  container.replaceChildren();
  if (!overview || !overview.ok) {
    container.appendChild(
      el("div", "empty-small", (overview && overview.error) || "Clash API 不可用")
    );
    return;
  }
  const groups = overview.groups || [];
  if (!groups.length) {
    container.appendChild(el("div", "empty-small", "Clash 暂无策略组"));
    return;
  }
  for (const group of groups) {
    container.appendChild(buildProxyGroup(group));
  }
}

function buildProxyGroup(group) {
  const wrap = el("div", "proxy-group");
  const head = el("div", "proxy-group-head");

  const title = el("div", "proxy-group-title");
  const nameLine = el("div", "proxy-group-name");
  nameLine.appendChild(el("strong", "", group.name));
  nameLine.appendChild(el("span", "proxy-type-badge", proxyTypeLabel(group.type)));
  if (group.auto) nameLine.appendChild(el("span", "proxy-type-badge auto", "自动"));
  if (group.fixed) nameLine.appendChild(el("span", "proxy-type-badge fixed", "已固定"));
  title.appendChild(nameLine);

  const exitLine = el("div", "proxy-exit");
  exitLine.appendChild(el("span", "proxy-exit-label", "实际出口"));
  exitLine.appendChild(el("span", "proxy-exit-name", group.exit || "—"));
  exitLine.appendChild(el("span", "proxy-delay", fmtDelay(group.delay, group.alive)));
  if ((group.chain || []).length) {
    exitLine.appendChild(el("span", "proxy-chain", group.chain.join(" → ")));
  }
  title.appendChild(exitLine);
  head.appendChild(title);

  const tools = el("div", "proxy-tools");
  if ((group.options || []).length > 30) {
    const input = el("input", "proxy-search");
    input.type = "search";
    input.placeholder = "筛选节点";
    input.value = state.proxyFilters[group.name] || "";
    input.addEventListener("input", () => {
      state.proxyFilters[group.name] = input.value.trim().toLowerCase();
      const entry = state.proxyEls[group.name];
      if (entry) renderProxyNodes(entry.group, entry.grid);
    });
    tools.appendChild(input);
  }
  if (group.fixed) {
    const autoButton = el("button", "ghost-btn auto-btn", "恢复自动");
    autoButton.type = "button";
    autoButton.addEventListener("click", () => restoreProxyAuto(group.name));
    tools.appendChild(autoButton);
  }
  const testButton = el("button", "ghost-btn", "测速");
  testButton.type = "button";
  testButton.addEventListener("click", () => testProxyGroup(group.name, testButton));
  tools.appendChild(testButton);
  head.appendChild(tools);

  wrap.appendChild(head);
  const grid = el(
    "div",
    group.selectable ? "proxy-node-grid" : "proxy-node-grid readonly"
  );
  wrap.appendChild(grid);
  state.proxyEls[group.name] = { group, grid, node: wrap };
  renderProxyNodes(group, grid);
  return wrap;
}

function renderProxyNodes(group, grid) {
  const query = state.proxyFilters[group.name] || "";
  const options = (group.options || []).filter((option) => {
    if (!query) return true;
    return `${option.name} ${option.exit || ""} ${option.type || ""}`
      .toLowerCase()
      .includes(query);
  });
  grid.replaceChildren();
  if (!options.length) {
    grid.appendChild(el("div", "empty-small", "没有匹配的节点"));
    return;
  }
  for (const option of options) {
    const chip = el(group.selectable ? "button" : "div", "proxy-node");
    if (group.selectable) chip.type = "button";
    chip.classList.toggle("selected", Boolean(option.selected));
    chip.classList.toggle("exit", option.name === group.exit);
    chip.classList.toggle("dead", !option.alive || option.delay === 0);
    if (option.is_group) chip.classList.add("group");
    chip.appendChild(el("span", "proxy-node-name", option.name));

    const meta = [];
    if (option.is_group) {
      const label = proxyTypeLabel(option.type);
      meta.push(
        option.exit && option.exit !== option.name ? `${label} → ${option.exit}` : label
      );
    }
    meta.push(fmtDelay(option.delay, option.alive));
    chip.appendChild(el("span", "proxy-node-meta", meta.join(" · ")));

    if (group.selectable) {
      chip.disabled = state.proxyGroupBusy === group.name;
      chip.addEventListener("click", () => selectProxyNode(group.name, option.name));
    }
    grid.appendChild(chip);
  }
}

function rerenderProxyGroup(name) {
  const entry = state.proxyEls[name];
  if (entry) renderProxyNodes(entry.group, entry.grid);
}

async function selectProxyNode(group, name) {
  if (state.proxyGroupBusy) return;
  state.proxyGroupBusy = group;
  rerenderProxyGroup(group);
  setText("proxyMeta", `切换 ${group} 中…`);
  try {
    const response = await fetch("/api/proxies/select", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ group, name }),
    });
    const data = await response.json();
    if (!data.ok) throw new Error(data.error || "选择失败");
    applyProxyOverview(data.overview);
  } catch (error) {
    alert(`切换节点失败:\n${error.message}`);
    updateProxyMeta(state.proxyOverview);
  } finally {
    state.proxyGroupBusy = "";
    rerenderProxyGroup(group);
  }
}

async function restoreProxyAuto(group) {
  if (state.proxyGroupBusy) return;
  state.proxyGroupBusy = group;
  rerenderProxyGroup(group);
  setText("proxyMeta", `恢复 ${group} 自动选择中…`);
  try {
    const response = await fetch("/api/proxies/auto", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ group }),
    });
    const data = await response.json();
    if (!data.ok) throw new Error(data.error || "恢复自动失败");
    applyProxyOverview(data.overview);
  } catch (error) {
    alert(`恢复自动失败:\n${error.message}`);
    updateProxyMeta(state.proxyOverview);
  } finally {
    state.proxyGroupBusy = "";
    rerenderProxyGroup(group);
  }
}

async function testProxyGroup(group, button) {
  if (state.proxyGroupBusy) return;
  state.proxyGroupBusy = group;
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "测速中…";
  setText("proxyMeta", `正在测试 ${group}…`);
  try {
    const response = await fetch("/api/proxies/delay", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target: group }),
    });
    const data = await response.json();
    if (!data.ok) throw new Error(data.error || "测速失败");
    applyProxyOverview(data.overview);
  } catch (error) {
    alert(`测速失败:\n${error.message}`);
    updateProxyMeta(state.proxyOverview);
  } finally {
    state.proxyGroupBusy = "";
    button.disabled = false;
    button.textContent = original;
  }
}

const KEY_MGMT_LABELS = {
  sae: "WPA3-SAE",
  "wpa-psk": "WPA2/WPA3",
  "wpa-eap": "WPA-Enterprise",
};

const PMF_LABELS = {
  "0": "默认",
  "1": "关闭",
  "2": "可选",
  "3": "必需",
};

async function refreshHotspot() {
  try {
    state.hotspot = await fetchJSON("/api/hotspot");
    renderHotspot();
  } catch (error) {
    setText("hotspotMeta", `读取失败：${error.message}`);
  }
}

function renderHotspot() {
  const data = state.hotspot;
  if (!data) return;
  const revealBtn = $("hotspotReveal");
  if (!data.available) {
    setText("hotspotMeta", "未配置热点");
    setText("hotspotSsid", "--");
    setText("hotspotState", "--");
    setText("hotspotSecurity", "--");
    setText("hotspotCipher", "--");
    setText("hotspotPassword", "--");
    setText("hotspotPasswordFile", data.password_file || "--");
    state.hotspotPassword = "";
    if (revealBtn) revealBtn.disabled = true;
    return;
  }
  if (revealBtn) revealBtn.disabled = false;

  const security = data.security || {};
  const keyMgmt = KEY_MGMT_LABELS[security.key_mgmt] || security.key_mgmt || "未设置";
  const pmf = PMF_LABELS[String(security.pmf)] || security.pmf || "默认";
  const cipher = security.pairwise || security.group
    ? `${(security.pairwise || "--").toUpperCase()} / ${(security.group || "--").toUpperCase()}`
    : "未限制";

  setText("hotspotSsid", data.ssid || "--");
  setText("hotspotState", data.active ? "已启动" : "未激活");
  setText("hotspotSecurity", `${keyMgmt} · PMF ${pmf}`);
  setText("hotspotCipher", cipher);

  state.hotspotPassword = data.password || "";
  if (state.hotspotPassword) {
    setText(
      "hotspotPassword",
      state.hotspotReveal ? state.hotspotPassword : "•".repeat(Math.min(state.hotspotPassword.length, 16)),
    );
  } else {
    setText("hotspotPassword", "（未读取到）");
  }
  setText(
    "hotspotPasswordFile",
    data.password_file ? `${data.password_file}${data.password_file_exists ? "" : "（尚未生成）"}` : "--",
  );
  setText("hotspotMeta", data.active ? "热点运行中" : "热点未激活");
  if (revealBtn) revealBtn.textContent = state.hotspotReveal ? "隐藏密码" : "显示密码";
}

async function copyHotspotPassword() {
  if (!state.hotspotPassword) {
    setText("hotspotPasswordHint", "没有可复制的密码");
    return;
  }
  try {
    await navigator.clipboard.writeText(state.hotspotPassword);
    setText("hotspotPasswordHint", "已复制到剪贴板");
  } catch (error) {
    setText("hotspotPasswordHint", "复制失败，请先显示密码手动复制");
  }
  setTimeout(() => setText("hotspotPasswordHint", ""), 2500);
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

function mergedDevices() {
  // 元数据来自 /api/status（在线、接口、信号），速率与流量来自 /api/devices/traffic
  const meta = ((state.status || {}).devices || {}).items || [];
  const traffic = (state.traffic || {}).items || [];
  const byIp = new Map();
  const byMac = new Map();
  for (const item of meta) {
    if (item.ip) byIp.set(item.ip, item);
    if (item.mac) byMac.set(item.mac, item);
  }
  const rows = [];
  const seen = new Set();
  for (const item of traffic) {
    const info = (item.ip && byIp.get(item.ip)) || (item.mac && byMac.get(item.mac)) || {};
    const row = {
      ...item,
      ...info,
      ip: item.ip || info.ip,
      mac: item.mac || info.mac,
      hostname: item.hostname || info.hostname,
      rx_rate_mbps: item.rx_rate != null ? item.rx_rate : info.rx_rate_mbps,
      tx_rate_mbps: item.tx_rate != null ? item.tx_rate : info.tx_rate_mbps,
      rx_bytes: item.rx_bytes != null ? item.rx_bytes : info.rx_bytes,
      tx_bytes: item.tx_bytes != null ? item.tx_bytes : info.tx_bytes,
      online: info.online !== undefined ? info.online : true,
    };
    if (row.ip) seen.add(row.ip);
    if (row.mac) seen.add(row.mac);
    rows.push(row);
  }
  // 没有出现流量、但仍在设备表里的（离线、仅租约）也要显示
  for (const item of meta) {
    if ((item.ip && seen.has(item.ip)) || (item.mac && seen.has(item.mac))) continue;
    rows.push({ ...item });
  }
  return rows;
}

// 排序只看"稳定的身份与状态"：分组（本机 USB 口 > 普通设备 > 本机 TUN/VPN）、
// 在线状态、IPv4 数值。速率、累计流量、名称、厂商一律不参与，避免每 2 秒换位。
function ipv4SortKey(ip) {
  const text = String(ip || "").trim();
  const parts = text.split(".");
  if (
    parts.length === 4 &&
    parts.every((part) => /^\d{1,3}$/.test(part) && Number(part) <= 255)
  ) {
    return parts.reduce((acc, part) => acc * 256 + Number(part), 0);
  }
  return Number.MAX_SAFE_INTEGER; // IPv6 / 无 IP 排在本组末尾
}

function isHostUsbRow(item) {
  return Boolean(item.is_local) && /USB 网口/.test(String(item.name || ""));
}

function deviceSortGroup(item) {
  if (isHostUsbRow(item)) return 0; // 本机（USB 网口）固定第一
  if (item.is_local) return 2; // 本机 TUN / VPN 网口行排在所有设备之后
  return 1;
}

function compareDevices(a, b) {
  const groupDiff = deviceSortGroup(a) - deviceSortGroup(b);
  if (groupDiff !== 0) return groupDiff;
  const offlineA = a.online === false ? 1 : 0;
  const offlineB = b.online === false ? 1 : 0;
  if (offlineA !== offlineB) return offlineA - offlineB;
  const keyA = ipv4SortKey(a.ip);
  const keyB = ipv4SortKey(b.ip);
  if (keyA !== keyB) return keyA - keyB;
  // 没有 IP 时用 MAC 兜底，保证顺序仍然确定
  return String(a.mac || "").localeCompare(String(b.mac || ""));
}

function renderDevices() {
  const tbody = $("deviceRateRows");
  const empty = $("deviceEmpty");
  if (!tbody) return;
  const items = mergedDevices().filter(deviceMatches).sort(compareDevices);
  if (!items.length) {
    tbody.replaceChildren();
    if (empty) empty.hidden = false;
    return;
  }
  if (empty) empty.hidden = true;
  tbody.replaceChildren(...items.map(deviceRow));
}

function deviceRow(item) {
  const row = el("tr");
  const online = item.online !== false;
  if (!online) row.style.opacity = "0.68";

  const statusCell = el("td");
  const statusLine = el("div", "cell-main");
  const dotClass = online ? "on" : item.lease_only ? "warn" : "off";
  statusLine.appendChild(el("span", `state-dot ${dotClass}`));
  statusLine.appendChild(
    el("span", "", online ? "在线" : item.lease_only ? "仅租约" : "离线")
  );
  statusCell.appendChild(statusLine);
  if (!online && item.last_seen) {
    statusCell.appendChild(el("div", "cell-sub", fmtAgo(item.last_seen)));
  }
  row.appendChild(statusCell);

  const nameCell = el("td");
  const nameLine = el("div", "cell-main");
  nameLine.appendChild(el("span", "cell-name", item.name || item.hostname || item.ip || "设备"));
  if (item.name_source === "self") {
    nameLine.appendChild(el("span", "type-badge", "自报名称"));
  } else if (item.is_local) {
    nameLine.appendChild(el("span", "type-badge proxy", "本机"));
  }
  if ((item.vendor || "").toLowerCase().includes("nintendo")) {
    nameLine.appendChild(el("span", "type-badge nintendo", "Nintendo"));
  } else if (item.vendor) {
    nameLine.appendChild(el("span", "type-badge", item.vendor));
  }
  if (item.name_note) nameLine.title = item.name_note;
  nameCell.appendChild(nameLine);
  const sub = [];
  if (item.type) sub.push(typeLabel(item.type));
  if (item.signal_dbm !== null && item.signal_dbm !== undefined) sub.push(`${item.signal_dbm} dBm`);
  if (item.connected_seconds) sub.push(`在线 ${fmtDuration(item.connected_seconds)}`);
  if (!online && item.lease_only) sub.push("DHCP 租约");
  if (sub.length) nameCell.appendChild(el("div", "cell-sub", sub.join(" · ")));
  row.appendChild(nameCell);

  const policyCell = el("td", "policy-cell");
  const isHostRow = item.is_local && /USB 网口/.test(item.name || "");
  if (isHostRow) {
    policyCell.appendChild(renderHostSwitch());
  } else if (item.mac) {
    policyCell.appendChild(renderPolicySwitch(item.mac));
  } else {
    policyCell.textContent = "—";
  }
  row.appendChild(policyCell);

  const idCell = el("td");
  idCell.appendChild(el("div", "mono", item.ip || "—"));
  if (item.mac) {
    const macLine = item.mac + (item.mac_random ? " · 随机 MAC" : "");
    idCell.appendChild(el("div", "cell-sub mono", macLine));
  }
  if (item.interface) {
    idCell.appendChild(el("div", "cell-sub", item.interface));
  }
  row.appendChild(idCell);

  row.appendChild(
    el("td", "num", item.rx_rate_mbps != null ? `↓ ${fmtMbps(item.rx_rate_mbps)}` : "—")
  );
  row.appendChild(
    el("td", "num", item.tx_rate_mbps != null ? `↑ ${fmtMbps(item.tx_rate_mbps)}` : "—")
  );
  row.appendChild(
    el(
      "td",
      "num",
      item.rx_bytes != null
        ? `${fmtBytes(item.rx_bytes)} / ${fmtBytes(item.tx_bytes)}`
        : "—"
    )
  );
  return row;
}

function policyFor(mac) {
  const data = state.devicePolicies || {};
  const found = (data.items || []).find((row) => row.mac === mac);
  if (found) return found;
  return {
    policy: data.default || "direct",
    effective: "direct",
    active: Boolean(data.active),
  };
}

function renderPolicyCell(item) {
  const cell = el("td", "policy-cell");
  if (!item.mac) {
    cell.textContent = "—";
    return cell;
  }
  cell.appendChild(renderPolicySwitch(item.mac));
  return cell;
}

function totalProxyOn() {
  const data = state.devicePolicies || {};
  if (data.total !== undefined) return Boolean(data.total);
  const proxy = (state.status || {}).proxy || {};
  return Boolean(proxy.tun || proxy.tproxy);
}

function renderPolicySwitch(mac) {
  const info = policyFor(mac);
  const total = totalProxyOn();
  const on = total && info.policy === "proxy";
  const button = el("button", `policy-switch ${on ? "on" : "off"}`);
  button.type = "button";
  button.setAttribute("role", "switch");
  button.setAttribute("aria-checked", on ? "true" : "false");
  button.dataset.mac = mac;
  button.title = !total
    ? "总代理已关闭，设备开关不生效"
    : on ? "当前：走代理（点击改为直连）" : "当前：直连（点击改为走代理）";
  button.disabled = state.policyBusy || !total;
  button.appendChild(el("span", "knob"));
  button.appendChild(el("span", "label", !total ? "总代理关" : on ? "走代理" : "直连"));
  const next = on ? "direct" : "proxy";
  button.addEventListener("click", () => setDevicePolicy(mac, next));
  return button;
}

function renderHostSwitch() {
  const proxy = (state.status || {}).proxy || {};
  const total = totalProxyOn();
  const on = total && Boolean(proxy.tun);
  const button = el("button", `policy-switch ${on ? "on" : "off"}`);
  button.type = "button";
  button.setAttribute("role", "switch");
  button.setAttribute("aria-checked", on ? "true" : "false");
  button.dataset.host = "1";
  button.title = !total
    ? "总代理已关闭，主机开关不生效"
    : on ? "主机走代理（TUN 开），点击关闭" : "主机直连（TUN 关），点击开启";
  button.disabled = state.policyBusy || !total;
  button.appendChild(el("span", "knob"));
  button.appendChild(el("span", "label", !total ? "总代理关" : on ? "走代理" : "直连"));
  button.addEventListener("click", () => setHostProxy(!on));
  return button;
}

async function setHostProxy(enable) {
  if (state.policyBusy) return;
  state.policyBusy = true;
  renderDevices();
  try {
    const result = await fetchJSON("/api/proxy/host", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enable }),
    });
    if (!result.ok) throw new Error(result.error || "主机代理切换失败");
  } catch (error) {
    console.error(error);
    window.alert(error.message || "主机代理切换失败");
  } finally {
    state.policyBusy = false;
    try {
      await refresh();
    } catch (error) {
      console.error(error);
    }
  }
}

function renderPolicyDefault() {
  const box = $("policyDefault");
  if (!box) return;
  const current = (state.devicePolicies || {}).default || "direct";
  box.querySelectorAll("button").forEach((button) => {
    button.classList.toggle("active", button.dataset.policy === current);
    button.disabled = state.policyBusy || !totalProxyOn();
  });
}

async function setDevicePolicy(mac, policy) {
  if (state.policyBusy) return;
  state.policyBusy = true;
  renderDevices();
  try {
    const response = await fetch("/api/devices/policy", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mac, policy }),
    });
    const data = await response.json();
    // 装表失败时后端会把策略回滚成直连，并返回失败的那一步（install.steps）
    if (!response.ok || !data.ok) {
      throw new Error(data.error || data.install?.detail || "设置失败");
    }
    state.devicePolicies = data;
  } catch (error) {
    alert(`设备代理策略设置失败: ${error.message}`);
  } finally {
    state.policyBusy = false;
    await refresh();
  }
}

async function setDefaultPolicy(policy) {
  if (state.policyBusy) return;
  state.policyBusy = true;
  renderPolicyDefault();
  try {
    const response = await fetch("/api/devices/policy/default", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ policy }),
    });
    const data = await response.json();
    if (!data.ok) throw new Error(data.error || "设置失败");
    state.devicePolicies = data;
  } catch (error) {
    alert(`默认代理策略设置失败: ${error.message}`);
  } finally {
    state.policyBusy = false;
    await refresh();
  }
}

function deviceMatches(item) {
  if (state.deviceFilter === "online" && item.online === false) return false;
  if (state.deviceFilter === "offline" && item.online !== false) return false;
  if (state.deviceFilter === "proxied" && policyFor(item.mac).policy !== "proxy") return false;
  if (!state.search) return true;
  const haystack = [
    item.name,
    item.hostname,
    item.ip,
    item.mac,
    item.vendor,
    item.interface,
    item.type,
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

// ---------- 宽带测速 ----------
const SPEEDTEST_POLL_ACTIVE = 500;
const SPEEDTEST_POLL_IDLE = 5000;

function fmtNum(value, digits = 1) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return Number(value).toFixed(digits);
}

function speedtestBadge(result) {
  const badge = $("speedtestEgress");
  if (!badge) return;
  const direct = !result || result.direct !== false;
  badge.textContent = direct ? "直连" : "经代理";
  badge.className = `speed-badge ${direct ? "direct" : "proxy"}`;
}

function clearSpeedtestError(force = false) {
  const node = $("speedtestError");
  if (!node) return;
  if (!force && state.speedtestNoticeUntil && Date.now() < state.speedtestNoticeUntil) return;
  state.speedtestNoticeUntil = 0;
  node.hidden = true;
  node.textContent = "";
}

function showSpeedtestError(message, holdMs = 6000) {
  const node = $("speedtestError");
  if (!node) return;
  state.speedtestNoticeUntil = Date.now() + holdMs;
  node.hidden = false;
  node.textContent = message;
}

function updateSpeedtestButton() {
  const button = $("speedtestStart");
  if (!button) return;
  const running = !!(state.speedtest && state.speedtest.running);
  button.disabled = false;
  button.classList.toggle("pending", running);
  button.textContent = running ? "测速中…" : state.speedtest && state.speedtest.result ? "重新测速" : "开始测速";
  button.title = running ? "已有测速任务在运行" : "开始下行 / 上行 / 延迟测试";
}

function renderSpeedtest(data) {
  const running = !!data.running;
  const result = data.result || null;
  updateSpeedtestButton();
  speedtestBadge(result);

  const down =
    result && result.down_mbps !== null && result.down_mbps !== undefined
      ? result.down_mbps
      : data.live && data.live.down_mbps;
  const up =
    result && result.up_mbps !== null && result.up_mbps !== undefined
      ? result.up_mbps
      : data.live && data.live.up_mbps;
  setText("speedDown", down === null || down === undefined ? "—" : fmtNum(down));
  setText("speedUp", up === null || up === undefined ? "—" : fmtNum(up));
  setText("speedLatency", result ? fmtNum(result.latency_ms) : "—");
  setText("speedJitter", result ? fmtNum(result.jitter_ms) : "—");

  const phaseLabel = data.phase_label || (running ? "测速中" : "待测速");
  setText("speedtestPhase", running ? `${phaseLabel}…` : phaseLabel);
  const progress = $("speedtestProgress");
  if (progress) progress.style.width = `${Math.max(0, Math.min(100, Number(data.progress) || 0))}%`;

  if (data.error) {
    showSpeedtestError(data.error);
  } else {
    clearSpeedtestError();
  }

  let meta = "多线程直连国内镜像，绑定上行网卡，绕开 Clash";
  if (running && result === null) {
    meta = data.phase === "latency" ? "正在直连探测延迟与抖动" : "测速进行中，接口仍可正常响应";
  } else if (result) {
    const parts = [];
    if (result.iface) parts.push(result.iface);
    if (result.direct) parts.push("直连");
    if (result.proxy_active) parts.push("Clash 已开启，本次已绕过");
    if (result.duration_s) parts.push(`耗时 ${result.duration_s}s`);
    if (result.notes) parts.push(result.notes);
    meta = parts.join(" · ");
  }
  setText("speedtestMeta", meta);

  const endpoint = $("speedtestEndpoint");
  if (endpoint) {
    const hosts = result && result.download_endpoints ? result.download_endpoints : [];
    endpoint.textContent = hosts.length ? `下载端点：${hosts.join(" / ")}` : "";
  }

  const foot = $("speedtestChartFoot");
  if (foot) {
    foot.textContent = running
      ? "实时采样：每 0.25s 一个点 · 绿色为下载，紫色为上传"
      : result
        ? "本轮下行 / 上行采样曲线"
        : "测速开始后显示实时采样";
  }
  const live = $("speedtestLive");
  if (live) {
    if (running && data.live) {
      const current = data.live.down_mbps ?? data.live.up_mbps;
      live.textContent = current === null || current === undefined ? "" : `当前 ${fmtNum(current)} Mbps`;
    } else {
      live.textContent = "";
    }
  }

  if (state.speedtestChart) {
    state.speedtestChart.setSamples(data.samples || [], running, data.phase);
  }
  renderSpeedtestHistory(data.recent || []);
}

function renderSpeedtestHistory(items) {
  const list = $("speedtestHistory");
  const meta = $("speedtestHistoryMeta");
  if (!list) return;
  list.replaceChildren();
  const valid = items.filter((item) => item && item.ok !== false && item.down_mbps !== null);
  if (meta) meta.textContent = items.length ? `${items.length} 条` : "--";
  if (state.speedtestTrend) state.speedtestTrend.setHistory(valid);
  if (!items.length) {
    list.append(el("div", "empty-small", "还没有测速记录"));
    return;
  }
  items.slice(0, 8).forEach((item) => {
    const row = el("div", `speed-history-item${item.ok === false ? " failed" : ""}`);
    const head = el("div", "speed-history-headline");
    head.append(el("span", "speed-history-time", item.time ? item.time.slice(5) : "--"));
    const badge = el("span", `speed-badge ${item.direct === false ? "proxy" : "direct"}`, item.direct === false ? "经代理" : "直连");
    head.append(badge);
    row.append(head);
    if (item.ok === false) {
      row.append(el("div", "speed-history-values", item.error || "测速失败"));
    } else {
      row.append(
        el(
          "div",
          "speed-history-values",
          `↓ ${fmtNum(item.down_mbps)} · ↑ ${fmtNum(item.up_mbps)} Mbps`
        )
      );
      const detail = [];
      if (item.latency_ms !== null && item.latency_ms !== undefined) detail.push(`${fmtNum(item.latency_ms)} ms`);
      if (item.jitter_ms !== null && item.jitter_ms !== undefined) detail.push(`抖动 ${fmtNum(item.jitter_ms)}`);
      if (item.iface) detail.push(item.iface);
      if (item.duration_s) detail.push(`${item.duration_s}s`);
      row.append(el("div", "speed-history-detail", detail.join(" · ")));
    }
    list.append(row);
  });
}

async function startSpeedtest() {
  if (state.speedtestBusy) {
    showSpeedtestError("正在启动测速，请稍候…");
    return;
  }
  state.speedtestBusy = true;
  updateSpeedtestButton();
  clearSpeedtestError(true);
  try {
    // 不依赖本地 running 状态：由后端统一判定是否已有任务，409 时把原因显示出来
    const response = await fetch("/api/speedtest/start", { method: "POST", cache: "no-store" });
    const data = await response.json();
    if (!response.ok || !data.ok) {
      showSpeedtestError(data.error || `启动失败（HTTP ${response.status}）`, 8000);
    } else {
      await refreshSpeedtest();
    }
  } catch (error) {
    showSpeedtestError(`启动测速失败：${error.message}`);
  } finally {
    state.speedtestBusy = false;
    updateSpeedtestButton();
  }
}

function scheduleSpeedtestPoll(delay) {
  if (state.speedtestTimer) clearTimeout(state.speedtestTimer);
  state.speedtestTimer = setTimeout(refreshSpeedtest, delay);
}

async function refreshSpeedtest() {
  try {
    const data = await fetchJSON("/api/speedtest/status");
    state.speedtest = data;
    renderSpeedtest(data);
    scheduleSpeedtestPoll(data.running ? SPEEDTEST_POLL_ACTIVE : SPEEDTEST_POLL_IDLE);
  } catch (error) {
    scheduleSpeedtestPoll(SPEEDTEST_POLL_IDLE);
  }
}

class SpeedChart {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.samples = [];
    this.running = false;
    this.phase = "idle";
    this.width = 0;
    this.height = 0;
    this.resize = this.resize.bind(this);
    window.addEventListener("resize", this.resize);
    if (window.ResizeObserver) new ResizeObserver(this.resize).observe(canvas.parentElement);
    this.resize();
  }

  setSamples(samples, running, phase) {
    this.samples = samples || [];
    this.running = !!running;
    this.phase = phase || "idle";
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

    if (!this.samples.length) {
      ctx.fillStyle = faint;
      ctx.font = '12px "PingFang SC", system-ui, sans-serif';
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      const text =
        this.phase === "latency" ? "正在测量延迟…" : this.running ? "等待采样…" : "等待测速";
      ctx.fillText(text, this.width / 2, this.height / 2);
      return;
    }

    const padding = { left: 62, right: 14, top: 24, bottom: 24 };
    const plotWidth = Math.max(10, this.width - padding.left - padding.right);
    const plotHeight = Math.max(10, this.height - padding.top - padding.bottom);
    const maxT = Math.max(1, ...this.samples.map((item) => Number(item.t) || 0));
    const maxMbps = Math.max(1, ...this.samples.map((item) => Number(item.mbps) || 0));
    const niceMax = niceNumber(maxMbps);
    const xFor = (t) => padding.left + (Math.min(maxT, Math.max(0, t)) / maxT) * plotWidth;
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
      ctx.fillText(fmtNum(value, 0), padding.left - 8, y);
    }
    ctx.fillStyle = faint;
    ctx.font = '10px "JetBrains Mono", ui-monospace, monospace';
    ctx.textAlign = "left";
    ctx.textBaseline = "top";
    ctx.fillText("Mbps", padding.left, 4);

    let segment = [];
    let phase = "";
    const flush = () => {
      if (!segment.length) return;
      const color = phase === "upload" ? txColor : rxColor;
      ctx.beginPath();
      segment.forEach((item, index) => {
        const x = xFor(item.t);
        const y = yFor(item.mbps);
        if (index === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      ctx.stroke();
      if (phase !== "upload" && segment.length > 1) {
        ctx.save();
        ctx.globalAlpha = 0.09;
        ctx.beginPath();
        segment.forEach((item, index) => {
          const x = xFor(item.t);
          const y = yFor(item.mbps);
          if (index === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        });
        ctx.lineTo(xFor(segment[segment.length - 1].t), padding.top + plotHeight);
        ctx.lineTo(xFor(segment[0].t), padding.top + plotHeight);
        ctx.closePath();
        ctx.fillStyle = color;
        ctx.fill();
        ctx.restore();
      }
      const last = segment[segment.length - 1];
      ctx.beginPath();
      ctx.arc(xFor(last.t), yFor(last.mbps), 2.8, 0, Math.PI * 2);
      ctx.fillStyle = color;
      ctx.fill();
      segment = [];
    };

    this.samples.forEach((item) => {
      if (item.phase !== phase) {
        flush();
        phase = item.phase;
      }
      segment.push(item);
    });
    flush();

    ctx.fillStyle = muted;
    ctx.font = '11px "JetBrains Mono", ui-monospace, monospace';
    ctx.textBaseline = "top";
    ctx.textAlign = "left";
    ctx.fillText("0s", padding.left, this.height - 18);
    ctx.textAlign = "right";
    ctx.fillText(`${fmtNum(maxT)}s`, this.width - padding.right, this.height - 18);
  }
}

class SpeedTrend {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.items = [];
    this.width = 0;
    this.height = 0;
    this.resize = this.resize.bind(this);
    window.addEventListener("resize", this.resize);
    if (window.ResizeObserver) new ResizeObserver(this.resize).observe(canvas.parentElement);
    this.resize();
  }

  setHistory(items) {
    this.items = (items || []).slice(0, 12).reverse();
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
    if (!this.items.length) {
      ctx.fillStyle = faint;
      ctx.font = '11px "PingFang SC", system-ui, sans-serif';
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText("暂无趋势", this.width / 2, this.height / 2);
      return;
    }
    const padding = { left: 8, right: 8, top: 10, bottom: 18 };
    const plotWidth = Math.max(10, this.width - padding.left - padding.right);
    const plotHeight = Math.max(10, this.height - padding.top - padding.bottom);
    const maxValue = Math.max(
      1,
      ...this.items.map((item) => Math.max(Number(item.down_mbps) || 0, Number(item.up_mbps) || 0))
    );
    const niceMax = niceNumber(maxValue);
    const xFor = (index) =>
      this.items.length === 1
        ? padding.left + plotWidth / 2
        : padding.left + (index / (this.items.length - 1)) * plotWidth;
    const yFor = (value) => padding.top + plotHeight * (1 - value / niceMax);
    ctx.strokeStyle = gridColor;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(padding.left, yFor(0));
    ctx.lineTo(this.width - padding.right, yFor(0));
    ctx.stroke();
    const draw = (key, color) => {
      ctx.beginPath();
      this.items.forEach((item, index) => {
        const x = xFor(index);
        const y = yFor(Number(item[key]) || 0);
        if (index === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.8;
      ctx.lineJoin = "round";
      ctx.stroke();
      this.items.forEach((item, index) => {
        ctx.beginPath();
        ctx.arc(xFor(index), yFor(Number(item[key]) || 0), 2.2, 0, Math.PI * 2);
        ctx.fillStyle = color;
        ctx.fill();
      });
    };
    draw("down_mbps", rxColor);
    draw("up_mbps", txColor);
  }
}

// ---------- 网口实时吞吐 ----------
const THROUGHPUT_POLL_MS = 1000;
const USAGE_POLL_MS = 10000;
const USAGE_TREND_POLL_MS = 60000;
const SYSTEM_HEALTH_POLL_MS = 30000;

function throughputPair(rx, tx) {
  return `↓ ${fmtNum(rx)} · ↑ ${fmtNum(tx)}`;
}

function renderThroughput(data) {
  state.throughput = data;
  if (!state.throughputIface && data.iface) state.throughputIface = data.iface;

  const select = $("throughputIface");
  if (select) {
    const options = data.interfaces || [];
    const signature = options.map((item) => `${item.name}:${item.up ? 1 : 0}`).join(",");
    if (signature !== state.throughputSignatures) {
      state.throughputSignatures = signature;
      select.replaceChildren();
      options.forEach((item) => {
        const option = el(
          "option",
          null,
          `${item.name}${item.up ? "" : "（未启用）"}${item.default ? " · 默认" : ""}`
        );
        option.value = item.name;
        select.append(option);
      });
    }
    if (data.iface) select.value = data.iface;
  }

  const keep = $("throughputKeep");
  if (keep && document.activeElement !== keep) keep.checked = !!data.keep;

  setText("throughputDown", fmtNum(data.current && data.current.rx));
  setText("throughputUp", fmtNum(data.current && data.current.tx));
  setText(
    "throughputAvg",
    data.avg ? throughputPair(data.avg.rx, data.avg.tx) : "—"
  );
  setText(
    "throughputPeak",
    data.peak ? throughputPair(data.peak.rx, data.peak.tx) : "—"
  );
  setText("throughputNote", data.note || "网口层吞吐（NIC）");
  setText(
    "throughputState",
    data.active
      ? `采样中 · ${data.iface} · 1s / 点${data.keep ? " · 后台记录中" : ""}`
      : "已暂停（保持页面打开会自动恢复）"
  );

  if (state.throughputChart) {
    state.throughputChart.setData(data.samples || [], data.window_s || 60, data.active);
  }
}

async function refreshThroughput() {
  if (document.hidden) return;
  try {
    const query = state.throughputIface
      ? `?iface=${encodeURIComponent(state.throughputIface)}`
      : "";
    const data = await fetchJSON(`/api/throughput${query}`);
    if (!state.throughputIface) state.throughputIface = data.iface || "";
    renderThroughput(data);
  } catch (error) {
    setText("throughputState", "监控读取失败");
  }
  if (state.throughputTimer) clearTimeout(state.throughputTimer);
  state.throughputTimer = setTimeout(refreshThroughput, THROUGHPUT_POLL_MS);
}

async function toggleThroughputKeep(enabled) {
  try {
    await fetch("/api/throughput/keep", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
  } catch (error) {
    setText("throughputState", `后台记录设置失败：${error.message}`);
  }
  refreshThroughput();
}

class ThroughputChart {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.samples = [];
    this.window = 60;
    this.active = false;
    this.width = 0;
    this.height = 0;
    this.resize = this.resize.bind(this);
    window.addEventListener("resize", this.resize);
    if (window.ResizeObserver) new ResizeObserver(this.resize).observe(canvas.parentElement);
    this.resize();
  }

  setData(samples, windowSeconds, active) {
    this.samples = samples || [];
    this.window = windowSeconds || 60;
    this.active = !!active;
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

    if (!this.samples.length) {
      ctx.fillStyle = faint;
      ctx.font = '12px "PingFang SC", system-ui, sans-serif';
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(this.active ? "等待采样…" : "打开页面后自动开始采样", this.width / 2, this.height / 2);
      return;
    }

    const padding = { left: 62, right: 14, top: 24, bottom: 24 };
    const plotWidth = Math.max(10, this.width - padding.left - padding.right);
    const plotHeight = Math.max(10, this.height - padding.top - padding.bottom);
    const end = this.samples[this.samples.length - 1].t;
    const start = end - this.window;
    const maxValue = Math.max(
      1,
      ...this.samples.map((item) => Math.max(Number(item.rx) || 0, Number(item.tx) || 0))
    );
    const niceMax = niceNumber(maxValue);
    const xFor = (t) =>
      padding.left + (Math.min(1, Math.max(0, (t - start) / this.window))) * plotWidth;
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
      ctx.fillText(fmtNum(value, 0), padding.left - 8, y);
    }
    ctx.fillStyle = faint;
    ctx.font = '10px "JetBrains Mono", ui-monospace, monospace';
    ctx.textAlign = "left";
    ctx.textBaseline = "top";
    ctx.fillText("Mbps", padding.left, 4);

    const series = (key, color) => {
      ctx.beginPath();
      this.samples.forEach((item, index) => {
        const x = xFor(item.t);
        const y = yFor(Number(item[key]) || 0);
        if (index === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      ctx.stroke();
      const last = this.samples[this.samples.length - 1];
      ctx.beginPath();
      ctx.arc(xFor(last.t), yFor(Number(last[key]) || 0), 2.8, 0, Math.PI * 2);
      ctx.fillStyle = color;
      ctx.fill();
    };
    series("rx", rxColor);
    series("tx", txColor);

    ctx.fillStyle = muted;
    ctx.font = '11px "JetBrains Mono", ui-monospace, monospace';
    ctx.textBaseline = "top";
    ctx.textAlign = "left";
    ctx.fillText("60s 前", padding.left, this.height - 18);
    ctx.textAlign = "right";
    ctx.fillText("现在", this.width - padding.right, this.height - 18);
  }
}

// ---------- 使用时长与系统健康 ----------

function fmtUsageDuration(seconds) {
  const n = Math.max(0, Number(seconds) || 0);
  if (n < 60) return `${Math.round(n)}s`;
  if (n < 3600) return `${Math.floor(n / 60)}m`;
  return `${Math.floor(n / 3600)}h ${Math.floor((n % 3600) / 60)}m`;
}

function usageEmpty(message) {
  return el("div", "empty-small", message);
}

function renderUsage(data) {
  state.usage = data;
  const enabled = data.enabled !== false;
  const x11 = data.x11 || {};
  const available = x11.available !== false;

  setText("usageMeta", data.note || "按前台窗口统计");
  let stateText = "采样中";
  if (!enabled) stateText = "采集未启用";
  else if (!available) stateText = "X11 不可用";
  else if (data.idle) stateText = data.idle_reason ? `空闲 · ${data.idle_reason}` : "空闲";
  else stateText = `采样中 · 每 ${fmtNum(data.sample_interval, 0)}s`;
  setText("usageState", stateText);

  setText("usageTotal", fmtUsageDuration(data.total_seconds || 0));

  const current = data.current;
  if (current) {
    const running = current.since ? Date.now() / 1000 - current.since : 0;
    setText("usageCurrent", `当前：${current.app}${running > 0 ? ` · ${fmtUsageDuration(running)}` : ""}`);
  } else {
    setText("usageCurrent", data.idle ? "当前空闲" : "—");
  }

  const list = $("usageList");
  if (!list) return;
  const items = data.items || [];
  if (!items.length) {
    const reason = !enabled
      ? "使用时长采集未启用"
      : !available
        ? (x11.error || "X11 不可用，暂时无法记录前台应用")
        : "今天还没有前台使用记录";
    list.replaceChildren(usageEmpty(reason));
    return;
  }
  const rows = items.slice(0, 10).map((item) => {
    const row = el("div", "usage-row");
    const head = el("div", "usage-row-head");
    head.appendChild(el("span", "usage-app", item.app));
    head.appendChild(
      el("span", "usage-time", `${fmtUsageDuration(item.seconds)} · ${fmtNum(item.percent, 1)}%`)
    );
    row.appendChild(head);
    const bar = el("div", "usage-bar");
    const fill = el("i");
    fill.style.width = `${Math.max(1.5, Math.min(100, Number(item.percent) || 0))}%`;
    bar.appendChild(fill);
    row.appendChild(bar);
    return row;
  });
  list.replaceChildren(...rows);
}

function renderUsageTrend(data) {
  state.usageTrend = data;
  setText(
    "usageTrendMeta",
    data.total_seconds
      ? `${data.days} 天 · ${fmtUsageDuration(data.total_seconds)}`
      : `${data.days} 天`
  );
  if (state.usageTrendChart) state.usageTrendChart.setData(data.items || []);

  const host = $("usageTopApps");
  if (!host) return;
  const apps = data.apps || [];
  if (!apps.length) {
    host.replaceChildren(usageEmpty("还没有使用记录"));
    return;
  }
  host.replaceChildren(
    ...apps.slice(0, 5).map((item) => {
      const row = el("div", "usage-app-row");
      row.appendChild(el("span", "usage-app", item.app));
      row.appendChild(
        el("span", "usage-time", `${fmtUsageDuration(item.seconds)} · ${fmtNum(item.percent, 1)}%`)
      );
      return row;
    })
  );
}

function renderSystemHealth(data) {
  state.systemHealth = data;
  const latest = data.latest || {};
  const has = (value) => value !== null && value !== undefined && Number.isFinite(Number(value));
  setText("healthCpu", has(latest.cpu_pct) ? `${fmtNum(latest.cpu_pct, 0)}%` : "—");
  setText("healthMem", has(latest.mem_pct) ? `${fmtNum(latest.mem_pct, 0)}%` : "—");
  setText("healthSwap", has(latest.swap_pct) ? `${fmtNum(latest.swap_pct, 0)}%` : "—");
  setText("healthDisk", latest.disk_total ? `${fmtBytes(latest.disk_free)} 可用` : "—");
  setText("healthTemp", has(latest.temp_c) ? `${fmtNum(latest.temp_c, 1)} °C` : "无传感器");
  setText(
    "systemHealthMeta",
    `每 ${fmtNum(data.interval, 0)}s · 最近 ${fmtNum(data.hours, 0)}h`
  );
  if (state.systemHealthChart) {
    state.systemHealthChart.setData(data.points || [], data.hours || 6);
  }
}

async function refreshUsage() {
  if (!document.hidden) {
    try {
      renderUsage(await fetchJSON("/api/usage"));
    } catch (error) {
      setText("usageState", "使用时长读取失败");
    }
  }
  if (state.usageTimer) clearTimeout(state.usageTimer);
  state.usageTimer = setTimeout(refreshUsage, document.hidden ? HIDDEN_POLL_MS : USAGE_POLL_MS);
}

async function refreshUsageTrend() {
  if (!document.hidden) {
    try {
      renderUsageTrend(await fetchJSON("/api/usage/trend?days=7"));
    } catch (error) {
      setText("usageTrendMeta", "趋势读取失败");
    }
  }
  if (state.usageTrendTimer) clearTimeout(state.usageTrendTimer);
  state.usageTrendTimer = setTimeout(
    refreshUsageTrend,
    document.hidden ? HIDDEN_POLL_MS : USAGE_TREND_POLL_MS
  );
}

async function refreshSystemHealth() {
  if (!document.hidden) {
    try {
      renderSystemHealth(await fetchJSON("/api/system/health?hours=6"));
    } catch (error) {
      setText("systemHealthMeta", "健康曲线读取失败");
    }
  }
  if (state.systemHealthTimer) clearTimeout(state.systemHealthTimer);
  state.systemHealthTimer = setTimeout(
    refreshSystemHealth,
    document.hidden ? HIDDEN_POLL_MS : SYSTEM_HEALTH_POLL_MS
  );
}

class UsageTrendChart {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.items = [];
    this.width = 0;
    this.height = 0;
    this.resize = this.resize.bind(this);
    window.addEventListener("resize", this.resize);
    if (window.ResizeObserver) new ResizeObserver(this.resize).observe(canvas.parentElement);
    this.resize();
  }

  setData(items) {
    this.items = (items || []).slice(-14);
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
    const accent = styles.getPropertyValue("--accent").trim() || "#5b8cff";
    const rxColor = styles.getPropertyValue("--rx").trim() || "#41d1c4";
    const gridColor = styles.getPropertyValue("--border-soft").trim() || "#1a2332";
    const faint = styles.getPropertyValue("--faint").trim() || "#5d687b";
    const muted = styles.getPropertyValue("--muted").trim() || "#8e99ad";

    if (!this.items.length) {
      ctx.fillStyle = faint;
      ctx.font = '12px "PingFang SC", system-ui, sans-serif';
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText("等待使用数据", this.width / 2, this.height / 2);
      return;
    }

    const padding = { left: 46, right: 10, top: 14, bottom: 22 };
    const plotWidth = Math.max(10, this.width - padding.left - padding.right);
    const plotHeight = Math.max(10, this.height - padding.top - padding.bottom);
    const maxValue = Math.max(1, ...this.items.map((item) => Number(item.seconds) || 0));
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
      ctx.fillText(fmtUsageDuration(value), padding.left - 8, y);
    }

    const slot = plotWidth / this.items.length;
    const barWidth = Math.max(4, slot * 0.56);
    this.items.forEach((item, index) => {
      const value = Math.max(0, Number(item.seconds) || 0);
      const x = padding.left + slot * (index + 0.5);
      const height = Math.max(value > 0 ? 2 : 1, (value / niceMax) * plotHeight);
      ctx.fillStyle = index === this.items.length - 1 ? rxColor : accent;
      ctx.globalAlpha = value > 0 ? 0.9 : 0.25;
      ctx.fillRect(x - barWidth / 2, padding.top + plotHeight - height, barWidth, height);
      ctx.globalAlpha = 1;
      if (this.items.length <= 8 || index % 2 === 0) {
        ctx.fillStyle = muted;
        ctx.font = '10px "JetBrains Mono", ui-monospace, monospace';
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        ctx.fillText(item.label || "", x, this.height - 17);
      }
    });
  }
}

class SystemHealthChart {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.points = [];
    this.hours = 6;
    this.width = 0;
    this.height = 0;
    this.resize = this.resize.bind(this);
    window.addEventListener("resize", this.resize);
    if (window.ResizeObserver) new ResizeObserver(this.resize).observe(canvas.parentElement);
    this.resize();
  }

  setData(points, hours) {
    this.points = points || [];
    this.hours = hours || 6;
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
    const accent = styles.getPropertyValue("--accent").trim() || "#5b8cff";
    const rxColor = styles.getPropertyValue("--rx").trim() || "#41d1c4";
    const accent2 = styles.getPropertyValue("--accent-2").trim() || "#8a6cff";
    const yellow = styles.getPropertyValue("--yellow").trim() || "#f5b84b";
    const gridColor = styles.getPropertyValue("--border-soft").trim() || "#1a2332";
    const faint = styles.getPropertyValue("--faint").trim() || "#5d687b";
    const muted = styles.getPropertyValue("--muted").trim() || "#8e99ad";

    if (!this.points.length) {
      ctx.fillStyle = faint;
      ctx.font = '12px "PingFang SC", system-ui, sans-serif';
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText("等待健康采样", this.width / 2, this.height / 2);
      return;
    }

    const padding = { left: 44, right: 44, top: 14, bottom: 22 };
    const plotWidth = Math.max(10, this.width - padding.left - padding.right);
    const plotHeight = Math.max(10, this.height - padding.top - padding.bottom);
    const temps = this.points
      .map((item) => Number(item.temp_c))
      .filter((value) => Number.isFinite(value));
    const tempMax = Math.max(60, Math.ceil(Math.max(0, ...temps) / 10) * 10 || 100);
    const start = this.points[0].ts;
    const end = this.points[this.points.length - 1].ts;
    const span = Math.max(1, end - start);
    const xFor = (ts) => padding.left + Math.min(1, Math.max(0, (ts - start) / span)) * plotWidth;
    const yForPct = (value) => padding.top + plotHeight * (1 - Math.min(100, Math.max(0, value)) / 100);
    const yForTemp = (value) =>
      padding.top + plotHeight * (1 - Math.min(tempMax, Math.max(0, value)) / tempMax);

    ctx.lineWidth = 1;
    ctx.strokeStyle = gridColor;
    ctx.font = '10px "JetBrains Mono", ui-monospace, monospace';
    ctx.textBaseline = "middle";
    for (let step = 0; step <= 4; step += 1) {
      const y = padding.top + plotHeight * (1 - step / 4);
      ctx.beginPath();
      ctx.moveTo(padding.left, y);
      ctx.lineTo(this.width - padding.right, y);
      ctx.stroke();
      ctx.fillStyle = muted;
      ctx.textAlign = "right";
      ctx.fillText(`${step * 25}%`, padding.left - 8, y);
      ctx.fillStyle = yellow;
      ctx.textAlign = "left";
      ctx.fillText(`${Math.round((tempMax * step) / 4)}°`, this.width - padding.right + 6, y);
    }

    const series = (key, color, axis, dashed = false) => {
      ctx.beginPath();
      let started = false;
      this.points.forEach((item) => {
        const value = Number(item[key]);
        if (!Number.isFinite(value)) {
          started = false;
          return;
        }
        const x = xFor(item.ts);
        const y = axis(value);
        if (!started) {
          ctx.moveTo(x, y);
          started = true;
        } else {
          ctx.lineTo(x, y);
        }
      });
      ctx.setLineDash(dashed ? [4, 4] : []);
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.8;
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      ctx.stroke();
      ctx.setLineDash([]);
      const last = this.points[this.points.length - 1];
      const value = Number(last[key]);
      if (Number.isFinite(value)) {
        ctx.beginPath();
        ctx.arc(xFor(last.ts), axis(value), 2.4, 0, Math.PI * 2);
        ctx.fillStyle = color;
        ctx.fill();
      }
    };

    series("cpu_pct", accent, yForPct);
    series("mem_pct", rxColor, yForPct);
    series("swap_pct", accent2, yForPct, true);
    series("temp_c", yellow, yForTemp, true);

    ctx.fillStyle = muted;
    ctx.font = '11px "JetBrains Mono", ui-monospace, monospace';
    ctx.textBaseline = "top";
    ctx.textAlign = "left";
    ctx.fillText(`${fmtNum(this.hours, 0)}h 前`, padding.left, this.height - 17);
    ctx.textAlign = "right";
    ctx.fillText("现在", this.width - padding.right, this.height - 17);
  }
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
    refreshProxies(true);
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
      renderDevices();
    });
  }

  const search = $("deviceSearch");
  if (search) {
    search.addEventListener("input", () => {
      state.search = search.value.trim().toLowerCase();
      renderDevices();
    });
  }

  const defaultPolicy = $("policyDefault");
  if (defaultPolicy) {
    defaultPolicy.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-policy]");
      if (button) setDefaultPolicy(button.dataset.policy);
    });
  }
}

function init() {
  state.chart = new TrafficChart($("trafficChart"));
  state.speedtestChart = new SpeedChart($("speedChart"));
  state.speedtestTrend = new SpeedTrend($("speedTrend"));
  state.throughputChart = new ThroughputChart($("throughputChart"));
  state.usageTrendChart = new UsageTrendChart($("usageTrendChart"));
  state.systemHealthChart = new SystemHealthChart($("systemHealthChart"));
  const speedtestButton = $("speedtestStart");
  if (speedtestButton) speedtestButton.addEventListener("click", startSpeedtest);
  const throughputSelect = $("throughputIface");
  if (throughputSelect) {
    throughputSelect.addEventListener("change", () => {
      state.throughputIface = throughputSelect.value;
      if (state.throughputChart) state.throughputChart.setData([], 60, true);
      refreshThroughput();
    });
  }
  const throughputKeep = $("throughputKeep");
  if (throughputKeep) {
    throughputKeep.addEventListener("change", () => toggleThroughputKeep(throughputKeep.checked));
  }
  bindFilters();
  const proxyButton = $("proxyToggle");
  if (proxyButton) proxyButton.addEventListener("click", toggleProxy);
  const proxyRefresh = $("proxyRefresh");
  if (proxyRefresh) proxyRefresh.addEventListener("click", () => refreshProxies(true));
  const hotspotRefresh = $("hotspotRefresh");
  if (hotspotRefresh) hotspotRefresh.addEventListener("click", refreshHotspot);
  const hotspotReveal = $("hotspotReveal");
  if (hotspotReveal) {
    hotspotReveal.addEventListener("click", () => {
      state.hotspotReveal = !state.hotspotReveal;
      renderHotspot();
    });
  }
  const hotspotCopy = $("hotspotCopy");
  if (hotspotCopy) hotspotCopy.addEventListener("click", copyHotspotPassword);
  refresh();
  refreshRouter();
  scheduleRefresh();
  refreshProxies();
  refreshHotspot();
  refreshSpeedtest();
  refreshThroughput();
  refreshUsage();
  refreshUsageTrend();
  refreshSystemHealth();
  setInterval(refreshProxies, PROXY_POLL_MS);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      refresh();
      refreshRouter();
      refreshProxies();
      refreshHotspot();
      refreshSpeedtest();
      refreshThroughput();
      refreshUsage();
      refreshUsageTrend();
      refreshSystemHealth();
    }
    // 前后台切换后按新的节拍重新排期（前台 2s / 后台 15s）
    scheduleRefresh();
    scheduleRouter();
  });
}

document.addEventListener("DOMContentLoaded", init);
