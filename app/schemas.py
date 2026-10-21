"""Pydantic v2 schemas — 元数据驱动版.

大响应（曲线 / 对比）走 ``json_response`` 直接序列化，不经过这里的模型；
这些 schema 主要用于文档、参数校验和结构清晰的轻量接口。

Python 3.8 兼容：只用 ``typing.List/Dict/Optional``。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


# ── 配置层 ─────────────────────────────────────────────────────────────

class AttrDefOut(BaseModel):
    id: int
    code: str
    name: str
    value_type: str
    dim_code: Optional[str] = None
    unit: Optional[str] = None
    required: bool = False
    show_in_list: bool = True
    options: Optional[List[Any]] = None
    sort_order: int = 0
    is_active: bool = True

    model_config = ConfigDict(from_attributes=True)


class AttrDefIn(BaseModel):
    code: Optional[str] = None
    name: str
    value_type: str = "text"
    dim_code: Optional[str] = None
    unit: Optional[str] = None
    required: bool = False
    show_in_list: bool = True
    options: Optional[List[Any]] = None
    sort_order: int = 0


class AttrDefUpdate(BaseModel):
    name: Optional[str] = None
    value_type: Optional[str] = None
    dim_code: Optional[str] = None
    unit: Optional[str] = None
    required: Optional[bool] = None
    show_in_list: Optional[bool] = None
    options: Optional[List[Any]] = None
    sort_order: Optional[int] = None
    is_active: Optional[bool] = None


class DimEntryOut(BaseModel):
    id: int
    dim_code: str
    name: str
    parent_id: Optional[int] = None
    path: str = ""
    sort_order: int = 0

    model_config = ConfigDict(from_attributes=True)


class TreeLevel(BaseModel):
    source: str                       # attr / vehicle / test_mode
    key: Optional[str] = None
    label: Optional[str] = None
    missing: Optional[str] = None
    empty_label: Optional[str] = None


class TreeProfileOut(BaseModel):
    code: str
    name: str
    levels: List[Dict[str, Any]] = Field(default_factory=list)
    is_default: bool = False

    model_config = ConfigDict(from_attributes=True)


class DatasetTypeOut(BaseModel):
    code: str
    name: str
    description: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class ConditionOut(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    category: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class MetaOut(BaseModel):
    attrs: List[AttrDefOut]
    dims: Dict[str, List[DimEntryOut]]
    tree_profiles: List[TreeProfileOut]
    dataset_types: List[DatasetTypeOut]
    conditions: List[ConditionOut]
    mode_suggestions: List[str]


# ── 导航树 ─────────────────────────────────────────────────────────────

class SegmentRef(BaseModel):
    """分析单元：车 × 试验模式。"""
    vehicle_id: int
    vehicle_name: str
    mode_id: Optional[int] = None
    mode: Optional[str] = None


class NavNodeOut(BaseModel):
    key: str
    label: str
    kind: str                          # attr / vehicle / test_mode / segment
    level: int = 0
    count: int = 0
    sort_order: int = 0
    children: List["NavNodeOut"] = Field(default_factory=list)

    # 仅 kind=attr 时有意义
    code: Optional[str] = None
    value_id: Optional[int] = None

    # 仅叶子（kind=segment）时有意义
    segment: Optional[SegmentRef] = None
    vehicle_id: Optional[int] = None
    vehicle_name: Optional[str] = None
    mode_id: Optional[int] = None
    mode: Optional[str] = None
    attrs: Optional[Dict[str, Any]] = None


class NavTreeOut(BaseModel):
    profile: Optional[TreeProfileOut] = None
    levels: List[Dict[str, Any]] = Field(default_factory=list)
    nodes: List[NavNodeOut] = Field(default_factory=list)


# ── 车辆 ───────────────────────────────────────────────────────────────

class VehicleModeOut(BaseModel):
    id: int
    mode: str
    sort_order: int = 0

    model_config = ConfigDict(from_attributes=True)


class TestOrderIn(BaseModel):
    discipline: str = "操稳"
    order_no: str


class TestOrderOut(BaseModel):
    id: int
    discipline: str
    order_no: str


class VehicleBriefOut(BaseModel):
    id: int
    name: str
    attrs: Dict[str, Any] = Field(default_factory=dict)
    modes: List[VehicleModeOut] = Field(default_factory=list)
    dataset_count: int = 0


class VehicleDetailOut(BaseModel):
    id: int
    name: str
    attrs: Dict[str, Any] = Field(default_factory=dict)
    modes: List[VehicleModeOut] = Field(default_factory=list)
    test_orders: List[TestOrderOut] = Field(default_factory=list)
    dataset_count: int = 0


class VehicleIn(BaseModel):
    name: str
    attrs: Dict[str, Any] = Field(default_factory=dict)
    modes: List[str] = Field(default_factory=list)
    test_orders: List[Union[str, TestOrderIn]] = Field(default_factory=list)


class VehicleUpdateIn(BaseModel):
    name: Optional[str] = None
    attrs: Optional[Dict[str, Any]] = None
    modes: Optional[List[str]] = None
    test_orders: Optional[List[Union[str, TestOrderIn]]] = None


# ── 数据 ───────────────────────────────────────────────────────────────

class IndicatorOut(BaseModel):
    name: str
    unit: Optional[str] = None
    raw_key: str
    value: Optional[float] = None
    text_value: Optional[str] = None


class SeriesItem(BaseModel):
    x: List[float] = Field(default_factory=list)
    y: List[float] = Field(default_factory=list)
    curvetype: Optional[str] = "line"
    curvename: Optional[str] = None


class CurveOut(BaseModel):
    name: str
    title: Optional[str] = None
    xlabel: Optional[str] = None
    ylabel: Optional[str] = None
    point_count: Optional[int] = None
    series_names: List[str] = Field(default_factory=list)
    series_map: Dict[str, Any] = Field(default_factory=dict)
    series: List[SeriesItem] = Field(default_factory=list)


class ExperimentOut(BaseModel):
    dataset_id: int
    exp_no: str
    is_average: bool = False
    name: str
    dataset_type: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None
    params: Dict[str, IndicatorOut] = Field(default_factory=dict)
    # {group: {curve_name: curve}}，group 由数据决定（cal / duiqi / 新分组）
    curves: Dict[str, Dict[str, CurveOut]] = Field(default_factory=dict)


class SegmentDataOut(BaseModel):
    vehicle_id: int
    vehicle_name: str
    mode_id: Optional[int] = None
    mode: Optional[str] = None
    attrs: Dict[str, Any] = Field(default_factory=dict)
    condition_id: int
    condition_name: str
    dataset_type: Optional[str] = None
    experiments: List[ExperimentOut] = Field(default_factory=list)
    curve_meta: Optional[Dict[str, Any]] = None


class CompareOut(BaseModel):
    condition_id: int
    condition_name: str
    dataset_type: Optional[str] = None
    vehicles: List[Dict[str, Any]] = Field(default_factory=list)
    curve_meta: Optional[Dict[str, Any]] = None


# ── 统计 ───────────────────────────────────────────────────────────────

class StatsIndicatorOut(BaseModel):
    value: Optional[float] = None
    text_value: Optional[str] = None
    unit: Optional[str] = None
    name: str


class StatsRowOut(BaseModel):
    vehicle_id: int
    vehicle: str
    mode_id: Optional[int] = None
    mode: Optional[str] = None
    condition: str
    dataset_id: int
    exp_no: str
    is_average: bool = False
    attrs: Dict[str, Any] = Field(default_factory=dict)
    indicators: Dict[str, StatsIndicatorOut] = Field(default_factory=dict)


class StatsPageOut(BaseModel):
    total: int
    offset: int
    limit: int
    rows: List[StatsRowOut] = Field(default_factory=list)


NavNodeOut.model_rebuild()
