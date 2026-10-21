#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
重置本机 PostgreSQL 密码（忘记 postgres 密码时用）

原理：临时把 pg_hba.conf 改成 trust（免密），reload 后用 psql 改密码，
      再把 pg_hba.conf 还原并 reload。整个过程不丢数据。

用法::

    python scripts/reset_pg_password.py                     # 新密码默认 postgres
    python scripts/reset_pg_password.py --password 123456
    python scripts/reset_pg_password.py --user postgres --port 5432
    python scripts/reset_pg_password.py --hba "C:\\Program Files\\PostgreSQL\\16\\data\\pg_hba.conf"

Windows 需要**管理员权限**运行（要重启服务）；Linux/macOS 需要 sudo。
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from typing import List, Optional

IS_WIN = platform.system() == "Windows"


# ── 定位 pg_hba.conf ──────────────────────────────────────────────────

def candidate_hba_paths() -> List[str]:
    out: List[str] = []
    if IS_WIN:
        for base in (r"C:\Program Files\PostgreSQL", r"C:\Program Files (x86)\PostgreSQL"):
            if os.path.isdir(base):
                for ver in sorted(os.listdir(base), reverse=True):
                    p = os.path.join(base, ver, "data", "pg_hba.conf")
                    if os.path.isfile(p):
                        out.append(p)
    else:
        out += [
            "/etc/postgresql/*/main/pg_hba.conf",
            "/var/lib/pgsql/*/data/pg_hba.conf",
            "/var/lib/postgresql/*/main/pg_hba.conf",
            "/opt/homebrew/var/postgresql*/pg_hba.conf",
            "/usr/local/var/postgres/pg_hba.conf",
            "/opt/pgsql/data/pg_hba.conf",
        ]
        import glob
        expanded: List[str] = []
        for pat in out:
            expanded.extend(sorted(glob.glob(pat), reverse=True))
        out = expanded
    return out


def find_hba(explicit: Optional[str] = None) -> Optional[str]:
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    for p in candidate_hba_paths():
        if os.path.isfile(p):
            return p
    return None


def data_dir_of(hba: str) -> str:
    return os.path.dirname(hba)


# ── 执行外部命令 ──────────────────────────────────────────────────────

def run(cmd: List[str], check: bool = True) -> subprocess.CompletedProcess:
    printable = " ".join(cmd)
    print("  $ " + printable)
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def find_pgtool(name: str, data_dir: str) -> str:
    """优先 PATH，其次 PostgreSQL 安装目录的 bin/。"""
    found = shutil.which(name)
    if found:
        return found
    bin_dir = os.path.join(os.path.dirname(data_dir), "bin")
    cand = os.path.join(bin_dir, name + (".exe" if IS_WIN else ""))
    if os.path.isfile(cand):
        return cand
    return name  # 交给 shell 去报错


def reload_pg(data_dir: str, port: int) -> bool:
    """先试 pg_ctl reload（不需要重启服务），失败再退回到重启服务。"""
    pg_ctl = find_pgtool("pg_ctl", data_dir)
    try:
        run([pg_ctl, "reload", "-D", data_dir])
        return True
    except Exception as e:
        print("  pg_ctl reload 失败：%s，尝试重启服务…" % e)

    if IS_WIN:
        # Windows 服务名形如 postgresql-x64-16
        try:
            out = subprocess.run(
                ["sc", "query", "type=", "service", "state=", "all"],
                capture_output=True, text=True,
            ).stdout
            names = [
                ln.split(":")[1].strip()
                for ln in out.splitlines()
                if "SERVICE_NAME" in ln and "postgresql" in ln.lower()
            ]
        except Exception:
            names = []
        if not names:
            # 猜一个：从 data 目录的版本号推
            ver = os.path.basename(os.path.dirname(data_dir))
            names = ["postgresql-x64-%s" % ver]
        for svc in names:
            try:
                run(["net", "stop", svc], check=False)
                run(["net", "start", svc])
                return True
            except Exception:
                continue
    else:
        for svc in ("postgresql", "postgresql-16", "postgresql-15", "postgresql-14"):
            try:
                run(["sudo", "systemctl", "restart", svc])
                return True
            except Exception:
                continue
        try:
            run(["brew", "services", "restart", "postgresql"])
            return True
        except Exception:
            pass
    return False


# ── 主流程 ────────────────────────────────────────────────────────────

TRUST_LINES = (
    "# >>> reset_pg_password 临时免密（还原后会自动删除）\n"
    "local   all             all                                     trust\n"
    "host    all             all             127.0.0.1/32            trust\n"
    "host    all             all             ::1/128                 trust\n"
    "# <<< reset_pg_password\n"
)


def main() -> int:
    ap = argparse.ArgumentParser(description="重置本机 PostgreSQL 用户密码")
    ap.add_argument("--user", default="postgres", help="要改密码的用户（默认 postgres）")
    ap.add_argument("--password", default="postgres", help="新密码（默认 postgres）")
    ap.add_argument("--port", type=int, default=5432, help="端口（默认 5432）")
    ap.add_argument("--hba", default=None, help="pg_hba.conf 路径（自动查找失败时手动指定）")
    args = ap.parse_args()

    hba = find_hba(args.hba)
    if not hba:
        print("✗ 找不到 pg_hba.conf。请手动指定：")
        print('    python scripts/reset_pg_password.py --hba "C:\\Program Files\\PostgreSQL\\16\\data\\pg_hba.conf"')
        return 1
    data_dir = data_dir_of(hba)
    print("找到 pg_hba.conf : %s" % hba)
    print("data 目录        : %s" % data_dir)

    backup = hba + ".bak"
    if not os.path.exists(backup):
        shutil.copy2(hba, backup)
    print("已备份           : %s" % backup)

    try:
        with open(hba, "r", encoding="utf-8", errors="replace") as fh:
            original = fh.read()
        with open(hba, "w", encoding="utf-8") as fh:
            fh.write(TRUST_LINES + "\n" + original)

        print("\n[1/3] 让 PostgreSQL 重新加载配置 …")
        if not reload_pg(data_dir, args.port):
            print("✗ 无法 reload/重启 PostgreSQL。请手动重启服务后重跑本脚本。")
            return 1

        print("\n[2/3] 修改密码 …")
        psql = find_pgtool("psql", data_dir)
        sql = "ALTER USER %s WITH PASSWORD '%s';" % (args.user, args.password)
        res = subprocess.run(
            [psql, "-U", args.user, "-h", "127.0.0.1", "-p", str(args.port),
             "-d", "postgres", "-c", sql],
            capture_output=True, text=True,
        )
        print(res.stdout.strip() or res.stderr.strip())
        if res.returncode != 0:
            print("✗ 改密码失败，pg_hba.conf 已保留为 trust 状态，请手动处理。")
            return 1
        print("✓ 密码已重置")
    finally:
        print("\n[3/3] 还原 pg_hba.conf …")
        shutil.copy2(backup, hba)
        reload_pg(data_dir, args.port)
        print("✓ 已还原（备份仍在 %s，确认无问题后可删）" % backup)

    print("\n完成。连接串：")
    print("  DATABASE_URL=postgresql+psycopg2://%s:%s@localhost:%d/vehicle_test"
          % (args.user, args.password, args.port))
    return 0


if __name__ == "__main__":
    sys.exit(main())
