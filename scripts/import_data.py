#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
导入真实试验数据（替代 seed_data.py 的造数流程）。

数据目录约定
============

最省事的做法 —— 和 ``data/`` 现有布局一致::

    data/
    ├── 极光EV-1/
    │   ├── 角阶跃.json
    │   ├── 角脉冲.json
    │   └── 稳态回转.json
    └── 星河EV-01/
        └── …

即 **一台车一个文件夹，一个工况一个文件**。

文件格式：JSON 或 msgpack
-------------------------

后缀决定怎么读，内容结构完全一致（解出来都是 dict）：

============================  ==========================
`.json`                       标准 JSON（utf-8，可带 BOM）
`.msgpack` `.msg` `.mpack`    msgpack；**算法侧常见的落盘格式**
============================  ==========================

msgpack 需要额外装库：``pip install msgpack``。
脚本会把 msgpack 里可能残留的 bytes key（老版本 dump 出来的中文 key）
统一解成 str，不用手工清洗。

默认这些后缀都认；要限定只读某一种，用
``--ext .msgpack``（逗号分隔可写多个）。

JSON 内容两种写法都支持
-----------------------

**写法 A（推荐，与算法侧 dict 一致）**：顶层 key = 实验号::

    {
      "1":    [params1, curves_cal1, curves_duiqi1],
      "2":    [params2, curves_cal2, curves_duiqi2],
      "平均": [paramsAvg, curves_calAvg, curves_duiqiAvg]
    }

其中 ``curves_cal`` / ``curves_duiqi`` 形如::

    {"方向盘转角-时间": {"curve": [{"x": [...], "y": [...],
                                    "curvetype": "line", "curvename": "滤波"}],
                        "title": "方向盘转角vs时间",
                        "xlabel": "时间（s）", "ylabel": "方向盘转角（deg）"}}

**写法 B（扁平）**：顶层直接是 params，曲线可选::

    {"params": {...}, "curves_cal": {...}, "curves_duiqi": {...}}

元信息从哪来
------------

**数据文件里只有指标和曲线，没有任何车辆属性**（平台/品牌/驱动方式等
算法侧不产出，只能另外给）。这些属性从 CSV 来：

* **平台 / 品牌 / 驱动方式**：优先读同目录下的 ``vehicles.csv``
  （列：车型,平台,品牌,驱动方式[,车架号,年款]）。没有 CSV 就按
  ``车型名`` 推断（前缀当品牌），或全部落到「默认平台 / 默认品牌」。
  **CSV 的「车型」必须和数据文件夹名一致**；CSV 里多列几台没数据的车无妨，
  ``vehicles`` 表是按扫描到的文件夹建的，不是按 CSV 建的。
* **工况分类**：可用 ``--conditions-csv`` 指定 name,category,description。
  不给也能导，只是 ``category`` / ``description`` 为空。
* **平均实验**：顶层 key 为 ``平均``/``avg``/``average`` 即识别为平均，
  并自动置 ``is_average`` + ``sort_order=999``。

想要一份能直接跑的示例目录（含 CSV 和两种写法），执行::

    python scripts/make_example_data.py --out ./example_data

用法
====

    # 预览（不写库，只看解析结果）
    python scripts/import_data.py --data-dir ./data --dry-run

    # 正式导入（会先清空现有数据！）
    python scripts/import_data.py --data-dir ./data

    # 保留现有数据，按「车 × 工况」覆盖导入
    python scripts/import_data.py --data-dir ./data --append

    # 数据是 msgpack 落盘的（算法侧常见），直接指过去即可：
    #   python scripts/import_data.py --data-dir ./real_data
    # 后缀认 .json / .msgpack / .msg / .mpack；要限定只读某种：
    python scripts/import_data.py --data-dir ./real_data --ext .msgpack

解析与入库的全部规则都在 ``app/importer.py``，这个文件只是命令行外壳 ——
上传页面（``POST /api/upload``）调的是**同一个** :func:`importer.import_items`，
所以两边行为不会漂移。改规则请改 ``app/importer.py``。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Tuple

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app import importer
from app.database import SessionLocal
from app.importer import READABLE_EXTS


def main() -> int:
    ap = argparse.ArgumentParser(description="导入真实试验数据")
    ap.add_argument("--data-dir", default=os.path.join(_ROOT, "data"),
                    help="数据目录（一台车一个文件夹）")
    ap.add_argument("--vehicles-csv", default=None,
                    help="车辆元信息 CSV（默认找 <data-dir>/vehicles.csv）")
    ap.add_argument("--conditions-csv", default=None,
                    help="工况分类 CSV（name,category,description）")
    ap.add_argument("--append", action="store_true",
                    help="保留现有数据，按「车 × 工况」覆盖导入（默认会先清空）")
    ap.add_argument("--dry-run", action="store_true", help="只预览，不写库")
    ap.add_argument("--ext", default=None,
                    help="只认指定后缀，逗号分隔。默认 .json,.msgpack,.msg,.mpack")
    args = ap.parse_args()

    exts: Tuple[str, ...] = READABLE_EXTS
    if args.ext:
        exts = tuple(
            e.strip().lower() if e.strip().startswith(".") else "." + e.strip().lower()
            for e in args.ext.split(",") if e.strip()
        )

    t0 = time.time()
    print("扫描数据目录: %s  （后缀 %s）" % (args.data_dir, ", ".join(exts)))
    meta = importer.scan_dir(args.data_dir, exts=exts)

    # 显式指定的 CSV 优先于目录里自动找到的
    if args.vehicles_csv:
        meta["vmeta"] = importer.read_vehicles_csv(args.vehicles_csv)
        meta["vehicles_csv"] = args.vehicles_csv
    if args.conditions_csv:
        meta["cmeta"] = importer.read_conditions_csv(args.conditions_csv)
        meta["conditions_csv"] = args.conditions_csv

    items = meta["items"]
    for e in meta["errors"]:
        print("  ! 跳过 %s：%s" % (e["file"], e["error"]))
    if not items:
        print("没有找到可导入的数据。请确认目录结构是 <data-dir>/<车型>/<工况>.<后缀>")
        return 1

    print(importer.preview_text(items, meta))

    if args.dry_run:
        print("\n--dry-run：未写库。样例：")
        it = items[0]
        label = it["vehicle"] + (" / " + it["mode"] if it.get("mode") else "")
        if it["kind"] == "payload":
            print("  %s / %s → 未知格式，整块存入 datasets.payload"
                  % (label, it["condition"]))
        else:
            print("  %s / %s → 实验号 %s"
                  % (label, it["condition"],
                     [e for e in sorted(it["experiments"], key=importer.exp_sort_key)]))
        return 0

    # ── 建表 / 清空 ──
    if not args.append:
        print("\n清空现有数据 …")
        importer.reset_schema()
    else:
        importer.ensure_schema()

    db = SessionLocal()
    try:
        stats = importer.import_items(
            db, items, meta["vmeta"], meta["cmeta"],
            append=args.append,
            progress=lambda d, t: print("  已处理 %d/%d 份文件 …" % (d, t)) if d % 10 == 0 else None,
        )
    finally:
        db.close()

    print("\n导入完成：%d 份文件，用时 %.1fs" % (stats["files"], time.time() - t0))
    print("  车辆 %d 台 / 模式 %d 种 / 工况 %d 个 / 数据集 %d 条"
          % (stats["vehicles"], stats["modes"], stats["conditions"],
             stats["datasets"]))
    print("  指标 %d 行 / 曲线 %d 条 / 采样点 %s"
          % (stats["indicators"], stats["curves"],
             "{:,}".format(stats["points"])))
    n = importer.recalc_curve_meta()
    if n:
        print("  重算 point_count: %d 条" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
