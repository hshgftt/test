"""
CRUD layer – all database queries.

Operations matching the required APIs:

1. ``get_tree``                  → platform → brand → vehicle (directory tree)
2. ``get_vehicle_condition_data``→ params + curves_cal + curves_duiqi for one vehicle × condition
3. ``get_compare``               → same payload for several vehicles (page 1 comparison)
4. ``get_all_indicators``        → all indicators nested by hierarchy (page 2 table)

Scale notes (200 cars × 20 工况 × 10万点)
-----------------------------------------
* Curves are **never** down-sampled — the front-end wants full resolution.
  Instead the payload can be narrowed by ``exp_nos`` / ``curve_names`` so a
  request only carries what the page actually renders.

  ``curve_names`` is pushed **into SQL** (``selectinload(...).and_(...)``),
  not applied in Python: a 10万点 curve is ~3 MB of JSONB, so filtering
  after the fact still pays the I/O + deserialisation for every curve.

* ``exp_scope='avg_first'`` 只取「平均」那一次（没有平均则取第一次）。
  前端默认视图（同图取线=平均 / 指标表=仅平均）只需要这一次实验，
  全量拉下来有 2/3 是白拉。

* ``meta_only=True`` 只返回曲线元信息（title/xlabel/ylabel/point_count），
  不碰 ``Curve.series``。稳态回转 14 条曲线时实测 328 MB → 0.16 MB，
  让「先看菜单再点菜」成为可能。

* ``get_all_indicators`` supports ``flat=True`` + ``limit/offset``.  At full
  scale the nested variant would be ~350k indicator values in one response,
  so page 2 must page through it — and the paging is done **in SQL**
  (``LIMIT/OFFSET`` over experiment-level rows), not by slicing a list that
  already holds every row in memory.

Python 3.8 compatible (``typing.Dict/List/Optional`` only).
"""
from __future__ import annotations

import os
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set

from sqlalchemy import and_, exists, false, func, select
from sqlalchemy.orm import Session, load_only, selectinload

from app.models import (
    Brand,
    Condition,
    Curve,
    Experiment,
    Indicator,
    Platform,
    Vehicle,
)


# ── helpers ─────────────────────────────────────────────────────────────

def _series_stats(
    series: Sequence[Dict[str, Any]],
) -> tuple:
    """Summarise a curve's series.

    Returns ``(series_names, series_map)`` where ``series_map`` maps each
    series' ``curvename`` to its length and order::

        (['原始数据', '滤波'], {'原始数据': {'index': 0, 'point_count': 3000}, …})

    The front-end uses this to offer whatever data kinds actually exist —
    the set is *not* limited to 原始数据 / 滤波.
    """
    names: List[str] = []
    smap: Dict[str, Dict[str, Any]] = {}
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


def _curve_payload(cur: Any, meta_only: bool = False) -> Dict[str, Any]:
    """把一条曲线组装成前端要的 payload。

    ``cur`` 只要**有这几个属性**就行 —— ORM 的 ``Curve`` 和 Core 查询返回的
    ``Row`` 都能直接传（流式端点走 Core 查询，避免 ORM identity map 把
    上千条曲线对象一直攥在内存里，见 ``iter_compare_stream``）。

    ``meta_only=True`` 时刻意不读 ``cur.series`` —— 那是 JSONB 重列，
    读它等于把整个点集拉进 Python。
    """
    payload: Dict[str, Any] = {
        "name": cur.name,
        "title": cur.title,
        "xlabel": cur.xlabel,
        "ylabel": cur.ylabel,
        "point_count": cur.point_count or 0,
    }
    if meta_only:
        payload["series"] = []
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


def _exp_payload(
    exp: Experiment,
    curve_names: Optional[Set[str]] = None,
    meta_only: bool = False,
) -> Dict[str, Any]:
    """Assemble one experiment into {params, curves_cal, curves_duiqi}.

    ``curve_names`` optionally narrows the curves returned (the caller only
    renders some of them).  Series data itself is always full resolution.

    ``meta_only=True`` returns curve *metadata only* — no ``series`` payload
    and no ``series_names``/``series_map``.  In that mode the caller must
    have loaded the curves with ``load_only`` (see ``_load_experiments``);
    touching ``cur.series`` here would trigger a lazy SELECT per curve.

    Each full curve carries ``series_names`` (actual data kinds found in it)
    so the front-end never has to hard-code '原始数据' / '滤波'.
    """
    # ── params: indicators ordered by name (seq 已废弃，见 models.Indicator) ──
    params: Dict[str, Dict[str, Any]] = {}
    for ind in sorted(exp.indicators, key=lambda i: (i.name, i.raw_key)):
        params[ind.raw_key] = {
            "name": ind.name,
            "unit": ind.unit,
            "raw_key": ind.raw_key,
            "value": ind.value,
            "text_value": ind.text_value,
        }

    # ── curves: split by group, keep definition order ──
    cal: Dict[str, Dict[str, Any]] = {}
    dui: Dict[str, Dict[str, Any]] = {}
    for cur in sorted(exp.curves, key=lambda c: c.sort_order):
        if curve_names is not None and cur.name not in curve_names:
            continue
        payload = _curve_payload(cur, meta_only)
        (cal if cur.group == "cal" else dui)[cur.name] = payload

    return {
        "experiment_id": exp.id,
        "experiment_no": exp.experiment_no,
        "is_average": exp.is_average,
        "params": params,
        "curves_cal": cal,
        "curves_duiqi": dui,
    }


def _collect_curve_meta(
    vehicles: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Aggregate curve metadata across the whole compare response.

    Output::

        {
          "groups": {
            "cal":   {"series_names": ["原始数据", "滤波"],
                      "curves": {"方向盘转角-时间": {"series_names": […], "title": …}}},
            "duiqi": {…}
          }
        }

    The front-end drives its curve panel off this — no hard-coded names,
    works whatever groups / data kinds the payload turns out to contain.

    ⚠️ 它描述的是**本次响应里实际带到的曲线**，所以给了 ``curves=`` 时会跟着收敛。
    想让前端看到「菜单上有什么」，就用 ``meta_only=True`` 单独请求一次
    （那时没有 ``curves=`` 过滤，返回全部曲线的元信息）。
    """
    groups: Dict[str, Any] = {}
    for group in ("cal", "duiqi"):
        key = "curves_cal" if group == "cal" else "curves_duiqi"
        series_seen: List[str] = []
        curves: Dict[str, Any] = {}
        for veh in vehicles:
            for exp in veh.get("experiments") or []:
                for cname, cval in (exp.get(key) or {}).items():
                    entry = curves.get(cname)
                    if entry is None:
                        entry = {
                            "name": cname,
                            "title": cval.get("title") or cname,
                            "xlabel": cval.get("xlabel"),
                            "ylabel": cval.get("ylabel"),
                            "point_count": cval.get("point_count") or 0,
                            "series_names": [],
                        }
                        curves[cname] = entry
                    for nm in cval.get("series_names") or []:
                        if nm not in entry["series_names"]:
                            entry["series_names"].append(nm)
                        if nm not in series_seen:
                            series_seen.append(nm)
        groups[group] = {"series_names": series_seen, "curves": curves}
    return {"groups": groups}


# 曲线元信息只需要这些列；series 是重列（JSONB），meta_only 时刻意不取
_CURVE_META_COLS = (
    Curve.id,
    Curve.experiment_id,
    Curve.group,
    Curve.name,
    Curve.title,
    Curve.xlabel,
    Curve.ylabel,
    Curve.point_count,
    Curve.sort_order,
)


def _curve_option(
    curve_names: Optional[Set[str]] = None,
    meta_only: bool = False,
):
    """``selectinload(Experiment.curves)``，可选「按名过滤」+「只取元信息」。

    为什么要把 ``curve_names`` 下推到 SQL：以前它只在 ``_exp_payload`` 里
    过滤，而那时 ``selectinload`` 已经把**每条**曲线的 JSONB 读出来并反序列化
    成 Python dict 了。稳态回转 14 条曲线、10 万点/条 ≈ 328 MB，只画 1 条也要
    付这 328 MB。下推之后只读选中的那条。

    ``meta_only`` 再加一层 ``load_only`` —— 连选中的曲线也不读 series，
    只读标量列，用于「先拿元信息、再按需取数据」的两阶段加载。
    """
    rel = Experiment.curves
    if curve_names:
        rel = rel.and_(Curve.name.in_(list(curve_names)))
    opt = selectinload(rel)
    if meta_only:
        opt = opt.load_only(*_CURVE_META_COLS)
    return opt


def _apply_exp_scope(stmt, exp_scope: Optional[str]):
    """按口径收敛实验。

    * ``None`` / ``'all'`` —— 全部实验（默认，保持老行为）
    * ``'avg_first'``     —— 每台车取「平均」，没有平均就取第一次

    ``avg_first`` 与前端 ``expsFor()`` 的取线规则一致（平均优先，否则第一次），
    只是把它从「前端过滤」提前到「SQL 只读需要的行」。用窗口函数表达：

        ROW_NUMBER() OVER (PARTITION BY vehicle_id
                           ORDER BY is_average DESC, sort_order ASC) = 1

    ``sort_order`` 把「平均」放在 999，所以这个排序天然实现
    「有平均取平均、没有就取 sort_order 最小的那次」。

    注意 ``is_average`` 历史数据可能是 NULL，PG 的 ``DESC`` 默认 NULLS FIRST
    会让 NULL 排到最前 —— 所以用 ``coalesce`` 兜底成 false。
    """
    if exp_scope != "avg_first":
        return stmt

    rn = func.row_number().over(
        partition_by=Experiment.vehicle_id,
        order_by=[
            func.coalesce(Experiment.is_average, false()).desc(),
            Experiment.sort_order.asc(),
        ],
    ).label("rn")

    inner = stmt.with_only_columns(Experiment.id.label("id"), rn).subquery()
    return (
        select(Experiment)
        .join(inner, inner.c.id == Experiment.id)
        .where(inner.c.rn == 1)
    )


def _load_experiments(
    db: Session,
    vehicle_id: int,
    condition_id: int,
    exp_nos: Optional[Sequence[str]] = None,
    curve_names: Optional[Set[str]] = None,
    meta_only: bool = False,
    exp_scope: Optional[str] = None,
) -> List[Experiment]:
    """Experiments for a vehicle × condition, indicators+curves eager-loaded."""
    stmt = select(Experiment).where(
        Experiment.vehicle_id == vehicle_id,
        Experiment.condition_id == condition_id,
    )
    if exp_nos:
        stmt = stmt.where(Experiment.experiment_no.in_(list(exp_nos)))
    stmt = _apply_exp_scope(stmt, exp_scope).options(
        selectinload(Experiment.indicators),
        _curve_option(curve_names, meta_only),
    ).order_by(Experiment.sort_order)
    return list(db.execute(stmt).scalars().all())


def _load_experiments_bulk(
    db: Session,
    vehicle_ids: Sequence[int],
    condition_id: int,
    exp_nos: Optional[Sequence[str]] = None,
    curve_names: Optional[Set[str]] = None,
    meta_only: bool = False,
    exp_scope: Optional[str] = None,
) -> Dict[int, List[Experiment]]:
    """One query for many vehicles (avoids N+1 on the compare endpoint)."""
    stmt = select(Experiment).where(
        Experiment.vehicle_id.in_(list(vehicle_ids)),
        Experiment.condition_id == condition_id,
    )
    if exp_nos:
        stmt = stmt.where(Experiment.experiment_no.in_(list(exp_nos)))
    stmt = _apply_exp_scope(stmt, exp_scope).options(
        selectinload(Experiment.indicators),
        _curve_option(curve_names, meta_only),
    ).order_by(Experiment.vehicle_id, Experiment.sort_order)

    out: Dict[int, List[Experiment]] = {}
    for exp in db.execute(stmt).scalars().all():
        out.setdefault(exp.vehicle_id, []).append(exp)
    return out


# ── API 1: directory tree ───────────────────────────────────────────────

def get_tree(
    db: Session, platform_name: Optional[str] = None
) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    """Return ``{platform: {brand: [{id, name}, …]}}``."""
    stmt = (
        select(Platform.name, Brand.name, Vehicle.id, Vehicle.name)
        .join(Brand, Brand.platform_id == Platform.id)
        .join(Vehicle, Vehicle.brand_id == Brand.id)
        .order_by(Platform.name, Brand.name, Vehicle.name)
    )
    if platform_name:
        stmt = stmt.where(Platform.name == platform_name)

    tree: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    for pname, bname, vid, vname in db.execute(stmt):
        tree.setdefault(pname, {}).setdefault(bname, []).append(
            {"id": vid, "name": vname}
        )
    return tree


# ── API 2: one vehicle × condition ──────────────────────────────────────

def get_vehicle_condition_data(
    db: Session,
    vehicle_id: int,
    condition_id: int,
    exp_nos: Optional[Sequence[str]] = None,
    curve_names: Optional[Set[str]] = None,
    meta_only: bool = False,
    exp_scope: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    vehicle = db.get(Vehicle, vehicle_id)
    if vehicle is None:
        return None
    condition = db.get(Condition, condition_id)
    if condition is None:
        return None

    experiments = _load_experiments(
        db, vehicle_id, condition_id, exp_nos,
        curve_names=curve_names, meta_only=meta_only, exp_scope=exp_scope,
    )
    payloads = [_exp_payload(e, curve_names, meta_only) for e in experiments]
    return {
        "vehicle_id": vehicle_id,
        "vehicle_name": vehicle.name,
        "condition_id": condition_id,
        "condition_name": condition.name,
        "experiments": payloads,
        # 单车接口也带上 curve_meta，前端才能用同一套渲染逻辑
        "curve_meta": _collect_curve_meta([{"experiments": payloads}]),
    }


# ── API 2b: several vehicles × one condition (comparison) ───────────────

def get_compare(
    db: Session,
    vehicle_ids: Sequence[int],
    condition_id: int,
    exp_nos: Optional[Sequence[str]] = None,
    curve_names: Optional[Set[str]] = None,
    meta_only: bool = False,
    exp_scope: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Payload for page 1 – compare N vehicles under one condition."""
    condition = db.get(Condition, condition_id)
    if condition is None:
        return None

    # vehicles in ONE query (with brand + platform joined) …
    vstmt = (
        select(Vehicle, Brand.name.label("brand"), Platform.name.label("platform"))
        .join(Brand, Brand.id == Vehicle.brand_id)
        .join(Platform, Platform.id == Brand.platform_id)
        .where(Vehicle.id.in_(list(vehicle_ids)))
    )
    vehicles = {
        v.Vehicle.id: (v.Vehicle, v.brand, v.platform)
        for v in db.execute(vstmt)
    }

    # … all their experiments in ONE query
    exp_map = _load_experiments_bulk(
        db, vehicle_ids, condition_id, exp_nos,
        curve_names=curve_names, meta_only=meta_only, exp_scope=exp_scope,
    )

    out: List[Dict[str, Any]] = []
    for vid in vehicle_ids:
        bundle = vehicles.get(vid)
        if bundle is None:
            continue
        vehicle, brand_name, platform_name = bundle
        experiments = exp_map.get(vid, [])
        if not experiments:
            continue
        out.append({
            "vehicle_id": vid,
            "vehicle_name": vehicle.name,
            "brand_name": brand_name,
            "platform_name": platform_name,
            "drive_type": vehicle.drive_type,
            "experiments": [
                _exp_payload(e, curve_names, meta_only) for e in experiments
            ],
        })

    return {
        "condition_id": condition_id,
        "condition_name": condition.name,
        "vehicles": out,
        # 前端据此渲染曲线选择区：有哪些组、每组有哪些曲线、每条曲线有哪些数据种类
        "curve_meta": _collect_curve_meta(out),
    }


# ── API 2c: same thing, but streamed ───────────────────────────────────

#: 流式端点每批取多少条曲线的 series。
#:
#: 这是**内存**旋钮，不是速度旋钮：24 条 × 1 万点 × 2 series ≈ 48 万个数字，
#: 约 15 MB Python 对象，序列化完就丢。调大省 SQL 往返，调小降峰值。
STREAM_BATCH_CURVES = int(os.getenv("STREAM_BATCH_CURVES", "24"))


def iter_compare_stream(
    db: Session,
    vehicle_ids: Sequence[int],
    condition_id: int,
    exp_nos: Optional[Sequence[str]] = None,
    curve_names: Optional[Set[str]] = None,
    exp_scope: Optional[str] = None,
    batch_curves: Optional[int] = None,
) -> Iterator[Dict[str, Any]]:
    """``get_compare`` 的流式版本 —— 逐条产出 NDJSON 对象。

    ── 为什么不复用 ``get_compare`` ──

    那个函数必须先把**整个响应**组装成一个 dict，再 ``json.dumps`` 成一个
    几百 MB 的字符串。实测服务端内存放大系数 ~8×（ORM 对象 + dict + JSON
    字符串 + gzip 缓冲），357 MB 的响应峰值 RSS 就是 2.7 GB。

    流式把「持有全量」换成「持有一条曲线」：序列化完一条就交出去，下一轮
    循环它就能被回收。峰值内存与「选了多少条曲线」无关。

    产出四种行::

        {"type": "meta",  ...}   骨架：车辆 + 指标 + 曲线元信息（无 series）
        {"type": "curve", ...}   一条曲线的 series，重复 N 次
        {"type": "done",  ...}   统计 curves / points（HTTP 层再补 ms）
        {"type": "error", ...}   生成中的异常（此时 200 已经发出去了）

    ``meta`` 行**先发**，前端拿到就能立刻渲染指标表和曲线选择区，
    不用等最后一条曲线到齐。

    ── 两个内存上的坑（都踩过）──

    1. 取 series 用的是 **Core 查询**（``select(Curve.id, …, Curve.series)``）
       而不是 ``select(Curve)``。ORM 查询会把每条曲线对象放进 Session 的
       identity map 并**一直持有**，几百条曲线就把省下来的内存又吃回去了。
    2. 骨架转成纯 dict 之后立刻 ``expunge_all()``，同理。

    曲线按 ``STREAM_BATCH_CURVES`` 条一批取，每批一次 ``IN`` 查询。
    """
    condition = db.get(Condition, condition_id)
    if condition is None:
        yield {"type": "error", "detail": "Condition not found"}
        return

    # ── 1. 车辆元信息（轻量）──────────────────────────────────────────
    vstmt = (
        select(Vehicle, Brand.name.label("brand"), Platform.name.label("platform"))
        .join(Brand, Brand.id == Vehicle.brand_id)
        .join(Platform, Platform.id == Brand.platform_id)
        .where(Vehicle.id.in_(list(vehicle_ids)))
    )
    vmeta: Dict[int, Dict[str, Any]] = {}
    for row in db.execute(vstmt):
        veh = row.Vehicle
        vmeta[veh.id] = {
            "vehicle_id": veh.id,
            "vehicle_name": veh.name,
            "brand_name": row.brand,
            "platform_name": row.platform,
            "drive_type": veh.drive_type,
        }

    # ── 2. 实验骨架 + 指标（一次批量；指标是 EAV 小行，不是瓶颈）──────
    #     curves 走 meta_only 语义：只读标量列，series 留到第 3 步按批取
    estmt = select(Experiment).where(
        Experiment.vehicle_id.in_(list(vehicle_ids)),
        Experiment.condition_id == condition_id,
    )
    if exp_nos:
        estmt = estmt.where(Experiment.experiment_no.in_(list(exp_nos)))
    estmt = _apply_exp_scope(estmt, exp_scope).options(
        selectinload(Experiment.indicators),
        _curve_option(curve_names, meta_only=True),
    ).order_by(Experiment.vehicle_id, Experiment.sort_order)

    exps = list(db.execute(estmt).scalars().all())

    out: List[Dict[str, Any]] = []
    exp_ids: List[int] = []
    exp2veh: Dict[int, int] = {}
    for vid in vehicle_ids:                       # 保持调用方给的顺序
        meta = vmeta.get(vid)
        if meta is None:
            continue
        mine = [e for e in exps if e.vehicle_id == vid]
        if not mine:
            continue
        for e in mine:
            exp_ids.append(e.id)
            exp2veh[e.id] = vid
        out.append(dict(meta, experiments=[
            _exp_payload(e, curve_names, meta_only=True) for e in mine
        ]))

    yield {
        "type": "meta",
        "condition_id": condition_id,
        "condition_name": condition.name,
        "vehicles": out,
        # 这里的 curve_meta 没有 series_names（骨架里没有 series）——
        # 前端本来就把 series_names 当可选字段（见 types.CurveMeta）。
        "curve_meta": _collect_curve_meta(out),
    }

    # 骨架已经变成纯 dict，ORM 对象可以放掉了：它们还挂在 identity map 上，
    # 不 expunge 的话下面每批的曲线对象会越积越多。
    db.expunge_all()

    # ── 3. 逐批取曲线的 series（唯一的重活）──────────────────────────
    step = batch_curves or STREAM_BATCH_CURVES
    n_curves = 0
    n_points = 0

    if exp_ids:
        # 先只拿「有哪些曲线」的轻量索引（不带 series 列）
        #
        # ⚠️ 排序按**曲线**而不是按实验 —— 这不是为了好看，是**渲染性能**：
        #
        #   按实验排（experiment_id, sort_order）→ 每个时间窗口里 7 条曲线都在变
        #     → 前端每次重绘要重画**全部 7 张图**（25 车 × 3 实验 × 2 series
        #       = 每图 150 条线），卡顿就是这么来的。
        #   按曲线排（group, name, experiment_id）→ 同一时间只有**一条**曲线在变
        #     → 每次重绘只重画对应的**那一张**图。
        #
        # 发送顺序不影响正确性：前端是按 (experiment_id, name) 找位置挂数据的。
        cstmt = (
            select(Curve.id, Curve.experiment_id, Curve.group)
            .where(Curve.experiment_id.in_(exp_ids))
            .order_by(Curve.group, Curve.name, Curve.experiment_id)
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
            for k in chunk:                       # 按 chunk 顺序发，保证稳定
                row = by_id.get(k.id)
                if row is None:
                    continue
                payload = _curve_payload(row)
                n_curves += 1
                n_points += payload["point_count"]
                yield {
                    "type": "curve",
                    "vehicle_id": exp2veh.get(k.experiment_id),
                    "experiment_id": k.experiment_id,
                    "group": k.group,
                    "curve": payload,
                }
            del rows, by_id                       # 早点让这一批可以被回收

    yield {"type": "done", "curves": n_curves, "points": n_points}


# ── API 3: all indicators nested by hierarchy ──────────────────────────

def _indicator_stmt(
    platform_name: Optional[str],
    condition_name: Optional[str],
    indicator_name: Optional[str],
    average_only: bool = False,
):
    stmt = (
        select(
            Platform.name.label("platform"),
            Brand.name.label("brand"),
            Vehicle.name.label("vehicle"),
            Condition.name.label("condition"),
            Experiment.experiment_no.label("exp_no"),
            Experiment.is_average.label("is_average"),
            Experiment.sort_order.label("sort_order"),
            Indicator.raw_key.label("raw_key"),
            Indicator.name.label("ind_name"),
            Indicator.unit.label("unit"),
            Indicator.value,
            Indicator.text_value,
        )
        .join(Brand, Brand.platform_id == Platform.id)
        .join(Vehicle, Vehicle.brand_id == Brand.id)
        .join(Experiment, Experiment.vehicle_id == Vehicle.id)
        .join(Condition, Condition.id == Experiment.condition_id)
        .join(Indicator, Indicator.experiment_id == Experiment.id)
        .order_by(
            Platform.name, Brand.name, Vehicle.name,
            Condition.name, Experiment.sort_order,
            Indicator.name, Indicator.raw_key,
        )
    )
    if platform_name:
        stmt = stmt.where(Platform.name == platform_name)
    if condition_name:
        stmt = stmt.where(Condition.name == condition_name)
    if indicator_name:
        stmt = stmt.where(Indicator.name == indicator_name)
    # 「仅平均」在 SQL 里过滤 —— 否则分页 total 会把非平均实验也算进去
    if average_only:
        stmt = stmt.where(Experiment.is_average.is_(True))
    return stmt


def get_all_indicators(
    db: Session,
    platform_name: Optional[str] = None,
    condition_name: Optional[str] = None,
    indicator_name: Optional[str] = None,
    average_only: bool = False,
) -> Dict[str, Any]:
    """Return all indicators nested as::

        platform → brand → vehicle → condition → experiment_no → {raw_key: {value, unit}}

    Full scale warning: this is ~350k indicator values for 200 cars × 20 工况.
    Use ``get_all_indicators_flat`` with paging for the real table view.
    """
    result: Dict[str, Any] = {}
    for row in db.execute(
        _indicator_stmt(platform_name, condition_name, indicator_name, average_only)
    ):
        (
            result
            .setdefault(row.platform, {})
            .setdefault(row.brand, {})
            .setdefault(row.vehicle, {})
            .setdefault(row.condition, {})
            .setdefault(row.exp_no, {})
        )[row.raw_key] = {
            "value": row.value,
            "text_value": row.text_value,
            "unit": row.unit,
            "name": row.ind_name,
        }
    return result


def _indicator_key_stmt(
    platform_name: Optional[str],
    condition_name: Optional[str],
    indicator_name: Optional[str],
    average_only: bool = False,
):
    """实验级「一行」的 key（不含指标值），供 SQL 侧分页使用。

    与 ``_indicator_stmt`` 的区别：那个是 (实验 × 指标) 的笛卡尔展开，
    200 车 × 20 工况 × 30 指标 ≈ 35 万行；这个每个实验只出一行
    （≈ 1.2 万行），所以 ``COUNT`` / ``LIMIT`` / ``OFFSET`` 都能真正下推。
    """
    stmt = (
        select(
            Experiment.id.label("exp_id"),
            Experiment.experiment_no.label("exp_no"),
            Experiment.is_average.label("is_average"),
            Experiment.sort_order.label("sort_order"),
            Platform.name.label("platform"),
            Brand.name.label("brand"),
            Vehicle.name.label("vehicle"),
            Condition.name.label("condition"),
        )
        .join(Vehicle, Vehicle.id == Experiment.vehicle_id)
        .join(Brand, Brand.id == Vehicle.brand_id)
        .join(Platform, Platform.id == Brand.platform_id)
        .join(Condition, Condition.id == Experiment.condition_id)
    )
    if platform_name:
        stmt = stmt.where(Platform.name == platform_name)
    if condition_name:
        stmt = stmt.where(Condition.name == condition_name)
    if average_only:
        stmt = stmt.where(Experiment.is_average.is_(True))
    if indicator_name:
        # 只保留「确实有该指标」的实验 —— 否则 total 会把空实验也算进去
        stmt = stmt.where(
            exists().where(and_(
                Indicator.experiment_id == Experiment.id,
                Indicator.name == indicator_name,
            ))
        )
    return stmt


def get_all_indicators_flat(
    db: Session,
    platform_name: Optional[str] = None,
    condition_name: Optional[str] = None,
    indicator_name: Optional[str] = None,
    average_only: bool = False,
    limit: int = 200,
    offset: int = 0,
) -> Dict[str, Any]:
    """Paged, table-ready variant of ``get_all_indicators``.

    One row per (platform, brand, vehicle, condition, experiment_no); the
    indicators live in a ``{raw_key: {value, unit, …}}`` map so the front-end
    can turn them into columns.

    ``average_only=True`` keeps just the 平均 run of every vehicle × condition
    (a vehicle may have an arbitrary number of non-average runs, or none).

    分页是**在 SQL 里做的**：先在实验级 key 上 ``COUNT`` / ``ORDER BY … LIMIT
    OFFSET``，再只为这一页的实验取指标。以前是先把 35 万行全读进内存、
    在 Python 里 groupby 完再切片 —— 翻到第 2 页和第 1 页一样贵，
    而且 200 车时那份 list 本身就有几百 MB。

    Returns ``{total, offset, limit, rows}``.
    """
    key_stmt = _indicator_key_stmt(
        platform_name, condition_name, indicator_name, average_only
    )

    total = int(
        db.execute(select(func.count()).select_from(key_stmt.subquery())).scalar() or 0
    )

    page_stmt = key_stmt.order_by(
        Platform.name, Brand.name, Vehicle.name,
        Condition.name, Experiment.sort_order, Experiment.experiment_no,
    ).offset(offset)
    if limit:
        page_stmt = page_stmt.limit(limit)
    keys = list(db.execute(page_stmt))

    # 只为这一页的实验取指标（一次 IN 查询）
    exp_ids = [k.exp_id for k in keys]
    ind_map: Dict[int, Dict[str, Any]] = {}
    if exp_ids:
        istmt = (
            select(
                Indicator.experiment_id,
                Indicator.raw_key,
                Indicator.name,
                Indicator.unit,
                Indicator.value,
                Indicator.text_value,
            )
            .where(Indicator.experiment_id.in_(exp_ids))
            .order_by(Indicator.name, Indicator.raw_key)
        )
        for eid, raw_key, iname, iunit, ivalue, itext in db.execute(istmt):
            ind_map.setdefault(eid, {})[raw_key] = {
                "value": ivalue,
                "text_value": itext,
                "unit": iunit,
                "name": iname,
            }

    rows = [
        {
            "platform": k.platform,
            "brand": k.brand,
            "vehicle": k.vehicle,
            "condition": k.condition,
            "experiment_no": k.exp_no,
            "is_average": bool(k.is_average),
            "indicators": ind_map.get(k.exp_id, {}),
        }
        for k in keys
    ]

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "rows": rows,
    }


def count_indicator_rows(
    db: Session,
    platform_name: Optional[str] = None,
    condition_name: Optional[str] = None,
    average_only: bool = False,
) -> int:
    """Number of experiment-level rows (for the page-2 pager)."""
    stmt = (
        select(func.count())
        .select_from(Experiment)
        .join(Vehicle, Vehicle.id == Experiment.vehicle_id)
        .join(Brand, Brand.id == Vehicle.brand_id)
        .join(Platform, Platform.id == Brand.platform_id)
        .join(Condition, Condition.id == Experiment.condition_id)
    )
    if platform_name:
        stmt = stmt.where(Platform.name == platform_name)
    if condition_name:
        stmt = stmt.where(Condition.name == condition_name)
    if average_only:
        stmt = stmt.where(Experiment.is_average.is_(True))
    return int(db.execute(stmt).scalar() or 0)


# ── supporting queries ──────────────────────────────────────────────────

def get_conditions(db: Session) -> List[Dict[str, Any]]:
    rows = db.query(Condition).order_by(Condition.name).all()
    return [
        {"id": c.id, "name": c.name, "description": c.description, "category": c.category}
        for c in rows
    ]


def get_vehicle_conditions(db: Session, vehicle_id: int) -> List[Dict[str, Any]]:
    """Conditions that have at least one experiment for the given vehicle."""
    vehicle = db.get(Vehicle, vehicle_id)
    if vehicle is None:
        return []

    stmt = (
        select(Condition.id, Condition.name, Condition.description, Condition.category)
        .join(Experiment, Experiment.condition_id == Condition.id)
        .where(Experiment.vehicle_id == vehicle_id)
        .distinct()
        .order_by(Condition.name)
    )
    return [
        {"id": r[0], "name": r[1], "description": r[2], "category": r[3]}
        for r in db.execute(stmt)
    ]


def get_vehicle_conditions_bulk(
    db: Session, vehicle_ids: Sequence[int]
) -> Dict[int, List[Dict[str, Any]]]:
    """Conditions available per vehicle — one query for the whole selection."""
    stmt = (
        select(
            Experiment.vehicle_id,
            Condition.id, Condition.name, Condition.description, Condition.category,
        )
        .join(Condition, Condition.id == Experiment.condition_id)
        .where(Experiment.vehicle_id.in_(list(vehicle_ids)))
        .distinct()
        .order_by(Condition.name)
    )
    out: Dict[int, List[Dict[str, Any]]] = {}
    seen = set()
    for vid, cid, cname, cdesc, ccat in db.execute(stmt):
        key = (vid, cid)
        if key in seen:
            continue
        seen.add(key)
        out.setdefault(vid, []).append(
            {"id": cid, "name": cname, "description": cdesc, "category": ccat}
        )
    return out


def get_vehicles_brief(db: Session) -> List[Dict[str, Any]]:
    """Flat vehicle list with platform/brand for the front-end selector."""
    stmt = (
        select(
            Vehicle.id, Vehicle.name, Vehicle.drive_type,
            Brand.name.label("brand"), Platform.name.label("platform"),
        )
        .join(Brand, Brand.id == Vehicle.brand_id)
        .join(Platform, Platform.id == Brand.platform_id)
        .order_by(Platform.name, Brand.name, Vehicle.name)
    )
    return [
        {"id": r[0], "name": r[1], "drive_type": r[2], "brand": r[3], "platform": r[4]}
        for r in db.execute(stmt)
    ]


def get_curve_names(db: Session, condition_id: int) -> List[str]:
    """Distinct curve names under a condition (lets the UI pick subsets)."""
    stmt = (
        select(Curve.name)
        .join(Experiment, Experiment.id == Curve.experiment_id)
        .where(Experiment.condition_id == condition_id)
        .distinct()
        .order_by(Curve.name)
    )
    return [r[0] for r in db.execute(stmt)]


# ── 规模估算（防 OOM 的准入检查） ───────────────────────────────────────

#: 单条曲线里的 series 条数取不到便宜的方式，按「原始数据 + 滤波」估 2 条
_ASSUMED_SERIES_PER_CURVE = 2


def estimate_compare_numbers(
    db: Session,
    vehicle_count: int,
    condition_id: int,
    curve_names: Optional[Set[str]] = None,
    exp_scope: Optional[str] = None,
) -> int:
    """粗估一次 /api/compare 会返回多少个数字（不是字节）。

    为什么不用「车辆数上限」：那个尺度是错的。200 台车只画 1 条曲线
    （实测 34 MB）完全没问题，而 50 台车画全部 7 条曲线（357 MB）才是
    会崩的那个。所以按**实际负载**卡，而不是按车辆数卡。

    估算公式::

        车辆数 × 实验数 × 曲线数 × series条数 × 单曲线点数 × 2(x,y)

    精度不重要 —— 它只需要区分「30 万」和「4000 万」两个数量级。
    ``Curve.point_count`` 是冗余列（导入时写好的采样点数），所以这里
    不需要解析任何 JSONB。
    """
    row = db.execute(
        select(
            func.max(Curve.point_count),
            func.count(func.distinct(Curve.name)),
        )
        .join(Experiment, Experiment.id == Curve.experiment_id)
        .where(Experiment.condition_id == condition_id)
    ).one()
    max_points = int(row[0] or 0)
    n_curves = len(curve_names) if curve_names else int(row[1] or 0)
    if not max_points or not n_curves:
        return 0

    if exp_scope == "avg_first":
        n_exps = 1
    else:
        counts = list(db.execute(
            select(func.count(Experiment.id))
            .where(Experiment.condition_id == condition_id)
            .group_by(Experiment.vehicle_id)
        ).scalars())
        n_exps = max(counts) if counts else 1

    return (
        vehicle_count * n_exps * n_curves
        * _ASSUMED_SERIES_PER_CURVE * max_points * 2
    )
