#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据导入：**解析**（JSON / msgpack / CSV 元信息）+ **写库**。

为什么单独成模块
================

导入规则原本内联在 ``scripts/import_data.py`` 的 ``main()`` 里，只有命令行能用。
上传页面（``POST /api/upload``）需要**同一套**规则 —— 否则「命令行导入」和
「页面导入」会慢慢长出两套行为（平均实验怎么识别、``raw_key`` 怎么拆、
CSV 用哪一列关联车型），早晚对不上，而且对不上时很难发现。

所以这里只放纯逻辑，命令行和 HTTP 接口都调它，**规则只有一份**。

数据目录约定
============

    <root>/<车型>/<工况>.<后缀>
    <root>/vehicles.csv      （可选）
    <root>/conditions.csv    （可选）

后缀认 ``.json`` / ``.msgpack`` / ``.msg`` / ``.mpack``，后缀决定读法，
内容结构完全一致。

上传场景下 ``<root>`` 这一层是**浏览器的文件夹选择器**带来的（webkitRelativePath
的第一个片段），前端会剥掉；这里再用 :func:`strip_common_root` 兜一次底，
避免用户直接选到「data」这一层时车型名变成 ``data``。
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import time
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

from sqlalchemy.orm import joinedload

from app.database import Base, SessionLocal, engine
from app.models import (
    Brand,
    Condition,
    Curve,
    Experiment,
    Indicator,
    Platform,
    Vehicle,
)

AVG_KEYS = {"平均", "avg", "average", "AVE", "AVE."}
CURVE_GROUP_KEYS = ("curve", "curves", "series")

MSGPACK_EXTS = (".msgpack", ".msg", ".mpack")
JSON_EXTS = (".json",)
READABLE_EXTS: Tuple[str, ...] = JSON_EXTS + MSGPACK_EXTS

# 元信息 CSV 的文件名（不区分大小写）。用户手写的中文名也认，
# 免得因为叫「车辆信息.csv」就静默地拿不到品牌/平台。
VEHICLES_CSV_NAMES = {
    "vehicles.csv", "vehicle.csv", "车辆信息.csv", "车型信息.csv", "车型.csv",
}
CONDITIONS_CSV_NAMES = {
    "conditions.csv", "condition.csv", "工况信息.csv", "工况.csv",
}


# ═══════════════════════════════════════════════════════════════════════
# 文件读取：JSON / msgpack
# ═══════════════════════════════════════════════════════════════════════

def _decode_msgpack_keys(obj: Any) -> Any:
    """把 msgpack 里可能残留的 bytes key/value 转成 str。

    float 数组原样保留 —— 曲线点动辄几十万个，逐个转换会显著拖慢导入。
    """
    if isinstance(obj, dict):
        out: Dict[Any, Any] = {}
        for k, v in obj.items():
            if isinstance(k, (bytes, bytearray)):
                try:
                    k = bytes(k).decode("utf-8")
                except UnicodeDecodeError:
                    k = bytes(k).decode("latin-1")
            out[k] = _decode_msgpack_keys(v)
        return out
    if isinstance(obj, (bytes, bytearray)):
        try:
            return bytes(obj).decode("utf-8")
        except UnicodeDecodeError:
            return obj
    if isinstance(obj, list):
        return [_decode_msgpack_keys(x) for x in obj]
    return obj


def _msgpack_lib():
    try:
        import msgpack  # type: ignore
    except ImportError:
        raise SystemExit(
            "读取 msgpack 文件需要 msgpack 库，请先安装：\n"
            "    pip install msgpack"
        )
    return msgpack


def load_msgpack_bytes(raw: bytes) -> Any:
    """读 msgpack 字节流。

    msgpack 的 str/bytes 语义在 0.5→1.0 之间变过：老版本 dump 出来的
    中文 key 可能是 bytes。这里走 ``raw=False`` 再做一次兜底解码，
    避免出现 ``b'\\xe8\\xa7\\x92...'`` 这种 key 导致指标名全乱。
    """
    mp = _msgpack_lib()
    data = mp.unpackb(raw, raw=False, strict_map_key=False)
    return _decode_msgpack_keys(data)


def _looks_like_msgpack(raw: bytes) -> bool:
    """msgpack 的 map/array 首字节落在 0x80-0x9F（fixmap/fixarray）、0xDC-0xDF。"""
    return bool(raw) and (0x80 <= raw[0] <= 0x9F or 0xDC <= raw[0] <= 0xDF)


def load_data_bytes(raw: bytes, filename: str = "") -> Any:
    """按后缀（认不出就按内容嗅探）解析一份数据文件，返回 Python 对象。"""
    lower = (filename or "").lower()
    if lower.endswith(MSGPACK_EXTS):
        return load_msgpack_bytes(raw)
    if lower.endswith(JSON_EXTS):
        return json.loads(raw.decode("utf-8-sig"))
    if _looks_like_msgpack(raw):
        return load_msgpack_bytes(raw)
    return json.loads(raw.decode("utf-8-sig"))


def load_data_file(path: str) -> Any:
    """按后缀读取磁盘上的数据文件（JSON / msgpack）。"""
    with open(path, "rb") as fh:
        raw = fh.read()
    return load_data_bytes(raw, path)


# ═══════════════════════════════════════════════════════════════════════
# raw_key 解析（与 seed_data.parse_raw_key 保持同一套语义）
# ═══════════════════════════════════════════════════════════════════════

def parse_raw_key(raw_key: str) -> Tuple[Optional[int], str, Optional[str]]:
    """raw_key → (数字前缀 or None, name, unit or None)。

    参数/曲线名都可能是 ``'13-侧向加速度稳态值(g)'`` / ``'侧向加速度稳态值(g)'``
    / ``'侧向加速度稳态值'`` 三种形态。数字前缀只用于排序，不入库。
    """
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


def is_average_key(k: str) -> bool:
    return str(k).strip() in AVG_KEYS


def exp_sort_key(k: str) -> Tuple[int, float, str]:
    """排序：数字实验在前（按数值），平均最后。"""
    if is_average_key(k):
        return (1, 0.0, k)
    m = re.match(r"^\s*(\d+)\s*$", str(k))
    if m:
        return (0, float(m.group(1)), str(k))
    return (0, 9999.0, str(k))


# ═══════════════════════════════════════════════════════════════════════
# 单份数据的解析
# ═══════════════════════════════════════════════════════════════════════

def looks_like_params(d: Any) -> bool:
    """指标 dict：值都是标量（数字 / 字符串 / None）。"""
    if not isinstance(d, dict) or not d:
        return False
    return all(not isinstance(v, dict) for v in d.values())


def looks_like_curves(d: Any) -> bool:
    """曲线 dict：值是 dict 且含 curve/curves/series。"""
    if not isinstance(d, dict) or not d:
        return False
    for v in d.values():
        if isinstance(v, dict) and any(g in v for g in CURVE_GROUP_KEYS):
            return True
    return False


def normalize_curves(raw: Any) -> Dict[str, dict]:
    """把各家的曲线写法统一成 ``{name: {curve: [...], title, xlabel, ylabel}}``。"""
    out: Dict[str, dict] = {}
    if not isinstance(raw, dict):
        return out
    for cname, cval in raw.items():
        if not isinstance(cval, dict):
            continue
        series = None
        for g in CURVE_GROUP_KEYS:
            if g in cval:
                series = cval[g]
                break
        if series is None:
            # 可能直接就是 {"x": [...], "y": [...]} 的单条线
            if "x" in cval and "y" in cval:
                series = [cval]
            else:
                continue
        if isinstance(series, dict):
            series = [series]
        norm: List[dict] = []
        for i, s in enumerate(series if isinstance(series, list) else []):
            if not isinstance(s, dict):
                continue
            norm.append({
                "x": s.get("x") or [],
                "y": s.get("y") or [],
                "curvetype": s.get("curvetype") or "line",
                # 没有 curvename 时给个稳定的兜底名，前端按原样展示
                "curvename": s.get("curvename") or ("series%d" % i),
            })
        if not norm:
            continue
        _, nm, _u = parse_raw_key(str(cname))
        out[str(cname)] = {
            "curve": norm,
            "title": cval.get("title") or nm,
            "xlabel": cval.get("xlabel"),
            "ylabel": cval.get("ylabel"),
        }
    return out


def parse_payload(obj: Any) -> Dict[str, Dict[str, Any]]:
    """把一份数据规范化成 ``{实验号: {params, curves_cal, curves_duiqi}}``。

    支持两种写法：

    * **A（推荐）** 顶层 key = 实验号 → ``{exp_no: [params, cal, duiqi]}``
    * **B（扁平）** 顶层直接是 params → ``{params, curves_cal, curves_duiqi}``
      （等价于单次实验，实验号记作 ``'1'``）
    """
    result: Dict[str, Dict[str, Any]] = {}

    if isinstance(obj, dict) and isinstance(obj.get("params"), (dict, list)):
        result["1"] = {
            "params": obj.get("params") or {},
            "curves_cal": normalize_curves(obj.get("curves_cal")),
            "curves_duiqi": normalize_curves(obj.get("curves_duiqi")),
        }
        return result

    if not isinstance(obj, dict):
        return result

    for exp_no, arr in obj.items():
        if isinstance(arr, list):
            params = arr[0] if len(arr) > 0 and isinstance(arr[0], dict) else {}
            cal = arr[1] if len(arr) > 1 else {}
            dui = arr[2] if len(arr) > 2 else {}
        elif isinstance(arr, dict):
            params = arr.get("params") or {}
            cal = arr.get("curves_cal") or {}
            dui = arr.get("curves_duiqi") or {}
        else:
            continue
        # 容错：有人把 params 和 curves 混在一个 dict 里
        if isinstance(params, dict):
            extra_cal = {k: v for k, v in params.items() if looks_like_curves({k: v})}
            if extra_cal:
                cal = {**extra_cal, **(cal or {})}
                params = {k: v for k, v in params.items() if k not in extra_cal}
        result[str(exp_no)] = {
            "params": params if isinstance(params, dict) else {},
            "curves_cal": normalize_curves(cal),
            "curves_duiqi": normalize_curves(dui),
        }
    return result


# ═══════════════════════════════════════════════════════════════════════
# 元信息 CSV
# ═══════════════════════════════════════════════════════════════════════

def _csv_rows(src: Any) -> List[Dict[str, str]]:
    """读 CSV。``src`` 可以是路径、bytes，或 CSV 文本本身。"""
    if src is None:
        return []
    if isinstance(src, (bytes, bytearray)):
        text = bytes(src).decode("utf-8-sig", errors="replace")
    elif isinstance(src, str) and os.path.isfile(src):
        with open(src, "r", encoding="utf-8-sig", newline="") as fh:
            text = fh.read()
    elif isinstance(src, str):
        text = src.lstrip("\ufeff")
    else:
        return []
    return [r for r in csv.DictReader(io.StringIO(text))]


def read_vehicles_csv(src: Any) -> Dict[str, Dict[str, Any]]:
    """vehicles.csv → ``{车型名: {platform, brand, drive_type, vin, model_year}}``"""
    out: Dict[str, Dict[str, Any]] = {}
    for row in _csv_rows(src):
        def pick(*names: str) -> str:
            for n in names:
                v = (row.get(n) or "").strip()
                if v:
                    return v
            return ""
        name = pick("车型", "车型名", "vehicle", "name")
        if not name:
            continue
        out[name] = {
            "platform": pick("平台", "platform") or "默认平台",
            "brand": pick("品牌", "brand") or "默认品牌",
            "drive_type": pick("驱动方式", "驱动", "drive_type") or None,
            "vin": pick("车架号", "vin") or None,
            "model_year": pick("年款", "model_year") or None,
        }
    return out


def read_conditions_csv(src: Any) -> Dict[str, Dict[str, Optional[str]]]:
    """conditions.csv → ``{工况名: {category, description}}``"""
    out: Dict[str, Dict[str, Optional[str]]] = {}
    for row in _csv_rows(src):
        name = (row.get("name") or row.get("工况") or "").strip()
        if not name:
            continue
        out[name] = {
            "category": (row.get("category") or row.get("分类") or "").strip() or None,
            "description": (row.get("description") or row.get("说明") or "").strip() or None,
        }
    return out


def infer_vehicle(name: str, meta: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """没有 CSV 时按车型名兜底推断品牌（``'极光EV-01'`` → 品牌 ``'极光'``）。

    **数据文件里没有任何车辆属性**（算法侧不产出平台/品牌/驱动方式），
    所以 CSV 是唯一的来源；缺了就只能猜。
    """
    if name in meta:
        m = dict(meta[name])
    else:
        m = {"platform": "默认平台", "brand": "默认品牌",
             "drive_type": None, "vin": None, "model_year": None}
        mm = re.match(r"^([A-Za-z\u4e00-\u9fff]{2,6})", name)
        if mm:
            m["brand"] = mm.group(1)
    if m.get("model_year"):
        try:
            m["model_year"] = int(str(m["model_year"])[:4])
        except ValueError:
            m["model_year"] = None
    return m


# ═══════════════════════════════════════════════════════════════════════
# 扫描：路径列表 → 待导入条目
# ═══════════════════════════════════════════════════════════════════════

def normalize_relpath(p: str) -> str:
    """统一分隔符、去掉 ``./`` 与首尾斜杠，防止 ``a\\b`` 与 ``a/b`` 变成两台车。"""
    s = str(p or "").replace("\\", "/")
    s = re.sub(r"^\./+", "", s.strip())
    return s.strip("/")


def strip_ext(filename: str, exts: Tuple[str, ...] = READABLE_EXTS) -> str:
    """去掉数据后缀（``'角阶跃.msgpack'`` → ``'角阶跃'``）。"""
    lower = filename.lower()
    for ext in exts:
        if lower.endswith(ext):
            return filename[: -len(ext)]
    return filename


def _is_meta_csv(basename: str) -> bool:
    low = basename.lower()
    return low in VEHICLES_CSV_NAMES or low in CONDITIONS_CSV_NAMES


def strip_common_root(rels: List[str]) -> List[str]:
    """剥掉最外层那个「包裹文件夹」。

    浏览器用 ``webkitdirectory`` 选文件夹时，``webkitRelativePath`` 会把
    被选中的那一层也带上（``data/极光EV-1/角阶跃.json``）。前端会先剥一次，
    这里再兜一次底，因为**前端剥错时症状很隐蔽**：车型名会变成 ``data``，
    数据照样入库，只是目录树里多一层奇怪的「车型」—— 更糟的是
    ``data/示例车A/角阶跃.json`` 和 ``data/示例车B/角阶跃.json`` 会撞成
    同一台车，然后因为实验号重复直接 IntegrityError。

    怎么判断该剥几层：

    1. **有元信息 CSV 时以 CSV 为准** —— CSV 是放在数据根目录的，它所在的
       目录就是根。这条最可靠，因为 CSV 的位置是数据提供方自己定的。
    2. 没有 CSV 时，如果所有文件都至少有 3 层，那多出来的一层只能是包裹目录。

    注意**不能无条件剥**：用户直接把一台车的文件夹拖进来时，
    ``极光EV-1/角阶跃.json`` 里的 ``极光EV-1`` 就是车型名，剥了就没了。
    """
    if not rels:
        return rels
    parts = [r.split("/") for r in rels]

    csv_dirs = [len(p) - 1 for p in parts if p and _is_meta_csv(p[-1])]
    if csv_dirs:
        root = min(csv_dirs)
    elif all(len(p) >= 3 for p in parts) and len({p[0] for p in parts}) == 1:
        root = 1
    else:
        return rels

    if root <= 0:
        return rels
    # 比 root 还浅的（比如裹目录里的 README.txt）原样留着，
    # 让它走正常的「后缀不认识 → skipped」通道，不要静默丢掉。
    return ["/".join(p[root:]) if len(p) > root else "/".join(p) for p in parts]


def prepare_paths(rels: Iterable[str]) -> List[str]:
    """规范化相对路径 + 剥掉包裹目录。**只调用一次**（重复剥会少一层）。"""
    return strip_common_root([normalize_relpath(p) for p in rels])


class ItemBuilder:
    """边收边解析的累加器。

    为什么不「先把所有原始字节收齐再解析」：上传几十份曲线文件时，
    原始 JSON 文本和解析出来的对象会**同时**驻留内存，峰值直接翻倍。
    这里每份文件解析完就把 ``raw`` 丢掉，峰值只跟解析后的数据有关。
    """

    def __init__(self, exts: Tuple[str, ...] = READABLE_EXTS) -> None:
        self.exts = tuple(e.lower() for e in exts)
        self.items: List[Dict[str, Any]] = []
        self.errors: List[Dict[str, str]] = []
        self.skipped: List[str] = []
        self.vmeta: Dict[str, Dict[str, Any]] = {}
        self.cmeta: Dict[str, Dict[str, Optional[str]]] = {}
        self.vehicles_csv: Optional[str] = None
        self.conditions_csv: Optional[str] = None

    def add(self, relpath: str, raw: bytes) -> None:
        """收一份文件。``relpath`` 必须是已经 :func:`prepare_paths` 过的。"""
        rel = relpath
        if not rel:
            return
        base = rel.rsplit("/", 1)[-1]
        low = base.lower()

        if low in VEHICLES_CSV_NAMES:
            self.vmeta = read_vehicles_csv(raw)
            self.vehicles_csv = rel
            return
        if low in CONDITIONS_CSV_NAMES:
            self.cmeta = read_conditions_csv(raw)
            self.conditions_csv = rel
            return
        if not low.endswith(self.exts):
            self.skipped.append(rel)
            return

        parts = rel.split("/")
        if len(parts) < 2:
            self.errors.append({
                "file": rel,
                "error": "缺少车型文件夹：目录结构应为 <车型>/<工况>.<后缀>",
            })
            return
        vehicle = parts[0]
        condition = strip_ext(base, self.exts)

        try:
            obj = load_data_bytes(raw, base)
        except Exception as e:                                  # noqa: BLE001
            self.errors.append({
                "file": rel,
                "error": "解析失败: %s: %s" % (type(e).__name__, e),
            })
            return
        payload = parse_payload(obj)
        if not payload:
            self.errors.append({
                "file": rel, "error": "结构无法识别（既不是 params 也不是曲线）",
            })
            return

        self.items.append({
            "vehicle": vehicle,
            "condition": condition,
            "file": rel,
            "payload": payload,
        })

    def result(self) -> Dict[str, Any]:
        return {
            "items": self.items,
            "errors": self.errors,
            "skipped": self.skipped,
            "vmeta": self.vmeta,
            "cmeta": self.cmeta,
            "vehicles_csv": self.vehicles_csv,
            "conditions_csv": self.conditions_csv,
        }


def build_items(
    entries: Iterable[Tuple[str, bytes]],
    exts: Tuple[str, ...] = READABLE_EXTS,
) -> Dict[str, Any]:
    """``[(相对路径, 字节)]`` → 待导入条目 + 元信息 + 错误清单。

    命令行 / 测试用的一次性接口；上传接口走 :class:`ItemBuilder`，
    因为那边每份文件是分开读进来的。
    """
    pairs = list(entries)
    rels = prepare_paths([p for p, _ in pairs])
    b = ItemBuilder(exts)
    for rel, (_, raw) in zip(rels, pairs):
        b.add(rel, raw)
    return b.result()


def scan_dir(
    data_dir: str,
    exts: Tuple[str, ...] = READABLE_EXTS,
) -> Dict[str, Any]:
    """扫描磁盘目录，返回与 :func:`build_items` 相同的结构。"""
    if not os.path.isdir(data_dir):
        raise SystemExit("数据目录不存在: %s" % data_dir)

    entries: List[Tuple[str, bytes]] = []
    for entry in sorted(os.listdir(data_dir)):
        vdir = os.path.join(data_dir, entry)
        if not os.path.isdir(vdir):
            if os.path.isfile(vdir):
                entries.append((entry, _read_bytes(vdir)))
            continue
        for fn in sorted(os.listdir(vdir)):
            fpath = os.path.join(vdir, fn)
            if os.path.isfile(fpath):
                entries.append(("%s/%s" % (entry, fn), _read_bytes(fpath)))
    return build_items(entries, exts=exts)


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


# ═══════════════════════════════════════════════════════════════════════
# 统计 / 预览
# ═══════════════════════════════════════════════════════════════════════

def summarize(items: List[Dict[str, Any]], vmeta: Optional[Dict] = None) -> Dict[str, Any]:
    """把条目汇总成一份「将写入什么」的清单（dry-run 与上传预览共用）。"""
    vehicles = sorted({it["vehicle"] for it in items})
    conditions = sorted({it["condition"] for it in items})
    n_exp = n_ind = n_cur = n_pts = 0
    for it in items:
        for blob in it["payload"].values():
            n_exp += 1
            n_ind += len(blob.get("params") or {})
            for cdict in (blob.get("curves_cal"), blob.get("curves_duiqi")):
                for cval in (cdict or {}).values():
                    n_cur += 1
                    ser = cval.get("curve") or []
                    n_pts += len(ser[0].get("x", [])) if ser else 0
    return {
        "files": len(items),
        "vehicles": len(vehicles),
        "conditions": len(conditions),
        "experiments": n_exp,
        "indicators": n_ind,
        "curves": n_cur,
        "points": n_pts,
        "vehicle_names": vehicles,
        "condition_names": conditions,
        "meta_vehicles": len(vmeta or {}),
    }


def preview_text(items: List[Dict[str, Any]], meta: Dict[str, Any]) -> str:
    """命令行 dry-run 用的可读摘要。"""
    s = summarize(items, meta.get("vmeta"))
    lines = [
        "  车型 %d 台 / 工况 %d 个 / 车×工况文件 %d 份"
        % (s["vehicles"], s["conditions"], s["files"]),
        "  将写入：实验 %d 次 / 指标 %d 行 / 曲线 %d 条（%s 个采样点）"
        % (s["experiments"], s["indicators"], s["curves"], _fmt_int(s["points"])),
    ]
    if s["meta_vehicles"]:
        lines.append("  车辆元信息来自: %s（%d 台）"
                     % (meta.get("vehicles_csv"), s["meta_vehicles"]))
    else:
        lines.append("  ! 未找到 vehicles.csv，品牌将按车型名前缀推断")
    return "\n".join(lines)


def _fmt_int(n: int) -> str:
    return "{:,}".format(int(n))


# ═══════════════════════════════════════════════════════════════════════
# 写库
# ═══════════════════════════════════════════════════════════════════════

def reset_schema() -> None:
    """清空所有表（导入前用）。"""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def ensure_schema() -> None:
    Base.metadata.create_all(bind=engine)


def import_items(
    db,
    items: List[Dict[str, Any]],
    vmeta: Optional[Dict[str, Dict[str, Any]]] = None,
    cmeta: Optional[Dict[str, Dict[str, Optional[str]]]] = None,
    *,
    append: bool = False,
    progress: Optional[Callable[[int, int], None]] = None,
    batch: int = 10,
) -> Dict[str, Any]:
    """把 :func:`build_items` 的产物写进库。

    ``append=False``（默认）假定表已经清空过（:func:`reset_schema`）。

    ``append=True`` 是**按「车 × 工况」覆盖**，不是简单追加：
    同一个 (车型, 工况) 再导一次，会先删掉它原有的实验再写新的。
    理由 —— ``experiments`` 上有 ``uq_exp_vehicle_condition_no`` 唯一约束，
    简单追加遇到同一实验号会直接 IntegrityError；而且「重新上传一次同一份
    数据」本来就应该是覆盖语义，不该报错。层级（平台/品牌/车型/工况）
    也复用已有的，不重复插。
    """
    vmeta = vmeta or {}
    cmeta = cmeta or {}
    t0 = time.time()

    plat_cache: Dict[str, Platform] = {}
    brand_cache: Dict[Tuple[str, str], Brand] = {}
    veh_cache: Dict[Tuple[str, str], Vehicle] = {}
    cond_cache: Dict[str, Condition] = {}

    if append:
        for p in db.query(Platform).all():
            plat_cache[p.name] = p
        for b in db.query(Brand).options(joinedload(Brand.platform)).all():
            brand_cache[(b.platform.name, b.name)] = b
        for v in db.query(Vehicle).options(joinedload(Vehicle.brand)).all():
            veh_cache[(v.name, v.brand.name)] = v
        for c in db.query(Condition).all():
            cond_cache[c.name] = c

    # ── 工况 ──
    for cname in sorted({it["condition"] for it in items}):
        cm = cmeta.get(cname, {})
        cond = cond_cache.get(cname)
        if cond is None:
            cond = Condition(name=cname)
            db.add(cond)
            cond_cache[cname] = cond
        # CSV 给了分类/说明就补上（append 时也允许补齐）
        if cm.get("category"):
            cond.category = cm["category"]
        if cm.get("description"):
            cond.description = cm["description"]
    db.flush()

    # ── 平台 / 品牌 / 车型 ──
    vbrand: Dict[str, str] = {}
    for vname in sorted({it["vehicle"] for it in items}):
        m = infer_vehicle(vname, vmeta)
        pname, bname = m["platform"], m["brand"]
        vbrand[vname] = bname

        plat = plat_cache.get(pname)
        if plat is None:
            plat = Platform(name=pname)
            db.add(plat)
            db.flush()
            plat_cache[pname] = plat

        brand = brand_cache.get((pname, bname))
        if brand is None:
            brand = Brand(name=bname, platform_id=plat.id)
            db.add(brand)
            db.flush()
            brand_cache[(pname, bname)] = brand

        veh = veh_cache.get((vname, bname))
        if veh is None:
            veh = Vehicle(name=vname, brand_id=brand.id, extra_attrs={})
            db.add(veh)
            db.flush()
            veh_cache[(vname, bname)] = veh
        # 属性以 CSV 为准；CSV 没给就保持原值（不要用 None 覆盖已有信息）
        for attr in ("drive_type", "vin", "model_year"):
            if m.get(attr) is not None:
                setattr(veh, attr, m[attr])
    db.commit()

    # ── 实验 / 指标 / 曲线 ──
    n_exp = n_ind = n_cur = n_pts = 0
    done = 0
    total = len(items)
    for it in items:
        veh = veh_cache[(it["vehicle"], vbrand[it["vehicle"]])]
        cond = cond_cache[it["condition"]]

        if append:
            # 覆盖语义：先清掉这台车在这个工况下的旧实验。
            # Curve / Indicator 的外键是 ON DELETE CASCADE，数据库层会一起删。
            db.query(Experiment).filter(
                Experiment.vehicle_id == veh.id,
                Experiment.condition_id == cond.id,
            ).delete(synchronize_session=False)
            db.flush()

        exp_nos = sorted(it["payload"].keys(), key=exp_sort_key)
        for order, exp_no in enumerate(exp_nos):
            avg = is_average_key(exp_no)
            blob = it["payload"][exp_no]
            exp = Experiment(
                vehicle_id=veh.id, condition_id=cond.id,
                experiment_no="平均" if avg else exp_no,
                is_average=avg,
                sort_order=999 if avg else order,
            )
            db.add(exp)
            db.flush()
            n_exp += 1

            for rk, val in (blob["params"] or {}).items():
                _s, nm, unit = parse_raw_key(str(rk))
                if isinstance(val, dict):        # 防御：值是 dict 就取 value
                    val = val.get("value")
                fval: Optional[float] = None
                tval: Optional[str] = None
                try:
                    fval = float(val) if val is not None else None
                except (TypeError, ValueError):
                    tval = str(val) if val is not None else None
                db.add(Indicator(
                    experiment_id=exp.id, name=nm, unit=unit,
                    raw_key=str(rk), value=fval, text_value=tval,
                    category=cond.category,
                ))
                n_ind += 1

            for group, cdict in (("cal", blob["curves_cal"]),
                                 ("duiqi", blob["curves_duiqi"])):
                for corder, (ckey, cval) in enumerate((cdict or {}).items()):
                    ser = cval.get("curve") or []
                    npts = len(ser[0].get("x", [])) if ser else 0
                    _s, cnm, _u = parse_raw_key(str(ckey))
                    db.add(Curve(
                        experiment_id=exp.id, group=group, name=str(ckey),
                        title=cval.get("title") or cnm,
                        xlabel=cval.get("xlabel"), ylabel=cval.get("ylabel"),
                        series=ser, point_count=npts, sort_order=corder,
                    ))
                    n_cur += 1
                    n_pts += npts

        done += 1
        if done % batch == 0:
            db.commit()
        if progress is not None:
            progress(done, total)
    db.commit()

    return {
        "files": total,
        "experiments": n_exp,
        "indicators": n_ind,
        "curves": n_cur,
        "points": n_pts,
        "platforms": len(plat_cache),
        "brands": len(brand_cache),
        "vehicles": len(veh_cache),
        "conditions": len(cond_cache),
        "seconds": round(time.time() - t0, 2),
    }


def recalc_curve_meta() -> int:
    """导入后重算 ``curves.point_count``（老数据可能为空）。"""
    db = SessionLocal()
    try:
        rows = db.query(Curve).filter(Curve.point_count.is_(None)).all()
        for c in rows:
            ser = c.series or []
            c.point_count = len(ser[0].get("x", [])) if ser else 0
        if rows:
            db.commit()
        return len(rows)
    finally:
        db.close()
