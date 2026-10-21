"""数据导入：解析（JSON / msgpack / CSV）与写库。

目录约定
========

::

    <root>/
      vehicles.csv                # 可选，任意列：车名 + 任意车辆字段
      conditions.csv              # 可选，name,category,description
      <车名>/
        <工况>.json               # 无模式
        <模式>/<工况>.json        # 有模式（舒适 / 运动 / …）

* 中间目录就是试验模式（可以多层，按 ``/`` 拼接）。模式属于试验侧，
  每台车有几种完全由目录决定。
* 车名全局唯一。文件夹名可以带单号：``极光EV-01(CS-2024-001)``，
  括号里的内容会记为操稳单号；CSV 也可以写 ``操稳单号`` / ``平顺单号``，
  多个单号用 ``;``、``,``、``|``、``、`` 分隔。
* 车辆字段不写死。``vehicles.csv`` 的表头按 ``attr_defs.code`` 或
  ``attr_defs.name`` 匹配；匹配不到的列会**自动注册成新字段**，不需要改表。
* 数据格式不写死。已知格式（指标 + 曲线）解析成
  ``datasets / metric_values / curves``；其它格式整块放进 ``datasets.payload``。
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from sqlalchemy import delete, select

from app import crud
from app.database import Base, SessionLocal, engine
from app.metadata import seed_metadata
from app.models import (
    AttrDef,
    Condition,
    Curve,
    Dataset,
    DatasetType,
    MetricValue,
    TestOrder,
    Vehicle,
    VehicleMode,
)

AVG_KEYS = {"平均", "avg", "average", "AVE", "AVE."}
CURVE_GROUP_KEYS = ("curve", "curves", "series")

# ``.cw`` 是业务侧自定义的 msgpack 后缀，内容与 .msgpack 完全一致。
# 判断格式时后缀优先，后缀不认识再嗅探首字节（见 load_data_bytes）。
MSGPACK_EXTS = (".msgpack", ".msg", ".mpack", ".cw")
JSON_EXTS = (".json",)
READABLE_EXTS: Tuple[str, ...] = JSON_EXTS + MSGPACK_EXTS

VEHICLES_CSV_NAMES = {
    "vehicles.csv", "vehicle.csv", "车辆信息.csv", "车型信息.csv", "车型.csv",
}
CONDITIONS_CSV_NAMES = {
    "conditions.csv", "condition.csv", "工况信息.csv", "工况.csv",
}

# CSV 里不是「车辆属性」的列
NAME_COLUMNS = ("车名", "车型", "车辆名称", "名称", "vehicle", "name", "vehicle_name")
MODE_COLUMNS = ("模式", "试验模式", "车辆模式", "modes", "test_modes", "mode")
ORDER_COLUMNS = {
    "操稳": ("操稳单号", "操稳单", "handling_order", "handling_order_no",
             "test_order", "test_order_no", "单号"),
    "平顺": ("平顺单号", "平顺单", "ride_order", "ride_order_no"),
}

# 旧 CSV 表头 → 默认字段 code（默认字段的 name 与表头不一致时用）
HEADER_ALIASES = {
    "驱动方式": "drive_type",
    "车架号": "vin",
    "年款": "model_year",
}

DEFAULT_DATASET_TYPE = "handling_stability"


# ═══════════════════════════════════════════════════════════════════════
# 文件读取
# ═══════════════════════════════════════════════════════════════════════

def _decode_msgpack_keys(obj: Any) -> Any:
    """把 msgpack 里可能残留的 bytes key/value 转成 str（float 数组原样保留）。"""
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
    mp = _msgpack_lib()
    data = mp.unpackb(raw, raw=False, strict_map_key=False)
    return _decode_msgpack_keys(data)


def _looks_like_msgpack(raw: bytes) -> bool:
    return bool(raw) and (0x80 <= raw[0] <= 0x9F or 0xDC <= raw[0] <= 0xDF)


def load_data_bytes(raw: bytes, filename: str = "") -> Any:
    lower = (filename or "").lower()
    if lower.endswith(MSGPACK_EXTS):
        return load_msgpack_bytes(raw)
    if lower.endswith(JSON_EXTS):
        return json.loads(raw.decode("utf-8-sig"))
    if _looks_like_msgpack(raw):
        return load_msgpack_bytes(raw)
    return json.loads(raw.decode("utf-8-sig"))


def load_data_file(path: str) -> Any:
    with open(path, "rb") as fh:
        raw = fh.read()
    return load_data_bytes(raw, path)


# ═══════════════════════════════════════════════════════════════════════
# raw_key 解析
# ═══════════════════════════════════════════════════════════════════════

def parse_raw_key(raw_key: str) -> Tuple[Optional[int], str, Optional[str]]:
    """raw_key → (数字前缀 or None, name, unit or None)。数字前缀只用于排序。"""
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


# ═══════════════════════════════════════════════════════════════════════
# 工况识别：文件名不可靠时，靠内容里的指标名反推
# ═══════════════════════════════════════════════════════════════════════

def collect_metric_names(experiments: Dict[str, Dict[str, Any]]) -> Set[str]:
    """从 parse_payload 的结果里取指标名（已剥掉数字前缀和单位）。

    只认 params。曲线名带通道后缀，各工况重名太多，区分度不如指标。
    """
    out: Set[str] = set()
    for exp in (experiments or {}).values():
        params = exp.get("params") if isinstance(exp, dict) else None
        if not isinstance(params, dict):
            continue
        for raw_key in params:
            _seq, name, _unit = parse_raw_key(str(raw_key))
            if name:
                out.add(name)
    return out


def build_condition_index(db) -> Dict[str, Set[str]]:
    """``{工况名: 该工况下出现过的指标名}``，从库里已有数据反推。

    不维护规则表：真实工况的指标组合是稳定的，同一个工况换个文件名
    也能靠指标名认出来。

    这是**尽力而为**的辅助信息 —— 全新库还没建表时直接返回空，让导入
    退回「只靠文件名」的老路；不能因为拿不到索引就把上传整个打死。
    """
    try:
        rows = db.execute(
            select(Condition.name, MetricValue.name)
            .join(Dataset, Dataset.condition_id == Condition.id)
            .join(MetricValue, MetricValue.dataset_id == Dataset.id)
            .distinct()
        )
        out: Dict[str, Set[str]] = {}
        for cname, mname in rows:
            if cname and mname:
                out.setdefault(cname, set()).add(mname)
        return out
    except Exception:                             # noqa: BLE001
        # 表不存在 / 库还没初始化。回滚失败的事务，别污染调用方的 session。
        db.rollback()
        return {}


def rank_conditions(
    names: Set[str],
    index: Dict[str, Set[str]],
    top: int = 3,
) -> List[Tuple[float, str]]:
    """按指标名覆盖率给工况打分，降序返回前 ``top`` 个。"""
    scored: List[Tuple[float, str]] = []
    if not names or not index:
        return scored
    for cname, known in index.items():
        if not known:
            continue
        hit = len(names & known)
        if not hit:
            continue
        scored.append((hit / min(len(names), len(known)), cname))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return scored[:top]


def infer_condition(
    names: Set[str],
    index: Dict[str, Set[str]],
    threshold: float = 0.5,
) -> Optional[str]:
    """按指标名重合度挑最像的工况；认不出返回 None。

    分母用 ``min(len)`` 而不是并集：一份数据可能只带该工况的部分指标，
    但不会带上别的工况的指标 —— 覆盖率比 Jaccard 更贴合这种情形。

    **并列最高时返回 None。** 同一族试验（比如「角阶跃 / 角阶跃-高速 /
    侧向风敏感性」）的指标名可能完全一样，光看指标分不出来。这种情况
    宁可认不出、保留文件名，也不能随便挑一个 —— 归错工况比归不了更难发现。
    """
    scored = rank_conditions(names, index, top=2)
    if not scored:
        return None
    best_score, best = scored[0]
    if best_score < threshold:
        return None
    if len(scored) > 1 and scored[1][0] == best_score:
        return None
    return best


def match_condition_by_name(stem: str, known: Set[str]) -> Optional[str]:
    """文件名里含已知工况名就认，取最长匹配。

    取最长是为了让 ``角阶跃-高速`` 赢过 ``角阶跃`` —— 否则前缀相同的
    工况会被短的抢走。这样 ``20241021_角阶跃-高速_001.json`` 也能对上。
    """
    hits = [c for c in known if c and c in stem]
    return max(hits, key=len) if hits else None


def exp_sort_key(k: str) -> Tuple[int, float, str]:
    if is_average_key(k):
        return (1, 0.0, k)
    m = re.match(r"^\s*(\d+)\s*$", str(k))
    if m:
        return (0, float(m.group(1)), str(k))
    return (0, 9999.0, str(k))


# ═══════════════════════════════════════════════════════════════════════
# 单份数据解析
# ═══════════════════════════════════════════════════════════════════════

def looks_like_params(d: Any) -> bool:
    if not isinstance(d, dict) or not d:
        return False
    return all(not isinstance(v, dict) for v in d.values())


def looks_like_curves(d: Any) -> bool:
    if not isinstance(d, dict) or not d:
        return False
    for v in d.values():
        if isinstance(v, dict) and any(g in v for g in CURVE_GROUP_KEYS):
            return True
    return False


def normalize_curves(raw: Any) -> Dict[str, dict]:
    """把各家曲线写法统一成 ``{name: {curve: [...], title, xlabel, ylabel}}``。"""
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


def _curve_groups(arr: List[Any]) -> Dict[str, Dict[str, dict]]:
    """``[params, curves_cal, curves_duiqi, …]`` → ``{cal: …, duiqi: …}``.

    位置 1 固定是 ``cal``、位置 2 固定是 ``duiqi``；再往后的位置用
    ``group3`` / ``group4`` 命名，保证新格式可以带更多曲线组而不改这里。
    """
    groups: Dict[str, Dict[str, dict]] = {}
    names = ("cal", "duiqi")
    for i, blob in enumerate(arr[1:]):
        if not isinstance(blob, dict):
            continue
        name = names[i] if i < len(names) else "group%d" % (i + 1)
        groups[name] = normalize_curves(blob)
    return groups


def parse_payload(obj: Any) -> Dict[str, Dict[str, Any]]:
    """规范化成 ``{实验号: {"params": {...}, "curves": {group: {...}}}}``。

    支持：

    * **A（推荐）** ``{exp_no: [params, cal, duiqi]}``
    * **B（扁平）** ``{"params": {...}, "curves_cal": {...}, "curves_duiqi": {...}}``
    * **C（按组命名）** ``{exp_no: {"params": …, "curves_cal": …}}``
    """
    result: Dict[str, Dict[str, Any]] = {}

    if isinstance(obj, dict) and isinstance(obj.get("params"), (dict, list)):
        groups: Dict[str, Dict[str, dict]] = {}
        for key, val in obj.items():
            if key == "params":
                continue
            if key == "curves":
                groups["cal"] = normalize_curves(val)
            elif str(key).startswith("curves_"):
                groups[str(key)[len("curves_"):]] = normalize_curves(val)
        result["1"] = {
            "params": obj.get("params") if isinstance(obj.get("params"), dict) else {},
            "curves": groups,
        }
        return result

    if not isinstance(obj, dict):
        return result

    for exp_no, arr in obj.items():
        if isinstance(arr, list):
            params = arr[0] if len(arr) > 0 and isinstance(arr[0], dict) else {}
            groups = _curve_groups(arr)
        elif isinstance(arr, dict):
            params = arr.get("params") or {}
            groups = {}
            for key, val in arr.items():
                if key == "params":
                    continue
                if key == "curves":
                    groups["cal"] = normalize_curves(val)
                elif str(key).startswith("curves_"):
                    groups[str(key)[len("curves_"):]] = normalize_curves(val)
        else:
            continue

        # 容错：有人把曲线混进 params
        if isinstance(params, dict):
            extra = {k: v for k, v in params.items() if looks_like_curves({k: v})}
            if extra:
                cal = dict(groups.get("cal") or {})
                cal.update(normalize_curves(extra))
                groups["cal"] = cal
                params = {k: v for k, v in params.items() if k not in extra}

        result[str(exp_no)] = {
            "params": params if isinstance(params, dict) else {},
            "curves": groups,
        }
    return result


# ═══════════════════════════════════════════════════════════════════════
# CSV
# ═══════════════════════════════════════════════════════════════════════

def _csv_rows(src: Any) -> List[Dict[str, str]]:
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


def _split_multi(value: str) -> List[str]:
    return [x.strip() for x in re.split(r"[;,|、，]", value or "") if x.strip()]


def _pick(row: Dict[str, str], names: Iterable[str]) -> str:
    lower = {str(k).strip().lower(): (v or "").strip() for k, v in row.items()}
    for n in names:
        v = lower.get(n.lower(), "")
        if v:
            return v
    return ""


def read_vehicles_csv(src: Any) -> Dict[str, Dict[str, Any]]:
    """vehicles.csv → ``{车名: {"attrs": {表头: 值}, "modes": [...], "orders": [...]}}``.

    表头不在这里解释，写库时才按 ``attr_defs`` 映射；匹配不到的列会自动
    注册成新字段。
    """
    out: Dict[str, Dict[str, Any]] = {}
    for row in _csv_rows(src):
        name = _pick(row, NAME_COLUMNS)
        if not name:
            continue

        modes = _split_multi(_pick(row, MODE_COLUMNS))
        orders: List[Dict[str, str]] = []
        for discipline, cols in ORDER_COLUMNS.items():
            for no in _split_multi(_pick(row, cols)):
                orders.append({"discipline": discipline, "order_no": no})

        skip = {n.lower() for n in NAME_COLUMNS}
        skip.update(n.lower() for n in MODE_COLUMNS)
        for cols in ORDER_COLUMNS.values():
            skip.update(n.lower() for n in cols)

        attrs = {
            str(k).strip(): (v or "").strip()
            for k, v in row.items()
            if k and str(k).strip() and str(k).strip().lower() not in skip
            and (v or "").strip()
        }
        out[name] = {"attrs": attrs, "modes": modes, "orders": orders}
    return out


def read_conditions_csv(src: Any) -> Dict[str, Dict[str, Optional[str]]]:
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


# ═══════════════════════════════════════════════════════════════════════
# 扫描
# ═══════════════════════════════════════════════════════════════════════

def normalize_relpath(p: str) -> str:
    s = str(p or "").replace("\\", "/")
    s = re.sub(r"^\./+", "", s.strip())
    return s.strip("/")


def strip_ext(filename: str, exts: Tuple[str, ...] = READABLE_EXTS) -> str:
    lower = filename.lower()
    for ext in exts:
        if lower.endswith(ext):
            return filename[: -len(ext)]
    return filename


def _is_meta_csv(basename: str) -> bool:
    low = basename.lower()
    return low in VEHICLES_CSV_NAMES or low in CONDITIONS_CSV_NAMES


def strip_common_root(rels: List[str]) -> List[str]:
    """剥掉最外层包裹目录（浏览器选择文件夹时会带上被选中的那一层）。"""
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
    return ["/".join(p[root:]) if len(p) > root else "/".join(p) for p in parts]


def prepare_paths(rels: Iterable[str]) -> List[str]:
    return strip_common_root([normalize_relpath(p) for p in rels])


def split_vehicle_folder(folder: str) -> Tuple[str, Optional[str]]:
    """``极光EV-01(CS-2024-001)`` → ``("极光EV-01", "CS-2024-001")``。

    只认结尾的整对括号；没有括号就原样返回。
    """
    m = re.match(r"^(.*?)\s*[（(]\s*([^()（）]+?)\s*[）)]\s*$", folder)
    if m and m.group(1).strip():
        return m.group(1).strip(), m.group(2).strip()
    return folder.strip(), None


class ItemBuilder:
    """边收边解析的累加器。"""

    def __init__(
        self,
        exts: Tuple[str, ...] = READABLE_EXTS,
        dataset_type: str = DEFAULT_DATASET_TYPE,
        condition_index: Optional[Dict[str, Set[str]]] = None,
    ) -> None:
        self.exts = tuple(e.lower() for e in exts)
        self.dataset_type = dataset_type or DEFAULT_DATASET_TYPE
        self.items: List[Dict[str, Any]] = []
        self.errors: List[Dict[str, str]] = []
        self.skipped: List[str] = []
        self.vmeta: Dict[str, Dict[str, Any]] = {}
        self.cmeta: Dict[str, Dict[str, Optional[str]]] = {}
        self.vehicles_csv: Optional[str] = None
        self.conditions_csv: Optional[str] = None
        # 工况 → 指标名集合。调用方从库里带进来；没有也能跑，只是少一层依据。
        self.condition_index: Dict[str, Set[str]] = {
            k: set(v) for k, v in (condition_index or {}).items()
        }
        # 文件名本身就算已知工况的那些（conditions.csv / 库里已有的）
        self.known_conditions: Set[str] = set(self.condition_index)
        # 靠内容改判过的文件，result() 里回给调用方做提示
        self.inferred: List[Dict[str, str]] = []
        self.unresolved: List[Dict[str, str]] = []

    def add(self, relpath: str, raw: bytes) -> None:
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
            # CSV 里列出的工况名，等同于「已知工况」
            self.known_conditions |= set(self.cmeta)
            return
        if not low.endswith(self.exts):
            self.skipped.append(rel)
            return

        parts = rel.split("/")
        if len(parts) < 2:
            self.errors.append({
                "file": rel,
                "error": "缺少车名文件夹：目录结构应为 <车名>/[<模式>/]<工况>.<后缀>",
            })
            return

        folder, folder_order = split_vehicle_folder(parts[0])
        mode = "/".join(parts[1:-1]) or None
        # 文件名只是「候选工况」，最终归属在 result() 里定：
        # 文件名命中已知工况就用它，否则靠内容里的指标名反推。
        stem = strip_ext(base, self.exts)

        try:
            obj = load_data_bytes(raw, base)
        except Exception as e:                                  # noqa: BLE001
            self.errors.append({
                "file": rel,
                "error": "解析失败: %s: %s" % (type(e).__name__, e),
            })
            return

        experiments = parse_payload(obj) if self.dataset_type == DEFAULT_DATASET_TYPE else {}
        if experiments:
            kind = "experiments"
            payload = None
        else:
            # 未知格式：整块兜底进 datasets.payload，不丢数据
            kind = "payload"
            experiments = {}
            payload = obj

        self.items.append({
            "vehicle": folder,
            "folder_order": folder_order,
            "mode": mode,
            "condition": stem,
            "file": rel,
            "dataset_type": self.dataset_type,
            "kind": kind,
            "experiments": experiments,
            "payload": payload,
            # 内部字段，result() 用完就删
            "_stem": stem,
            "_metrics": collect_metric_names(experiments),
        })

    def _resolve_conditions(self) -> None:
        """给每份文件定工况。

        两轮：
        1. 文件名能对上已知工况（完全相等，或文件名里含工况名，如
           ``20241021_角阶跃-高速_001``）→ 采信文件名，并把这份数据的
           指标名并进索引，让后面的文件有更多依据；
        2. 剩下的靠指标名重合度反推。认不出就保留原文件名 —— 不猜。
        """
        index = {k: set(v) for k, v in self.condition_index.items()}
        known = set(self.known_conditions) | set(index)

        for it in self.items:
            stem = it["_stem"]
            hit = stem if stem in known else match_condition_by_name(stem, known)
            if hit:
                it["condition"] = hit
                it["_by"] = "filename"
                index.setdefault(hit, set()).update(it["_metrics"])

        for it in self.items:
            if it.get("_by"):
                continue
            guess = infer_condition(it["_metrics"], index)
            if guess:
                it["condition"] = guess
                it["_by"] = "content"
                self.inferred.append({
                    "file": it["file"], "filename": it["_stem"], "condition": guess,
                })
            else:
                it["_by"] = "filename-raw"
                self.unresolved.append({
                    "file": it["file"],
                    "filename": it["_stem"],
                    # 认不出时把最像的几个候选带上，用户照着改文件名即可
                    "candidates": [
                        c for _s, c in rank_conditions(it["_metrics"], index)
                    ],
                })

    def result(self) -> Dict[str, Any]:
        self._resolve_conditions()
        for it in self.items:
            it.pop("_stem", None)
            it.pop("_metrics", None)
            it.pop("_by", None)
        return {
            "items": self.items,
            "errors": self.errors,
            "skipped": self.skipped,
            "vmeta": self.vmeta,
            "cmeta": self.cmeta,
            "vehicles_csv": self.vehicles_csv,
            "conditions_csv": self.conditions_csv,
            "inferred_conditions": self.inferred,
            "unresolved_conditions": self.unresolved,
        }


def build_items(
    entries: Iterable[Tuple[str, bytes]],
    exts: Tuple[str, ...] = READABLE_EXTS,
    dataset_type: str = DEFAULT_DATASET_TYPE,
    condition_index: Optional[Dict[str, Set[str]]] = None,
) -> Dict[str, Any]:
    pairs = list(entries)
    rels = prepare_paths([p for p, _ in pairs])
    b = ItemBuilder(exts, dataset_type=dataset_type, condition_index=condition_index)
    for rel, (_, raw) in zip(rels, pairs):
        b.add(rel, raw)
    return b.result()


def scan_dir(
    data_dir: str,
    exts: Tuple[str, ...] = READABLE_EXTS,
    dataset_type: str = DEFAULT_DATASET_TYPE,
    condition_index: Optional[Dict[str, Set[str]]] = None,
) -> Dict[str, Any]:
    if not os.path.isdir(data_dir):
        raise SystemExit("数据目录不存在: %s" % data_dir)

    entries: List[Tuple[str, bytes]] = []
    for root, _dirs, files in os.walk(data_dir):
        for fn in sorted(files):
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, data_dir).replace("\\", "/")
            entries.append((rel, _read_bytes(full)))
    return build_items(
        entries, exts=exts, dataset_type=dataset_type,
        condition_index=condition_index,
    )


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


# ═══════════════════════════════════════════════════════════════════════
# 统计 / 预览
# ═══════════════════════════════════════════════════════════════════════

def summarize(items: List[Dict[str, Any]], vmeta: Optional[Dict] = None) -> Dict[str, Any]:
    vehicles: Set[str] = set()
    conditions: Set[str] = set()
    modes: Set[str] = set()
    types: Set[str] = set()
    n_ds = n_ind = n_cur = n_pts = 0

    for it in items:
        vehicles.add(it["vehicle"])
        conditions.add(it["condition"])
        types.add(it.get("dataset_type") or DEFAULT_DATASET_TYPE)
        if it.get("mode"):
            modes.add(it["mode"])
        if it["kind"] == "payload":
            n_ds += 1
            continue
        for blob in it["experiments"].values():
            n_ds += 1
            n_ind += len(blob.get("params") or {})
            for cdict in (blob.get("curves") or {}).values():
                for cval in (cdict or {}).values():
                    n_cur += 1
                    ser = cval.get("curve") or []
                    n_pts += len(ser[0].get("x", [])) if ser else 0

    return {
        "files": len(items),
        "vehicles": len(vehicles),
        "conditions": len(conditions),
        "modes": len(modes),
        "dataset_types": len(types),
        "datasets": n_ds,
        "indicators": n_ind,
        "curves": n_cur,
        "points": n_pts,
        "vehicle_names": sorted(vehicles),
        "condition_names": sorted(conditions),
        "mode_names": sorted(modes),
        "meta_vehicles": len(vmeta or {}),
    }


def preview_text(items: List[Dict[str, Any]], meta: Dict[str, Any]) -> str:
    s = summarize(items, meta.get("vmeta"))
    lines = [
        "  车辆 %d 台 / 模式 %d 种 / 工况 %d 个 / 文件 %d 份"
        % (s["vehicles"], s["modes"], s["conditions"], s["files"]),
        "  将写入：数据集 %d 条 / 指标 %d 行 / 曲线 %d 条（%s 个采样点）"
        % (s["datasets"], s["indicators"], s["curves"], _fmt_int(s["points"])),
    ]
    if s["meta_vehicles"]:
        lines.append("  车辆元信息来自: %s（%d 台）"
                     % (meta.get("vehicles_csv"), s["meta_vehicles"]))
    else:
        lines.append("  ! 未找到 vehicles.csv，车辆属性将为空（车名仍按文件夹写入）")
    return "\n".join(lines)


def _fmt_int(n: int) -> str:
    return "{:,}".format(int(n))


# ═══════════════════════════════════════════════════════════════════════
# 写库
# ═══════════════════════════════════════════════════════════════════════

def reset_schema() -> None:
    """清空所有表并写入默认元数据。"""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        seed_metadata(db)
    finally:
        db.close()


def ensure_schema() -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        seed_metadata(db)
    finally:
        db.close()


def _code_for(name: str) -> str:
    candidate = re.sub(r"[^a-zA-Z0-9_]+", "_", name).strip("_").lower()
    if candidate:
        return candidate[:50]
    import hashlib
    return "f_" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:10]


def _attr_lookup(db) -> Tuple[Dict[str, AttrDef], Dict[str, AttrDef]]:
    attrs = list(db.execute(select(AttrDef)).scalars())
    by_code = {a.code: a for a in attrs}
    by_name = {a.name.strip().lower(): a for a in attrs}
    return by_code, by_name


def _register_attr(db, header: str, values: Sequence[str],
                   by_code: Dict[str, AttrDef], by_name: Dict[str, AttrDef]) -> AttrDef:
    """把 CSV 表头映射到字段；匹配不到就自动注册一个新字段。"""
    key = header.strip()
    alias = HEADER_ALIASES.get(key)
    if alias and alias in by_code:
        return by_code[alias]
    if key in by_code:
        return by_code[key]
    if key.lower() in by_name:
        return by_name[key.lower()]

    value_type = "text"
    nums = [_parse_float(v) for v in values if v not in (None, "")]
    if nums and all(n is not None for n in nums):
        value_type = "number"

    code = _code_for(key)
    base = code
    i = 2
    while code in by_code:
        code = "%s_%d" % (base, i)
        i += 1

    attr = AttrDef(
        code=code, name=key, value_type=value_type,
        sort_order=1000 + len(by_code), show_in_list=True, is_active=True,
    )
    db.add(attr)
    db.flush()
    by_code[code] = attr
    by_name[key.lower()] = attr
    return attr


def _parse_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ensure_vehicle_modes(db, vehicle: Vehicle, modes: Iterable[str]) -> Dict[str, int]:
    existing = {
        m.mode: m for m in db.execute(
            select(VehicleMode).where(VehicleMode.vehicle_id == vehicle.id)
        ).scalars()
    }
    out = {name: obj.id for name, obj in existing.items() if obj.id is not None}
    order = len(existing)
    for raw in modes:
        name = str(raw).strip()
        if not name or name in existing:
            continue
        obj = VehicleMode(vehicle_id=vehicle.id, mode=name, sort_order=order)
        db.add(obj)
        db.flush()
        existing[name] = obj
        out[name] = obj.id
        order += 1
    return out


def _ensure_test_orders(
    db, vehicle: Vehicle, orders: Iterable[Dict[str, str]],
) -> Dict[Tuple[str, str], int]:
    existing = {
        (o.discipline, o.order_no): o for o in db.execute(
            select(TestOrder).where(TestOrder.vehicle_id == vehicle.id)
        ).scalars()
    }
    out = {
        key: obj.id for key, obj in existing.items() if obj.id is not None
    }
    for item in orders:
        discipline = str(item.get("discipline") or "操稳").strip()
        no = str(item.get("order_no") or "").strip()
        key = (discipline, no)
        if not no or key in existing:
            continue
        obj = TestOrder(vehicle_id=vehicle.id, discipline=discipline, order_no=no)
        db.add(obj)
        db.flush()
        existing[key] = obj
        out[key] = obj.id
    return out


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
    """把解析结果写进库。

    ``append=False`` 假定表刚重建过；``append=True`` 时按
    「车 × 模式 × 工况 × 数据格式 × 文件」覆盖，不会重复插入。
    """
    vmeta = vmeta or {}
    cmeta = cmeta or {}
    t0 = time.time()

    seed_metadata(db)
    by_code, by_name = _attr_lookup(db)

    # ── 自动注册 CSV 里出现的新字段 ──
    headers: Dict[str, List[str]] = {}
    for meta in vmeta.values():
        for header, value in (meta.get("attrs") or {}).items():
            headers.setdefault(header, []).append(value)
    header_codes: Dict[str, str] = {}
    for header, values in headers.items():
        attr = _register_attr(db, header, values, by_code, by_name)
        header_codes[header] = attr.code
    db.flush()

    # ── 工况 ──
    cond_cache: Dict[str, Condition] = {
        c.name: c for c in db.execute(select(Condition)).scalars()
    }
    for cname in sorted({it["condition"] for it in items}):
        cm = cmeta.get(cname, {})
        cond = cond_cache.get(cname)
        if cond is None:
            cond = Condition(name=cname)
            db.add(cond)
            db.flush()
            cond_cache[cname] = cond
        if cm.get("category"):
            cond.category = cm["category"]
        if cm.get("description"):
            cond.description = cm["description"]

    # ── 数据格式 ──
    type_codes = sorted({it.get("dataset_type") or DEFAULT_DATASET_TYPE for it in items})
    type_cache: Dict[str, DatasetType] = {
        t.code: t for t in db.execute(select(DatasetType)).scalars()
    }
    for code in type_codes:
        if code not in type_cache:
            t = DatasetType(code=code, name=code)
            db.add(t)
            db.flush()
            type_cache[code] = t

    # ── 车辆 / 属性 / 模式 / 单号 ──
    vehicles_in_items: Dict[str, List[Dict[str, Any]]] = {}
    for it in items:
        vehicles_in_items.setdefault(it["vehicle"], []).append(it)

    veh_cache: Dict[str, Vehicle] = {
        v.name: v for v in db.execute(select(Vehicle)).scalars()
    }
    mode_ids: Dict[int, Dict[str, int]] = {}
    order_ids: Dict[int, Dict[Tuple[str, str], int]] = {}

    for vname in sorted(vehicles_in_items):
        veh = veh_cache.get(vname)
        if veh is None:
            veh = Vehicle(name=vname)
            db.add(veh)
            db.flush()
            veh_cache[vname] = veh

        meta = vmeta.get(vname, {})
        attrs: Dict[str, Any] = {}
        for header, value in (meta.get("attrs") or {}).items():
            code = header_codes.get(header)
            if code:
                attrs[code] = value
        if attrs:
            crud.set_vehicle_attrs(db, veh, attrs)

        modes = list(meta.get("modes") or [])
        orders = list(meta.get("orders") or [])
        for it in vehicles_in_items[vname]:
            if it.get("mode"):
                modes.append(it["mode"])
            if it.get("folder_order"):
                orders.append({"discipline": "操稳", "order_no": it["folder_order"]})

        mode_ids[veh.id] = _ensure_vehicle_modes(db, veh, modes)
        order_ids[veh.id] = _ensure_test_orders(db, veh, orders)

    db.flush()

    # ── 数据集 / 指标 / 曲线 ──
    n_ds = n_ind = n_cur = n_pts = 0
    done = 0
    total = len(items)
    for it in items:
        veh = veh_cache[it["vehicle"]]
        cond = cond_cache[it["condition"]]
        type_obj = type_cache[it.get("dataset_type") or DEFAULT_DATASET_TYPE]
        mode_name = it.get("mode")
        mode_id = (mode_ids.get(veh.id) or {}).get(mode_name) if mode_name else None

        order_key = ("操稳", it["folder_order"]) if it.get("folder_order") else None
        test_order_id = (order_ids.get(veh.id) or {}).get(order_key) if order_key else None

        if append:
            # 覆盖语义：同一 slot 先删旧数据（Curve / MetricValue 由 FK 级联删除）
            stmt = delete(Dataset).where(
                Dataset.vehicle_id == veh.id,
                Dataset.condition_id == cond.id,
                Dataset.dataset_type_id == type_obj.id,
                Dataset.name == it["condition"],
            )
            if mode_id is None:
                stmt = stmt.where(Dataset.mode_id.is_(None))
            else:
                stmt = stmt.where(Dataset.mode_id == mode_id)
            db.execute(stmt)
            db.flush()

        if it["kind"] == "payload":
            db.add(Dataset(
                vehicle_id=veh.id, mode_id=mode_id,
                test_order_id=test_order_id,
                condition_id=cond.id, dataset_type_id=type_obj.id,
                name=it["condition"], exp_no="1", is_average=False,
                sort_order=0, payload=it["payload"],
            ))
            n_ds += 1
        else:
            exp_nos = sorted(it["experiments"].keys(), key=exp_sort_key)
            for order, exp_no in enumerate(exp_nos):
                avg = is_average_key(exp_no)
                blob = it["experiments"][exp_no]
                ds = Dataset(
                    vehicle_id=veh.id, mode_id=mode_id,
                    test_order_id=test_order_id,
                    condition_id=cond.id, dataset_type_id=type_obj.id,
                    name=it["condition"],
                    exp_no="平均" if avg else exp_no,
                    is_average=avg,
                    sort_order=999 if avg else order,
                )
                db.add(ds)
                db.flush()
                n_ds += 1

                for rk, val in (blob.get("params") or {}).items():
                    if isinstance(val, dict):
                        val = val.get("value")
                    _seq, nm, unit = parse_raw_key(str(rk))
                    fval = _parse_float(val)
                    tval = None if fval is not None else (
                        None if val is None else str(val)
                    )
                    db.add(MetricValue(
                        dataset_id=ds.id, name=nm, unit=unit, raw_key=str(rk),
                        value=fval, text_value=tval, category=cond.category,
                    ))
                    n_ind += 1

                for group, cdict in (blob.get("curves") or {}).items():
                    for corder, (ckey, cval) in enumerate((cdict or {}).items()):
                        ser = cval.get("curve") or []
                        npts = len(ser[0].get("x", [])) if ser else 0
                        _s, cnm, _u = parse_raw_key(str(ckey))
                        db.add(Curve(
                            dataset_id=ds.id, group=group, name=str(ckey),
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
        "datasets": n_ds,
        "experiments": n_ds,
        "indicators": n_ind,
        "curves": n_cur,
        "points": n_pts,
        "vehicles": len(veh_cache),
        "conditions": len(cond_cache),
        "modes": sum(len(v) for v in mode_ids.values()),
        "dataset_types": len(type_cache),
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
