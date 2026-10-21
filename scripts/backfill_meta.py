#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只补车辆元数据（属性 / 模式 / 单号），不动已导入的数据集。

为什么需要它
------------
``import_data.py`` 只从数据目录里的 ``vehicles.csv`` 读车辆属性。数据目录
没有这个文件时，导进来的车就只有名字 —— 而默认导航树的前几层是按
``platform / brand`` 这些属性分层的，属性全空时整棵树会被筛空。

重跑 ``import_data.py`` 能补上属性，但会把几 GB 的数据重灌一遍。这个脚本
只写 ``vehicle_attr_values`` / ``dim_entries`` / ``vehicle_modes`` /
``test_orders`` 四张表，几秒钟完事。

用法::

    python scripts/backfill_meta.py                    # 读 ./data/vehicles.csv
    python scripts/backfill_meta.py --csv 我的车辆.csv
    python scripts/backfill_meta.py --dry-run          # 只看映射结果，不写库
    python scripts/backfill_meta.py --create-missing   # CSV 有、库里没有的车也建

CSV 格式（首列是车名，其余列随便加，匹配不到的表头会自动注册成新字段）::

    车型,平台,品牌,驱动方式,车架号,年款
    星河EV-01,BEV平台,星河,后驱,LGWEF6E40PF000001,2024
"""
from __future__ import annotations

import argparse
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from sqlalchemy import select

from app import crud, importer
from app.database import SessionLocal
from app.models import Vehicle


def _resolve_code(header: str, by_code, by_name) -> str:
    """预览用：只查已有字段，不注册新的。"""
    key = header.strip()
    alias = importer.HEADER_ALIASES.get(key)
    attr = (by_code.get(alias) if alias else None)
    attr = attr or by_code.get(key) or by_name.get(key.lower())
    return attr.code if attr else "<新建:%s>" % key


def main() -> int:
    ap = argparse.ArgumentParser(description="补车辆属性/模式/单号（不动数据集）")
    ap.add_argument("--csv", default=os.path.join(_ROOT, "data", "vehicles.csv"),
                    help="vehicles.csv 路径（默认 ./data/vehicles.csv）")
    ap.add_argument("--dry-run", action="store_true", help="只预览，不写库")
    ap.add_argument("--create-missing", action="store_true",
                    help="CSV 里有、库里没有的车也一并创建")
    args = ap.parse_args()

    if not os.path.isfile(args.csv):
        print("找不到 CSV: %s" % args.csv)
        return 2

    with open(args.csv, "rb") as fh:
        vmeta = importer.read_vehicles_csv(fh.read())
    if not vmeta:
        print("CSV 没解析出任何车辆 —— 检查首列是不是「车型 / 车名」")
        return 2

    db = SessionLocal()
    try:
        by_code, by_name = importer._attr_lookup(db)

        # 表头 → 字段 code（和 import_items 里的行为保持一致）
        headers: dict[str, list[str]] = {}
        for meta in vmeta.values():
            for header, value in (meta.get("attrs") or {}).items():
                headers.setdefault(header, []).append(value)

        header_codes: dict[str, str] = {}
        if args.dry_run:
            for header in headers:
                header_codes[header] = _resolve_code(header, by_code, by_name)
        else:
            for header, values in headers.items():
                header_codes[header] = importer._register_attr(
                    db, header, values, by_code, by_name).code
            db.flush()

        vehicles = {v.name: v for v in db.execute(select(Vehicle)).scalars()}
        print("CSV %d 台车 | 库内 %d 台 | 表头映射 %s"
              % (len(vmeta), len(vehicles),
                 {h: c for h, c in header_codes.items()}))

        n_attr = n_mode = n_order = n_new = n_skip = 0
        for name in sorted(vmeta):
            meta = vmeta[name]
            veh = vehicles.get(name)
            if veh is None:
                if not args.create_missing:
                    n_skip += 1
                    continue
                if args.dry_run:
                    n_new += 1
                    continue
                veh = Vehicle(name=name)
                db.add(veh)
                db.flush()
                vehicles[name] = veh
                n_new += 1

            attrs = {}
            for header, value in (meta.get("attrs") or {}).items():
                code = header_codes.get(header)
                if code and value not in (None, ""):
                    attrs[code] = value
            if attrs:
                if not args.dry_run:
                    crud.set_vehicle_attrs(db, veh, attrs)
                n_attr += len(attrs)

            modes = list(meta.get("modes") or [])
            if modes:
                if not args.dry_run:
                    importer._ensure_vehicle_modes(db, veh, modes)
                n_mode += len(modes)

            orders = list(meta.get("orders") or [])
            if orders:
                if not args.dry_run:
                    importer._ensure_test_orders(db, veh, orders)
                n_order += len(orders)

        if args.dry_run:
            db.rollback()
            print("\n[dry-run] 属性 %d 项 / 模式 %d 个 / 单号 %d 条 / 新建车 %d 台，未写库"
                  % (n_attr, n_mode, n_order, n_new))
        else:
            db.commit()
            print("\n完成：属性 %d 项 / 模式 %d 个 / 单号 %d 条 / 新建车 %d 台"
                  % (n_attr, n_mode, n_order, n_new))
        if n_skip:
            print("跳过 %d 台（库里没有，加 --create-missing 可创建）" % n_skip)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
