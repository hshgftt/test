"""CRUD 层 — 全部查询都在这里，元数据驱动。

核心概念
========

* **车辆属性**：``attr_defs`` 定义字段，``vehicle_attr_values`` 存值。
  读的时候一次性把每台车的属性摊平成 ``{code: {name, value, …}}``，
  新增字段不需要改这里的代码。

* **分析单元 segment**：``(vehicle_id, mode_id)``。导航树的叶子就是
  segment，对比 / 取数 / 统计都以它为单位。

* **导航树投影**：``tree_profiles.levels`` 只声明层级，树节点在运行时
  按车辆实际数据生成，不落库。层级顺序、数量、来源都可以变。

* **数据信封**：``datasets`` 一条数据一行，标量在 ``metric_values``，
  曲线在 ``curves``（一条曲线一行，series 整块 JSONB）。曲线过滤和
  ``meta_only`` 都下推到 SQL。
"""
from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

from sqlalchemy import and_, exists, false, func, or_, select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.models import (
    AttrDef,
    Condition,
    Curve,
    Dataset,
    DatasetType,
    DimEntry,
    MetricValue,
    TestOrder,
    TreeProfile,
    Vehicle,
    VehicleAttrValue,
    VehicleMode,
)


# ═══════════════════════════════════════════════════════════════════════
# 元数据
# ═══════════════════════════════════════════════════════════════════════

def _attr_defs(db: Session, active_only: bool = True) -> List[AttrDef]:
    stmt = select(AttrDef).order_by(AttrDef.sort_order, AttrDef.id)
    if active_only:
        stmt = stmt.where(AttrDef.is_active.is_(True))
    return list(db.execute(stmt).scalars().all())


def _attr_levels(attrs: Sequence[AttrDef]) -> Dict[str, int]:
    """``attr.code`` → 它在所属 dim_code 里的层级（0 开始）。

    同一 dim_code 下的字段按 ``sort_order`` 定义层级：品牌(0) → 子品牌(1)。
    这是一种配置约定，不需要额外的表字段。
    """
    groups: Dict[str, List[str]] = {}
    for a in attrs:
        if a.value_type == "dim" and a.dim_code:
            groups.setdefault(a.dim_code, []).append(a.code)
    levels: Dict[str, int] = {}
    for _dim, codes in groups.items():
        for i, code in enumerate(codes):
            levels[code] = i
    return levels


def get_meta(db: Session) -> Dict[str, Any]:
    """所有前端需要的配置：字段、维度、树配置、数据格式、工况、模式建议。"""
    attrs = _attr_defs(db)

    dims: Dict[str, List[Dict[str, Any]]] = {}
    for e in db.execute(
        select(DimEntry).where(DimEntry.is_active.is_(True))
        .order_by(DimEntry.dim_code, DimEntry.sort_order, DimEntry.name)
    ).scalars():
        dims.setdefault(e.dim_code, []).append({
            "id": e.id, "dim_code": e.dim_code, "name": e.name,
            "parent_id": e.parent_id, "path": e.path,
            "sort_order": e.sort_order,
        })

    profiles = [
        {
            "code": p.code, "name": p.name, "levels": p.levels or [],
            "is_default": p.is_default,
        }
        for p in db.execute(select(TreeProfile).order_by(TreeProfile.id)).scalars()
    ]

    types = [
        {"code": t.code, "name": t.name, "description": t.description}
        for t in db.execute(
            select(DatasetType).where(DatasetType.is_active.is_(True))
            .order_by(DatasetType.id)
        ).scalars()
    ]

    modes = [
        r[0] for r in db.execute(
            select(VehicleMode.mode).distinct().order_by(VehicleMode.mode)
        )
    ]

    return {
        "attrs": [
            {
                "id": a.id, "code": a.code, "name": a.name,
                "value_type": a.value_type, "dim_code": a.dim_code,
                "unit": a.unit, "required": a.required,
                "show_in_list": a.show_in_list, "options": a.options,
                "sort_order": a.sort_order,
            }
            for a in attrs
        ],
        "dims": dims,
        "tree_profiles": profiles,
        "dataset_types": types,
        "conditions": get_conditions(db),
        "mode_suggestions": modes,
    }


def _profile_dict(p: TreeProfile) -> Dict[str, Any]:
    return {
        "code": p.code, "name": p.name, "levels": p.levels or [],
        "is_default": p.is_default,
    }


def create_tree_profile(db: Session, data: Dict[str, Any]) -> Dict[str, Any]:
    code = str(data.get("code") or "").strip()
    if not code:
        raise ValueError("profile code 不能为空")
    if db.execute(
        select(TreeProfile).where(TreeProfile.code == code)
    ).scalars().first():
        raise ValueError("profile code 已存在：%s" % code)
    levels = data.get("levels") or []
    if not isinstance(levels, list):
        raise ValueError("levels 必须是数组")
    profile = TreeProfile(
        code=code,
        name=str(data.get("name") or code),
        levels=levels,
        is_default=bool(data.get("is_default", False)),
    )
    if profile.is_default:
        for p in db.execute(select(TreeProfile)).scalars():
            p.is_default = False
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return _profile_dict(profile)


def update_tree_profile(
    db: Session, code: str, data: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    profile = db.execute(
        select(TreeProfile).where(TreeProfile.code == code)
    ).scalars().first()
    if profile is None:
        return None
    if "name" in data and data.get("name"):
        profile.name = str(data["name"])
    if "levels" in data:
        levels = data.get("levels") or []
        if not isinstance(levels, list):
            raise ValueError("levels 必须是数组")
        profile.levels = levels
    if "is_default" in data:
        profile.is_default = bool(data.get("is_default"))
        if profile.is_default:
            for p in db.execute(select(TreeProfile)).scalars():
                if p.id != profile.id:
                    p.is_default = False
    db.commit()
    db.refresh(profile)
    return _profile_dict(profile)


# ═══════════════════════════════════════════════════════════════════════
# 车辆属性读取
# ═══════════════════════════════════════════════════════════════════════

def _chain_ids(entry: DimEntry, dim_map: Dict[int, DimEntry]) -> List[int]:
    """从根到该节点的 id 链（按 path + 自身 id 还原）。"""
    ids: List[int] = []
    parts = [p for p in (entry.path or "").split("/") if p]
    for p in parts:
        try:
            i = int(p)
        except ValueError:
            continue
        if i != entry.id and i in dim_map:
            ids.append(i)
    if not ids or ids[-1] != entry.id:
        # path 没写全时沿 parent_id 兜底
        cur: Optional[DimEntry] = entry
        rev: List[int] = []
        seen: Set[int] = set()
        while cur is not None and cur.id not in seen:
            seen.add(cur.id)
            rev.append(cur.id)
            cur = dim_map.get(cur.parent_id) if cur.parent_id else None
        ids = list(reversed(rev))
    return ids


def load_vehicle_attrs(
    db: Session,
    vehicle_ids: Optional[Sequence[int]] = None,
) -> Dict[int, Dict[str, Dict[str, Any]]]:
    """一次性读出多台车的属性：``{vehicle_id: {code: value_dict}}``。

    ``value_dict``::

        {"code": "brand", "name": "品牌", "value": "星河", "value_id": 3,
         "unit": null, "value_type": "dim", "sort_order": 10}
    """
    attrs = _attr_defs(db)
    attr_by_id = {a.id: a for a in attrs}
    levels = _attr_levels(attrs)

    dim_map: Dict[int, DimEntry] = {
        e.id: e for e in db.execute(select(DimEntry)).scalars().all()
    }

    stmt = select(VehicleAttrValue)
    if vehicle_ids is not None:
        ids = list(vehicle_ids)
        if not ids:
            return {}
        stmt = stmt.where(VehicleAttrValue.vehicle_id.in_(ids))
    rows = list(db.execute(stmt).scalars().all())

    out: Dict[int, Dict[str, Dict[str, Any]]] = {}
    for row in rows:
        attr = attr_by_id.get(row.attr_id)
        if attr is None:
            continue
        bucket = out.setdefault(row.vehicle_id, {})

        value: Any = None
        value_id: Optional[int] = None
        sort_order = 0

        if attr.value_type == "dim" and row.value_dim_id:
            entry = dim_map.get(row.value_dim_id)
            if entry is not None:
                chain = _chain_ids(entry, dim_map)
                lv = levels.get(attr.code, 0)
                # 车辆只填了深一层的节点（如只写子品牌、没写品牌）时，
                # chain 会比 lv 短；此时直接取该节点自身，别把值丢掉。
                node = dim_map.get(chain[lv]) if lv < len(chain) else entry
                if node is not None:
                    value = node.name
                    value_id = node.id
                    sort_order = node.sort_order
        elif attr.value_type in ("number", "bool"):
            value = row.value_num
        else:
            value = row.value_text

        if value is None or value == "":
            continue
        bucket[attr.code] = {
            "code": attr.code, "name": attr.name, "value": value,
            "value_id": value_id, "unit": attr.unit,
            "value_type": attr.value_type, "sort_order": sort_order,
        }
    return out


def _vehicle_modes(db: Session, vehicle_ids: Sequence[int]) -> Dict[int, List[VehicleMode]]:
    out: Dict[int, List[VehicleMode]] = {}
    if not vehicle_ids:
        return out
    for m in db.execute(
        select(VehicleMode).where(VehicleMode.vehicle_id.in_(list(vehicle_ids)))
        .order_by(VehicleMode.sort_order, VehicleMode.mode)
    ).scalars():
        out.setdefault(m.vehicle_id, []).append(m)
    return out


# ═══════════════════════════════════════════════════════════════════════
# 导航树投影
# ═══════════════════════════════════════════════════════════════════════

def _profile_of(db: Session, code: Optional[str]) -> Optional[TreeProfile]:
    if code:
        return db.execute(
            select(TreeProfile).where(TreeProfile.code == code)
        ).scalars().first()
    return db.execute(
        select(TreeProfile).where(TreeProfile.is_default.is_(True)).order_by(TreeProfile.id)
    ).scalars().first() or db.execute(
        select(TreeProfile).order_by(TreeProfile.id)
    ).scalars().first()


def _filter_vehicle_ids(
    attrs_map: Dict[int, Dict[str, Dict[str, Any]]],
    filters: Dict[str, str],
) -> Set[int]:
    ids = set(attrs_map.keys())
    for code, want in (filters or {}).items():
        if want is None or want == "":
            continue
        ids = {
            vid for vid in ids
            if str((attrs_map.get(vid, {}).get(code) or {}).get("value") or "") == str(want)
        }
    return ids


def get_nav_tree(
    db: Session,
    profile_code: Optional[str] = None,
    filters: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """按 tree_profile 投影出一棵任意层级的导航树。

    返回::

        {"profile": {…}, "levels": […], "nodes": [{"key","label","kind",…}]}

    叶子节点 ``kind='segment'``，带 ``segment={vehicle_id, mode_id, …}``。
    层级数量、顺序、来源全部由 ``tree_profiles.levels`` 决定，这里的代码
    不含任何「平台 / 品牌 / 车」的假设。
    """
    profile = _profile_of(db, profile_code)
    if profile is None:
        return {"profile": None, "levels": [], "nodes": []}
    levels: List[Dict[str, Any]] = list(profile.levels or [])

    vehicles = list(db.execute(select(Vehicle).order_by(Vehicle.name)).scalars().all())
    vids = [v.id for v in vehicles]
    attrs_map = load_vehicle_attrs(db, vids)
    modes_map = _vehicle_modes(db, vids)

    allow = _filter_vehicle_ids(attrs_map, filters or {}) if filters else None

    roots: List[Dict[str, Any]] = []
    root_index: Dict[str, Dict[str, Any]] = {}
    wants_modes = any(lv.get("source") == "test_mode" for lv in levels)
    tm_level = next((lv for lv in levels if lv.get("source") == "test_mode"), {})
    mode_missing = tm_level.get("missing", "label")

    for v in vehicles:
        if allow is not None and v.id not in allow:
            continue
        vattrs = attrs_map.get(v.id, {})
        modes = modes_map.get(v.id, [])

        mode_choices: List[Tuple[Optional[VehicleMode], Dict[str, Any]]] = []
        if wants_modes:
            if modes:
                for m in modes:
                    mode_choices.append((m, {"mode": m.mode, "mode_id": m.id}))
            elif mode_missing == "skip":
                continue
            else:
                mode_choices.append((
                    None,
                    {"mode": tm_level.get("empty_label", "未分模式"), "mode_id": None},
                ))
        else:
            mode_choices.append((None, {"mode": None, "mode_id": None}))

        for mode_obj, mode_part in mode_choices:
            parent: Optional[Dict[str, Any]] = None
            container: Dict[str, Dict[str, Any]] = root_index
            parent_children: List[Dict[str, Any]] = roots
            ok = True

            for li, lv in enumerate(levels):
                parts = _level_parts(lv, li, v, vattrs, mode_obj, mode_part)
                if parts is None:
                    ok = False
                    break
                label, sort_key, kind, extra = parts
                key = "%d|%s|%s|%s" % (li, kind, extra.get("code") or "", label)
                node = container.get(key)
                if node is None:
                    node = {
                        "key": key, "label": label, "kind": kind,
                        "level": li, "children": [], "count": 0,
                        "sort_order": sort_key,
                    }
                    node.update(extra)
                    container[key] = node
                    parent_children.append(node)
                parent = node
                container = node.setdefault("_index", {})
                parent_children = node["children"]

            if not ok:
                continue

            seg = {
                "vehicle_id": v.id, "vehicle_name": v.name,
                "mode_id": mode_part.get("mode_id"),
                "mode": mode_part.get("mode"),
            }
            seg_key = "seg|%d|%s" % (v.id, mode_part.get("mode_id"))
            if levels:
                if parent is None:
                    continue
                target_children = parent["children"]
                target_index = parent.setdefault("_index", {})
                level_no = len(levels)
                label = mode_part.get("mode") or v.name
            else:
                # 没有配置任何层级：树就是 segment 平铺
                target_children = roots
                target_index = root_index
                level_no = 0
                label = ("%s · %s" % (v.name, mode_part["mode"])
                         if mode_part.get("mode") else v.name)
            if seg_key in target_index:
                continue
            seg_node = {
                "key": seg_key,
                "label": label,
                "kind": "segment",
                "level": level_no,
                "children": [],
                "count": 1,
                "sort_order": 0,
                "segment": seg,
                "vehicle_id": v.id,
                "vehicle_name": v.name,
                "mode_id": mode_part.get("mode_id"),
                "mode": mode_part.get("mode"),
                "attrs": {code: val.get("value") for code, val in vattrs.items()},
            }
            target_children.append(seg_node)
            target_index[seg_key] = seg_node

    def finalize(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        for n in nodes:
            n.pop("_index", None)
            kids = n.get("children") or []
            if kids:
                n["children"] = finalize(kids)
                n["count"] = sum(k.get("count") or 0 for k in n["children"])
        return sorted(
            nodes,
            key=lambda n: (n.get("sort_order") or 0, n.get("label") or ""),
        )

    return {
        "profile": {
            "code": profile.code, "name": profile.name,
            "levels": levels, "is_default": profile.is_default,
        },
        "levels": levels,
        "nodes": finalize(roots),
    }


def _level_parts(
    level: Dict[str, Any],
    li: int,
    vehicle: Vehicle,
    vattrs: Dict[str, Dict[str, Any]],
    mode_obj: Optional[VehicleMode],
    mode_part: Dict[str, Any],
) -> Optional[Tuple[str, Any, str, Dict[str, Any]]]:
    source = level.get("source")
    if source == "vehicle":
        return (vehicle.name, 0, "vehicle", {"vehicle_id": vehicle.id, "vehicle_name": vehicle.name})
    if source == "test_mode":
        if mode_obj is None and level.get("missing", "label") == "skip":
            return None
        label = mode_part.get("mode") or level.get("empty_label", "未分模式")
        return (label, 0, "test_mode", {"mode_id": mode_part.get("mode_id"), "mode": label})
    if source == "attr":
        code = level.get("key") or ""
        item = vattrs.get(code)
        if item is None or item.get("value") in (None, ""):
            if level.get("missing", "skip") == "skip":
                return None
            label = level.get("empty_label") or "未填写"
            return (label, 0, "attr", {"code": code})
        return (
            str(item["value"]), item.get("sort_order") or 0, "attr",
            {"code": code, "value_id": item.get("value_id")},
        )
    return None


# ═══════════════════════════════════════════════════════════════════════
# 维度字典写入
# ═══════════════════════════════════════════════════════════════════════

def resolve_dim_entry(
    db: Session,
    dim_code: str,
    name: str,
    parent_id: Optional[int] = None,
    sort_order: int = 0,
) -> DimEntry:
    """按 (dim_code, parent_id, name) 找到或创建维度节点，并维护 path。"""
    name = str(name).strip()
    q = select(DimEntry).where(
        DimEntry.dim_code == dim_code,
        DimEntry.name == name,
        DimEntry.parent_id.is_(None) if parent_id is None else DimEntry.parent_id == parent_id,
    )
    entry = db.execute(q).scalars().first()
    if entry is not None:
        return entry

    parent = db.get(DimEntry, parent_id) if parent_id else None
    entry = DimEntry(
        dim_code=dim_code, name=name, parent_id=parent_id,
        path=(parent.path if parent else "/"), sort_order=sort_order,
    )
    db.add(entry)
    db.flush()
    entry.path = ((parent.path if parent else "/") + "%d/" % entry.id)
    db.flush()
    return entry


def _dim_parent_entry(
    db: Session,
    vehicle: Vehicle,
    attr: AttrDef,
    attrs: Sequence[AttrDef],
    levels: Dict[str, int],
) -> Optional[DimEntry]:
    """找到本字段上一级（同 dim_code、level-1）的维度节点。"""
    lv = levels.get(attr.code, 0)
    if lv <= 0:
        return None
    siblings = [
        a for a in attrs
        if a.value_type == "dim" and a.dim_code == attr.dim_code
    ]
    if lv - 1 >= len(siblings):
        return None
    parent_attr = siblings[lv - 1]
    row = db.execute(
        select(VehicleAttrValue).where(
            VehicleAttrValue.vehicle_id == vehicle.id,
            VehicleAttrValue.attr_id == parent_attr.id,
        )
    ).scalars().first()
    if row is None or not row.value_dim_id:
        return None
    return db.get(DimEntry, row.value_dim_id)


def _parse_number(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def set_vehicle_attrs(
    db: Session,
    vehicle: Vehicle,
    values: Dict[str, Any],
) -> None:
    """按 ``{code: value}`` 写入车辆属性。

    * ``dim`` 字段：value 是维度名称，必要时自动创建节点（含父子链）。
    * ``number / bool``：写 ``value_num``。
    * 其它：写 ``value_text``。
    传空值（None / ""）表示删除该字段。
    """
    attrs = _attr_defs(db)
    attr_by_code = {a.code: a for a in attrs}
    level_map = _attr_levels(attrs)

    existing = {
        r.attr_id: r for r in db.execute(
            select(VehicleAttrValue).where(VehicleAttrValue.vehicle_id == vehicle.id)
        ).scalars()
    }

    # 按 sort_order 处理，保证父级维度先落库
    for attr in attrs:
        if attr.code not in values:
            continue
        raw = values[attr.code]
        row = existing.get(attr.id)

        if raw is None or (isinstance(raw, str) and not raw.strip()):
            if row is not None:
                db.delete(row)
            continue

        if attr.value_type == "dim":
            parent = _dim_parent_entry(db, vehicle, attr, attrs, level_map)
            entry = resolve_dim_entry(
                db, attr.dim_code or "", str(raw), parent.id if parent else None,
                sort_order=attr.sort_order,
            )
            if row is None:
                row = VehicleAttrValue(vehicle_id=vehicle.id, attr_id=attr.id)
                db.add(row)
                existing[attr.id] = row
            row.value_dim_id = entry.id
            row.value_text = entry.name
            row.value_num = None
        elif attr.value_type == "number":
            num = _parse_number(raw)
            if row is None:
                row = VehicleAttrValue(vehicle_id=vehicle.id, attr_id=attr.id)
                db.add(row)
                existing[attr.id] = row
            row.value_num = num
            row.value_text = None if num is None else str(raw)
            row.value_dim_id = None
        elif attr.value_type == "bool":
            num = 1.0 if str(raw).strip().lower() in ("1", "true", "yes", "是", "y") else 0.0
            if row is None:
                row = VehicleAttrValue(vehicle_id=vehicle.id, attr_id=attr.id)
                db.add(row)
                existing[attr.id] = row
            row.value_num = num
            row.value_text = str(raw)
            row.value_dim_id = None
        else:
            if isinstance(raw, (date, datetime)):
                raw = raw.isoformat()
            if row is None:
                row = VehicleAttrValue(vehicle_id=vehicle.id, attr_id=attr.id)
                db.add(row)
                existing[attr.id] = row
            row.value_text = str(raw)
            row.value_num = None
            row.value_dim_id = None

    db.flush()


def sync_vehicle_modes(db: Session, vehicle: Vehicle, modes: Iterable[str]) -> None:
    """把车辆模式同步成给定列表（新增 / 删除）。"""
    wanted: List[str] = []
    for m in modes or []:
        name = str(m).strip()
        if name and name not in wanted:
            wanted.append(name)

    existing = {
        m.mode: m for m in db.execute(
            select(VehicleMode).where(VehicleMode.vehicle_id == vehicle.id)
        ).scalars()
    }
    for name in wanted:
        if name not in existing:
            db.add(VehicleMode(
                vehicle_id=vehicle.id, mode=name, sort_order=len(existing),
            ))
            existing[name] = None
    for name, obj in list(existing.items()):
        if obj is not None and name not in wanted:
            db.delete(obj)
    db.flush()


def sync_test_orders(db: Session, vehicle: Vehicle, orders: Iterable[Any]) -> None:
    """同步试验单号。``orders`` 支持 ``["CS-001"]`` 或 ``[{discipline, order_no}]``。"""
    wanted: List[Tuple[str, str]] = []
    for o in orders or []:
        if isinstance(o, dict):
            discipline = str(o.get("discipline") or "操稳").strip()
            no = str(o.get("order_no") or "").strip()
        else:
            discipline, no = "操稳", str(o).strip()
        if no and (discipline, no) not in wanted:
            wanted.append((discipline, no))

    existing = {
        (o.discipline, o.order_no): o for o in db.execute(
            select(TestOrder).where(TestOrder.vehicle_id == vehicle.id)
        ).scalars()
    }
    for discipline, no in wanted:
        if (discipline, no) not in existing:
            db.add(TestOrder(
                vehicle_id=vehicle.id, discipline=discipline, order_no=no,
            ))
            existing[(discipline, no)] = None
    for key, obj in list(existing.items()):
        if obj is not None and key not in wanted:
            db.delete(obj)
    db.flush()


# ═══════════════════════════════════════════════════════════════════════
# 车辆详情 / 增删改
# ═══════════════════════════════════════════════════════════════════════

def get_vehicle_detail(db: Session, vehicle_id: int) -> Optional[Dict[str, Any]]:
    vehicle = db.get(Vehicle, vehicle_id)
    if vehicle is None:
        return None
    attrs = load_vehicle_attrs(db, [vehicle_id]).get(vehicle_id, {})
    modes = db.execute(
        select(VehicleMode).where(VehicleMode.vehicle_id == vehicle_id)
        .order_by(VehicleMode.sort_order, VehicleMode.mode)
    ).scalars().all()
    orders = db.execute(
        select(TestOrder).where(TestOrder.vehicle_id == vehicle_id)
        .order_by(TestOrder.discipline, TestOrder.order_no)
    ).scalars().all()
    n_datasets = int(db.execute(
        select(func.count(Dataset.id)).where(Dataset.vehicle_id == vehicle_id)
    ).scalar() or 0)
    return {
        "id": vehicle.id, "name": vehicle.name,
        "attrs": attrs,
        "modes": [{"id": m.id, "mode": m.mode, "sort_order": m.sort_order} for m in modes],
        "test_orders": [
            {"id": o.id, "discipline": o.discipline, "order_no": o.order_no}
            for o in orders
        ],
        "dataset_count": n_datasets,
    }


def create_vehicle(db: Session, data: Dict[str, Any]) -> Dict[str, Any]:
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError("车名不能为空")
    if db.execute(select(Vehicle).where(Vehicle.name == name)).scalars().first():
        raise ValueError("车名已存在：%s" % name)

    vehicle = Vehicle(name=name)
    db.add(vehicle)
    db.flush()
    set_vehicle_attrs(db, vehicle, data.get("attrs") or {})
    sync_vehicle_modes(db, vehicle, data.get("modes") or [])
    sync_test_orders(db, vehicle, data.get("test_orders") or [])
    db.commit()
    return get_vehicle_detail(db, vehicle.id) or {}


def update_vehicle(db: Session, vehicle_id: int, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    vehicle = db.get(Vehicle, vehicle_id)
    if vehicle is None:
        return None

    if "name" in data:
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("车名不能为空")
        other = db.execute(
            select(Vehicle).where(Vehicle.name == name, Vehicle.id != vehicle_id)
        ).scalars().first()
        if other:
            raise ValueError("车名已存在：%s" % name)
        vehicle.name = name

    if "attrs" in data:
        set_vehicle_attrs(db, vehicle, data.get("attrs") or {})
    if "modes" in data:
        sync_vehicle_modes(db, vehicle, data.get("modes") or [])
    if "test_orders" in data:
        sync_test_orders(db, vehicle, data.get("test_orders") or [])

    db.commit()
    return get_vehicle_detail(db, vehicle_id)


def delete_vehicle(db: Session, vehicle_id: int) -> bool:
    vehicle = db.get(Vehicle, vehicle_id)
    if vehicle is None:
        return False
    db.delete(vehicle)
    db.commit()
    return True


# ═══════════════════════════════════════════════════════════════════════
# 工况
# ═══════════════════════════════════════════════════════════════════════

def get_conditions(
    db: Session,
    vehicle_id: Optional[int] = None,
    mode_id: Optional[int] = None,
    dataset_type: Optional[str] = None,
) -> List[Dict[str, Any]]:
    stmt = select(Condition).order_by(Condition.name)
    if vehicle_id is not None or mode_id is not None or dataset_type:
        sub = select(Dataset.condition_id)
        if vehicle_id is not None:
            sub = sub.where(Dataset.vehicle_id == vehicle_id)
        if mode_id is not None:
            sub = sub.where(Dataset.mode_id == mode_id)
        if dataset_type:
            sub = sub.join(DatasetType, DatasetType.id == Dataset.dataset_type_id).where(
                DatasetType.code == dataset_type
            )
        stmt = stmt.where(Condition.id.in_(sub.distinct()))
    return [
        {
            "id": c.id, "name": c.name,
            "description": c.description, "category": c.category,
        }
        for c in db.execute(stmt).scalars()
    ]


def get_curve_names(
    db: Session,
    condition_id: int,
    dataset_type: Optional[str] = None,
) -> List[str]:
    stmt = (
        select(Curve.name).distinct()
        .join(Dataset, Dataset.id == Curve.dataset_id)
        .where(Dataset.condition_id == condition_id)
        .order_by(Curve.name)
    )
    if dataset_type:
        stmt = stmt.join(DatasetType, DatasetType.id == Dataset.dataset_type_id).where(
            DatasetType.code == dataset_type
        )
    return [r[0] for r in db.execute(stmt)]


# ═══════════════════════════════════════════════════════════════════════
# 数据集读取（单 segment / 多 segment）
# ═══════════════════════════════════════════════════════════════════════

def _series_stats(series: Sequence[Dict[str, Any]]) -> Tuple[List[str], Dict[str, Any]]:
    names: List[str] = []
    smap: Dict[str, Any] = {}
    for idx, s in enumerate(series or []):
        nm = (s or {}).get("curvename") or "series%d" % idx
        if nm in smap:
            continue
        names.append(nm)
        smap[nm] = {
            "index": idx,
            "point_count": len((s or {}).get("x") or []),
        }
    return names, smap


_CURVE_META_COLS = (
    Curve.id,
    Curve.dataset_id,
    Curve.group,
    Curve.name,
    Curve.title,
    Curve.xlabel,
    Curve.ylabel,
    Curve.point_count,
    Curve.sort_order,
)


def _curve_payload(cur: Any, meta_only: bool = False) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "name": cur.name,
        "title": cur.title,
        "xlabel": cur.xlabel,
        "ylabel": cur.ylabel,
        "point_count": cur.point_count or 0,
    }
    if meta_only:
        payload["series"] = []
        payload["series_names"] = []
        return payload
    series = cur.series or []
    n = cur.point_count
    if n is None:
        n = len(series[0].get("x", [])) if series else 0
    s_names, s_map = _series_stats(series)
    payload["point_count"] = n
    payload["series_names"] = s_names
    payload["series_map"] = s_map
    payload["series"] = series
    return payload


def _dataset_payload(
    ds: Dataset,
    curve_names: Optional[Set[str]] = None,
    meta_only: bool = False,
) -> Dict[str, Any]:
    params: Dict[str, Dict[str, Any]] = {}
    for m in sorted(ds.metrics, key=lambda x: (x.name, x.raw_key)):
        params[m.raw_key] = {
            "name": m.name, "unit": m.unit, "raw_key": m.raw_key,
            "value": m.value, "text_value": m.text_value,
        }

    groups: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for cur in sorted(ds.curves, key=lambda c: c.sort_order):
        if curve_names is not None and cur.name not in curve_names:
            continue
        groups.setdefault(cur.group, {})[cur.name] = _curve_payload(cur, meta_only)

    return {
        "dataset_id": ds.id,
        "exp_no": ds.exp_no,
        "is_average": bool(ds.is_average),
        "name": ds.name,
        "dataset_type": ds.dataset_type.code if ds.dataset_type else None,
        "payload": ds.payload,
        "params": params,
        # 不再写死 cal / duiqi：group 是数据驱动的，新格式可以自带新分组
        "curves": groups,
    }


def _curve_option(curve_names: Optional[Set[str]] = None, meta_only: bool = False):
    rel = Dataset.curves
    if curve_names:
        rel = rel.and_(Curve.name.in_(list(curve_names)))
    opt = selectinload(rel)
    if meta_only:
        opt = opt.load_only(*_CURVE_META_COLS)
    return opt


def _apply_exp_scope(stmt, exp_scope: Optional[str]):
    if exp_scope != "avg_first":
        return stmt
    rn = func.row_number().over(
        partition_by=[Dataset.vehicle_id, Dataset.mode_id],
        order_by=[
            func.coalesce(Dataset.is_average, false()).desc(),
            Dataset.sort_order.asc(),
        ],
    ).label("rn")
    inner = stmt.with_only_columns(Dataset.id.label("id"), rn).subquery()
    return (
        select(Dataset)
        .join(inner, inner.c.id == Dataset.id)
        .where(inner.c.rn == 1)
    )


def _segment_clause(segments: Sequence[Tuple[int, Optional[int]]]):
    clauses = []
    for vid, mid in segments:
        if mid is None:
            clauses.append(and_(
                Dataset.vehicle_id == vid, Dataset.mode_id.is_(None),
            ))
        else:
            clauses.append(and_(
                Dataset.vehicle_id == vid, Dataset.mode_id == mid,
            ))
    return or_(*clauses) if clauses else false()


def _load_datasets(
    db: Session,
    segments: Sequence[Tuple[int, Optional[int]]],
    condition_id: int,
    dataset_type: Optional[str] = None,
    exp_nos: Optional[Sequence[str]] = None,
    curve_names: Optional[Set[str]] = None,
    meta_only: bool = False,
    exp_scope: Optional[str] = None,
) -> Dict[Tuple[int, Optional[int]], List[Dataset]]:
    stmt = select(Dataset).where(
        Dataset.condition_id == condition_id,
        _segment_clause(segments),
    )
    if dataset_type:
        stmt = stmt.join(
            DatasetType, DatasetType.id == Dataset.dataset_type_id
        ).where(DatasetType.code == dataset_type)
    if exp_nos:
        stmt = stmt.where(Dataset.exp_no.in_(list(exp_nos)))
    stmt = _apply_exp_scope(stmt, exp_scope).options(
        selectinload(Dataset.metrics),
        joinedload(Dataset.dataset_type),
        _curve_option(curve_names, meta_only),
    ).order_by(Dataset.vehicle_id, Dataset.mode_id, Dataset.sort_order)

    out: Dict[Tuple[int, Optional[int]], List[Dataset]] = {}
    for ds in db.execute(stmt).scalars().all():
        out.setdefault((ds.vehicle_id, ds.mode_id), []).append(ds)
    return out


def _collect_curve_meta(vehicles: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    groups: Dict[str, Any] = {}
    for veh in vehicles:
        for exp in veh.get("experiments") or []:
            for group, cdict in (exp.get("curves") or {}).items():
                g = groups.setdefault(group, {"series_names": [], "curves": {}})
                for cname, cval in (cdict or {}).items():
                    entry = g["curves"].get(cname)
                    if entry is None:
                        entry = {
                            "name": cname,
                            "title": cval.get("title") or cname,
                            "xlabel": cval.get("xlabel"),
                            "ylabel": cval.get("ylabel"),
                            "point_count": cval.get("point_count") or 0,
                            "series_names": [],
                        }
                        g["curves"][cname] = entry
                    for nm in cval.get("series_names") or []:
                        if nm not in entry["series_names"]:
                            entry["series_names"].append(nm)
                        if nm not in g["series_names"]:
                            g["series_names"].append(nm)
    return {"groups": groups}


def _segment_meta(
    db: Session,
    segments: Sequence[Tuple[int, Optional[int]]],
    attrs_map: Optional[Dict[int, Dict[str, Dict[str, Any]]]] = None,
) -> Dict[Tuple[int, Optional[int]], Dict[str, Any]]:
    vids = sorted({vid for vid, _ in segments})
    vehicles = {
        v.id: v for v in db.execute(
            select(Vehicle).where(Vehicle.id.in_(vids))
        ).scalars()
    } if vids else {}
    modes = {
        m.id: m for m in db.execute(
            select(VehicleMode).where(
                VehicleMode.id.in_([mid for _, mid in segments if mid is not None])
            )
        ).scalars()
    } if any(mid is not None for _, mid in segments) else {}
    if attrs_map is None:
        attrs_map = load_vehicle_attrs(db, vids)

    out: Dict[Tuple[int, Optional[int]], Dict[str, Any]] = {}
    for vid, mid in segments:
        v = vehicles.get(vid)
        if v is None:
            continue
        m = modes.get(mid) if mid is not None else None
        out[(vid, mid)] = {
            "vehicle_id": vid,
            "vehicle_name": v.name,
            "mode_id": mid,
            "mode": m.mode if m else None,
            "attrs": attrs_map.get(vid, {}),
        }
    return out


def get_segment_condition_data(
    db: Session,
    vehicle_id: int,
    condition_id: int,
    mode_id: Optional[int] = None,
    dataset_type: Optional[str] = None,
    exp_nos: Optional[Sequence[str]] = None,
    curve_names: Optional[Set[str]] = None,
    meta_only: bool = False,
    exp_scope: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    condition = db.get(Condition, condition_id)
    if condition is None:
        return None
    if db.get(Vehicle, vehicle_id) is None:
        return None

    seg = (vehicle_id, mode_id)
    data = _load_datasets(
        db, [seg], condition_id, dataset_type, exp_nos,
        curve_names, meta_only, exp_scope,
    )
    meta = _segment_meta(db, [seg])[seg]
    payloads = [_dataset_payload(d, curve_names, meta_only) for d in data.get(seg, [])]

    return {
        "vehicle_id": meta["vehicle_id"],
        "vehicle_name": meta["vehicle_name"],
        "mode_id": meta["mode_id"],
        "mode": meta["mode"],
        "attrs": meta["attrs"],
        "condition_id": condition_id,
        "condition_name": condition.name,
        "dataset_type": dataset_type,
        "experiments": payloads,
        "curve_meta": _collect_curve_meta([{"experiments": payloads}]),
    }


def get_compare(
    db: Session,
    segments: Sequence[Tuple[int, Optional[int]]],
    condition_id: int,
    dataset_type: Optional[str] = None,
    exp_nos: Optional[Sequence[str]] = None,
    curve_names: Optional[Set[str]] = None,
    meta_only: bool = False,
    exp_scope: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    condition = db.get(Condition, condition_id)
    if condition is None:
        return None

    metas = _segment_meta(db, segments)
    data = _load_datasets(
        db, segments, condition_id, dataset_type, exp_nos,
        curve_names, meta_only, exp_scope,
    )

    out: List[Dict[str, Any]] = []
    for seg in segments:
        meta = metas.get(seg)
        if meta is None:
            continue
        datasets = data.get(seg, [])
        if not datasets:
            continue
        out.append({
            "vehicle_id": meta["vehicle_id"],
            "vehicle_name": meta["vehicle_name"],
            "mode_id": meta["mode_id"],
            "mode": meta["mode"],
            "attrs": meta["attrs"],
            "experiments": [
                _dataset_payload(d, curve_names, meta_only) for d in datasets
            ],
        })

    return {
        "condition_id": condition_id,
        "condition_name": condition.name,
        "dataset_type": dataset_type,
        "vehicles": out,
        "curve_meta": _collect_curve_meta(out),
    }


# ═══════════════════════════════════════════════════════════════════════
# 流式对比
# ═══════════════════════════════════════════════════════════════════════

STREAM_BATCH_CURVES = int(os.getenv("STREAM_BATCH_CURVES", "24"))


def iter_compare_stream(
    db: Session,
    segments: Sequence[Tuple[int, Optional[int]]],
    condition_id: int,
    dataset_type: Optional[str] = None,
    exp_nos: Optional[Sequence[str]] = None,
    curve_names: Optional[Set[str]] = None,
    exp_scope: Optional[str] = None,
    batch_curves: Optional[int] = None,
) -> Iterator[Dict[str, Any]]:
    """``get_compare`` 的 NDJSON 流式版本，按 segment 产出骨架 + 逐条曲线。"""
    condition = db.get(Condition, condition_id)
    if condition is None:
        yield {"type": "error", "detail": "Condition not found"}
        return

    metas = _segment_meta(db, segments)
    data = _load_datasets(
        db, segments, condition_id, dataset_type, exp_nos,
        curve_names, meta_only=True, exp_scope=exp_scope,
    )

    out: List[Dict[str, Any]] = []
    ds_ids: List[int] = []
    ds2seg: Dict[int, Tuple[int, Optional[int]]] = {}
    for seg in segments:
        meta = metas.get(seg)
        if meta is None:
            continue
        datasets = data.get(seg, [])
        if not datasets:
            continue
        for d in datasets:
            ds_ids.append(d.id)
            ds2seg[d.id] = seg
        out.append({
            "vehicle_id": meta["vehicle_id"],
            "vehicle_name": meta["vehicle_name"],
            "mode_id": meta["mode_id"],
            "mode": meta["mode"],
            "attrs": meta["attrs"],
            "experiments": [
                _dataset_payload(d, curve_names, meta_only=True) for d in datasets
            ],
        })

    yield {
        "type": "meta",
        "condition_id": condition_id,
        "condition_name": condition.name,
        "dataset_type": dataset_type,
        "vehicles": out,
        "curve_meta": _collect_curve_meta(out),
    }

    db.expunge_all()

    step = batch_curves or STREAM_BATCH_CURVES
    n_curves = 0
    n_points = 0

    if ds_ids:
        cstmt = (
            select(Curve.id, Curve.dataset_id, Curve.group)
            .where(Curve.dataset_id.in_(ds_ids))
            .order_by(Curve.group, Curve.name, Curve.dataset_id)
        )
        if curve_names:
            cstmt = cstmt.where(Curve.name.in_(list(curve_names)))
        keys = list(db.execute(cstmt))

        for i in range(0, len(keys), step):
            chunk = keys[i:i + step]
            rows = db.execute(
                select(
                    Curve.id, Curve.name, Curve.title,
                    Curve.xlabel, Curve.ylabel,
                    Curve.point_count, Curve.series,
                ).where(Curve.id.in_([k.id for k in chunk]))
            ).all()
            by_id = {r.id: r for r in rows}
            for k in chunk:
                row = by_id.get(k.id)
                if row is None:
                    continue
                payload = _curve_payload(row)
                n_curves += 1
                n_points += payload["point_count"]
                seg = ds2seg.get(k.dataset_id) or (None, None)
                yield {
                    "type": "curve",
                    "vehicle_id": seg[0],
                    "mode_id": seg[1],
                    "dataset_id": k.dataset_id,
                    "group": k.group,
                    "curve": payload,
                }
            del rows, by_id

    yield {"type": "done", "curves": n_curves, "points": n_points}


# ═══════════════════════════════════════════════════════════════════════
# 指标统计（分页 / 动态字段列）
# ═══════════════════════════════════════════════════════════════════════

def _filter_vehicle_ids_for_stats(
    db: Session,
    filters: Dict[str, str],
) -> Tuple[Optional[List[int]], Dict[int, Dict[str, Dict[str, Any]]]]:
    """按任意 attr 过滤车辆，返回 (允许的车辆 id 或 None, 属性表)。"""
    attrs_map = load_vehicle_attrs(db)
    if not filters:
        return None, attrs_map
    allow = _filter_vehicle_ids(attrs_map, filters)
    return sorted(allow), attrs_map


def _indicator_key_stmt(
    vehicle_ids: Optional[Sequence[int]],
    condition_name: Optional[str],
    indicator_name: Optional[str],
    average_only: bool,
    dataset_type: Optional[str],
    mode_id: Optional[int] = None,
    condition_id: Optional[int] = None,
):
    stmt = (
        select(
            Dataset.id.label("dataset_id"),
            Dataset.exp_no.label("exp_no"),
            Dataset.is_average.label("is_average"),
            Dataset.sort_order.label("sort_order"),
            Dataset.mode_id.label("mode_id"),
            Vehicle.id.label("vehicle_id"),
            Vehicle.name.label("vehicle"),
            VehicleMode.mode.label("mode"),
            Condition.name.label("condition"),
        )
        .join(Vehicle, Vehicle.id == Dataset.vehicle_id)
        .outerjoin(VehicleMode, VehicleMode.id == Dataset.mode_id)
        .join(Condition, Condition.id == Dataset.condition_id)
    )
    if dataset_type:
        stmt = stmt.join(
            DatasetType, DatasetType.id == Dataset.dataset_type_id
        ).where(DatasetType.code == dataset_type)
    if vehicle_ids is not None:
        stmt = stmt.where(Dataset.vehicle_id.in_(list(vehicle_ids)))
    if mode_id is not None:
        stmt = stmt.where(Dataset.mode_id == mode_id)
    if condition_id is not None:
        stmt = stmt.where(Dataset.condition_id == condition_id)
    elif condition_name:
        stmt = stmt.where(Condition.name == condition_name)
    if average_only:
        stmt = stmt.where(Dataset.is_average.is_(True))
    if indicator_name:
        stmt = stmt.where(exists().where(and_(
            MetricValue.dataset_id == Dataset.id,
            MetricValue.name == indicator_name,
        )))
    return stmt


def get_all_indicators_flat(
    db: Session,
    filters: Optional[Dict[str, str]] = None,
    condition_name: Optional[str] = None,
    indicator_name: Optional[str] = None,
    average_only: bool = False,
    dataset_type: Optional[str] = None,
    mode_id: Optional[int] = None,
    limit: int = 200,
    offset: int = 0,
    condition_id: Optional[int] = None,
) -> Dict[str, Any]:
    """分页的指标统计表。

    每行一个「车 × 模式 × 工况 × 实验」，``attrs`` 是车辆动态属性，
    ``indicators`` 是 ``{raw_key: {...}}``。字段列由前端按 ``/api/meta``
    决定，后端不再写死平台 / 品牌。
    """
    vehicle_ids, attrs_map = _filter_vehicle_ids_for_stats(db, filters or {})
    if vehicle_ids is not None and not vehicle_ids:
        return {"total": 0, "offset": offset, "limit": limit, "rows": []}

    key_stmt = _indicator_key_stmt(
        vehicle_ids, condition_name, indicator_name, average_only,
        dataset_type, mode_id, condition_id,
    )
    total = int(db.execute(
        select(func.count()).select_from(key_stmt.subquery())
    ).scalar() or 0)

    page_stmt = key_stmt.order_by(
        Vehicle.name, VehicleMode.mode, Condition.name,
        Dataset.sort_order, Dataset.exp_no,
    ).offset(offset)
    if limit:
        page_stmt = page_stmt.limit(limit)
    keys = list(db.execute(page_stmt))

    ds_ids = [k.dataset_id for k in keys]
    ind_map: Dict[int, Dict[str, Any]] = {}
    if ds_ids:
        istmt = (
            select(
                MetricValue.dataset_id, MetricValue.raw_key, MetricValue.name,
                MetricValue.unit, MetricValue.value, MetricValue.text_value,
            )
            .where(MetricValue.dataset_id.in_(ds_ids))
            .order_by(MetricValue.name, MetricValue.raw_key)
        )
        for dsid, raw_key, name, unit, value, text in db.execute(istmt):
            ind_map.setdefault(dsid, {})[raw_key] = {
                "value": value, "text_value": text,
                "unit": unit, "name": name,
            }

    rows = []
    for k in keys:
        rows.append({
            "vehicle_id": k.vehicle_id,
            "vehicle": k.vehicle,
            "mode_id": k.mode_id,
            "mode": k.mode,
            "condition": k.condition,
            "dataset_id": k.dataset_id,
            "exp_no": k.exp_no,
            "is_average": bool(k.is_average),
            "attrs": attrs_map.get(k.vehicle_id, {}),
            "indicators": ind_map.get(k.dataset_id, {}),
        })

    return {"total": total, "offset": offset, "limit": limit, "rows": rows}


# ═══════════════════════════════════════════════════════════════════════
# 规模估算
# ═══════════════════════════════════════════════════════════════════════

_ASSUMED_SERIES_PER_CURVE = 2


def estimate_compare_numbers(
    db: Session,
    segment_count: int,
    condition_id: int,
    curve_names: Optional[Set[str]] = None,
    exp_scope: Optional[str] = None,
    dataset_type: Optional[str] = None,
) -> int:
    stmt = (
        select(func.max(Curve.point_count), func.count(func.distinct(Curve.name)))
        .join(Dataset, Dataset.id == Curve.dataset_id)
        .where(Dataset.condition_id == condition_id)
    )
    if dataset_type:
        stmt = stmt.join(
            DatasetType, DatasetType.id == Dataset.dataset_type_id
        ).where(DatasetType.code == dataset_type)
    row = db.execute(stmt).one()
    max_points = int(row[0] or 0)
    n_curves = len(curve_names) if curve_names else int(row[1] or 0)
    if not max_points or not n_curves:
        return 0

    if exp_scope == "avg_first":
        n_exps = 1
    else:
        count_stmt = select(func.count(Dataset.id)).where(
            Dataset.condition_id == condition_id
        )
        if dataset_type:
            count_stmt = count_stmt.join(
                DatasetType, DatasetType.id == Dataset.dataset_type_id
            ).where(DatasetType.code == dataset_type)
        counts = list(db.execute(
            count_stmt.group_by(Dataset.vehicle_id, Dataset.mode_id)
        ).scalars())
        n_exps = max(counts) if counts else 1

    return segment_count * n_exps * n_curves * _ASSUMED_SERIES_PER_CURVE * max_points * 2


# ═══════════════════════════════════════════════════════════════════════
# 车辆列表 / 字段注册表维护
# ═══════════════════════════════════════════════════════════════════════

def list_vehicles_brief(
    db: Session,
    filters: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """车辆扁平列表：动态属性 + 模式 + 数据集数量。"""
    vehicles = list(db.execute(select(Vehicle).order_by(Vehicle.name)).scalars())
    vids = [v.id for v in vehicles]
    attrs_map = load_vehicle_attrs(db, vids)
    modes_map = _vehicle_modes(db, vids)
    if filters:
        allow = _filter_vehicle_ids(attrs_map, filters)
    else:
        allow = None

    counts: Dict[int, int] = {}
    if vids:
        for vid, n in db.execute(
            select(Dataset.vehicle_id, func.count(Dataset.id))
            .where(Dataset.vehicle_id.in_(vids))
            .group_by(Dataset.vehicle_id)
        ):
            counts[vid] = int(n)

    out: List[Dict[str, Any]] = []
    for v in vehicles:
        if allow is not None and v.id not in allow:
            continue
        out.append({
            "id": v.id,
            "name": v.name,
            "attrs": {
                code: val.get("value") for code, val in attrs_map.get(v.id, {}).items()
            },
            "modes": [
                {"id": m.id, "mode": m.mode, "sort_order": m.sort_order}
                for m in modes_map.get(v.id, [])
            ],
            "dataset_count": counts.get(v.id, 0),
        })
    return out


def get_attr_defs(db: Session, active_only: bool = False) -> List[AttrDef]:
    return _attr_defs(db, active_only=active_only)


def _attr_code_for(name: str) -> str:
    """给中文列名生成稳定、可读性尚可的 code。"""
    import hashlib
    import re

    candidate = re.sub(r"[^a-zA-Z0-9_]+", "_", name).strip("_").lower()
    if candidate:
        return candidate[:50]
    return "f_" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:10]


def create_attr_def(db: Session, data: Dict[str, Any]) -> AttrDef:
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError("字段名称不能为空")
    code = str(data.get("code") or "").strip() or _attr_code_for(name)
    if db.execute(select(AttrDef).where(AttrDef.code == code)).scalars().first():
        raise ValueError("字段 code 已存在：%s" % code)
    value_type = str(data.get("value_type") or "text")
    if value_type not in ("text", "number", "bool", "date", "dim"):
        raise ValueError("value_type 只能是 text/number/bool/date/dim")
    if value_type == "dim" and not data.get("dim_code"):
        raise ValueError("dim 类型字段必须指定 dim_code")

    attr = AttrDef(
        code=code,
        name=name,
        value_type=value_type,
        dim_code=(data.get("dim_code") or None),
        unit=(data.get("unit") or None),
        required=bool(data.get("required", False)),
        show_in_list=bool(data.get("show_in_list", True)),
        options=data.get("options"),
        sort_order=int(data.get("sort_order") or 0),
        is_active=True,
    )
    db.add(attr)
    db.commit()
    db.refresh(attr)
    return attr


def update_attr_def(
    db: Session, attr_id: int, data: Dict[str, Any],
) -> Optional[AttrDef]:
    attr = db.get(AttrDef, attr_id)
    if attr is None:
        return None
    for key in ("name", "unit", "dim_code", "value_type", "options"):
        if key in data:
            setattr(attr, key, data.get(key))
    for key in ("required", "show_in_list", "is_active"):
        if key in data:
            setattr(attr, key, bool(data.get(key)))
    if "sort_order" in data:
        attr.sort_order = int(data.get("sort_order") or 0)
    db.commit()
    db.refresh(attr)
    return attr


def get_indicator_names(
    db: Session,
    condition_id: Optional[int] = None,
    dataset_type: Optional[str] = None,
) -> List[str]:
    stmt = select(MetricValue.name).distinct().order_by(MetricValue.name)
    if condition_id is not None or dataset_type:
        stmt = stmt.join(Dataset, Dataset.id == MetricValue.dataset_id)
        if condition_id is not None:
            stmt = stmt.where(Dataset.condition_id == condition_id)
        if dataset_type:
            stmt = stmt.join(
                DatasetType, DatasetType.id == Dataset.dataset_type_id
            ).where(DatasetType.code == dataset_type)
    return [r[0] for r in db.execute(stmt)]
