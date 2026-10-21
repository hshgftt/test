#!/usr/bin/env bash
# 一键启动后端（Linux / macOS）
# 用法: ./start.sh          默认 8000 端口 + 热重载
#       PORT=8080 ./start.sh
set -e
cd "$(dirname "$0")"

# ── 只在【确认用 SQLite】且库文件缺失时才自动初始化 ──
#    用 PG 时（.env 里有 DATABASE_URL）绝不能自动 seed，
#    否则会往生产库里灌一份 tiny 数据。
USE_PG=0
if [ -f .env ] && grep -qE '^[[:space:]]*DATABASE_URL=' .env; then
  USE_PG=1
fi
[ -n "$SQLITE_PATH" ] && USE_PG=0
[ -n "$USE_SQLITE" ] && USE_PG=0

if [ "$USE_PG" = "1" ]; then
  echo "==> 检测到 .env 使用 PostgreSQL，跳过自动初始化"
  echo "    (需要建表请手动执行: python3 scripts/init_db.py)"
  echo "    (需要灌测试数据:    python3 scripts/seed_data.py --preset tiny)"
  echo "    (导入真实数据:      python3 scripts/import_data.py --data-dir ./data)"
else
  if [ ! -f dev.db ] && [ ! -f vehicle_test.db ]; then
    echo "==> 未发现 SQLite 数据库，先建表 + 灌数据（preset=${PRESET:-tiny}）"
    python3 scripts/init_db.py
    python3 scripts/seed_data.py --preset "${PRESET:-tiny}"
  fi
fi

echo "==> 启动后端: http://localhost:${PORT:-8000}  (Swagger: /docs)"
exec python3 -m app.main
