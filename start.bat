@echo off
REM 一键启动后端（Windows）
REM 用法: start.bat         默认 8000 端口 + 热重载
REM       set PORT=8080 && start.bat

cd /d "%~dp0"

REM ── 只在【确认用 SQLite】且库文件缺失时才自动初始化 ──
REM    用 PG 时（.env 里有 DATABASE_URL）绝不能自动 seed，
REM    否则会往生产库里灌一份 tiny 数据。
set USE_PG=0
findstr /B /C:"DATABASE_URL=" .env >nul 2>&1 && set USE_PG=1
if defined SQLITE_PATH set USE_PG=0
if defined USE_SQLITE set USE_PG=0

if "%USE_PG%"=="1" (
  echo ==^> 检测到 .env 使用 PostgreSQL，跳过自动初始化
  echo     ^(需要建表请手动执行: python scripts/init_db.py^)
  echo     ^(需要灌测试数据:    python scripts/seed_data.py --preset tiny^)
  echo     ^(导入真实数据:      python scripts/import_data.py --data-dir ./data^)
) else (
  if not exist dev.db if not exist vehicle_test.db (
    if "%PRESET%"=="" set PRESET=tiny
    echo ==^> 未发现 SQLite 数据库，先建表 + 灌数据（preset=%PRESET%）
    python scripts/init_db.py
    python scripts/seed_data.py --preset %PRESET%
  )
)

if "%PORT%"=="" set PORT=8000
echo ==^> 启动后端: http://localhost:%PORT%  (Swagger: /docs)
python -m app.main
pause
