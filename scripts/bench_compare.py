"""真实 HTTP 基准：200 车规模下各收敛手段的响应体积 / 耗时。

会临时起一个 uvicorn（GZIP 默认开），跑完自己关掉。
"""
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models import Condition, Experiment  # noqa: E402

PORT = int(os.getenv("BENCH_PORT", "8899"))
BASE = "http://127.0.0.1:%d" % PORT


def fetch(path, label, accept_gzip=True):
    url = BASE + path
    req = urllib.request.Request(url)
    if accept_gzip:
        req.add_header("Accept-Encoding", "gzip")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            raw = r.read()
            status = r.status
            enc = r.headers.get("Content-Encoding") or "-"
    except urllib.error.HTTPError as e:
        raw = e.read()
        status = e.code
        enc = "-"
    dt = time.time() - t0
    # 解码后看真实体积
    body = raw
    if enc == "gzip":
        import gzip

        try:
            body = gzip.decompress(raw)
        except Exception:
            body = raw
    print(
        "  %-42s %s  gz=%9s  raw=%9s  %6.2fs"
        % (label, status, _h(len(raw)), _h(len(body)), dt)
    )
    return status, len(body)


def _h(n):
    if n < 1024:
        return "%d B" % n
    if n < 1024 * 1024:
        return "%.0f KB" % (n / 1024)
    return "%.1f MB" % (n / 1024 / 1024)


def main():
    db = SessionLocal()
    cond = db.execute(
        select(Condition).where(Condition.name == "稳态回转")
    ).scalars().first()
    vids = [
        r[0] for r in db.execute(
            select(Experiment.vehicle_id)
            .where(Experiment.condition_id == cond.id)
            .distinct().order_by(Experiment.vehicle_id).limit(200)
        )
    ]
    curve_names = [r[0] for r in db.execute(
        select(Experiment.id).where(Experiment.id == -1)
    )]
    db.close()

    from app import crud
    from app.database import SessionLocal as SL

    d2 = SL()
    names = crud.get_curve_names(d2, cond.id)
    d2.close()

    print("工况: %s (id=%s)  车辆数: %d" % (cond.name, cond.id, len(vids)))
    print("曲线: %d 条 -> %s\n" % (len(names), names))

    env = dict(os.environ)
    env.update({
        "GZIP": "1",
        "GZIP_LEVEL": "1",
        "RELOAD": "0",
        "PORT": str(PORT),
        "PYTHONUNBUFFERED": "1",
    })
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    log = open(os.path.join(root, ".bench-server.log"), "w")
    srv = subprocess.Popen(
        [sys.executable, "-m", "app.main"],
        cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    try:
        # 等服务起来
        for _ in range(60):
            try:
                urllib.request.urlopen(BASE + "/api/health", timeout=2).read()
                break
            except Exception:
                time.sleep(0.5)
        else:
            print("服务没起来，日志：")
            log.flush()
            print(open(os.path.join(root, ".bench-server.log")).read()[-3000:])
            return

        v50 = ",".join(str(v) for v in vids[:50])
        v200 = ",".join(str(v) for v in vids)
        one = names[0]

        def q(**kw):
            return "/api/compare?" + urllib.parse.urlencode(kw)

        print("── 50 台车 ──")
        fetch(q(vehicle_ids=v50, condition_id=cond.id),
              "① 全部实验 × 全部曲线（估算 4200 万点，应放行）")
        fetch(q(vehicle_ids=v50, condition_id=cond.id, exp_scope="avg_first"),
              "② + exp_scope=avg_first")
        fetch(q(vehicle_ids=v50, condition_id=cond.id, exp_scope="avg_first",
                curves=one),
              "③ + 只取 1 条曲线")
        fetch(q(vehicle_ids=v50, condition_id=cond.id, exp_scope="avg_first",
                meta_only="1"),
              "④ + meta_only（菜单，不含 series）")

        print("\n── 200 台车（真正的用户场景）──")
        fetch(q(vehicle_ids=v200, condition_id=cond.id, exp_scope="avg_first"),
              "⑤ 平均 × 全部 7 条曲线（5600 万点 → 应 400）")
        fetch(q(vehicle_ids=v200, condition_id=cond.id, exp_scope="avg_first",
                curves=one),
              "⑥ 平均 × 1 条曲线（800 万点 → 应放行）")
        fetch(q(vehicle_ids=v200, condition_id=cond.id, meta_only="1",
                exp_scope="avg_first"),
              "⑦ 200 台车的菜单（meta_only 不受限）")
        fetch(q(vehicle_ids=v200, condition_id=cond.id),
              "⑧ 200 台 × 全部实验 × 全部曲线（1.68 亿点 → 应 400）")

        print("\n── 重复 id 去重 ──")
        fetch(q(vehicle_ids="%s,%s" % (vids[0], vids[0]), condition_id=cond.id,
                meta_only="1"),
              "⑨ 同一台车传两次")
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=10)
        except Exception:
            srv.kill()
        log.close()


if __name__ == "__main__":
    main()
