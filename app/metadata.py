"""默认元数据 — 字段注册表、维度、导航树配置、数据格式。

这里放的是**配置数据**，不是表结构。新增车辆字段 / 新层级 / 新数据格式
时只加一行配置（或让导入器按 CSV 表头自动注册），不改 models.py。

所有写入都是幂等的：按 ``code`` 判断，已存在就跳过，不会覆盖用户改过的值。
"""
from __future__ import annotations

from typing import Any, Dict, List

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models import AttrDef, DatasetType, DimEntry, TreeProfile


# ── 字段注册表 ─────────────────────────────────────────────────────────
#
# value_type : text / number / bool / date / dim
# dim_code   : 仅 value_type='dim' 时有意义，指向 dim_entries.dim_code
# options    : 非维度字段的枚举候选（前端直接渲染下拉）
#
# 同一 dim_code 下的多个字段按 sort_order 排成层级：
#   brand     (10) → sub_brand     (11)
#   platform  (20) → sub_platform  (21)
# 车辆可以只填最深一层，读树时沿 dim_entries.parent_id 补齐上层。
DEFAULT_ATTR_DEFS: List[Dict[str, Any]] = [
    {"code": "platform", "name": "平台", "value_type": "dim",
     "dim_code": "platform", "sort_order": 20, "show_in_list": True},
    {"code": "sub_platform", "name": "子平台", "value_type": "dim",
     "dim_code": "platform", "sort_order": 21, "show_in_list": True},
    {"code": "brand", "name": "品牌", "value_type": "dim",
     "dim_code": "brand", "sort_order": 10, "show_in_list": True},
    {"code": "sub_brand", "name": "子品牌", "value_type": "dim",
     "dim_code": "brand", "sort_order": 11, "show_in_list": True},
    {"code": "vehicle_type", "name": "车辆类型", "value_type": "text",
     "options": ["轿车", "SUV", "MPV", "皮卡", "跑车", "其它"],
     "sort_order": 30, "show_in_list": True},
    {"code": "production_date", "name": "生产日期", "value_type": "date",
     "sort_order": 40, "show_in_list": True},
    {"code": "model_year", "name": "年款", "value_type": "number",
     "sort_order": 41, "show_in_list": True},
    {"code": "vin", "name": "VIN号", "value_type": "text",
     "sort_order": 50, "show_in_list": False},
    {"code": "front_suspension", "name": "前悬类型", "value_type": "text",
     "options": ["麦弗逊", "双叉臂", "多连杆", "整体桥", "其它"],
     "sort_order": 60, "show_in_list": True},
    {"code": "rear_suspension", "name": "后悬类型", "value_type": "text",
     "options": ["多连杆", "扭力梁", "整体桥", "双叉臂", "其它"],
     "sort_order": 61, "show_in_list": True},
    {"code": "energy_type", "name": "能源形式", "value_type": "text",
     "options": ["纯电", "插电混动", "增程", "燃油", "氢燃料", "其它"],
     "sort_order": 70, "show_in_list": True},
    {"code": "drive_type", "name": "驱动形式", "value_type": "text",
     "options": ["前驱", "后驱", "四驱"],
     "sort_order": 71, "show_in_list": True},
    {"code": "department", "name": "所属部门", "value_type": "text",
     "sort_order": 80, "show_in_list": False},
    {"code": "sales_region", "name": "销售地", "value_type": "text",
     "sort_order": 90, "show_in_list": False},
]


# ── 导航树配置 ─────────────────────────────────────────────────────────
#
# source 只有三种：
#   attr       引用 attr_defs.code
#   vehicle    车名
#   test_mode  车辆试验模式（舒适 / 运动 / …）
#
# missing 决定该车没有此字段时的行为：
#   skip       跳过这一层（子品牌、子平台这类可选字段用这个）
#   label      生成一个占位节点（empty_label）
DEFAULT_TREE_PROFILES: List[Dict[str, Any]] = [
    {
        "code": "default",
        "name": "平台 → 品牌 → 车 → 模式",
        "is_default": True,
        "levels": [
            {"source": "attr", "key": "platform", "missing": "skip"},
            {"source": "attr", "key": "sub_platform", "missing": "skip"},
            {"source": "attr", "key": "brand", "missing": "skip"},
            {"source": "attr", "key": "sub_brand", "missing": "skip"},
            {"source": "vehicle", "label": "车名"},
            {"source": "test_mode", "label": "试验模式",
             "missing": "label", "empty_label": "未分模式"},
        ],
    },
    {
        "code": "brand_first",
        "name": "品牌 → 平台 → 车 → 模式",
        "is_default": False,
        "levels": [
            {"source": "attr", "key": "brand", "missing": "skip"},
            {"source": "attr", "key": "sub_brand", "missing": "skip"},
            {"source": "attr", "key": "platform", "missing": "skip"},
            {"source": "attr", "key": "sub_platform", "missing": "skip"},
            {"source": "vehicle", "label": "车名"},
            {"source": "test_mode", "label": "试验模式",
             "missing": "label", "empty_label": "未分模式"},
        ],
    },
    {
        "code": "flat",
        "name": "车 → 模式（平铺）",
        "is_default": False,
        "levels": [
            {"source": "vehicle", "label": "车名"},
            {"source": "test_mode", "label": "试验模式",
             "missing": "label", "empty_label": "未分模式"},
        ],
    },
]


# ── 数据格式注册 ───────────────────────────────────────────────────────
DEFAULT_DATASET_TYPES: List[Dict[str, Any]] = [
    {"code": "handling_stability", "name": "操稳数据",
     "description": "指标 + 计算曲线 / 对齐曲线"},
    {"code": "ride_comfort", "name": "平顺数据",
     "description": "预留：平顺试验数据格式"},
    {"code": "generic", "name": "通用数据",
     "description": "无法归入专用格式的结构，放在 datasets.payload"},
]


# ── 写入 ───────────────────────────────────────────────────────────────

def seed_metadata(db: Session) -> Dict[str, int]:
    """幂等写入默认元数据，返回本次新增的条数。"""
    added = {"attrs": 0, "tree_profiles": 0, "dataset_types": 0}

    existing_attrs = {a.code for a in db.query(AttrDef).all()}
    for item in DEFAULT_ATTR_DEFS:
        if item["code"] in existing_attrs:
            continue
        db.add(AttrDef(**item))
        existing_attrs.add(item["code"])
        added["attrs"] += 1

    existing_profiles = {p.code for p in db.query(TreeProfile).all()}
    for item in DEFAULT_TREE_PROFILES:
        if item["code"] in existing_profiles:
            continue
        db.add(TreeProfile(**item))
        existing_profiles.add(item["code"])
        added["tree_profiles"] += 1

    existing_types = {t.code for t in db.query(DatasetType).all()}
    for item in DEFAULT_DATASET_TYPES:
        if item["code"] in existing_types:
            continue
        db.add(DatasetType(**item))
        existing_types.add(item["code"])
        added["dataset_types"] += 1

    db.commit()
    return added


def clear_metadata(db: Session) -> None:
    """清空配置层（仅 replace 导入 / 重建演示库时使用）。"""
    db.execute(delete(DimEntry))
    db.execute(delete(TreeProfile))
    db.execute(delete(DatasetType))
    db.execute(delete(AttrDef))
    db.commit()
