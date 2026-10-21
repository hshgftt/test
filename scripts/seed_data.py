"""
Seed script — generates 操稳 (handling & stability) test data at scale.

Two outputs:
  1. Database (SQLite / PostgreSQL) — platforms / brands / vehicles /
     conditions / experiments / indicators / curves
  2. ``data/`` — JSON files, one folder per vehicle, one JSON per condition:

         data/极光EV-01/稳态回转.json

     Each JSON has the shape::

         {"1": [params1, curves_cal1, curves_duiqi1],
          "2": [params2, curves_cal2, curves_duiqi2],
          "平均": [...]}

Signals come from a physical 2nd-order step/pulse model, so every indicator
is *derived from* its own curve — indicators and curves are self-consistent.

Scale
-----
Points are per-工况: 稳态回转 is the long one (10万点 by default), the rest
are 5千~1万.  Everything is tunable::

    python scripts/seed_data.py --preset full
    python scripts/seed_data.py --vehicles 200 --conditions 20 \
           --points 5000 --steady-points 100000
    python scripts/seed_data.py --preset demo --no-json     # DB only

Presets (vehicles × conditions × points / 稳态回转 points, 2 runs + 平均):

    tiny      11 ×  5 ×   500 /   5000      ~10 MB    几秒
    demo     200 × 20 ×  1000 /  10000      ~5 GB     几分钟
    standard 200 × 20 ×  3000 /  30000      ~15 GB    十几分钟
    full     200 × 20 ×  6000 / 100000      ~40 GB    三四十分钟

Python 3.8 compatible (``typing.Dict/List/Optional`` only).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.config import DATABASE_URL, IS_SQLITE
from app.database import SessionLocal, engine

# ── optional numpy fast path ──────────────────────────────────────────
# 10万点 × 7通道 × (原始+滤波+平均) 在纯 Python 下每秒只能处理几万个数，
# numpy 能把信号生成/平滑/取整/平均整体提速 5~10 倍。没有 numpy 也能跑。
try:
    import numpy as _np
except Exception:                                    # pragma: no cover
    _np = None

HAS_NUMPY = _np is not None

from app.models import (
    Base,
    Brand,
    Condition,
    Curve,
    Experiment,
    Indicator,
    Platform,
    Vehicle,
)

# ── project paths ──────────────────────────────────────────────────────
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")


# ═══════════════════════════════════════════════════════════════════════
# 1. 工况 (conditions) — 20 个，按信号族分类，每个工况独立采样点数
# ═══════════════════════════════════════════════════════════════════════

# family: step(阶跃) / pulse(脉冲) / steady(稳态) / ret(回正) / slalom(蛇行)
CONDITION_SPECS: List[Dict[str, Any]] = [
    {"name": "角阶跃",        "family": "step",   "pts": 6000,  "dt": 0.005, "cat": "操稳试验",
     "desc": "方向盘角阶跃输入试验（转向盘转角阶跃）"},
    {"name": "角阶跃-高速",    "family": "step",   "pts": 6000,  "dt": 0.005, "cat": "操稳试验",
     "desc": "高速工况下的方向盘角阶跃输入试验"},
    {"name": "角阶跃-低速",    "family": "step",   "pts": 5000,  "dt": 0.005, "cat": "操稳试验",
     "desc": "低速工况下的方向盘角阶跃输入试验"},
    {"name": "角脉冲",        "family": "pulse",  "pts": 6000,  "dt": 0.005, "cat": "操稳试验",
     "desc": "方向盘角脉冲输入试验"},
    {"name": "角脉冲-左打",    "family": "pulse",  "pts": 5000,  "dt": 0.005, "cat": "操稳试验",
     "desc": "左打方向角脉冲输入试验"},
    {"name": "角脉冲-右打",    "family": "pulse",  "pts": 5000,  "dt": 0.005, "cat": "操稳试验",
     "desc": "右打方向角脉冲输入试验"},
    {"name": "稳态回转",       "family": "steady", "pts": 100000, "dt": 0.01, "cat": "操稳试验",
     "desc": "定半径稳态回转试验（长时程、采样点最多）"},
    {"name": "稳态回转-左转",   "family": "steady", "pts": 8000,  "dt": 0.005, "cat": "操稳试验",
     "desc": "左转定半径稳态回转试验"},
    {"name": "稳态回转-右转",   "family": "steady", "pts": 8000,  "dt": 0.005, "cat": "操稳试验",
     "desc": "右转定半径稳态回转试验"},
    {"name": "转向回正",       "family": "ret",    "pts": 6000,  "dt": 0.005, "cat": "操稳试验",
     "desc": "转向回正性能试验"},
    {"name": "转向回正-高速",   "family": "ret",    "pts": 6000,  "dt": 0.005, "cat": "操稳试验",
     "desc": "高速转向回正性能试验"},
    {"name": "蛇行",          "family": "slalom", "pts": 10000, "dt": 0.005, "cat": "操稳试验",
     "desc": "蛇行（正弦转向输入）试验"},
    {"name": "蛇行-高速",      "family": "slalom", "pts": 10000, "dt": 0.005, "cat": "操稳试验",
     "desc": "高速蛇行试验"},
    {"name": "双移线",        "family": "slalom", "pts": 10000, "dt": 0.005, "cat": "操稳试验",
     "desc": "双移线（ISO 3888-1）试验"},
    {"name": "单移线",        "family": "slalom", "pts": 8000,  "dt": 0.005, "cat": "操稳试验",
     "desc": "单移线试验"},
    {"name": "转向轻便性",     "family": "step",   "pts": 5000,  "dt": 0.005, "cat": "操稳试验",
     "desc": "低速转向轻便性试验"},
    {"name": "中间位置操纵性",  "family": "slalom", "pts": 8000,  "dt": 0.005, "cat": "操稳试验",
     "desc": "转向盘中间位置操纵稳定性试验"},
    {"name": "横摆角速度频率响应", "family": "pulse", "pts": 8000, "dt": 0.005, "cat": "操稳试验",
     "desc": "横摆角速度频率响应特性试验"},
    {"name": "侧向风敏感性",   "family": "step",   "pts": 6000,  "dt": 0.005, "cat": "操稳试验",
     "desc": "侧向风敏感性试验"},
    {"name": "弯道制动",      "family": "ret",    "pts": 6000,  "dt": 0.005, "cat": "操稳试验",
     "desc": "弯道制动稳定性试验"},
]

STEADY_CONDITION = "稳态回转"      # 10 万点那个


# ═══════════════════════════════════════════════════════════════════════
# 2. 指标模板 — 每个信号族一组 (name, unit)，按工况重新编号
# ═══════════════════════════════════════════════════════════════════════

FAMILY_TEMPLATES: Dict[str, List[Tuple[str, Optional[str]]]] = {
    "step": [
        ("车速", "km/h"), ("阶跃时间", "s"), ("方向盘转角速率", "deg/s"),
        ("侧向加速度稳态值", "g"), ("侧向加速度响应时间", "s"),
        ("横摆角速度超调量", "%"), ("横摆角速度响应时间", "s"),
        ("横摆角速度峰值响应时间", "s"), ("横摆角速度峰值迟滞", "s"),
        ("侧倾角斜率", "deg/s"), ("最大侧倾角速度-通道直读", "deg/s"),
        ("最大侧倾角速度-求导计算", "deg/s"), ("最大侧倾角加速度", "deg/s^2"),
        ("侧倾角响应时间", "s"), ("侧倾角超调量", "%"),
        ("侧倾角稳态值", "deg"), ("最大侧倾角", "deg"),
        ("前轴柔度", "deg/g"), ("后轴柔度", "deg/g"),
        ("横摆角速度总方差", "s"), ("侧向加速度总方差", "s"),
        ("方向盘转角稳态值", "deg"), ("侧向加速度峰值响应时间", "s"),
        ("侧向加速度峰值迟滞", "s"), ("侧向加速度超调量", "%"),
        ("侧向加速度稳态增益", "g/deg"), ("横摆角速度稳态增益", "1/s"),
    ],
    "pulse": [
        ("车速", "km/h"), ("脉冲宽度", "s"), ("方向盘转角峰值", "deg"),
        ("横摆角速度峰值", "deg/s"), ("横摆角速度峰值时间", "s"),
        ("侧向加速度峰值", "g"), ("横摆角速度增益", "1/s"),
        ("侧向加速度增益", "g/deg"), ("横摆角速度响应时间", "s"),
        ("谐振频率", "Hz"), ("谐振峰水平", "dB"), ("相位滞后角", "deg"),
    ],
    "steady": [
        ("车速", "km/h"), ("转弯半径", "m"), ("侧向加速度", "g"),
        ("方向盘转角", "deg"), ("横摆角速度", "deg/s"), ("侧倾角", "deg"),
        ("不足转向度", "deg/g"), ("侧倾梯度", "deg/g"), ("转向半径比", None),
        ("前后轴侧偏角差", "deg"),
    ],
    "ret": [
        ("车速", "km/h"), ("松开时刻横摆角速度", "deg/s"), ("回正时间", "s"),
        ("残留横摆角速度", "deg/s"), ("横摆角速度超调量", "%"),
        ("回正超调量", "%"), ("回正响应时间", "s"), ("方向盘回正残余角", "deg"),
    ],
    "slalom": [
        ("车速", "km/h"), ("通过时间", "s"), ("方向盘转角幅值", "deg"),
        ("横摆角速度幅值", "deg/s"), ("侧向加速度幅值", "g"), ("侧倾角幅值", "deg"),
        ("横摆角速度相位滞后", "deg"), ("侧向加速度相位滞后", "deg"),
        ("侧倾角相位滞后", "deg"), ("横摆角速度增益", "1/s"), ("转向灵敏度", "g/deg"),
    ],
}


# ═══════════════════════════════════════════════════════════════════════
# 3. 曲线定义
# ═══════════════════════════════════════════════════════════════════════

CURVE_DEFS: List[Tuple[str, str, str, str, bool]] = [
    ("方向盘转角-时间", "方向盘转角vs时间", "时间（s）", "方向盘转角（deg）", True),
    ("侧向加速度-时间", "侧向加速度vs时间", "时间（s）", "侧向加速度（m/s^2）", True),
    ("横摆角速度-时间", "横摆角速度vs时间", "时间（s）", "横摆角速度（deg/s）", True),
    ("侧倾角-时间", "侧倾角vs时间", "时间（s）", "侧倾角（deg）", True),
    ("侧倾角速度-时间", "侧倾角速度vs时间", "时间（s）", "侧倾角速度（deg/s）", True),
    ("前轴侧偏角-时间", "前轴侧偏角vs时间", "时间（s）", "前轴侧偏角（deg）", False),
    ("后轴侧偏角-时间", "后轴侧偏角vs时间", "时间（s）", "后轴侧偏角（deg）", False),
]


# ═══════════════════════════════════════════════════════════════════════
# 4. helpers
# ═══════════════════════════════════════════════════════════════════════

def parse_raw_key(raw_key: str):
    """raw_key → ``(seq, name, unit)``.

    算法侧的 key **不一定带数字前缀**，所以要兼容三类写法::

        '13-侧向加速度稳态值(g)'   → (13,  '侧向加速度稳态值', 'g')
        '13-侧向加速度稳态值'      → (13,  '侧向加速度稳态值', None)
        '侧向加速度稳态值(g)'      → (None,'侧向加速度稳态值', 'g')
        '侧向加速度稳态值'         → (None,'侧向加速度稳态值', None)

    注意 ``seq`` 只是数字前缀的拆解产物，**不要拿它跨工况排序**：
    同一个指标在不同工况下编号完全不同（「车速」角阶跃=10 / 角脉冲=100），
    且编号本身不唯一（17 重复）。排序请用 ``name``。
    """
    seq = None
    body = raw_key

    # ① 可选数字前缀（兼容 '13-' / '13.' / '13_' 三种分隔符）
    m = re.match(r"^\s*(\d+)\s*[-._、]\s*(.+)$", raw_key)
    if m:
        seq = int(m.group(1))
        body = m.group(2)

    # ② 可选单位后缀（取最外层括号，支持括号里有括号）
    unit = None
    m = re.match(r"^(.*?)\(([^()]+)\)\s*$", body)
    if m:
        body = m.group(1)
        unit = m.group(2).strip()

    name = body.strip()
    # 极端情况：key 本身是空的，拿原文兜底，避免 name 为空串
    if not name:
        name = raw_key.strip()
    return seq, name, unit or None


def r2(v) -> float:
    return round(float(v), 4)


def r2list(vals) -> List[float]:
    """Round a channel to 4 decimals — vectorised when numpy is present."""
    if HAS_NUMPY:
        return _np.round(_np.asarray(vals, dtype="float64"), 4).tolist()
    return [r2(v) for v in vals]


def time_base(n: int, dt: float) -> List[float]:
    if HAS_NUMPY:
        return (_np.arange(n, dtype="float64") * dt).round(6).tolist()
    return [round(i * dt, 6) for i in range(n)]


def step_response(t, t_start, amp, zeta, wn):
    if zeta >= 1:
        zeta = 0.999
    wd = wn * math.sqrt(1 - zeta * zeta)
    k = zeta / math.sqrt(1 - zeta * zeta)
    if HAS_NUMPY:
        ta = _np.asarray(t, dtype="float64")
        tt = _np.clip(ta - t_start, 0.0, None)
        env = _np.exp(-zeta * wn * tt)
        out = amp * (1 - env * (_np.cos(wd * tt) + k * _np.sin(wd * tt)))
        out = _np.where(ta < t_start, 0.0, out)
        return out.tolist()
    out = []
    ap = out.append
    for ti in t:
        if ti < t_start:
            ap(0.0)
        else:
            tt = ti - t_start
            env = math.exp(-zeta * wn * tt)
            ap(amp * (1 - env * (math.cos(wd * tt) + k * math.sin(wd * tt))))
    return out


def smooth(vals, window=3):
    if HAS_NUMPY:
        a = _np.asarray(vals, dtype="float64")
        pad = window // 2
        if pad:
            a = _np.pad(a, (pad, pad), mode="edge")
        kern = _np.ones(window, dtype="float64") / window
        return _np.convolve(a, kern, mode="valid").tolist()
    n = len(vals)
    half = window // 2
    out = []
    ap = out.append
    for i in range(n):
        lo = i - half
        if lo < 0:
            lo = 0
        hi = i + half + 1
        if hi > n:
            hi = n
        ap(sum(vals[lo:hi]) / (hi - lo))
    return out


def deriv(vals, dt):
    if HAS_NUMPY:
        a = _np.asarray(vals, dtype="float64")
        out = _np.empty_like(a)
        out[0] = 0.0
        out[1:] = (a[1:] - a[:-1]) / dt
        return out.tolist()
    out = [0.0]
    ap = out.append
    inv = 1.0 / dt
    for i in range(1, len(vals)):
        ap((vals[i] - vals[i - 1]) * inv)
    return out


def noise(vals, amp, rng):
    if HAS_NUMPY:
        a = _np.asarray(vals, dtype="float64")
        sub = _np.random.default_rng(rng.randrange(2 ** 32))
        return (a + sub.normal(0.0, amp, a.shape)).tolist()
    g = rng.gauss
    return [v + g(0, amp) for v in vals]


def mean_curves(arrays: List[List[float]]) -> List[float]:
    """Element-wise mean across runs (used to build the 平均 curve)."""
    if HAS_NUMPY:
        stacked = _np.vstack([_np.asarray(a, dtype="float64") for a in arrays])
        return _np.round(stacked.mean(axis=0), 4).tolist()
    n = len(arrays[0])
    return [r2(sum(a[i] for a in arrays) / len(arrays)) for i in range(n)]


def time_to_pct(t, y, t_start, pct, steady):
    target = pct * steady
    for i, ti in enumerate(t):
        if ti >= t_start and y[i] >= target:
            return ti - t_start
    return 0.0


def peak_info(t, y, t_start):
    pk_v, pk_t = -1e18, t_start
    for i, ti in enumerate(t):
        if ti >= t_start and y[i] > pk_v:
            pk_v, pk_t = y[i], ti
    return pk_t, pk_v


def amp_of(vals) -> float:
    return (max(vals) - min(vals)) / 2.0


# ═══════════════════════════════════════════════════════════════════════
# 5. 指标 raw_key 生成（按工况分配序号段，保证 raw_key 全局语义唯一）
# ═══════════════════════════════════════════════════════════════════════

def build_condition_indices(specs: List[Dict[str, Any]]) -> None:
    seq = 10
    for spec in specs:
        tmpl = FAMILY_TEMPLATES[spec["family"]]
        keys = []
        for i, (nm, unit) in enumerate(tmpl):
            keys.append("%d-%s(%s)" % (seq + i, nm, unit) if unit
                        else "%d-%s" % (seq + i, nm))
        spec["indices"] = keys
        # 下一个工况从下一个整十段开始，避免序号交错
        seq = ((seq + len(tmpl)) // 10 + 1) * 10


# ═══════════════════════════════════════════════════════════════════════
# 6. signal generation
# ═══════════════════════════════════════════════════════════════════════

def gen_signals(spec: Dict[str, Any], veh: dict, rng: random.Random) -> dict:
    """Generate raw + filtered channels for one run."""
    family = spec["family"]
    n = spec["pts"]
    dt = spec["dt"]
    name = spec["name"]

    t = time_base(n, dt)
    mass = veh.get("extra_attrs", {}).get("mass", 1800)
    awd = veh.get("drive_type") == "四驱"

    if "高速" in name:
        speed = 120.0
    elif "低速" in name:
        speed = 40.0
    elif family == "slalom":
        speed = 60.0
    elif family == "steady":
        speed = 80.0
    else:
        speed = 80.0

    if family == "step":
        t0, t_rise = 0.5, 0.15
        swa_max = round(rng.uniform(45, 75), 2)
        swa = []
        ap = swa.append
        for ti in t:
            if ti < t0:
                ap(0.0)
            elif ti < t0 + t_rise:
                ap(swa_max * (ti - t0) / t_rise)
            else:
                ap(swa_max)
    elif family == "pulse":
        t0, pw = 0.5, 0.35
        swa_max = round(rng.uniform(50, 90), 2)
        swa = [swa_max * math.sin(math.pi * (ti - t0) / pw)
               if t0 <= ti <= t0 + pw else 0.0 for ti in t]
    elif family == "steady":
        t0 = 0.4
        swa_max = round(rng.uniform(60, 120), 2)
        swa = [swa_max * min(max((ti - t0) / 1.2, 0.0), 1.0) for ti in t]
    elif family == "ret":
        t0, t_release = 0.5, 2.0
        swa_max = round(rng.uniform(50, 80), 2)
        swa = []
        ap = swa.append
        for ti in t:
            if ti < t0:
                ap(0.0)
            elif ti < t_release:
                ap(swa_max * min((ti - t0) / 0.8, 1.0))
            else:
                ap(swa_max * max(0.0, 1 - (ti - t_release) / 0.7))
    else:  # slalom
        t0, f = 0.0, 0.5
        swa_max = round(rng.uniform(60, 110), 2)
        swa = [swa_max * math.sin(2 * math.pi * f * ti) for ti in t]

    if family == "slalom":
        f = 0.5
        ay = [rng.uniform(3.5, 5.5) * math.sin(2 * math.pi * f * ti - 0.35) for ti in t]
        avz = [rng.uniform(12, 20) * math.sin(2 * math.pi * f * ti - 0.45) for ti in t]
        roll = [rng.uniform(2.0, 4.0) * math.sin(2 * math.pi * f * ti - 0.55) for ti in t]
    elif family == "pulse":
        ay = step_response(t, 0.5, rng.uniform(3.0, 5.0), 0.45, 7.0)
        ay = [v * math.exp(-1.2 * max(0.0, ti - 0.9)) if ti > 0.9 else v
              for ti, v in zip(t, ay)]
        avz = step_response(t, 0.5, rng.uniform(10, 18), 0.40, 6.5)
        avz = [v * math.exp(-1.1 * max(0.0, ti - 0.9)) if ti > 0.9 else v
               for ti, v in zip(t, avz)]
        roll = step_response(t, 0.5, rng.uniform(1.5, 3.5), 0.35, 5.0)
        roll = [v * math.exp(-1.3 * max(0.0, ti - 0.9)) if ti > 0.9 else v
                for ti, v in zip(t, roll)]
    else:
        zeta_ay = 0.55 if awd else 0.50
        wn_ay = 9.0 - mass / 1200.0
        ay = step_response(t, t0, rng.uniform(3.5, 5.8), zeta_ay, wn_ay)
        avz = step_response(t, t0, rng.uniform(11, 21), 0.45, wn_ay - 1.0)
        roll = step_response(t, t0, rng.uniform(1.8, 4.2), 0.38, wn_ay - 3.0)
        if family == "ret":
            ay = [v * max(0.0, 1 - (ti - 2.0) / 0.9) if ti > 2.0 else v
                  for ti, v in zip(t, ay)]
            avz = [v * max(0.0, 1 - (ti - 2.0) / 0.8) if ti > 2.0 else v
                   for ti, v in zip(t, avz)]
            roll = [v * max(0.0, 1 - (ti - 2.0) / 1.0) if ti > 2.0 else v
                    for ti, v in zip(t, roll)]

    rollrate = deriv(roll, dt)
    rollacc = deriv(rollrate, dt)
    kf = rng.uniform(0.9, 1.6) / 5.0
    kr = rng.uniform(0.5, 1.1) / 5.0
    af = [kf * v for v in ay]
    ar = [kr * v for v in ay]

    chans = {
        "方向盘转角": noise(swa, 0.35, rng),
        "侧向加速度": noise(ay, 0.06, rng),
        "横摆角速度": noise(avz, 0.20, rng),
        "侧倾角": noise(roll, 0.03, rng),
        "侧倾角速度": noise(rollrate, 0.25, rng),
        "前轴侧偏角": noise(af, 0.02, rng),
        "后轴侧偏角": noise(ar, 0.02, rng),
    }
    return {
        "t": t, "t0": t0, "dt": dt, "speed": speed, "swa_max": swa_max,
        "chans": chans, "rollacc": rollacc, "mass": mass, "awd": awd,
        "family": family, "cond": name,
    }


# ═══════════════════════════════════════════════════════════════════════
# 7. indicator computation (derived from the signals themselves)
# ═══════════════════════════════════════════════════════════════════════

def compute_values(sig: dict, rng: random.Random) -> Dict[str, float]:
    """Physical indicator values keyed by *name* (not by raw_key).

    The caller maps these names onto the condition's own raw_keys, so one
    piece of physics serves all 20 工况.
    """
    t, t0, dt = sig["t"], sig["t0"], sig["dt"]
    family = sig["family"]
    filt = sig["filt"]          # 滤波后的通道（seed 里算一次，此处复用）
    p: Dict[str, float] = {}

    ay = filt["侧向加速度"]
    avz = filt["横摆角速度"]
    roll = filt["侧倾角"]
    swa = filt["方向盘转角"]
    rr = filt["侧倾角速度"]
    ra = sig["rollacc"]
    tail = max(15, len(t) // 100)
    avg_tail = lambda v: sum(v[-tail:]) / float(tail)

    ay_s = avg_tail(ay)
    avz_s = avg_tail(avz)
    roll_s = avg_tail(roll)
    swa_s = avg_tail(swa)

    if family == "step":
        t_rise = 0.15
        swa_rate = max(abs(x) for x in deriv(swa, dt))
        ay_t90 = time_to_pct(t, ay, t0, 0.9, ay_s)
        avz_t90 = time_to_pct(t, avz, t0, 0.9, avz_s)
        roll_t90 = time_to_pct(t, roll, t0, 0.9, roll_s)
        avz_pk_t, avz_pk = peak_info(t, avz, t0)
        ay_pk_t, ay_pk = peak_info(t, ay, t0)
        roll_pk = max(roll)
        p["车速"] = sig["speed"]
        p["阶跃时间"] = t_rise
        p["方向盘转角速率"] = swa_rate
        p["侧向加速度稳态值"] = ay_s / 9.81
        p["侧向加速度响应时间"] = ay_t90
        p["横摆角速度超调量"] = (avz_pk - avz_s) / avz_s * 100 if avz_s else 0.0
        p["横摆角速度响应时间"] = avz_t90
        p["横摆角速度峰值响应时间"] = avz_pk_t - t0
        p["横摆角速度峰值迟滞"] = avz_pk_t - t0 - 0.5 * t_rise
        p["侧倾角斜率"] = max(abs(x) for x in rr)
        p["最大侧倾角速度-通道直读"] = max(abs(x) for x in rr)
        p["最大侧倾角速度-求导计算"] = max(abs(x) for x in deriv(roll, dt))
        p["最大侧倾角加速度"] = max(abs(x) for x in ra)
        p["侧倾角响应时间"] = roll_t90
        p["侧倾角超调量"] = (roll_pk - roll_s) / roll_s * 100 if roll_s else 0.0
        p["侧倾角稳态值"] = roll_s
        p["最大侧倾角"] = roll_pk
        p["前轴柔度"] = rng.uniform(2.5, 5.5)
        p["后轴柔度"] = rng.uniform(1.0, 3.0)
        p["横摆角速度总方差"] = rng.uniform(0.08, 0.25)
        p["侧向加速度总方差"] = rng.uniform(0.06, 0.20)
        p["方向盘转角稳态值"] = swa_s
        p["侧向加速度峰值响应时间"] = ay_pk_t - t0
        p["侧向加速度峰值迟滞"] = ay_pk_t - t0 - 0.5 * t_rise
        p["侧向加速度超调量"] = (ay_pk - ay_s) / ay_s * 100 if ay_s else 0.0
        p["侧向加速度稳态增益"] = (ay_s / 9.81) / swa_s if swa_s else 0.0
        p["横摆角速度稳态增益"] = avz_s / swa_s if swa_s else 0.0

    elif family == "pulse":
        avz_pk_t, avz_pk = peak_info(t, avz, 0.5)
        ay_pk_t, ay_pk = peak_info(t, ay, 0.5)
        swa_pk = max(abs(x) for x in swa)
        p["车速"] = sig["speed"]
        p["脉冲宽度"] = 0.35
        p["方向盘转角峰值"] = swa_pk
        p["横摆角速度峰值"] = avz_pk
        p["横摆角速度峰值时间"] = avz_pk_t - 0.5
        p["侧向加速度峰值"] = ay_pk / 9.81
        p["横摆角速度增益"] = avz_pk / swa_pk if swa_pk else 0.0
        p["侧向加速度增益"] = (ay_pk / 9.81) / swa_pk if swa_pk else 0.0
        p["横摆角速度响应时间"] = time_to_pct(t, avz, 0.5, 0.9, avz_pk)
        p["谐振频率"] = rng.uniform(0.9, 1.6)
        p["谐振峰水平"] = rng.uniform(1.5, 6.0)
        p["相位滞后角"] = rng.uniform(15, 45)

    elif family == "steady":
        p["车速"] = sig["speed"]
        p["转弯半径"] = sig["speed"] / 3.6 / avz_s * 57.3 if avz_s else 40.0
        p["侧向加速度"] = ay_s / 9.81
        p["方向盘转角"] = swa_s
        p["横摆角速度"] = avz_s
        p["侧倾角"] = roll_s
        p["不足转向度"] = rng.uniform(1.5, 4.5)
        p["侧倾梯度"] = roll_s / (ay_s / 9.81) if ay_s else 0.0
        p["转向半径比"] = rng.uniform(1.0, 1.35)
        p["前后轴侧偏角差"] = rng.uniform(0.5, 2.5)

    elif family == "ret":
        idx_rel = min(range(len(t)), key=lambda i: abs(t[i] - 2.0))
        p["车速"] = sig["speed"]
        p["松开时刻横摆角速度"] = avz[idx_rel]
        p["回正时间"] = rng.uniform(0.5, 1.4)
        p["残留横摆角速度"] = abs(avz[-1])
        p["横摆角速度超调量"] = rng.uniform(2, 18)
        p["回正超调量"] = rng.uniform(1, 12)
        p["回正响应时间"] = rng.uniform(0.25, 0.8)
        p["方向盘回正残余角"] = abs(swa[-1])

    else:  # slalom
        a_swa = amp_of(swa)
        a_avz = amp_of(avz)
        a_ay = amp_of(ay)
        a_roll = amp_of(roll)
        p["车速"] = sig["speed"]
        p["通过时间"] = rng.uniform(9, 14)
        p["方向盘转角幅值"] = a_swa
        p["横摆角速度幅值"] = a_avz
        p["侧向加速度幅值"] = a_ay / 9.81
        p["侧倾角幅值"] = a_roll
        p["横摆角速度相位滞后"] = rng.uniform(20, 50)
        p["侧向加速度相位滞后"] = rng.uniform(15, 40)
        p["侧倾角相位滞后"] = rng.uniform(30, 65)
        p["横摆角速度增益"] = a_avz / a_swa if a_swa else 0.0
        p["转向灵敏度"] = (a_ay / 9.81) / a_swa if a_swa else 0.0

    return p


def synth_by_unit(unit: Optional[str], rng: random.Random) -> float:
    """Fallback value when a name has no physics behind it."""
    if unit in ("g", "g/deg"):
        return rng.uniform(0.2, 0.8)
    if unit == "%":
        return rng.uniform(1, 25)
    if unit == "s":
        return rng.uniform(0.1, 2.0)
    if unit == "deg":
        return rng.uniform(0.5, 8.0)
    if unit == "deg/s":
        return rng.uniform(5, 40)
    if unit == "Hz":
        return rng.uniform(0.5, 2.5)
    return rng.uniform(0.5, 20.0)


def build_params(spec: Dict[str, Any], sig: dict, rng: random.Random) -> Dict[str, float]:
    values = compute_values(sig, rng)
    out: Dict[str, float] = {}
    for raw_key in spec["indices"]:
        _seq, name, unit = parse_raw_key(raw_key)
        out[raw_key] = r2(values.get(name, synth_by_unit(unit, rng)))
    return out


# ═══════════════════════════════════════════════════════════════════════
# 8. curve assembly
# ═══════════════════════════════════════════════════════════════════════

def filter_channels(sig: dict) -> Dict[str, Tuple[List[float], List[float]]]:
    """Return {channel: (rounded origin, rounded filter)} — rounded ONCE.

    Rounding once per channel (instead of once per curve/series) is what makes
    10万点 × 7 通道 affordable.
    """
    out: Dict[str, Tuple[List[float], List[float]]] = {}
    for k, v in sig["chans"].items():
        out[k] = (r2list(v), r2list(sig["filt"][k]))
    return out


def build_curves(t_r: List[float], chans_r: Dict[str, Tuple[List[float], List[float]]],
                 duiqi: bool = False) -> Dict[str, dict]:
    """x is shared by every curve and every series (same list object)."""
    out: Dict[str, dict] = {}
    for cname, title, xl, yl, has_raw in CURVE_DEFS:
        chan = chans_r[cname.split("-")[0]]
        series: List[dict] = []
        if not duiqi:
            series.append({"x": t_r, "y": chan[0],
                           "curvetype": "line", "curvename": "原始数据"})
            if has_raw:
                series.append({"x": t_r, "y": chan[1],
                               "curvetype": "line", "curvename": "滤波"})
        else:
            series.append({"x": t_r, "y": chan[1],
                           "curvetype": "line", "curvename": "滤波"})
        out[cname] = {"curve": series, "title": title, "xlabel": xl, "ylabel": yl}
    return out


def average_curves(curve_list: List[Dict[str, dict]]) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for k in curve_list[0]:
        ser: List[dict] = []
        for si in range(len(curve_list[0][k]["curve"])):
            xs = curve_list[0][k]["curve"][si]["x"]
            ys = mean_curves([cl[k]["curve"][si]["y"] for cl in curve_list])
            ser.append({
                "x": xs, "y": ys, "curvetype": "line",
                "curvename": curve_list[0][k]["curve"][si].get("curvename", "滤波"),
            })
        out[k] = {
            "curve": ser,
            "title": curve_list[0][k]["title"],
            "xlabel": curve_list[0][k]["xlabel"],
            "ylabel": curve_list[0][k]["ylabel"],
        }
    return out


def average_params(param_list: List[Dict[str, float]]) -> Dict[str, float]:
    return {k: r2(sum(pl.get(k, 0.0) for pl in param_list) / len(param_list))
            for k in param_list[0]}


# ═══════════════════════════════════════════════════════════════════════
# 9. dimension generation (200 台车)
# ═══════════════════════════════════════════════════════════════════════

PLATFORM_BRANDS: List[Tuple[str, str, List[str]]] = [
    ("BEV平台", "EV", ["极光", "星河", "雷霆"]),
    ("PHEV平台", "PHEV", ["混动先锋", "绿动"]),
    ("ICE平台", "ICE", ["经典动力", "锐驰"]),
    ("氢燃料平台", "H2", ["蓝氢"]),
]

DRIVE_TYPES = ["四驱", "后驱", "前驱"]
BATTERIES = ["宁德时代80kWh", "比亚迪刀片70kWh", "磷酸铁锂60kWh", "三元锂90kWh"]
MOTORS = ["双电机", "单电机", "增程器+单电机"]


def build_vehicles(n: int, rng: random.Random) -> List[dict]:
    """Round-robin n vehicles across platforms × brands."""
    brands = [(plat, tag, br) for plat, tag, brs in PLATFORM_BRANDS for br in brs]
    out: List[dict] = []
    counters: Dict[Tuple[str, str], int] = {}
    for i in range(n):
        plat, tag, br = brands[i % len(brands)]
        key = (plat, br)
        counters[key] = counters.get(key, 0) + 1
        idx = counters[key]
        out.append({
            "platform": plat,
            "brand": br,
            "name": "%s%s-%02d" % (br, tag, idx),
            "drive_type": DRIVE_TYPES[i % len(DRIVE_TYPES)],
            "vin": "LJV%06d" % (i + 1),
            "model_year": 2022 + (i % 4),
            "extra_attrs": {
                "battery": BATTERIES[i % len(BATTERIES)],
                "motor": MOTORS[i % len(MOTORS)],
                "mass": 1650 + (i * 37) % 750,
            },
        })
        _ = rng
    return out


# ═══════════════════════════════════════════════════════════════════════
# 10. main seed
# ═══════════════════════════════════════════════════════════════════════

def seed(args) -> None:
    rng = random.Random(args.seed)
    t_start = time.time()

    # ── 工况 spec 处理 ──
    # 先按【完整 20 个工况】分配序号段，这样不论 --conditions 取几个，
    # 同一个工况的 raw_key 始终是固定的（例如 稳态回转 永远是 160-169）。
    specs = [dict(s) for s in CONDITION_SPECS]
    build_condition_indices(specs)

    if args.conditions and args.conditions < len(specs):
        # 保留 稳态回转，其余取前 N-1 个
        head = [s for s in specs if s["name"] != STEADY_CONDITION][: args.conditions - 1]
        steady = [s for s in specs if s["name"] == STEADY_CONDITION]
        specs = head + steady
    if args.points:
        for s in specs:
            if s["name"] != STEADY_CONDITION:
                s["pts"] = args.points
    if args.steady_points:
        for s in specs:
            if s["name"] == STEADY_CONDITION:
                s["pts"] = args.steady_points

    vehicles = build_vehicles(args.vehicles, rng)
    runs = args.runs

    print("Resetting database …")
    # SQLite drop_all 后文件不会缩小，重建时越跑越慢 —— 直接删文件（seed 本就是全量重建）
    if IS_SQLITE:
        engine.dispose()
        _path = DATABASE_URL.split("///", 1)[-1]
        for _p in (_path, _path + "-wal", _path + "-shm"):
            if os.path.exists(_p):
                os.remove(_p)
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    if args.json:
        if os.path.isdir(DATA_DIR):
            shutil.rmtree(DATA_DIR)
        os.makedirs(DATA_DIR, exist_ok=True)

    db = SessionLocal()
    try:
        # ── platforms / brands / vehicles ──
        plat_objs: Dict[str, Platform] = {}
        brand_objs: Dict[Tuple[str, str], Brand] = {}
        veh_objs: List[Vehicle] = []

        for v in vehicles:
            plat = plat_objs.get(v["platform"])
            if plat is None:
                plat = Platform(name=v["platform"])
                db.add(plat)
                db.flush()
                plat_objs[v["platform"]] = plat
            bkey = (v["platform"], v["brand"])
            brand = brand_objs.get(bkey)
            if brand is None:
                brand = Brand(name=v["brand"], platform_id=plat.id)
                db.add(brand)
                db.flush()
                brand_objs[bkey] = brand
            veh = Vehicle(
                name=v["name"], brand_id=brand.id, drive_type=v["drive_type"],
                vin=v["vin"], model_year=v["model_year"], extra_attrs=v["extra_attrs"],
            )
            db.add(veh)
            veh_objs.append(veh)
        db.commit()
        for v in veh_objs:
            db.refresh(v, ["id"])

        # ── conditions ──
        cond_objs: Dict[str, Condition] = {}
        for s in specs:
            c = Condition(name=s["name"], description=s["desc"], category=s["cat"])
            db.add(c)
            cond_objs[s["name"]] = c
        db.commit()
        for c in cond_objs.values():
            db.refresh(c, ["id"])

        total_exp = total_ind = total_cur = 0
        total_bytes = 0

        for vi, (v, veh_obj) in enumerate(zip(vehicles, veh_objs), start=1):
            veh_dir = os.path.join(DATA_DIR, v["name"])
            if args.json:
                os.makedirs(veh_dir, exist_ok=True)

            for s in specs:
                cond_obj = cond_objs[s["name"]]

                param_list: List[Dict[str, float]] = []
                cal_list: List[Dict[str, dict]] = []
                dui_list: List[Dict[str, dict]] = []

                for run_i in range(1, runs + 1):
                    sig = gen_signals(s, v, rng)
                    sig["filt"] = {k: smooth(vv, 3) for k, vv in sig["chans"].items()}
                    chans_r = filter_channels(sig)
                    t_r = r2list(sig["t"])
                    param_list.append(build_params(s, sig, rng))
                    cal_list.append(build_curves(t_r, chans_r, duiqi=False))
                    dui_list.append(build_curves(t_r, chans_r, duiqi=True))
                    del sig, chans_r

                # ── 平均 ──
                param_list.append(average_params(param_list))
                cal_list.append(average_curves(cal_list))
                dui_list.append(average_curves(dui_list))

                # ── 落 JSON: {"1":[p,cal,dui], ..., "平均":[...]} ──
                if args.json:
                    payload: Dict[str, Any] = {}
                    for i in range(runs):
                        payload[str(i + 1)] = [param_list[i], cal_list[i], dui_list[i]]
                    payload["平均"] = [param_list[-1], cal_list[-1], dui_list[-1]]
                    fpath = os.path.join(veh_dir, "%s.json" % s["name"])
                    with open(fpath, "w", encoding="utf-8") as fh:
                        json.dump(payload, fh, ensure_ascii=False)
                    total_bytes += os.path.getsize(fpath)
                    payload = None

                # ── 入库 ──
                for i in range(runs + 1):
                    no = "平均" if i == runs else str(i + 1)
                    exp = Experiment(
                        vehicle_id=veh_obj.id, condition_id=cond_obj.id,
                        experiment_no=no, is_average=(i == runs),
                        sort_order=999 if i == runs else i,
                    )
                    db.add(exp)
                    db.flush()
                    total_exp += 1

                    params = param_list[i]
                    # seq 不再入库（见 models.Indicator），只取 name / unit
                    db.add_all([
                        Indicator(
                            experiment_id=exp.id,
                            name=parse_raw_key(rk)[1], unit=parse_raw_key(rk)[2],
                            raw_key=rk, value=val, category=s["cat"],
                        )
                        for rk, val in params.items()
                    ])
                    total_ind += len(params)

                    for group, cdict in (("cal", cal_list[i]), ("duiqi", dui_list[i])):
                        for order, (ckey, cval) in enumerate(cdict.items()):
                            npts = len(cval["curve"][0]["x"])
                            db.add(Curve(
                                experiment_id=exp.id, group=group, name=ckey,
                                title=cval["title"], xlabel=cval["xlabel"],
                                ylabel=cval["ylabel"], series=cval["curve"],
                                point_count=npts, sort_order=order,
                            ))
                            total_cur += 1

                param_list = cal_list = dui_list = None   # free memory
                db.commit()

            if vi % max(1, args.vehicles // 20) == 0 or vi == args.vehicles:
                el = time.time() - t_start
                print("  [%3d/%d 车] %6.1fs  实验 %d  指标 %d  曲线 %d"
                      % (vi, args.vehicles, el, total_exp, total_ind, total_cur))

    finally:
        db.close()

    print("\nSeed complete  (%.1fs)" % (time.time() - t_start))
    print("  Platforms   : %d" % len(plat_objs))
    print("  Brands      : %d" % len(brand_objs))
    print("  Vehicles    : %d" % len(vehicles))
    print("  Conditions  : %d" % len(specs))
    print("  Experiments : %d" % total_exp)
    print("  Indicators  : %d" % total_ind)
    print("  Curves      : %d" % total_cur)
    if args.json:
        n_files = sum(len(fs) for _, _, fs in os.walk(DATA_DIR))
        print("  JSON files  : %d  →  %s  (%.2f GB)"
              % (n_files, DATA_DIR, total_bytes / 1024 ** 3))
    if IS_SQLITE:
        path = DATABASE_URL.split("///", 1)[-1]
        if os.path.exists(path):
            print("  DB file     : %s  (%.2f GB)"
                  % (path, os.path.getsize(path) / 1024 ** 3))


def main() -> None:
    p = argparse.ArgumentParser(description="生成车辆操稳试验数据")
    p.add_argument("--preset", choices=["tiny", "demo", "standard", "full"],
                   default="demo", help="规模预设（默认 demo）")
    p.add_argument("--vehicles", type=int, default=0, help="车辆数（覆盖 preset）")
    p.add_argument("--conditions", type=int, default=0, help="工况数（覆盖 preset）")
    p.add_argument("--runs", type=int, default=0, help="每台车每工况实验次数（不含平均）")
    p.add_argument("--points", type=int, default=0, help="普通工况采样点数")
    p.add_argument("--steady-points", type=int, default=0,
                   help="%s 的采样点数" % STEADY_CONDITION)
    p.add_argument("--no-json", dest="json", action="store_false",
                   help="不导出 data/*.json（只写库，快很多）")
    p.add_argument("--seed", type=int, default=20240115, help="随机种子")
    args = p.parse_args()

    PRESETS = {
        "tiny":     dict(vehicles=11,  conditions=5,  runs=2, points=500,  steady_points=5000),
        "demo":     dict(vehicles=200, conditions=20, runs=2, points=1000, steady_points=10000),
        "standard": dict(vehicles=200, conditions=20, runs=2, points=3000, steady_points=30000),
        "full":     dict(vehicles=200, conditions=20, runs=2, points=6000, steady_points=100000),
    }
    base = PRESETS[args.preset]
    for k, val in base.items():
        if getattr(args, k, 0) in (0, None):
            setattr(args, k, val)
    if args.runs <= 0:
        args.runs = 2

    n_exp = args.vehicles * args.conditions * (args.runs + 1)
    # 单次实验 ≈ 点数 × 21 数组 × 2(x,y) × 7 字节
    gb_per_pt = 21 * 2 * 7 / 1024 ** 3
    est = n_exp * gb_per_pt * (args.points * (args.conditions - 1) / args.conditions
                               + args.steady_points / args.conditions)
    print("配置: %d 车 × %d 工况 × %d 次实验(+平均) | 普通 %d 点 / %s %d 点 | JSON=%s"
          % (args.vehicles, args.conditions, args.runs, args.points,
             STEADY_CONDITION, args.steady_points, args.json))
    print("预计: %d 条实验 / %d 条曲线，库+JSON 合计约 %.2f GB"
          % (n_exp, n_exp * 14, est * 2))
    if est > 3:
        print("      ⚠ 大批量生成较慢（demo 预设实测约 18 分钟），建议后台跑：")
        print("        nohup python scripts/seed_data.py --preset %s > seed.log 2>&1 &"
              % args.preset)
    seed(args)


if __name__ == "__main__":
    main()
