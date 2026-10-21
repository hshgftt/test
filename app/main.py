"""
FastAPI application – vehicle 操稳 test data management.

Run:
    cd vehicle-test-db
    python -m app.main
    # or: uvicorn app.main:app --reload --port 8000

Swagger UI:  http://localhost:8000/docs
ReDoc:       http://localhost:8000/redoc

Python 3.8 compatible (``typing.List/Optional`` only).
"""
from __future__ import annotations

import json
import os
import sys
import time

# Allow `python app/main.py` (not just `python -m app.main` / `uvicorn`):
# put the project root on sys.path before importing the `app` package below.
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from typing import Any, Dict, List, Optional, Sequence, Set

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from sqlalchemy.orm import Session
from starlette.responses import StreamingResponse

from app import crud, importer
from app.config import IS_SQLITE, describe
from app.database import SessionLocal, get_db
from app.models import Vehicle
from app.schemas import ConditionOut
from app.timing import ENABLED as TIMING_ON, Timer, fmt_size


def _json_dumps(obj: Any) -> bytes:
    """Fast JSON encoding — orjson when available, stdlib otherwise."""
    try:
        import orjson  # type: ignore
        return orjson.dumps(obj)
    except Exception:  # pragma: no cover - orjson is optional
        return json.dumps(
            obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")


def json_response(obj: Any, timer: Optional[Timer] = None) -> Response:
    """Return raw JSON bypassing pydantic validation.

    Curve payloads can be tens of MB; running them through a response_model
    would validate every single float.  The data is plain JSON already
    (it comes straight out of a JSON column), so we serialize directly.

    ``timer`` 传入时，序列化耗时计入 ``serialize`` 分段。
    """
    if timer is None:
        content = _json_dumps(obj)
    else:
        with timer.stage("serialize"):
            content = _json_dumps(obj)
        timer.mark(resp=fmt_size(len(content)))
    return Response(content=content, media_type="application/json")


app = FastAPI(
    title="车辆操稳试验数据管理 API",
    description="管理车辆操稳工况的指标与曲线（支持 200 车 × 20 工况 × 10万点曲线）",
    version="3.0.0",
)

# ── CORS: allow the Vue dev server (and any local origin) ──
#   expose_headers 必须列出自定义响应头，否则跨域时浏览器会把它屏蔽掉
#   （CORS 规范默认只暴露 6 个 simple response header），
#   前端就读不到 X-Server-Time-Ms，耗时拆分里「服务端」那一格永远是 —。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # dev only; tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Server-Time-Ms", "Server-Timing"],
)

# ── gzip: numeric JSON shrinks ~3×.
#    Level 9 (Starlette 默认) 压 2.5MB 要 ~0.9s，反而比不压慢 10 倍；
#    用 level 1 只要 ~0.1s。GZIP=0 可完全关闭（本机调试推荐）。 ──
if os.getenv("GZIP", "1").strip().lower() not in ("0", "false", "no"):
    _gz_kwargs = {"minimum_size": int(os.getenv("GZIP_MIN", "10000"))}
    try:
        app.add_middleware(
            GZipMiddleware,
            compresslevel=int(os.getenv("GZIP_LEVEL", "1")),
            **_gz_kwargs,
        )
    except TypeError:                     # 老版本 Starlette 没有 compresslevel
        app.add_middleware(GZipMiddleware, **_gz_kwargs)


# ── 单次对比的负载上限（防 OOM） ────────────────────────────────────────
#
#  为什么**不是**「车辆数上限」：那个尺度是错的。实测（稳态回转，7 条
#  1 万点曲线）：
#
#      50 台车 × 全部实验 × 全部曲线  → 357 MB / 11.5s   ← 危险
#      50 台车 × 平均    × 全部曲线  → 119 MB /  4.2s   ← 尚可
#      50 台车 × 平均    × 1 条曲线  →  18 MB /  0.6s   ← 轻松
#     200 台车 × 平均    × 1 条曲线  →  34 MB           ← 轻松
#
#  也就是说 200 台车可以很轻、50 台车也可以很重。按车辆数卡会同时
#  误伤前者、放过后者。所以按**估算的采样点总数**卡，并且 meta_only
#  不受限制（它只返回元信息，实测 205 KB）。
#
#  服务端序列化的内存放大系数实测 ~8x（ORM 对象 + dict + json.dumps
#  字符串 + gzip 缓冲），5000 万个数字 ≈ 340 MB JSON ≈ 2.7 GB RSS。
MAX_COMPARE_NUMBERS = int(os.getenv("MAX_COMPARE_NUMBERS", "50000000"))


def _parse_vehicle_ids(raw: str) -> List[int]:
    try:
        ids = [int(x) for x in raw.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(
            status_code=400, detail="vehicle_ids must be comma-separated integers"
        )
    if not ids:
        raise HTTPException(status_code=400, detail="vehicle_ids is empty")
    # 去重但保持顺序 —— 重复 id 会让同一台车画两遍、白付一倍内存
    seen: set = set()
    uniq: List[int] = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            uniq.append(i)
    return uniq


# ── shared query parsing helpers ────────────────────────────────────────

def _split_csv(value: Optional[str]) -> Optional[List[str]]:
    if value is None:
        return None
    items = [v.strip() for v in value.split(",") if v.strip()]
    return items or None


def _exp_scope_of(value: Optional[str]) -> Optional[str]:
    """实验口径：``avg_first`` = 平均优先（无则第一次），其余/空 = 全部。"""
    if value and value.strip().lower() in ("avg_first", "avg", "average"):
        return "avg_first"
    return None


def _check_compare_scale(
    db: Session,
    ids: Sequence[int],
    condition_id: int,
    curve_names: Optional[Set[str]],
    exp_scope: Optional[str],
) -> None:
    """负载准入：估算的采样点总数超限就 400，并给出可操作的收敛建议。"""
    est = crud.estimate_compare_numbers(
        db, len(ids), condition_id,
        curve_names=curve_names, exp_scope=exp_scope,
    )
    if est <= MAX_COMPARE_NUMBERS:
        return
    raise HTTPException(
        status_code=400,
        detail=(
            "响应过大：约 %s 万个采样点，超过上限 %s 万。请任选其一收敛："
            "① 加 curves= 只取要画的曲线（最有效，1 条曲线能省一个数量级）；"
            "② 加 exp_scope=avg_first 只取平均实验（省 ~2/3）；"
            "③ 先用 meta_only=1 拿元信息再按需取数据；"
            "④ 减少车辆数；⑤ 调大环境变量 MAX_COMPARE_NUMBERS。"
            % (round(est / 10000), round(MAX_COMPARE_NUMBERS / 10000))
        ),
    )


# ── 请求耗时埋点 ────────────────────────────────────────────────────────
#
#  每个请求打一行 stderr 日志，并把 `Server-Timing` 响应头带回前端，
#  这样浏览器 Network 面板 / 前端代码都能读到真实的**服务端**耗时
#  （浏览器里的 "Time" 还含网络传输和 JSON 解析，不是一码事）。
#
#  端到端链路：
#      db + serialize  ← 本中间件测的服务端部分
#      + 网络传输 + 前端 JSON.parse + Vue 响应式赋值 + ECharts 渲染  ← 前端测
#
#  TIMING=0 可整体关闭。

@app.middleware("http")
async def timing_middleware(request, call_next):
    t = Timer(request.url.path)
    with t.stage("server"):
        response = await call_next(request)

    # ── 流式响应特殊处理 ──
    #   StreamingResponse 的响应头在**第一条数据生成之前**就发出去了，
    #   这里测到的只是「路由函数返回」的耗时（≈0），写进 X-Server-Time-Ms
    #   会变成「服务端只花了 0.5ms」这种误导。真实的生成耗时在流末尾的
    #   done 行里（字段 ms），前端从那里读。
    if isinstance(response, StreamingResponse):
        t.finish(stream=True)
        return response

    response.headers["Server-Timing"] = t.summary()
    # 自定义头便于前端直接读（Server-Timing 解析起来麻烦）
    if TIMING_ON:
        response.headers["X-Server-Time-Ms"] = "%.1f" % (t.elapsed * 1000)
    else:
        response.headers["X-Server-Time-Ms"] = "0"
    # 中间件这一行是「请求总时长」，含 FastAPI 路由 + 依赖注入开销
    t.finish()
    return response


# ── API 1: directory tree ───────────────────────────────────────────────

@app.get("/api/tree", summary="生成目录树", tags=["目录树"])
def get_tree(
    platform: Optional[str] = Query(None, description="按平台名过滤"),
    db: Session = Depends(get_db),
) -> dict:
    """平台 → 品牌 → 车型 的目录树。

    返回格式::

        {"BEV平台": {"极光": [{"id": 1, "name": "极光EV-1"}, …], …}, …}
    """
    return crud.get_tree(db, platform_name=platform)


@app.get("/api/vehicles", summary="车辆扁平列表（含平台/品牌）", tags=["目录树"])
def list_vehicles(
    limit: int = Query(0, description="0 = 全部"),
    db: Session = Depends(get_db),
):
    """前端选择器用：每台车带 platform / brand / drive_type。"""
    rows = crud.get_vehicles_brief(db)
    return rows if not limit else rows[:limit]


# ── API 2: one vehicle × condition ──────────────────────────────────────

@app.get(
    "/api/vehicles/{vehicle_id}/conditions/{condition_id}/data",
    summary="返回指定车+工况的指标与曲线",
    tags=["数据查询"],
)
def get_vehicle_condition_data(
    vehicle_id: int,
    condition_id: int,
    exp: Optional[str] = Query(
        None, description="只取指定实验号，逗号分隔，如 '1,2' 或 '平均'"
    ),
    curves: Optional[str] = Query(
        None, description="只取指定曲线名，逗号分隔（不传=全部）"
    ),
    exp_scope: Optional[str] = Query(
        None, description="avg_first = 每台车取「平均」（无则第一次）"
    ),
    meta_only: bool = Query(
        False, description="true = 只返回曲线元信息，不含 series 数据"
    ),
    db: Session = Depends(get_db),
):
    """返回每台车该工况下所有实验（含 '平均'）的
    ``params`` / ``curves_cal`` / ``curves_duiqi``（曲线为全分辨率，不降采样）。"""
    t = Timer("vehicle_cond")
    with t.stage("db"):
        data = crud.get_vehicle_condition_data(
            db, vehicle_id, condition_id,
            exp_nos=_split_csv(exp),
            curve_names=set(_split_csv(curves)) if curves else None,
            meta_only=meta_only,
            exp_scope=_exp_scope_of(exp_scope),
        )
    if data is None:
        t.finish(status=404)
        raise HTTPException(status_code=404, detail="Vehicle or condition not found")
    t.finish(exps=len(data.get("experiments") or []))
    return json_response(data, t)


# ── API 2b: multi-vehicle comparison (page 1) ───────────────────────────

@app.get("/api/compare", summary="多车对比（页面1）", tags=["数据查询"])
def compare_vehicles(
    vehicle_ids: str = Query(..., description="逗号分隔的车辆ID，如 1,2,3"),
    condition_id: int = Query(..., description="工况ID"),
    exp: Optional[str] = Query(
        None, description="只取指定实验号，如 '平均'（强烈建议，可省 3/4 流量）"
    ),
    curves: Optional[str] = Query(None, description="只取指定曲线名，逗号分隔"),
    exp_scope: Optional[str] = Query(
        None,
        description="avg_first = 每台车只取「平均」（没有平均则取第一次）。"
                    "前端默认视图只需要这一次实验，能省 ~2/3 流量。",
    ),
    meta_only: bool = Query(
        False,
        description="true = 只返回车辆列表 + 指标 + 曲线元信息（不含 series）。"
                    "用于「先看菜单再点菜」：稳态回转 14 条曲线时 328MB → 0.16MB。",
    ),
    db: Session = Depends(get_db),
):
    """一次拿多台车在同一工况下的完整数据，用于对比画图与指标表格。

    10 万点工况下每台车每实验约 7 MB，所以请用 ``exp`` / ``curves`` /
    ``exp_scope`` 收敛，或先用 ``meta_only=1`` 拿元信息再按需取数据。

    准入检查按**估算的采样点总数**（不是车辆数，见 ``MAX_COMPARE_NUMBERS``），
    超了返回 400 并给出收敛建议；``meta_only=1`` 不受此限制。
    """
    ids = _parse_vehicle_ids(vehicle_ids)
    picks = set(_split_csv(curves)) if curves else None
    scope = _exp_scope_of(exp_scope)
    if not meta_only:
        _check_compare_scale(db, ids, condition_id, picks, scope)

    t = Timer("compare")
    with t.stage("db"):
        data = crud.get_compare(
            db, ids, condition_id,
            exp_nos=_split_csv(exp),
            curve_names=picks,
            meta_only=meta_only,
            exp_scope=scope,
        )
    if data is None:
        t.finish(status=404)
        raise HTTPException(status_code=404, detail="Condition not found")
    t.finish(vehicles=len(data.get("vehicles") or []), meta_only=meta_only)
    return json_response(data, t)


# ── API 2c: same payload, streamed as NDJSON ────────────────────────────

@app.get(
    "/api/compare/stream",
    summary="多车对比（NDJSON 流式，推荐）",
    tags=["数据查询"],
)
def compare_stream(
    vehicle_ids: str = Query(..., description="逗号分隔的车辆ID，如 1,2,3"),
    condition_id: int = Query(..., description="工况ID"),
    exp: Optional[str] = Query(None, description="只取指定实验号，如 '平均'"),
    curves: Optional[str] = Query(
        None, description="只取指定曲线名，逗号分隔（强烈建议传，见下）"
    ),
    exp_scope: Optional[str] = Query(
        None,
        description="avg_first = 每台车只取「平均」（没有平均则取第一次），省 ~2/3",
    ),
    db: Session = Depends(get_db),
):
    """和 ``/api/compare`` 返回**同样的数据**，但按 NDJSON 逐行流出。

    每行一个 JSON 对象::

        {"type":"meta",  "condition_id":…, "vehicles":[…], "curve_meta":{…}}
        {"type":"curve", "vehicle_id":…, "experiment_id":…, "group":"cal",
                         "curve":{…含 series…}}
        {"type":"done",  "curves":1400, "points":9600000, "ms":8421.3}

    **为什么值得用**：``/api/compare`` 必须先把整个响应组装成一个 dict、
    再 ``json.dumps`` 成一个几百 MB 的字符串（实测内存放大 ~8×：ORM 对象 +
    dict + JSON 字符串 + gzip 缓冲）。这里序列化完一条曲线就交出去，
    服务端峰值内存与「选了多少条曲线」无关。前端也不用 ``JSON.parse``
    一个几百 MB 的字符串，而是逐行 parse 小对象 —— 而且**第一行 meta
    到手就能渲染指标表和曲线选择区**，不用等最后一条曲线。

    ``meta`` 行里的曲线是**元信息**（不含 series），所以 ``curve_meta``
    里拿不到 ``series_names``（数据种类存在 series 里）。

    客户端按 ``Content-Type: application/x-ndjson`` 识别，按 ``\\n`` 切行。
    正常结束的标志是收到 ``done`` 行；中途出错会收到 ``error`` 行
    （那时 HTTP 200 已经发出去了，改不了状态码）。

    准入检查与 ``/api/compare`` 共用（``MAX_COMPARE_NUMBERS``），超了返回
    400 —— 那时还没开始流，所以是干净的 JSON 错误响应，不是 error 行。
    """
    ids = _parse_vehicle_ids(vehicle_ids)
    picks = set(_split_csv(curves)) if curves else None
    scope = _exp_scope_of(exp_scope)
    _check_compare_scale(db, ids, condition_id, picks, scope)
    exp_nos = _split_csv(exp)

    def gen():
        # 自己开一个 session：请求作用域的那个要等响应彻底发完才关闭，
        # 这里能保证流一结束（包括客户端中途断开）就把连接还给池子。
        s = SessionLocal()
        t0 = time.perf_counter()
        try:
            for obj in crud.iter_compare_stream(
                s, ids, condition_id,
                exp_nos=exp_nos, curve_names=picks, exp_scope=scope,
            ):
                if obj.get("type") == "done":
                    # 流式响应没法用响应头报耗时（头早就发出去了），
                    # 把服务端生成耗时放进 done 行。
                    obj["ms"] = round((time.perf_counter() - t0) * 1000, 1)
                yield _json_dumps(obj) + b"\n"
        except Exception as exc:                  # noqa: BLE001
            # 200 已经发出去了，改不了状态码 —— 只能把错误当成一行数据发过去，
            # 让前端能区分「正常结束」和「加载失败」。
            yield _json_dumps({
                "type": "error",
                "detail": "%s: %s" % (type(exc).__name__, exc),
            }) + b"\n"
        finally:
            s.close()

    return StreamingResponse(
        gen(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store"},
    )


# ── API 2d: 上传数据文件并导入 ──────────────────────────────────────────
#
#  为什么需要它：`scripts/import_data.py` 只能读**服务器本机**的目录，而数据是
#  算法侧在别的机器上产出的。让使用者把文件夹直接拖进浏览器是最省事的路径 ——
#  不用共享盘、不用先手工拷到服务器再敲命令。
#
#  实现上有两个容易踩的点：
#
#  1. **相对路径必须单独传**。浏览器的 multipart 只发 `filename`（就是文件名），
#     `webkitRelativePath`（`车型/工况.json`）**不会**进请求体。所以前端把路径
#     列表当成一个普通 form 字段（`paths`，JSON 数组）一起发过来。
#  2. **不能先把所有文件读完再解析**。一份曲线数据几十 MB，全部攒下来再解析
#     等于「原始文本 + 解析结果」同时驻留。这里逐个读、解析完立刻丢掉原始字节
#     （见 `importer.ItemBuilder`），峰值只跟解析后的数据有关。
#
#  解析与入库规则（平均实验的识别、raw_key 的拆法、CSV 的关联键）与命令行
#  **共用** `app/importer.py`，所以不会出现「命令行导进去和页面导进去不一样」。

#  上传体积闸门。曲线是成串的浮点数，几百 MB 很常见；超了直接拒，别把 RSS 顶爆。
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "512"))


def _parse_paths_field(paths: str, files: List[UploadFile]) -> List[str]:
    """`paths` 字段 → 与 files 对齐的路径列表。

    正常是 JSON 数组；解析不出来就按换行切；实在没有就退化成用 filename
    （那样车型名会丢，只在单文件夹上传时可用）。
    """
    rel: List[str] = []
    raw = (paths or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                rel = [str(x) for x in parsed]
        except Exception:                                   # noqa: BLE001
            rel = [p.strip() for p in raw.replace("\r", "\n").split("\n") if p.strip()]
    if len(rel) != len(files):
        rel = [(f.filename or "") for f in files]
    return rel


@app.post("/api/upload", summary="上传数据文件并导入", tags=["数据导入"])
def upload_data(
    files: List[UploadFile] = File(..., description="数据文件，可多选"),
    paths: str = Form(
        "", description="与 files 一一对应的相对路径 JSON 数组（'车型/工况.json'）"
    ),
    mode: str = Form(
        "replace", description="replace = 先清空再导入；append = 按「车 × 工况」覆盖"
    ),
    dry_run: bool = Form(False, description="true = 只解析预览，不写库"),
    exts: Optional[str] = Form(None, description="只认指定后缀，逗号分隔"),
):
    """接收浏览器上传的数据文件夹，解析后导入。

    请求是 ``multipart/form-data``：

    * ``files``   — 数据文件（``.json`` / ``.msgpack`` / ``.msg`` / ``.mpack``），
      以及可选的 ``vehicles.csv`` / ``conditions.csv``
    * ``paths``   — 与 ``files`` 顺序一致的相对路径，例如
      ``["极光EV-1/角阶跃.json", "vehicles.csv"]``
    * ``mode``    — ``replace``（默认，先清空）/ ``append``
    * ``dry_run`` — ``true`` 时只返回解析摘要，一个字都不写库

    返回::

        {
          "ok": true,
          "dry_run": false,
          "mode": "replace",
          "received": {"files": 12, "bytes": 34567890},
          "summary": {"vehicles": 3, "conditions": 5, "experiments": 42,
                      "indicators": 900, "curves": 210, "points": 1234567, …},
          "imported": {"seconds": 8.4, "vehicles": 3, …} | null,
          "errors":  [{"file": "…", "error": "…"}],   # 解析失败的（跳过，不中断）
          "skipped": ["README.txt"],                  # 后缀不认识的
          "curve_meta_fixed": 0                       # 补算 point_count 的条数
        }

    单份文件解析失败**不会**中断整次导入，会出现在 ``errors`` 里。
    体积超过 ``MAX_UPLOAD_MB``（默认 512）返回 413。
    """
    if not files:
        raise HTTPException(status_code=400, detail="没有收到任何文件")

    mode = (mode or "replace").strip().lower()
    if mode not in ("replace", "append"):
        raise HTTPException(status_code=400, detail="mode 只能是 replace 或 append")

    exts_tuple = importer.READABLE_EXTS
    if exts:
        exts_tuple = tuple(
            e.strip().lower() if e.strip().startswith(".") else "." + e.strip().lower()
            for e in exts.split(",") if e.strip()
        ) or importer.READABLE_EXTS

    rels = importer.prepare_paths(_parse_paths_field(paths, files))

    t = Timer("upload")
    limit = MAX_UPLOAD_MB * 1024 * 1024
    total_bytes = 0
    builder = importer.ItemBuilder(exts_tuple)

    # 同步读 UploadFile 的底层文件对象：这个端点是 `def`，FastAPI 会放到
    # 线程池里跑，所以阻塞读不会卡住事件循环。
    with t.stage("parse"):
        for rel, f in zip(rels, files):
            f.file.seek(0)
            raw = f.file.read()
            total_bytes += len(raw)
            if total_bytes > limit:
                raise HTTPException(
                    status_code=413,
                    detail="上传体积超过上限 %d MB（当前已读到 %d MB）。"
                           "可调大环境变量 MAX_UPLOAD_MB，或分批上传。"
                           % (MAX_UPLOAD_MB, round(total_bytes / 1048576)),
                )
            builder.add(rel, raw)
            del raw

    meta = builder.result()
    items = meta["items"]
    if not items:
        raise HTTPException(
            status_code=400,
            detail="没有解析出任何可用数据。请确认目录结构是 <车型>/<工况>.<后缀>；"
                   + ("已跳过 %d 个文件。" % len(meta["skipped"]) if meta["skipped"] else "")
                   + ("解析失败 %d 个。" % len(meta["errors"]) if meta["errors"] else ""),
        )

    summary = importer.summarize(items, meta["vmeta"])
    resp: Dict[str, Any] = {
        "ok": True,
        "dry_run": bool(dry_run),
        "mode": mode,
        "received": {"files": len(files), "bytes": total_bytes},
        "summary": summary,
        "vehicles_csv": meta["vehicles_csv"],
        "conditions_csv": meta["conditions_csv"],
        "imported": None,
        "curve_meta_fixed": 0,
        "errors": meta["errors"][:50],
        "error_count": len(meta["errors"]),
        "skipped": meta["skipped"][:50],
        "skipped_count": len(meta["skipped"]),
    }
    if dry_run:
        t.finish(files=len(files), dry_run=True)
        return json_response(resp, t)

    if mode == "replace":
        with t.stage("reset"):
            importer.reset_schema()
    else:
        importer.ensure_schema()

    db = SessionLocal()
    try:
        with t.stage("import"):
            stats = importer.import_items(
                db, items, meta["vmeta"], meta["cmeta"], append=(mode == "append"),
            )
        resp["imported"] = stats
        resp["curve_meta_fixed"] = importer.recalc_curve_meta()
    finally:
        db.close()

    t.finish(files=len(files), mode=mode, vehicles=stats["vehicles"])
    return json_response(resp, t)


# ── API 3: all indicators nested by hierarchy (page 2) ─────────────────
@app.get("/api/indicators/all", summary="返回所有指标(按层级嵌套)", tags=["数据查询"])
def get_all_indicators(
    platform: Optional[str] = Query(None, description="按平台过滤"),
    condition: Optional[str] = Query(None, description="按工况名过滤"),
    indicator: Optional[str] = Query(None, description="按指标名过滤"),
    average: bool = Query(
        False, description="true=只统计『平均』实验（实际数据里平均很少，默认全部）"
    ),
    flat: bool = Query(False, description="true=分页扁平结构（大数据量必用）"),
    limit: int = Query(200, ge=1, le=5000, description="flat 模式每页行数"),
    offset: int = Query(0, ge=0, description="flat 模式偏移量"),
    db: Session = Depends(get_db),
):
    """返回所有车（或选定平台所有车）的所有指标。

    * ``flat=false`` → ``platform → brand → vehicle → condition → exp → {raw_key: …}``
    * ``flat=true``  → ``{total, offset, limit, rows:[{platform,brand,vehicle,
      condition,experiment_no,is_average,indicators:{…}}]}``

    ``average=true`` 时只保留每台车每个工况的『平均』那一次实验。
    每台车每个工况的实验次数并不固定（可能 1 次、3 次、或者没有平均），
    所以这里只提供「仅平均 / 全部」两种口径。

    200 车 × 20 工况下嵌套结构约 35 万个指标值，页面 2 请用 ``flat=true`` 分页。
    """
    t = Timer("indicators_all")
    with t.stage("db"):
        if flat:
            data = crud.get_all_indicators_flat(
                db, platform_name=platform, condition_name=condition,
                indicator_name=indicator, average_only=average,
                limit=limit, offset=offset,
            )
        else:
            data = crud.get_all_indicators(
                db, platform_name=platform, condition_name=condition,
                indicator_name=indicator, average_only=average,
            )
    t.finish(mode="flat" if flat else "nested",
             rows=(data.get("total") if flat else len(data or [])))
    return json_response(data, t)


@app.get("/api/indicators/count", summary="指标表格总行数（分页用）", tags=["数据查询"])
def count_indicator_rows(
    platform: Optional[str] = Query(None),
    condition: Optional[str] = Query(None),
    average: bool = Query(False, description="true=只统计『平均』实验"),
    db: Session = Depends(get_db),
):
    t = Timer("indicators_count")
    with t.stage("db"):
        total = crud.count_indicator_rows(db, platform, condition, average)
    t.finish(total=total)
    return json_response({"total": total}, t)


# ── supporting endpoints ────────────────────────────────────────────────

@app.get("/api/conditions", summary="所有工况列表", tags=["辅助"],
         response_model=List[ConditionOut])
def list_conditions(db: Session = Depends(get_db)):
    return crud.get_conditions(db)


@app.get(
    "/api/vehicles/{vehicle_id}/conditions",
    summary="某车型可用的工况列表", tags=["辅助"],
    response_model=List[ConditionOut],
)
def list_vehicle_conditions(vehicle_id: int, db: Session = Depends(get_db)):
    conds = crud.get_vehicle_conditions(db, vehicle_id)
    if not conds and not db.get(Vehicle, vehicle_id):
        raise HTTPException(status_code=404, detail="Vehicle not found")
    return conds


@app.get("/api/curve-names", summary="某工况下的曲线名列表", tags=["辅助"])
def list_curve_names(condition_id: int, db: Session = Depends(get_db)):
    return crud.get_curve_names(db, condition_id)


@app.get("/api/health", summary="健康检查", tags=["辅助"])
def health():
    return {"status": "ok", "database": describe(), "sqlite": IS_SQLITE}


if __name__ == "__main__":
    import uvicorn

    # Run with:  python -m app.main      (recommended)
    #        or  python app/main.py
    #        or  uvicorn app.main:app --reload --port 8000
    #
    # Host / port / reload are overridable via env:  HOST, PORT, RELOAD
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # reload spawns a child process; make sure it can import the `app` package
    os.environ["PYTHONPATH"] = _root + os.pathsep + os.environ.get("PYTHONPATH", "")

    uvicorn.run(
        "app.main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=os.getenv("RELOAD", "1").strip().lower() not in ("0", "false", "no"),
    )
