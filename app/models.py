"""
SQLAlchemy 2.0 ORM models for vehicle 操稳 (handling & stability) test data.

Schema overview
================

    platforms  1──N  brands  1──N  vehicles
                                         │
                                         │ 1
                                         N
                                    experiments  N──1  conditions
                                         │ 1
                                    ┌────┴────┐
                                    N         N
                               indicators   curves

Design rationale
----------------
* **Indicators** are stored as key-value rows (EAV style).  The raw key
  looks like ``'10-车速(km/h)'`` — we split it into ``name`` (车速) and
  ``unit`` (km/h) *and* keep ``raw_key`` verbatim so the original
  dictionary can be reconstructed losslessly.  New indicator names never
  require a migration.  The numeric prefix is deliberately **not** stored:
  it is optional in real data and meaningless across conditions.

* **Curves** come in two groups — ``curves_cal`` (原始/滤波, group='cal')
  and ``curves_duiqi`` (对齐后, group='duiqi').  One `curves` row holds
  one named curve (e.g. '方向盘转角-时间') and its full payload
  (title / xlabel / ylabel / series) in JSONB.  Because the front-end
  always fetches an entire curve for plotting (never queries individual
  y-values), JSONB gives O(1) full-row retrieval.

* **Experiments** keyed by a *string* ``experiment_no`` so that the
  special '平均' (average) entry fits naturally alongside '1', '2', …
  ``is_average`` + ``sort_order`` keep ordering deterministic
  (numeric runs first, 平均 last).

* **Vehicle attributes** use dedicated columns for common fields plus a
  flexible ``extra_attrs`` JSONB column — no migration for new attrs.
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
# SQLite does not understand JSONB, so this variant keeps the schema portable.
JSONType = JSON().with_variant(JSONB, "postgresql")


# ── dimension tables (platform → brand → vehicle) ──────────────────────

class Platform(Base):
    """整车平台 (top of the hierarchy)."""

    __tablename__ = "platforms"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    brands: Mapped[List["Brand"]] = relationship(
        back_populates="platform", cascade="all, delete-orphan",
    )


class Brand(Base):
    """品牌 (belongs to a platform)."""

    __tablename__ = "brands"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), index=True)
    platform_id: Mapped[int] = mapped_column(
        ForeignKey("platforms.id", ondelete="CASCADE"), index=True,
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    platform: Mapped["Platform"] = relationship(back_populates="brands")
    vehicles: Mapped[List["Vehicle"]] = relationship(
        back_populates="brand", cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint("name", "platform_id", name="uq_brand_platform"),
    )


class Vehicle(Base):
    """车型 (belongs to a brand)."""

    __tablename__ = "vehicles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    brand_id: Mapped[int] = mapped_column(
        ForeignKey("brands.id", ondelete="CASCADE"), index=True,
    )
    # ── common fixed attributes ──
    drive_type: Mapped[Optional[str]] = mapped_column(String(50))       # 驱动方式
    vin: Mapped[Optional[str]] = mapped_column(String(50))             # 车架号
    model_year: Mapped[Optional[int]] = mapped_column(Integer)         # 年款
    # ── flexible extension point ──
    extra_attrs: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSONType, default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    brand: Mapped["Brand"] = relationship(back_populates="vehicles")
    experiments: Mapped[List["Experiment"]] = relationship(
        back_populates="vehicle", cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint("name", "brand_id", name="uq_vehicle_brand"),
    )


# ── condition (工况) lookup ─────────────────────────────────────────────

class Condition(Base):
    """工况 — e.g. 角阶跃 / 角脉冲 / 稳态圆周 (操稳试验)."""

    __tablename__ = "conditions"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text)
    category: Mapped[Optional[str]] = mapped_column(String(100))        # 工况分类
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    experiments: Mapped[List["Experiment"]] = relationship(
        back_populates="condition",
    )


# ── experiment (one test run of a vehicle under a condition) ────────────

class Experiment(Base):
    """一次实验 (a single test run).

    ``experiment_no`` is a *string* so the special '平均' (average)
    entry fits alongside '1', '2', ….  A vehicle × condition pair may
    be tested a different number of times.
    """

    __tablename__ = "experiments"

    id: Mapped[int] = mapped_column(primary_key=True)
    vehicle_id: Mapped[int] = mapped_column(
        ForeignKey("vehicles.id", ondelete="CASCADE"), index=True,
    )
    condition_id: Mapped[int] = mapped_column(
        ForeignKey("conditions.id"), index=True,
    )
    # '1', '2', '3', …  or  '平均'
    experiment_no: Mapped[str] = mapped_column(String(20))
    is_average: Mapped[bool] = mapped_column(default=False)            # 是否平均工况
    sort_order: Mapped[int] = mapped_column(Integer, default=0)        # 排序（平均放最后）
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    vehicle: Mapped["Vehicle"] = relationship(back_populates="experiments")
    condition: Mapped["Condition"] = relationship(back_populates="experiments")
    indicators: Mapped[List["Indicator"]] = relationship(
        back_populates="experiment", cascade="all, delete-orphan",
    )
    curves: Mapped[List["Curve"]] = relationship(
        back_populates="experiment", cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint(
            "vehicle_id", "condition_id", "experiment_no",
            name="uq_exp_vehicle_condition_no",
        ),
        Index("ix_exp_vehicle_cond_sort", "vehicle_id", "condition_id", "sort_order"),
    )


# ── indicator (EAV-style key-value row) ─────────────────────────────────

class Indicator(Base):
    """指标 — one row per indicator per experiment.

    Raw key example::

        '13-侧向加速度稳态值(g)'
         │   │            └─ unit
         │   └─ name
         └─ 数字前缀（**不入库**）

    ``name`` / ``unit`` 从 raw_key 拆出来做检索与展示，``raw_key`` 原样保留
    保证无损还原。数字前缀不再单独存列 —— 它可能缺失（真实数据里会出现
    ``'侧向加速度稳态值(g)'`` 这种无编号写法）、跨工况含义不一致
    （「车速」角阶跃=10 / 角脉冲=100），且不唯一（17 重复），
    保留它只会误导排序。前端按 ``name`` 排序。
    """

    __tablename__ = "indicators"

    id: Mapped[int] = mapped_column(primary_key=True)
    experiment_id: Mapped[int] = mapped_column(
        ForeignKey("experiments.id", ondelete="CASCADE"),
    )
    name: Mapped[str] = mapped_column(String(200), index=True)         # 指标名 (车速)
    unit: Mapped[Optional[str]] = mapped_column(String(50))            # 单位 (km/h)
    raw_key: Mapped[str] = mapped_column(String(300))                  # 原始 key ('10-车速(km/h)')
    value: Mapped[Optional[float]] = mapped_column(Float)              # 数值
    text_value: Mapped[Optional[str]] = mapped_column(String(500))     # 文本值（备用）
    category: Mapped[Optional[str]] = mapped_column(String(100))       # 指标分类
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    experiment: Mapped["Experiment"] = relationship(back_populates="indicators")

    __table_args__ = (
        # composite index: all indicators of an experiment
        Index("ix_indicators_exp_name", "experiment_id", "name"),
        Index("ix_indicators_exp_raw", "experiment_id", "raw_key"),
    )


# ── curve (JSONB payload) ───────────────────────────────────────────────

class Curve(Base):
    """曲线 — one row per named curve per experiment.

    Two *groups* exist per experiment:
      * ``group='cal'``    → curves_cal   (含 '原始数据' + '滤波' series)
      * ``group='duiqi'``  → curves_duiqi (对齐后的 '滤波' series)

    ``series`` holds the full payload in JSONB::

        [
          {'x': [...], 'y': [...], 'curvetype': 'line', 'curvename': '原始数据'},
          {'x': [...], 'y': [...], 'curvetype': 'line', 'curvename': '滤波'},
        ]
    """

    __tablename__ = "curves"

    id: Mapped[int] = mapped_column(primary_key=True)
    experiment_id: Mapped[int] = mapped_column(
        ForeignKey("experiments.id", ondelete="CASCADE"),
    )
    group: Mapped[str] = mapped_column(String(20), index=True)         # 'cal' | 'duiqi'
    name: Mapped[str] = mapped_column(String(200))                     # '方向盘转角-时间'
    title: Mapped[Optional[str]] = mapped_column(String(200))          # '方向盘转角vs时间'
    xlabel: Mapped[Optional[str]] = mapped_column(String(100))         # '时间（s）'
    ylabel: Mapped[Optional[str]] = mapped_column(String(200))         # '方向盘转角（deg）'
    series: Mapped[List] = mapped_column(JSONType)                     # [{x,y,curvetype,curvename}, …]
    # 原始采样点数（冗余存储，避免为了知道长度去解析整个 series）
    point_count: Mapped[Optional[int]] = mapped_column(Integer)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)        # 曲线顺序
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    experiment: Mapped["Experiment"] = relationship(back_populates="curves")

    __table_args__ = (
        Index("ix_curves_exp_group", "experiment_id", "group", "sort_order"),
    )
