#!/usr/bin/env bash
# 起后端 + 前端 → 跑端到端验证 → 全部关掉。
# 必须写成一条命令执行：沙箱里后台进程活不过一条命令。
set -u

ROOT=/f/A_projects/vehicle-test-db
FE=$ROOT/frontend
PY=E:/python314/python.exe
NODE="D:/Program Files/nodejs/node.exe"
WS="C:/Users/LAPTOP/.workbuddy-ai/binaries/node/workspace/node_modules"
CHROME="C:/Users/LAPTOP/.agent-browser/browsers/chrome-153.0.8010.52/chrome.exe"

# 系统代理会让 Chromium 连 127.0.0.1 都走代理 → 导航永久挂起
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY

# 后端用 8010 而不是 8000：8000 常被本地正在跑的服务占着，
# 那样 e2e 起的后端会静默失败，请求全打到那个服务上，测出来的数字全是假的。
BE_PORT=8010
FE_PORT=5174

cleanup() {
  [ -n "${BE_PID:-}" ] && kill "$BE_PID" 2>/dev/null
  [ -n "${FE_PID:-}" ] && kill "$FE_PID" 2>/dev/null
  wait 2>/dev/null
}
trap cleanup EXIT

cd "$ROOT" || exit 1
GZIP=1 GZIP_LEVEL=1 RELOAD=0 PORT=$BE_PORT "$PY" -m app.main >/tmp/e2e-be.log 2>&1 &
BE_PID=$!

cd "$FE" || exit 1
BE_PORT=$BE_PORT "$NODE" node_modules/vite/bin/vite.js --port $FE_PORT --strictPort >/tmp/e2e-fe.log 2>&1 &
FE_PID=$!

echo "等待服务就绪…"
for i in $(seq 1 60); do
  be=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$BE_PORT/api/health" 2>/dev/null)
  fe=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$FE_PORT/compare" 2>/dev/null)
  if [ "$be" = "200" ] && [ "$fe" = "200" ]; then
    echo "  后端 $be / 前端 $fe 就绪（第 ${i} 次探测）"
    break
  fi
  sleep 1
done

# 端口被占时 uvicorn 会静默退出，而健康探测会打到那个占用者上（照样 200）——
# 必须显式拦住，否则后面所有测量都是在测别人的服务。
if grep -q "10048" /tmp/e2e-be.log 2>/dev/null; then
  echo "后端端口 $BE_PORT 已被占用，e2e 会打到别的服务上，中止。"
  head -6 /tmp/e2e-be.log
  exit 1
fi

if [ "$be" != "200" ] || [ "$fe" != "200" ]; then
  echo "服务没起来。后端日志尾部："
  tail -30 /tmp/e2e-be.log
  echo "前端日志尾部："
  tail -30 /tmp/e2e-fe.log
  exit 1
fi

cd "$FE" || exit 1
# 注意：Node 不认 /f/... 这种 Git-Bash 路径，会把它当成「当前盘根 + /f/...」，
# 截图会落到 F:\f\... 去。这里必须给 Windows 形式。
NODE_PATH="$WS" CHROME_PATH="$CHROME" UI_BASE="http://127.0.0.1:$FE_PORT" \
  SHOT_DIR="F:/A_projects/vehicle-test-db/frontend/e2e-shots" \
  "$NODE" scripts/e2e_compare.cjs
RC=$?

echo "e2e 退出码: $RC"
exit $RC
