"""Pydantic v2 response schemas (adapted to 操稳 test structure).

Python 3.8 compatible: uses ``typing.List/Dict/Optional`` instead of the
3.9+ builtin generics (``list[...]`` / ``dict[...]``).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict


# ── leaf schemas ────────────────────────────────────────────────────────

class IndicatorOut(BaseModel):
    """One indicator derived from a raw key like '13-侧向加速度稳态值(g)'.

    数字前缀不再单独返回 —— 真实数据里可能没有（``'侧向加速度稳态值(g)'``），
    跨工况含义也不一致，保留只会误导排序。前端按 ``name`` 排序。
    """
    name: str                          # 指标名 (侧向加速度稳态值)
    unit: Optional[str] = None         # 单位 (g)
    raw_key: str                       # 原始 key ('13-侧向加速度稳态值(g)')
    value: Optional[float] = None      # 数值
    text_value: Optional[str] = None   # 文本值（备用）

    model_config = ConfigDict(from_attributes=True)


class SeriesItem(BaseModel):
    """One line inside a curve: {x, y, curvetype, curvename}."""
    x: List[float]
    y: List[float]
    curvetype: Optional[str] = "line"
    curvename: Optional[str] = None


class CurveOut(BaseModel):
    """One named curve, e.g. '方向盘转角-时间'.

    ``series_names`` / ``series_map`` describe which data kinds are actually
    stored in this curve.  Do **not** assume it is exactly
    ``['原始数据', '滤波']`` — the set is data-driven and may be larger,
    smaller, or named differently.
    """
    name: str
    title: Optional[str] = None
    xlabel: Optional[str] = None
    ylabel: Optional[str] = None
    point_count: Optional[int] = None   # 原始采样点数（前端展示用）
    series_names: List[str] = []        # 实际存在的数据种类，按存储顺序
    series_map: Dict[str, Dict[str, Any]] = {}   # {curvename: {index, point_count}}
    series: List[SeriesItem]

    model_config = ConfigDict(from_attributes=True)


# ── nested schemas ──────────────────────────────────────────────────────

class ExperimentOut(BaseModel):
    """One test run: params + curves_cal + curves_duiqi."""
    experiment_id: int
    experiment_no: str                                  # '1' / '2' / '平均'
    is_average: bool = False
    params: Dict[str, IndicatorOut]                     # {raw_key: indicator}
    curves_cal: Dict[str, CurveOut]                     # {curve name: curve}
    curves_duiqi: Dict[str, CurveOut]

    model_config = ConfigDict(from_attributes=True)


class VehicleConditionDataOut(BaseModel):
    """API-2 response: indicators + curves for one vehicle × condition."""
    vehicle_id: int
    vehicle_name: str
    condition_id: int
    condition_name: str
    experiments: List[ExperimentOut]


class CompareVehicleOut(BaseModel):
    """One vehicle's payload inside the multi-vehicle comparison."""
    vehicle_id: int
    vehicle_name: str
    brand_name: Optional[str] = None
    platform_name: Optional[str] = None
    drive_type: Optional[str] = None
    experiments: List[ExperimentOut]


class CompareOut(BaseModel):
    """API for page 1: compare several vehicles under one condition.

    ``curve_meta`` aggregates what the payload actually contains so the
    front-end can render the curve panel dynamically:

        {"groups": {"cal": {"series_names": [...], "curves": {name: {...}}}, "duiqi": {...}}}
    """
    condition_id: int
    condition_name: str
    vehicles: List[CompareVehicleOut]
    curve_meta: Optional[Dict[str, Any]] = None


class VehicleBrief(BaseModel):
    id: int
    name: str


class ConditionOut(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    category: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)
