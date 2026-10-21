"""SQLAlchemy 2.0 ORM models — 元数据驱动版.

Schema overview
================

    配置层（只加数据，不改表结构）
      attr_defs     字段注册表
      dim_entries   维度层级字典（品牌/子品牌、平台/子平台 …）
      tree_profiles 导航树层级配置
      dataset_types 数据格式注册

    实例层
      vehicles            1──N vehicle_attr_values
                          1──N vehicle_modes
                          1──N test_orders
                          1──N datasets

    数据层
      datasets        N──1 conditions / dataset_types / vehicle_modes / test_orders
                      1──N metric_values

Design rationale
----------------
* **车辆基本信息不再建列。** 每个字段在 ``attr_defs`` 里是一行，
  具体值写在 ``vehicle_attr_values``（EAV）。新增字段 = INSERT 一行，
  不改表；字段未来增删都不影响其它字段的数据。

* **层级不写死。** ``dim_entries`` 用 ``parent_id`` 表达同一维度内部的
  父子级（如 品牌 → 子品牌、平台 → 子平台）；``tree_profiles.levels``
  是 JSON 配置，声明导航树用哪些层级、什么顺序。树在运行时按车辆实际
  数据投影，不持久化父子节点。

* **车名全局唯一**（``vehicles.name`` unique）。一台车可有多个操稳/平顺
  单号，因此单号单独存 ``test_orders``，按 (车, 学科, 单号) 唯一；
  单号可能缺失，所以 ``datasets.test_order_id`` 可空。

* **试验模式属于试验侧。** ``vehicle_modes`` 按车存模式（舒适/运动/…），
  不同车辆的模式数量不固定。分析单元 segment = (vehicle_id, mode_id)，
  对比、取数都以它为单位；``datasets.mode_id`` 可空（该车没有模式时）。

* **数据格式不写死。** ``datasets`` 是统一信封：一条数据一行，
  ``dataset_type_id`` 决定 payload 的结构含义，``payload``（JSONB）
  整块存原始结构。新增格式 = 在 ``dataset_types`` 加一行，
  不需要 ALTER TABLE，也不需要拆新表。

* **标量单独抽一层。** 只有需要跨车统计/筛选的数值才写入
  ``metric_values``；曲线等大块结构留在 ``datasets.payload`` 里。
  指标名与旧版一致按 EAV 存，``raw_key`` 原样保留，数字前缀不入库。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

# JSONB on PostgreSQL, plain JSON on any other backend (SQLite for local dev).
JSONType = JSON().with_variant(JSONB, "postgresql")


# ── 配置层 ─────────────────────────────────────────────────────────────

class AttrDef(Base):
    """车辆基本信息字段定义（字段注册表）.

    ``code`` 是程序里引用的稳定键（如 ``brand`` / ``platform``），
    ``name`` 是展示名（品牌 / 平台）。``value_type`` 决定值写进
    ``vehicle_attr_values`` 的哪一列：

    ==========  ==========================================
    value_type  落库
    ==========  ==========================================
    text         value_text
    number       value_num（同时回填 value_text）
    bool         value_num（0/1）
    date         value_text（ISO 字符串）
    dim          value_dim_id（引用 dim_entries）
    ==========  ==========================================

    ``dim_code`` 仅对 ``value_type='dim'`` 有意义，指向
    ``dim_entries.dim_code``（如 brand / platform）。
    """

    __tablename__ = "attr_defs"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    value_type: Mapped[str] = mapped_column(String(20), default="text")
    dim_code: Mapped[Optional[str]] = mapped_column(String(50), index=True)
    unit: Mapped[Optional[str]] = mapped_column(String(50))
    required: Mapped[bool] = mapped_column(default=False)
    show_in_list: Mapped[bool] = mapped_column(default=True)
    # 非维度表的枚举选项（如 能源形式）；value_type='dim' 时留空
    options: Mapped[Optional[List]] = mapped_column(JSONType)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    values: Mapped[List["VehicleAttrValue"]] = relationship(
        back_populates="attr", cascade="all, delete-orphan",
    )


class DimEntry(Base):
    """维度字典的一项，同一维度内可多层（品牌 → 子品牌）.

    车辆只引用最深节点（例如只写「子品牌」），导航树投影时沿
    ``parent_id`` 向上补齐「品牌」层；``path`` 是物化路径
    （如 ``/1/7/``），便于一次查出祖先链。
    """

    __tablename__ = "dim_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    dim_code: Mapped[str] = mapped_column(String(50), index=True)
    name: Mapped[str] = mapped_column(String(100))
    parent_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("dim_entries.id", ondelete="CASCADE"), index=True,
    )
    path: Mapped[str] = mapped_column(String(500), default="", index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    parent: Mapped[Optional["DimEntry"]] = relationship(
        back_populates="children", remote_side="DimEntry.id",
    )
    children: Mapped[List["DimEntry"]] = relationship(
        back_populates="parent", cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint("dim_code", "parent_id", "name", name="uq_dim_entry_sibling"),
        Index("ix_dim_entries_dim_parent", "dim_code", "parent_id", "sort_order"),
    )


class TreeProfile(Base):
    """导航树配置 — 只存层级声明，不存树节点.

    ``levels`` 形如::

        [
          {"source": "attr", "key": "platform", "missing": "skip"},
          {"source": "attr", "key": "brand",    "missing": "skip"},
          {"source": "vehicle"},
          {"source": "test_mode", "label": "试验模式"}
        ]

    ``source`` 只有三种：``attr``（引用 attr_defs.code）、
    ``vehicle``（车名）、``test_mode``（vehicle_modes.mode）。
    ``missing='skip'`` 表示该车没有此字段时直接跳过这一层，
    不生成空节点。
    """

    __tablename__ = "tree_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    levels: Mapped[List] = mapped_column(JSONType, default=list)
    is_default: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class DatasetType(Base):
    """数据格式注册表.

    一种新的试验数据格式（操稳、平顺、未来的 …）只在这里加一行；
    ``datasets.dataset_type_id`` 决定 payload 怎么解释。``code``
    是程序里引用的稳定键。
    """

    __tablename__ = "dataset_types"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[Optional[str]] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    datasets: Mapped[List["Dataset"]] = relationship(back_populates="dataset_type")


class Condition(Base):
    """工况 — 角阶跃 / 角脉冲 / 稳态回转 …（沿用原有的查询维度）."""

    __tablename__ = "conditions"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text)
    category: Mapped[Optional[str]] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    datasets: Mapped[List["Dataset"]] = relationship(back_populates="condition")


# ── 实例层 ─────────────────────────────────────────────────────────────

class Vehicle(Base):
    """车辆主表 — 车名全局唯一，其余属性全部走 EAV."""

    __tablename__ = "vehicles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    attr_values: Mapped[List["VehicleAttrValue"]] = relationship(
        back_populates="vehicle", cascade="all, delete-orphan",
    )
    modes: Mapped[List["VehicleMode"]] = relationship(
        back_populates="vehicle", cascade="all, delete-orphan",
    )
    test_orders: Mapped[List["TestOrder"]] = relationship(
        back_populates="vehicle", cascade="all, delete-orphan",
    )
    datasets: Mapped[List["Dataset"]] = relationship(
        back_populates="vehicle", cascade="all, delete-orphan",
    )


class VehicleAttrValue(Base):
    """某台车的某个字段值 — 一台车 × 一个字段只有一行.

    按 ``attr_defs.value_type`` 写入对应列；未填写的字段不建行，
    读取时返回 null（而不是空字符串）。
    """

    __tablename__ = "vehicle_attr_values"

    id: Mapped[int] = mapped_column(primary_key=True)
    vehicle_id: Mapped[int] = mapped_column(
        ForeignKey("vehicles.id", ondelete="CASCADE"), index=True,
    )
    attr_id: Mapped[int] = mapped_column(
        ForeignKey("attr_defs.id", ondelete="CASCADE"), index=True,
    )
    value_text: Mapped[Optional[str]] = mapped_column(String(500))
    value_num: Mapped[Optional[float]] = mapped_column(Float)
    value_dim_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("dim_entries.id", ondelete="SET NULL"), index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(),
    )

    vehicle: Mapped["Vehicle"] = relationship(back_populates="attr_values")
    attr: Mapped["AttrDef"] = relationship(back_populates="values")
    dim_entry: Mapped[Optional["DimEntry"]] = relationship()

    __table_args__ = (
        UniqueConstraint("vehicle_id", "attr_id", name="uq_vehicle_attr"),
        # 按维度节点分组（导航树投影）
        Index("ix_vav_attr_dim", "attr_id", "value_dim_id"),
        # 数值字段筛选/排序
        Index("ix_vav_attr_num", "attr_id", "value_num"),
    )


class VehicleMode(Base):
    """车辆拥有的试验模式（舒适 / 运动 / …）.

    不同车辆的模式数量不固定，所以按车建行，而不是做成全局字典。
    (vehicle_id, mode) 唯一。
    """

    __tablename__ = "vehicle_modes"

    id: Mapped[int] = mapped_column(primary_key=True)
    vehicle_id: Mapped[int] = mapped_column(
        ForeignKey("vehicles.id", ondelete="CASCADE"), index=True,
    )
    mode: Mapped[str] = mapped_column(String(50))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    vehicle: Mapped["Vehicle"] = relationship(back_populates="modes")
    datasets: Mapped[List["Dataset"]] = relationship(back_populates="mode")

    __table_args__ = (
        UniqueConstraint("vehicle_id", "mode", name="uq_vehicle_mode"),
    )


class TestOrder(Base):
    """试验单号 — 一台车可以有多个操稳单号 / 平顺单号.

    单号按「车 × 学科 × 单号」唯一；与数据集是多对一，且允许缺失
    （datasets.test_order_id 可空）。平顺数据暂不设计，但
    ``discipline`` 已预留，后续补表不需要改这里。
    """

    __tablename__ = "test_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    vehicle_id: Mapped[int] = mapped_column(
        ForeignKey("vehicles.id", ondelete="CASCADE"), index=True,
    )
    discipline: Mapped[str] = mapped_column(String(20), default="操稳")   # 操稳 / 平顺
    order_no: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    vehicle: Mapped["Vehicle"] = relationship(back_populates="test_orders")
    datasets: Mapped[List["Dataset"]] = relationship(back_populates="test_order")

    __table_args__ = (
        UniqueConstraint(
            "vehicle_id", "discipline", "order_no", name="uq_test_order",
        ),
    )


# ── 数据层 ─────────────────────────────────────────────────────────────

class Dataset(Base):
    """统一数据信封 — 一条数据集一行，payload 存原始结构.

    对应导入时的一个数据文件（如 ``舒适/角阶跃.json``）。关系全部
    抽成外键，payload 保持原样：

    * ``mode_id``       该数据属于哪个试验模式（无模式的车可空）
    * ``test_order_id`` 关联的操稳/平顺单号（单号缺失时可空）
    * ``condition_id``  工况
    * ``dataset_type_id`` 数据格式（决定 payload 的含义）

    ``exp_no`` 仍是字符串（``'1'`` / ``'2'`` / ``'平均'``），
    ``is_average`` + ``sort_order`` 保证排序稳定。
    """

    __tablename__ = "datasets"

    id: Mapped[int] = mapped_column(primary_key=True)
    vehicle_id: Mapped[int] = mapped_column(
        ForeignKey("vehicles.id", ondelete="CASCADE"), index=True,
    )
    mode_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("vehicle_modes.id", ondelete="SET NULL"), index=True,
    )
    test_order_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("test_orders.id", ondelete="SET NULL"), index=True,
    )
    condition_id: Mapped[int] = mapped_column(
        ForeignKey("conditions.id"), index=True,
    )
    dataset_type_id: Mapped[int] = mapped_column(
        ForeignKey("dataset_types.id"), index=True,
    )
    name: Mapped[str] = mapped_column(String(300))                     # 文件 / 数据集名
    source_path: Mapped[Optional[str]] = mapped_column(String(1000))   # 导入时的相对路径
    exp_no: Mapped[str] = mapped_column(String(20), default="1")       # '1' / '2' / '平均'
    is_average: Mapped[bool] = mapped_column(default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[Dict[str, Any]] = mapped_column(JSONType)          # 任意结构，格式由 type 决定
    # 冗余采样点数：曲线接口估算负载用，避免为了知道长度去解析 payload
    point_count: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    vehicle: Mapped["Vehicle"] = relationship(back_populates="datasets")
    mode: Mapped[Optional["VehicleMode"]] = relationship(back_populates="datasets")
    test_order: Mapped[Optional["TestOrder"]] = relationship(back_populates="datasets")
    condition: Mapped["Condition"] = relationship(back_populates="datasets")
    dataset_type: Mapped["DatasetType"] = relationship(back_populates="datasets")
    metrics: Mapped[List["MetricValue"]] = relationship(
        back_populates="dataset", cascade="all, delete-orphan",
    )

    __table_args__ = (
        # 同一 (车, 模式, 工况, 格式, 文件名, 实验号) 只存一条：重复导入即覆盖
        UniqueConstraint(
            "vehicle_id", "mode_id", "condition_id", "dataset_type_id",
            "name", "exp_no", name="uq_dataset_slot",
        ),
        Index("ix_datasets_vehicle_condition", "vehicle_id", "condition_id"),
        Index("ix_datasets_condition_type", "condition_id", "dataset_type_id"),
    )


class MetricValue(Base):
    """从 payload 里抽出的标量指标 — 只有需要跨车统计的数值才进这里.

    指标名 / 单位 / raw_key 的规则沿用旧版：``raw_key`` 原样保留，
    数字前缀（``'13-'``）不入库、不排序。
    """

    __tablename__ = "metric_values"

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("datasets.id", ondelete="CASCADE"), index=True,
    )
    name: Mapped[str] = mapped_column(String(200))                      # 指标名
    unit: Mapped[Optional[str]] = mapped_column(String(50))            # 单位
    raw_key: Mapped[str] = mapped_column(String(300))                  # 原始 key
    value: Mapped[Optional[float]] = mapped_column(Float)              # 数值
    text_value: Mapped[Optional[str]] = mapped_column(String(500))     # 文本值（备用）
    category: Mapped[Optional[str]] = mapped_column(String(100))       # 指标分类
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    dataset: Mapped["Dataset"] = relationship(back_populates="metrics")

    __table_args__ = (
        Index("ix_metric_values_dataset_name", "dataset_id", "name"),
        Index("ix_metric_values_name", "name"),
    )
