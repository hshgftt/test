#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
验证上传导入接口（``POST /api/upload``）。

**为什么用临时 SQLite 库**：这个脚本会调 ``mode=replace``，也就是 drop_all ——
拿真库跑一次就把 PG 里的数据清空了。所以这里把 ``SQLITE_PATH`` 指到一个临时
文件上（必须在 import app.* 之前设好，engine 是模块级创建的）。

跑法::

    python scripts/verify_upload.py

覆盖：
  1. dry_run 不写库
  2. replace 导入后各表条数与解析摘要一致
  3. CSV 元信息真的落到了平台/品牌/驱动方式上
  4. append 重复上传是**幂等**的（按「车 × 工况」覆盖，不产生重复）
  5. 坏文件只进 errors，不中断整次导入
  6. 没传 paths 时 → 400（拿不到车型名）
  7. 超过 MAX_UPLOAD_MB → 413
  8. msgpack 文件能导
  9. `data/车型/工况.json`（多裹一层）→ 车型名是「车型」不是「data」
 10. `车型/工况.json`（直接选单车文件夹）→ 车型名**不能**被剥掉
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# ⚠️ 必须在 import app.* 之前设：app.database 在导入时就用 SQLITE_PATH 建 engine
_TMP = tempfile.mkdtemp(prefix="verify_upload_")
os.environ["SQLITE_PATH"] = os.path.join(_TMP, "upload_verify.db")
os.environ["GZIP"] = "0"
# 1 MB 闸门：示例数据只有几十 KB，不影响正常用例，又便于测 413
os.environ["MAX_UPLOAD_MB"] = "1"

from fastapi.testclient import TestClient            # noqa: E402

from app import importer                             # noqa: E402
from app.database import SessionLocal                # noqa: E402
from app.main import app                             # noqa: E402
from app.models import Condition, Curve, Experiment, Indicator, Platform, Vehicle  # noqa: E402

# 先把空表建出来，这样「dry_run 没写库」的断言查的是空表而不是「表不存在」
importer.ensure_schema()

_client = TestClient(app)

PASS = 0
FAIL = 0


def ok(cond: bool, label: str, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  \u2705 %s%s" % (label, ("  " + extra) if extra else ""))
    else:
        FAIL += 1
        print("  \u274c %s%s" % (label, ("  " + extra) if extra else ""))


def post_upload(entries, mode="replace", dry_run=False, send_paths=True):
    """entries: [(相对路径, bytes)]"""
    files = [
        ("files", (os.path.basename(p), data, "application/octet-stream"))
        for p, data in entries
    ]
    data = {"mode": mode, "dry_run": "true" if dry_run else "false"}
    if send_paths:
        data["paths"] = json.dumps([p for p, _ in entries], ensure_ascii=False)
    return _client.post("/api/upload", files=files, data=data)


def load_example_gen():
    """复用 make_example_data 的造数函数，保证格式与文档一致。"""
    path = os.path.join(_ROOT, "scripts", "make_example_data.py")
    spec = importlib.util.spec_from_file_location("_mkex", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def db_counts():
    db = SessionLocal()
    try:
        return {
            "platforms": db.query(Platform).count(),
            "vehicles": db.query(Vehicle).count(),
            "conditions": db.query(Condition).count(),
            "experiments": db.query(Experiment).count(),
            "indicators": db.query(Indicator).count(),
            "curves": db.query(Curve).count(),
        }
    finally:
        db.close()


def vehicle_rows():
    db = SessionLocal()
    try:
        return {
            v.name: {
                "platform": v.brand.platform.name,
                "brand": v.brand.name,
                "drive_type": v.drive_type,
                "vin": v.vin,
                "model_year": v.model_year,
            }
            for v in db.query(Vehicle).all()
        }
    finally:
        db.close()


def build_example_entries(mk, wrapper=""):
    """把示例数据造进内存。wrapper 非空时给所有路径加一层包裹目录。"""
    j = json.dumps
    entries = []

    def add(rel, obj):
        entries.append(((wrapper + rel) if wrapper else rel, j(obj, ensure_ascii=False).encode("utf-8")))

    vcsv = (
        "车型,平台,品牌,驱动方式,车架号,年款\n"
        "示例车A,EPA1,星河,后驱,LGWEF6E40PF000001,2024\n"
        "示例车B,EPA2,星河,四驱,LGWEF6E40PF000002,2025\n"
        "示例车C,EPA2,云驰,前驱,LGWEF6E40PF000003,2025\n"
    )
    ccsv = "name,category,description\n角阶跃,操纵性,阶跃\n角脉冲,操纵性,脉冲\n"
    entries.append(((wrapper + "vehicles.csv") if wrapper else "vehicles.csv",
                    vcsv.encode("utf-8-sig")))
    entries.append(((wrapper + "conditions.csv") if wrapper else "conditions.csv",
                    ccsv.encode("utf-8-sig")))
    add("示例车A/角阶跃.json", {
        "1": [mk._params(1.0), mk._curves_cal(), mk._curves_duiqi()],
        "2": [mk._params(1.03), mk._curves_cal(), mk._curves_duiqi()],
        "平均": [mk._params(1.015), mk._curves_cal(), mk._curves_duiqi()],
    })
    add("示例车A/角脉冲.json", {
        "1": [mk._params(0.95), mk._curves_cal(), mk._curves_duiqi()],
        "2": [mk._params(0.97), mk._curves_cal(), mk._curves_duiqi()],
    })
    add("示例车B/角阶跃.json", {
        "params": mk._params(1.05),
        "curves_cal": mk._curves_cal(),
        "curves_duiqi": mk._curves_duiqi(),
    })
    return entries


def main() -> int:
    mk = load_example_gen()
    print("临时库: %s" % os.environ["SQLITE_PATH"])
    print("=" * 60)

    # ── 1. dry_run 不写库 ──
    print("\n[1] dry_run 只预览")
    entries = build_example_entries(mk)
    r = post_upload(entries, mode="replace", dry_run=True)
    ok(r.status_code == 200, "HTTP 200", "got %d" % r.status_code)
    body = r.json()
    s = body.get("summary") or {}
    ok(body.get("imported") is None, "imported 为空（没写库）")
    ok(db_counts()["experiments"] == 0, "库里实验数仍为 0", str(db_counts()["experiments"]))
    ok(s.get("vehicles") == 2, "预览车型 2 台", str(s.get("vehicles")))
    ok(s.get("conditions") == 2, "预览工况 2 个", str(s.get("conditions")))
    ok(s.get("experiments") == 6, "预览实验 6 次", str(s.get("experiments")))
    ok(s.get("indicators") == 42, "预览指标 42 行", str(s.get("indicators")))
    ok(s.get("curves") == 36, "预览曲线 36 条", str(s.get("curves")))
    ok(s.get("points") == 7200, "预览采样点 7200", str(s.get("points")))

    # ── 2. replace 真导入 ──
    print("\n[2] mode=replace 真导入")
    r = post_upload(entries, mode="replace")
    ok(r.status_code == 200, "HTTP 200", "got %d" % r.status_code)
    body = r.json()
    imp = body.get("imported") or {}
    cnt = db_counts()
    ok(cnt["experiments"] == 6, "库内实验 6", str(cnt["experiments"]))
    ok(cnt["indicators"] == 42, "库内指标 42", str(cnt["indicators"]))
    ok(cnt["curves"] == 36, "库内曲线 36", str(cnt["curves"]))
    ok(cnt["vehicles"] == 2, "库内车型 2", str(cnt["vehicles"]))
    ok(imp.get("curves") == 36, "返回值与库一致", str(imp.get("curves")))

    # ── 3. CSV 元信息 ──
    print("\n[3] CSV 元信息落到车辆属性上")
    rows = vehicle_rows()
    a = rows.get("示例车A") or {}
    ok(a.get("platform") == "EPA1", "示例车A 平台 = EPA1", str(a.get("platform")))
    ok(a.get("brand") == "星河", "示例车A 品牌 = 星河", str(a.get("brand")))
    ok(a.get("drive_type") == "后驱", "示例车A 驱动 = 后驱", str(a.get("drive_type")))
    ok(a.get("vin") == "LGWEF6E40PF000001", "示例车A 车架号", str(a.get("vin")))
    ok(a.get("model_year") == 2024, "示例车A 年款 = 2024", str(a.get("model_year")))
    db = SessionLocal()
    try:
        c = db.query(Condition).filter(Condition.name == "角阶跃").first()
        cat = c.category if c else None
    finally:
        db.close()
    ok(cat == "操纵性", "工况分类 = 操纵性", str(cat))

    # ── 4. append 幂等 ──
    print("\n[4] mode=append 重复上传同一份数据（应幂等）")
    r = post_upload(entries, mode="append")
    ok(r.status_code == 200, "HTTP 200", "got %d" % r.status_code)
    cnt2 = db_counts()
    ok(cnt2 == cnt, "各表条数不变（无重复）", "%s → %s" % (cnt["curves"], cnt2["curves"]))
    ok(len(vehicle_rows()) == 2, "车型仍是 2 台（没重复插）", str(len(vehicle_rows())))

    # ── 5. 坏文件 ──
    print("\n[5] 坏文件只进 errors，不中断")
    bad = list(entries) + [("示例车A/坏文件.json", b"{ this is not json")]
    r = post_upload(bad, mode="replace")
    ok(r.status_code == 200, "HTTP 200", "got %d" % r.status_code)
    body = r.json()
    errs = body.get("errors") or []
    ok(len(errs) == 1 and errs[0]["file"].endswith("坏文件.json"),
       "坏文件出现在 errors", json.dumps(errs, ensure_ascii=False)[:120])
    ok((body.get("imported") or {}).get("curves") == 36, "其余数据照常导入")

    # ── 6. 没传 paths ──
    print("\n[6] 没传 paths → 400")
    r = post_upload(entries, mode="replace", send_paths=False)
    ok(r.status_code == 400, "HTTP 400", "got %d" % r.status_code)

    # ── 7. 超过 MAX_UPLOAD_MB ──
    print("\n[7] 超过 MAX_UPLOAD_MB(1MB) → 413")
    big = [("示例车A/巨大.json", b"x" * (1024 * 1024 + 64))]
    r = post_upload(big, mode="replace")
    ok(r.status_code == 413, "HTTP 413", "got %d" % r.status_code)

    # ── 8. msgpack ──
    print("\n[8] msgpack 文件")
    try:
        import msgpack  # type: ignore
        has_mp = True
    except ImportError:
        has_mp = False
    if has_mp:
        mp_entries = [
            ("mp车/角阶跃.msgpack", msgpack.dumps(
                {"1": [mk._params(1.0), mk._curves_cal(), mk._curves_duiqi()]},
                use_bin_type=True)),
        ]
        r = post_upload(mp_entries, mode="replace")
        ok(r.status_code == 200, "HTTP 200", "got %d" % r.status_code)
        body = r.json()
        ok((body.get("imported") or {}).get("curves") == 6, "msgpack 导入 6 条曲线",
           str((body.get("imported") or {}).get("curves")))
        ok("mp车" in vehicle_rows(), "车型名 = mp车", str(list(vehicle_rows())))
    else:
        print("  \u26a0\ufe0f 未安装 msgpack，跳过")

    # ── 9. 多裹一层目录 ──
    print("\n[9] `data/车型/工况.json` → 车型名不应是 data")
    wrapped = build_example_entries(mk, wrapper="data/")
    r = post_upload(wrapped, mode="replace")
    ok(r.status_code == 200, "HTTP 200", "got %d" % r.status_code)
    names = set(vehicle_rows())
    ok(names == {"示例车A", "示例车B"}, "车型名 = 示例车A/B", str(sorted(names)))

    # ── 10. 直接选单车文件夹 ──
    print("\n[10] `车型/工况.json` → 车型名不能被剥掉")
    single = [
        ("示例车A/角阶跃.json", json.dumps(
            {"1": [mk._params(1.0), mk._curves_cal(), mk._curves_duiqi()]},
            ensure_ascii=False).encode("utf-8")),
    ]
    r = post_upload(single, mode="replace")
    ok(r.status_code == 200, "HTTP 200", "got %d" % r.status_code)
    names = set(vehicle_rows())
    ok(names == {"示例车A"}, "车型名 = 示例车A", str(sorted(names)))

    print("\n" + "=" * 60)
    print("断言: %d 通过 / %d 失败" % (PASS, FAIL))
    print("=" * 60)
    shutil.rmtree(_TMP, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
