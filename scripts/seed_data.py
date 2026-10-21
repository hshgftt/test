"""造演示数据 — 直接走导入器，和真实导入共用同一套规则。

生成内容
========

* 车辆：平台 / 子平台、品牌 / 子品牌、车辆类型、生产日期、VIN、前后悬、
  能源形式、驱动形式、所属部门、销售地；部分车辆带操稳单号（可多个）。
* 试验模式：不同车辆模式数量不固定 —— 有的没有模式、有的只有「舒适」、
  有的「舒适 + 运动」。
* 数据：每个 (车 × 模式 × 工况) 一份，含多次实验 + 平均，指标 + 计算曲线 +
  对齐曲线。曲线来自二阶系统物理模型，指标由同一组信号推导，二者自洽。

实现方式
========

物理信号 → 组装成 ``{实验号: [params, cal, duiqi]}`` → 交给
``app.importer.import_items`` 写库。这样演示数据和真实导入走的是同一套
解析/落库逻辑，不会出现「造数能查、导入查不到」的两套行为。

用法::

    python scripts/seed_data.py --preset tiny
    python scripts/seed_data.py --vehicles 20 --conditions 6 --points 500 --no-json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import importer
from app.database import SessionLocal

# ── optional numpy fast path ──────────────────────────────────────────
try:
    import numpy as _np
except Exception:                                    # pragma: no cover
    _np = None

HAS_NUMPY = _np is not None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
DATASET_TYPE = "handling_stability"


# ═══════════════════════════════════════════════════════════════════════
# 1. 工况
# ═══════════════════════════════════════════════════════════════════════

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

STEADY_CONDITION = "稳态回转"


# ═══════════════════════════════════════════════════════════════════════
# 2. 指标模板
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

def parse_raw_key(raw_key: str) -> Tuple[Optional[int], str, Optional[str]]:
    seq: Optional[int] = None
    body = raw_key
    m = re.match(r"^\s*(\d+)\s*[-._、]\s*(.+)$", raw_key)
    if m:
        seq = int(m.group(1))
        body = m.group(2)
    unit = None
    m = re.match(r"^(.*?)\(([^()]+)\)\s*$", body)
    if m:
        body = m.group(1)
        unit = m.group(2).strip()
    name = body.strip() or raw_key.strip()
    return seq, name, unit or None


def r2(v: float) -> float:
    return round(float(v), 4)


def r2list(vals: List[float]) -> List[float]:
    if HAS_NUMPY:
        return _np.round(_np.asarray(vals, dtype="float64"), 4).tolist()
    return [r2(v) for v in vals]


def time_base(n: int, dt: float) -> List[float]:
    if HAS_NUMPY:
        return (_np.arange(n, dtype="float64") * dt).round(6).tolist()
    return [round(i * dt, 6) for i in range(n)]


def step_response(t: List[float], t_start: float, amp: float, zeta: float, wn: float) -> List[float]:
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
    out: List[float] = []
    for ti in t:
        if ti < t_start:
            out.append(0.0)
        else:
            tt = ti - t_start
            env = math.exp(-zeta * wn * tt)
            out.append(amp * (1 - env * (math.cos(wd * tt) + k * math.sin(wd * tt))))
    return out


def smooth(vals: List[float], window: int = 3) -> List[float]:
    if HAS_NUMPY:
        a = _np.asarray(vals, dtype="float64")
        pad = window // 2
        if pad:
            a = _np.pad(a, (pad, pad), mode="edge")
        return _np.convolve(a, _np.ones(window) / window, mode="valid").tolist()
    n = len(vals)
    half = window // 2
    out: List[float] = []
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        out.append(sum(vals[lo:hi]) / (hi - lo))
    return out


def deriv(vals: List[float], dt: float) -> List[float]:
    if HAS_NUMPY:
        a = _np.asarray(vals, dtype="float64")
        out = _np.empty_like(a)
        out[0] = 0.0
        out[1:] = (a[1:] - a[:-1]) / dt
        return out.tolist()
    out = [0.0]
    inv = 1.0 / dt
    for i in range(1, len(vals)):
        out.append((vals[i] - vals[i - 1]) * inv)
    return out


def noise(vals: List[float], amp: float, rng: random.Random) -> List[float]:
    if HAS_NUMPY:
        a = _np.asarray(vals, dtype="float64")
        sub = _np.random.default_rng(rng.randrange(2 ** 32))
        return (a + sub.normal(0.0, amp, a.shape)).tolist()
    return [v + rng.gauss(0, amp) for v in vals]


def mean_curves(arrays: List[List[float]]) -> List[float]:
    if HAS_NUMPY:
        stacked = _np.vstack([_np.asarray(a, dtype="float64") for a in arrays])
        return _np.round(stacked.mean(axis=0), 4).tolist()
    n = len(arrays[0])
    return [r2(sum(a[i] for a in arrays) / len(arrays)) for i in range(n)]


def time_to_pct(t: List[float], y: List[float], t_start: float, pct: float, steady: float) -> float:
    target = pct * steady
    for i, ti in enumerate(t):
        if ti >= t_start and y[i] >= target:
            return ti - t_start
    return 0.0


def peak_info(t: List[float], y: List[float], t_start: float) -> Tuple[float, float]:
    pk_v, pk_t = -1e18, t_start
    for i, ti in enumerate(t):
        if ti >= t_start and y[i] > pk_v:
            pk_v, pk_t = y[i], ti
    return pk_t, pk_v


def amp_of(vals: List[float]) -> float:
    return (max(vals) - min(vals)) / 2.0


# ═══════════════════════════════════════════════════════════════════════
# 5. 指标 raw_key
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
        seq = ((seq + len(tmpl)) // 10 + 1) * 10


# ═══════════════════════════════════════════════════════════════════════
# 6. 信号生成
# ═══════════════════════════════════════════════════════════════════════

def gen_signals(spec: Dict[str, Any], veh: Dict[str, Any], rng: random.Random) -> Dict[str, Any]:
    family = spec["family"]
    n = spec["pts"]
    dt = spec["dt"]
    name = spec["name"]

    t = time_base(n, dt)
    mass = veh.get("mass", 1800)
    awd = veh.get("drive_type") == "四驱"

    if "高速" in name:
        speed = 120.0
    elif "低速" in name:
        speed = 40.0
    elif family == "slalom":
        speed = 60.0
    else:
        speed = 80.0

    if family == "step":
        t0, t_rise = 0.5, 0.15
        swa_max = round(rng.uniform(45, 75), 2)
        swa = []
        for ti in t:
            if ti < t0:
                swa.append(0.0)
            elif ti < t0 + t_rise:
                swa.append(swa_max * (ti - t0) / t_rise)
            else:
                swa.append(swa_max)
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
        for ti in t:
            if ti < t0:
                swa.append(0.0)
            elif ti < t_release:
                swa.append(swa_max * min((ti - t0) / 0.8, 1.0))
            else:
                swa.append(swa_max * max(0.0, 1 - (ti - t_release) / 0.7))
    else:
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
# 7. 指标计算
# ═══════════════════════════════════════════════════════════════════════

def compute_values(sig: Dict[str, Any], rng: random.Random) -> Dict[str, float]:
    t, t0, dt = sig["t"], sig["t0"], sig["dt"]
    family = sig["family"]
    filt = sig["filt"]
    p: Dict[str, float] = {}

    ay = filt["侧向加速度"]
    avz = filt["横摆角速度"]
    roll = filt["侧倾角"]
    swa = filt["方向盘转角"]
    rr = filt["侧倾角速度"]
    ra = sig["rollacc"]
    tail = max(15, len(t) // 100)
    avg_tail = lambda v: sum(v[-tail:]) / float(tail)  # noqa: E731

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

    else:
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


def build_params(spec: Dict[str, Any], sig: Dict[str, Any], rng: random.Random) -> Dict[str, float]:
    values = compute_values(sig, rng)
    out: Dict[str, float] = {}
    for raw_key in spec["indices"]:
        _seq, name, unit = parse_raw_key(raw_key)
        out[raw_key] = r2(values.get(name, synth_by_unit(unit, rng)))
    return out


# ═══════════════════════════════════════════════════════════════════════
# 8. 曲线组装
# ═══════════════════════════════════════════════════════════════════════

def filter_channels(sig: Dict[str, Any]) -> Dict[str, Tuple[List[float], List[float]]]:
    return {k: (r2list(v), r2list(sig["filt"][k])) for k, v in sig["chans"].items()}


def build_curves(
    t_r: List[float],
    chans_r: Dict[str, Tuple[List[float], List[float]]],
    duiqi: bool = False,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for cname, title, xl, yl, has_raw in CURVE_DEFS:
        chan = chans_r[cname.split("-")[0]]
        series: List[Dict[str, Any]] = []
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


def average_curves(curve_list: List[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k in curve_list[0]:
        ser: List[Dict[str, Any]] = []
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
# 9. 车辆
# ═══════════════════════════════════════════════════════════════════════

# 平台 → (子平台列表, 品牌列表)
PLATFORM_SPECS: List[Tuple[str, List[str], List[str]]] = [
    ("BEV平台", ["E1平台", "E2平台"], ["星河", "极光", "雷霆"]),
    ("PHEV平台", ["P1平台"], ["混动先锋", "绿动"]),
    ("ICE平台", [], ["经典动力", "锐驰"]),
    ("氢燃料平台", [], ["蓝氢"]),
]

SUB_BRANDS = {"星河": ["星河智电", "星河出行"], "极光": ["极光性能"]}
MODES = [(), ("舒适",), ("舒适", "运动")]
VEHICLE_TYPES = ["轿车", "SUV", "MPV", "跑车"]
DRIVE_TYPES = ["四驱", "后驱", "前驱"]
ENERGY_BY_PLATFORM = {
    "BEV平台": "纯电",
    "PHEV平台": "插电混动",
    "ICE平台": "燃油",
    "氢燃料平台": "氢燃料",
}
FRONT_SUSP = ["麦弗逊", "双叉臂", "多连杆"]
REAR_SUSP = ["多连杆", "扭力梁", "整体桥", "双叉臂"]
DEPARTMENTS = ["整车试验部", "底盘调校部", "性能开发部"]
REGIONS = ["中国", "欧洲", "中东", "东南亚"]


def build_vehicles(n: int) -> List[Dict[str, Any]]:
    combos = [
        (plat, sub, br)
        for plat, subs, brs in PLATFORM_SPECS
        for sub in (subs or [""])
        for br in brs
    ]
    out: List[Dict[str, Any]] = []
    counters: Dict[Tuple[str, str], int] = {}
    for i in range(n):
        plat, sub, br = combos[i % len(combos)]
        counters[(plat, br)] = counters.get((plat, br), 0) + 1
        idx = counters[(plat, br)]
        code = {"BEV平台": "EV", "PHEV平台": "PHEV",
                "ICE平台": "ICE", "氢燃料平台": "H2"}[plat]
        name = "%s%s-%02d" % (br, code, idx)

        subs = SUB_BRANDS.get(br, [])
        sub_brand = subs[i % len(subs)] if subs else None
        modes = list(MODES[i % len(MODES)])
        orders = ["CS-2024-%03d" % (i + 1)]
        if i % 7 == 0:
            orders.append("CS-2024-%03d-B" % (i + 1))

        attrs: Dict[str, Any] = {
            "platform": plat,
            "brand": br,
            "vehicle_type": VEHICLE_TYPES[i % len(VEHICLE_TYPES)],
            "production_date": "2024-%02d-%02d" % (i % 12 + 1, i % 28 + 1),
            "model_year": 2023 + (i % 3),
            "vin": "LJV%08d" % (i + 1),
            "front_suspension": FRONT_SUSP[i % len(FRONT_SUSP)],
            "rear_suspension": REAR_SUSP[i % len(REAR_SUSP)],
            "energy_type": ENERGY_BY_PLATFORM[plat],
            "drive_type": DRIVE_TYPES[i % len(DRIVE_TYPES)],
            "department": DEPARTMENTS[i % len(DEPARTMENTS)],
            "sales_region": REGIONS[i % len(REGIONS)],
        }
        if sub:
            attrs["sub_platform"] = sub
        if sub_brand:
            attrs["sub_brand"] = sub_brand

        out.append({
            "name": name,
            "attrs": attrs,
            "modes": modes,
            "orders": [{"discipline": "操稳", "order_no": o} for o in orders],
            "folder_order": orders[0],
            "drive_type": attrs["drive_type"],
            "mass": 1650 + (i * 37) % 750,
        })
    return out


# ═══════════════════════════════════════════════════════════════════════
# 10. 单份数据的实验结构
# ═══════════════════════════════════════════════════════════════════════

def build_experiments(
    spec: Dict[str, Any], veh: Dict[str, Any], runs: int, rng: random.Random,
) -> Dict[str, Any]:
    param_list: List[Dict[str, float]] = []
    cal_list: List[Dict[str, Any]] = []
    dui_list: List[Dict[str, Any]] = []

    for _ in range(runs):
        sig = gen_signals(spec, veh, rng)
        sig["filt"] = {k: smooth(v, 3) for k, v in sig["chans"].items()}
        chans_r = filter_channels(sig)
        t_r = r2list(sig["t"])
        param_list.append(build_params(spec, sig, rng))
        cal_list.append(build_curves(t_r, chans_r, duiqi=False))
        dui_list.append(build_curves(t_r, chans_r, duiqi=True))

    param_list.append(average_params(param_list))
    cal_list.append(average_curves(cal_list))
    dui_list.append(average_curves(dui_list))

    experiments: Dict[str, Any] = {}
    for i in range(runs):
        experiments[str(i + 1)] = [param_list[i], cal_list[i], dui_list[i]]
    experiments["平均"] = [param_list[-1], cal_list[-1], dui_list[-1]]
    return experiments


# ═══════════════════════════════════════════════════════════════════════
# 11. main
# ═══════════════════════════════════════════════════════════════════════

def seed(args: Any) -> None:
    rng = random.Random(args.seed)
    t_start = time.time()

    specs = [dict(s) for s in CONDITION_SPECS]
    build_condition_indices(specs)
    if args.conditions and args.conditions < len(specs):
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

    vehicles = build_vehicles(args.vehicles)
    runs = max(1, args.runs)

    if args.json and os.path.isdir(DATA_DIR):
        import shutil
        shutil.rmtree(DATA_DIR)
    if args.json:
        os.makedirs(DATA_DIR, exist_ok=True)

    print("重建数据库（清空全部数据 + 写入默认元数据）…")
    importer.reset_schema()

    db = SessionLocal()
    n_exp = n_ind = n_cur = n_pts = 0
    try:
        for vi, veh in enumerate(vehicles, start=1):
            items: List[Dict[str, Any]] = []
            modes: List[Optional[str]] = list(veh["modes"]) or [None]

            for mode in modes:
                for spec in specs:
                    experiments = build_experiments(spec, veh, runs, rng)
                    items.append({
                        "vehicle": veh["name"],
                        "folder_order": veh["folder_order"],
                        "mode": mode,
                        "condition": spec["name"],
                        "file": "%s/%s/%s.json" % (
                            veh["name"], mode or "", spec["name"]),
                        "dataset_type": DATASET_TYPE,
                        "kind": "experiments",
                        "experiments": importer.parse_payload(experiments),
                        "payload": None,
                    })
                    if args.json:
                        _write_json(veh["name"], veh["folder_order"], mode, spec["name"], experiments)

            vmeta = {veh["name"]: {
                "attrs": veh["attrs"],
                "modes": veh["modes"],
                "orders": veh["orders"],
            }}
            stats = importer.import_items(
                db, items, vmeta, {}, append=True,
            )
            n_exp += stats["datasets"]
            n_ind += stats["indicators"]
            n_cur += stats["curves"]
            n_pts += stats["points"]

            items = []
            if vi % max(1, len(vehicles) // 20) == 0 or vi == len(vehicles):
                print("  [%3d/%d 车] %6.1fs  数据集 %d  指标 %d  曲线 %d"
                      % (vi, len(vehicles), time.time() - t_start,
                         n_exp, n_ind, n_cur))
    finally:
        db.close()

    print("\nSeed complete  (%.1fs)" % (time.time() - t_start))
    print("  Vehicles    : %d（模式组合：%s）"
          % (len(vehicles), " / ".join("/".join(v["modes"]) or "无模式" for v in vehicles[:3]) + " …"))
    print("  Conditions  : %d" % len(specs))
    print("  Datasets    : %d" % n_exp)
    print("  Indicators  : %d" % n_ind)
    print("  Curves      : %d" % n_cur)
    print("  Points      : %s" % "{:,}".format(n_pts))
    if args.json:
        print("  JSON files  : %s" % DATA_DIR)


def _write_json(
    name: str, order: Optional[str], mode: Optional[str],
    condition: str, experiments: Dict[str, Any],
) -> None:
    folder = name + ("(%s)" % order if order else "")
    d = os.path.join(DATA_DIR, folder)
    if mode:
        d = os.path.join(d, mode)
    os.makedirs(d, exist_ok=True)
    # 演示导出使用「平均」而不是 avg，和真实数据习惯一致
    with open(os.path.join(d, "%s.json" % condition), "w", encoding="utf-8") as fh:
        json.dump(experiments, fh, ensure_ascii=False)


def main() -> None:
    p = argparse.ArgumentParser(description="生成车辆操稳试验演示数据")
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

    print("配置: %d 车 × %d 工况 × %d 次实验(+平均) | 普通 %d 点 / %s %d 点 | JSON=%s"
          % (args.vehicles, args.conditions, args.runs, args.points,
             STEADY_CONDITION, args.steady_points, args.json))
    seed(args)


if __name__ == "__main__":
    main()
