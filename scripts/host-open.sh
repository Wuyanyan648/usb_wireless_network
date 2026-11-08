#!/usr/bin/env bash
# 登录时使用：如果当前是家庭网关模式，打开上位机界面
# 由 ~/.config/autostart/usbnic-host.desktop 调用，不需要 root
set -euo pipefail

URL="http://127.0.0.1:8787"

# 等待后台服务就绪（最多 60 秒）
ready=0
for _ in $(seq 1 60); do
  if curl -sf "$URL/api/health" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
[[ $ready -eq 1 ]] || exit 0

# 开机自启的网关配置可能稍晚于登录完成，最多再等 90 秒
mode=""
for _ in $(seq 1 90); do
  mode=$(curl -s "$URL/api/status" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("mode", ""))' 2>/dev/null || true)
  [[ $mode == "gateway" ]] && break
  sleep 1
done
[[ $mode == "gateway" ]] || exit 0

if command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$URL" >/dev/null 2>&1 || true
fi
