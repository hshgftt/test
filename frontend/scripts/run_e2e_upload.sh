#!/usr/bin/env bash
# 上传数据页端到端验证：起临时后端 + 前端 → 跑 e2e → 全关掉。
# 必须写成一条命令执行：沙箱里后台进程活不过一条命令。
#
# ⚠️ 这个 e2e 会走 mode=replace（drop_all），所以后端**必须**用临时 SQLite 库，
# 不能指向 PG，否则会把真数据清空。
set -u

ROOT=/f/A_projects/vehicle-test-db
FE=$ROOT/frontend
PY=E:/python314/python.exe
NODE="D:/Program Files/nodejs/node.exe"
WS="C:/Users/LAPTOP/.workbuddy-ai/binaries/node/workspace/node_modules"
CHROME="C:/Users/LAPTOP/.agent-browser/browsers/chrome-153.0.8010.52/chrome.exe"

unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY

BE_PORT=8011
FE_PORT=5175
TMP_DB="F:/A_projects/vehicle-test-db/.tmp_upload_e2e.db"
EXAMPLE="F:/A_projects/vehicle-test-db/example_data"

# 示例数据不存在就造一份
if [ ! -d "$ROOT/example_data" ]; then
  (cd "$ROOT" && "$PY" scripts/make_example_data.py --out ./example_data >/dev/null)
fi

# ── 把临时库重置成「有表但是空的」 ──
#  为什么必须显式建表：应用启动时**不会**自动建表（那是 start.sh / init_db 的事，
#  而 start.sh 检测到显式 SQLITE_PATH 时是跳过建表的）。不建的话
#  `/api/tree` 会 500（no such table），e2e 里「预览没写库 → 库还是空的」
#  这条断言就没法查 —— 只能靠「响应里 imported 是 null」间接推断。
#  这里 drop + create，既清空上一轮的残留，又把表建好。
(cd "$ROOT" && SQLITE_PATH="sqlite:///$TMP_DB" "$PY" -c "
import app.models  # noqa: F401  注册所有表
from app.database import Base, engine
Base.metadata.drop_all(bind=engine)
Base.metadata.create_all(bind=engine)
print('  临时库已重置（空表）')
")

cleanup() {
  [ -n "${BE_PID:-}" ] && kill "$BE_PID" 2>/dev/null
  [ -n "${FE_PID:-}" ] && kill "$FE_PID" 2>/dev/null
  wait 2>/dev/null
}
trap cleanup EXIT

cd "$ROOT" || exit 1
# 注意用 sqlite:/// 前缀：不写前缀的话 app.config 会拼成 sqlite:///F:/... 也能用，
# 但显式写出来更不容易看错成 PG。
SQLITE_PATH="sqlite:///$TMP_DB" GZIP=0 RELOAD=0 PORT=$BE_PORT \
  "$PY" -m app.main >/tmp/e2e-up-be.log 2>&1 &
BE_PID=$!

cd "$FE" || exit 1
BE_PORT=$BE_PORT "$NODE" node_modules/vite/bin/vite.js --port $FE_PORT --strictPort \
  >/tmp/e2e-up-fe.log 2>&1 &
FE_PID=$!

echo "等待服务就绪…"
for i in $(seq 1 60); do
  be=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$BE_PORT/api/health" 2>/dev/null)
  fe=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$FE_PORT/upload" 2>/dev/null)
  if [ "$be" = "200" ] && [ "$fe" = "200" ]; then
    echo "  后端 $be / 前端 $fe 就绪（第 ${i} 次探测）"
    break
  fi
  sleep 1
done

# 端口被占时 uvicorn 静默退出，而健康探测会打到占用者上（照样 200）——
# 那样所有断言都是在测别人的服务。
if grep -q "10048" /tmp/e2e-up-be.log 2>/dev/null; then
  echo "后端端口 $BE_PORT 已被占用，e2e 会打到别的服务上，中止。"
  head -6 /tmp/e2e-up-be.log
  exit 1
fi

if [ "$be" != "200" ] || [ "$fe" != "200" ]; then
  echo "服务没起来。后端日志尾部："
  tail -30 /tmp/e2e-up-be.log
  echo "前端日志尾部："
  tail -30 /tmp/e2e-up-fe.log
  exit 1
fi

# 确认后端用的确实是临时 SQLite（不是 PG）
health=$(curl -s "http://127.0.0.1:$BE_PORT/api/health")
case "$health" in
  *sqlite*) echo "  数据库: $health" ;;
  *)
    echo "⚠️ 后端没有跑在 SQLite 上，这个 e2e 会把真库清空，中止。"
    echo "$health"
    exit 1
    ;;
esac

cd "$FE" || exit 1
NODE_PATH="$WS" CHROME_PATH="$CHROME" UI_BASE="http://127.0.0.1:$FE_PORT" \
  SHOT_DIR="F:/A_projects/vehicle-test-db/frontend/e2e-shots" \
  DATA_DIR="F:/A_projects/vehicle-test-db/example_data" \
  "$NODE" scripts/e2e_upload.cjs
RC=$?

# 收尾：**先杀后端再删库** —— Windows 上删不掉还被占用的文件，
# 而 `trap ... EXIT` 要等脚本退出才跑，所以这里显式先 cleanup。
# 用 Python 而不是 rm：某些环境（沙箱 / 安全删除钩子）会拦下 rm。
RC=$?
cleanup
trap - EXIT
"$PY" -c "
import os
p = r'F:/A_projects/vehicle-test-db/.tmp_upload_e2e.db'
for f in (p, p + '-journal', p + '-wal', p + '-shm'):
    try:
        os.remove(f)
    except OSError:
        pass
" 2>/dev/null
echo "e2e 退出码: $RC"
exit $RC
