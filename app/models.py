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
                      1──N curves

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
  ``dataset_type_id`` 决定这条数据怎么解释。新增格式 = 在
  ``dataset_types`` 加一行，不需要 ALTER TABLE。

* **大块数据按用途拆开存，不塞进一个 payload。** 这是旧版最关键的性能设计，
  不能丢：

  - 标量指标 → ``metric_values``（EAV）。跨车统计、按指标名筛选都走这张表。
  - 曲线 → ``curves``，**一条曲线一行**，``series`` 整块 JSONB，
    另存 ``point_count``。这样 ``curves=`` 只勾选部分曲线时可以下推到 SQL
    （``selectinload`` 只加载选中的行），``meta_only`` 也能完全不碰
    ``series`` —— 曲线单条可达 10 万点，一条 dataset 一个 payload 的话，
    读任何一条线都得把全部线反序列化出来。
  - 其余无法归入上面两类的内容 → ``datasets.payload``（可空）。新格式如果
    自带特殊结构，放这里；有标量和曲线的部分仍走前两张表。

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
    dim_code: Mapped[str] = mapped_column(String(50))
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
        ForeignKey("vehicles.id", ondelete="CASCADE"),
    )
    attr_id: Mapped[int] = mapped_column(
        ForeignKey("attr_defs.id", ondelete="CASCADE"),
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
        ForeignKey("vehicles.id", ondelete="CASCADE"),
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
        ForeignKey("vehicles.id", ondelete="CASCADE"),
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
    """统一数据信封 — 一条数据集（≈ 一次实验）一行.

    对应导入数据里的一个实验号（如 ``舒适/角阶跃.json`` 里的 ``'1'``）。
    关系全部抽成外键：

    * ``mode_id``       该数据属于哪个试验模式（无模式的车可空）
    * ``test_order_id`` 关联的操稳/平顺单号（单号缺失时可空）
    * ``condition_id``  工况
    * ``dataset_type_id`` 数据格式（决定这条数据怎么解释）

    数据本身按用途分三处，payload 只做兜底，不是原始文件的完整拷贝：

    * 标量指标 → ``metric_values``
    * 曲线     → ``curves``（一条曲线一行，整块 JSONB）
    * 其余结构 → ``payload``（可空）

    ``exp_no`` 仍是字符串（``'1'`` / ``'2'`` / ``'平均'``），
    ``is_average`` + ``sort_order`` 保证排序稳定。
    """

    __tablename__ = "datasets"

    id: Mapped[int] = mapped_column(primary_key=True)
    vehicle_id: Mapped[int] = mapped_column(
        ForeignKey("vehicles.id", ondelete="CASCADE"),
    )
    mode_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("vehicle_modes.id", ondelete="SET NULL"), index=True,
    )
    test_order_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("test_orders.id", ondelete="SET NULL"), index=True,
    )
    condition_id: Mapped[int] = mapped_column(
        ForeignKey("conditions.id"),
    )
    dataset_type_id: Mapped[int] = mapped_column(
        ForeignKey("dataset_types.id"),
    )
    name: Mapped[str] = mapped_column(String(300))                     # 文件 / 数据集名
    exp_no: Mapped[str] = mapped_column(String(20), default="1")       # '1' / '2' / '平均'
    is_average: Mapped[bool] = mapped_column(default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    # 兜底容器：只放无法归入 metric_values / curves 的结构，可为空
    payload: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    vehicle: Mapped["Vehicle"] = relationship(back_populates="datasets")
    mode: Mapped[Optional["VehicleMode"]] = relationship(back_populates="datasets")
    test_order: Mapped[Optional["TestOrder"]] = relationship(back_populates="datasets")
    condition: Mapped["Condition"] = relationship(back_populates="datasets")
    dataset_type: Mapped["DatasetType"] = relationship(back_populates="datasets")
    metrics: Mapped[List["MetricValue"]] = relationship(
        back_populates="dataset", cascade="all, delete-orphan",
    )
    curves: Mapped[List["Curve"]] = relationship(
        back_populates="dataset", cascade="all, delete-orphan",
    )

    __table_args__ = (
        # 同一 (车, 模式, 工况, 格式, 文件名, 实验号) 只存一条：重复导入即覆盖
        UniqueConstraint(
            "vehicle_id", "mode_id", "condition_id", "dataset_type_id",
            "name", "exp_no", name="uq_dataset_slot",
        ),
        # 单车详情 / 重导时按车 × 工况取数，并保证实验顺序
        Index("ix_datasets_vehicle_condition_sort",
              "vehicle_id", "condition_id", "sort_order"),
        # 对比取数的主路径：先收敛工况 + 数据格式，再按车 / 模式取
        Index("ix_datasets_compare",
              "condition_id", "dataset_type_id", "vehicle_id", "mode_id"),
    )


class MetricValue(Base):
    """从 payload 里抽出的标量指标 — 只有需要跨车统计的数值才进这里.

    指标名 / 单位 / raw_key 的规则沿用旧版：``raw_key`` 原样保留，
    数字前缀（``'13-'``）不入库、不排序。

    两条热路径各配一个复合索引：

    * 单车/对比取数：``dataset_id + name``（也支撑 dataset_id 前缀查询）
    * 跨车指标统计：``name + dataset_id``（先按指标名收敛，再回连数据集）
    * 按原始 key 精确查：``dataset_id + raw_key``（旧版保留项）
    """

    __tablename__ = "metric_values"

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("datasets.id", ondelete="CASCADE"),
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
        Index("ix_metric_values_dataset_raw", "dataset_id", "raw_key"),
        Index("ix_metric_values_name_dataset", "name", "dataset_id"),
    )


# ── 曲线 ───────────────────────────────────────────────────────────────

class Curve(Base):
    """曲线 — 一条曲线一行，series 整块 JSONB（沿用旧版存储策略）.

    为什么必须独立成表：

    * 曲线只会整体取回来画图，不会按 y 值查询，所以整块 JSONB 是 O(1)
      读取，不需要拆 ``points(curve_id, x, y)``。
    * 单条曲线可达 10 万点，一条数据集的全部曲线几十万点。只有独立成行，
      才能做到「只取勾选的曲线」：``curves=`` 过滤能下推到 SQL
      （只加载命中的 ``Curve.series``），``meta_only`` 也能只读元信息、
      完全不碰 series。
    * ``point_count`` 冗余存储，负载估算时不用解析 series。

    ``group`` 沿用 ``'cal'``（计算曲线）/ ``'duiqi'``（对齐曲线），
    前端按组展示；新格式可以新增 group 取值，不需要改表。
    """

    __tablename__ = "curves"

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("datasets.id", ondelete="CASCADE"),
    )
    group: Mapped[str] = mapped_column(String(20), index=True)         # 'cal' | 'duiqi'
    name: Mapped[str] = mapped_column(String(200))                     # '方向盘转角-时间'
    title: Mapped[Optional[str]] = mapped_column(String(200))
    xlabel: Mapped[Optional[str]] = mapped_column(String(100))
    ylabel: Mapped[Optional[str]] = mapped_column(String(200))
    series: Mapped[List] = mapped_column(JSONType)                     # [{x,y,curvetype,curvename}, …]
    point_count: Mapped[Optional[int]] = mapped_column(Integer)        # 采样点数（冗余）
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    dataset: Mapped["Dataset"] = relationship(back_populates="curves")

    __table_args__ = (
        Index("ix_curves_dataset_group", "dataset_id", "group", "sort_order"),
    )
