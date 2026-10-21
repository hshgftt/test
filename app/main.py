"""FastAPI application — 元数据驱动的车辆试验数据平台.

核心接口
========

    配置    GET  /api/meta
    导航树  GET  /api/tree
    车辆    GET/POST/PUT/DELETE /api/vehicles[/{id}]
    数据    GET  /api/segments/{vehicle_id}/conditions/{condition_id}/data
            GET  /api/compare
            GET  /api/compare/stream       (NDJSON 流式)
    统计    GET  /api/indicators/all
    导入    POST /api/upload

``segment`` = ``(vehicle_id, mode_id)``，是导航树叶子、对比、取数的基本单位。
所有层级 / 车辆字段 / 数据格式都由数据库里的配置决定，这里不含任何
「平台 → 品牌 → 车」的硬编码。

Python 3.8 compatible (``typing.List/Optional`` only).
"""
from __future__ import annotations

import json
import os
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
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
from app.schemas import (
    AttrDefIn,
    AttrDefUpdate,
    ConditionOut,
    TreeProfileOut,
    VehicleIn,
    VehicleUpdateIn,
)
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
    """大响应直接序列化，绕过 pydantic 校验（曲线可能是几十 MB）。"""
    if timer is None:
        content = _json_dumps(obj)
    else:
        with timer.stage("serialize"):
            content = _json_dumps(obj)
        timer.mark(resp=fmt_size(len(content)))
    return Response(content=content, media_type="application/json")


app = FastAPI(
    title="车辆试验数据管理 API",
    description="元数据驱动：车辆字段、导航树层级、数据格式均可配置扩展",
    version="4.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # dev only; tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Server-Time-Ms", "Server-Timing"],
)

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


# ── 单次对比的负载上限（防 OOM）：按估算的采样点总数卡 ──────────────────
MAX_COMPARE_NUMBERS = int(os.getenv("MAX_COMPARE_NUMBERS", "50000000"))


# ═══════════════════════════════════════════════════════════════════════
# 参数解析
# ═══════════════════════════════════════════════════════════════════════

def _parse_segments(raw: str) -> List[Tuple[int, Optional[int]]]:
    """``"1:2,1:3,5"`` → ``[(1, 2), (1, 3), (5, None)]``（去重保序）。"""
    out: List[Tuple[int, Optional[int]]] = []
    seen: Set[Tuple[int, Optional[int]]] = set()
    for item in (raw or "").split(","):
        part = item.strip()
        if not part:
            continue
        try:
            if ":" in part:
                vid_s, mid_s = part.split(":", 1)
                vid = int(vid_s)
                mid = int(mid_s) if mid_s.strip() else None
            else:
                vid, mid = int(part), None
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="segments 格式应为 vehicle_id:mode_id，逗号分隔，如 1:2,1:3,5",
            )
        seg = (vid, mid)
        if seg not in seen:
            seen.add(seg)
            out.append(seg)
    if not out:
        raise HTTPException(status_code=400, detail="segments 不能为空")
    return out


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


def _filters_from_request(request: Request) -> Dict[str, str]:
    """``?f_brand=星河&f_platform=BEV平台`` → ``{"brand": "星河", ...}``。

    任意车辆字段都能作为过滤条件，不需要为每个字段加查询参数。
    """
    out: Dict[str, str] = {}
    for key, value in request.query_params.items():
        if key.startswith("f_") and value != "":
            out[key[2:]] = value
    return out


def _check_compare_scale(
    db: Session,
    segments: Sequence[Tuple[int, Optional[int]]],
    condition_id: int,
    curve_names: Optional[Set[str]],
    exp_scope: Optional[str],
    dataset_type: Optional[str],
) -> None:
    est = crud.estimate_compare_numbers(
        db, len(segments), condition_id,
        curve_names=curve_names, exp_scope=exp_scope,
        dataset_type=dataset_type,
    )
    if est <= MAX_COMPARE_NUMBERS:
        return
    raise HTTPException(
        status_code=400,
        detail=(
            "响应过大：约 %s 万个采样点，超过上限 %s 万。请任选其一收敛："
            "① 加 curves= 只取要画的曲线（最有效）；"
            "② 加 exp_scope=avg_first 只取平均实验（省 ~2/3）；"
            "③ 先用 meta_only=1 拿元信息再按需取数据；"
            "④ 减少勾选的分析单元；⑤ 调大环境变量 MAX_COMPARE_NUMBERS。"
            % (round(est / 10000), round(MAX_COMPARE_NUMBERS / 10000))
        ),
    )


@app.middleware("http")
async def timing_middleware(request, call_next):
    t = Timer(request.url.path)
    with t.stage("server"):
        response = await call_next(request)

    if isinstance(response, StreamingResponse):
        t.finish(stream=True)
        return response

    response.headers["Server-Timing"] = t.summary()
    response.headers["X-Server-Time-Ms"] = (
        "%.1f" % (t.elapsed * 1000) if TIMING_ON else "0"
    )
    t.finish()
    return response


# ═══════════════════════════════════════════════════════════════════════
# 配置层
# ═══════════════════════════════════════════════════════════════════════

@app.get("/api/meta", summary="所有前端需要的配置", tags=["配置"])
def get_meta(db: Session = Depends(get_db)):
    """字段注册表、维度字典、导航树配置、数据格式、工况、模式建议。"""
    t = Timer("meta")
    with t.stage("db"):
        data = crud.get_meta(db)
    t.finish()
    return json_response(data, t)


@app.get("/api/attrs", summary="车辆字段注册表", tags=["配置"])
def list_attrs(active_only: bool = Query(False), db: Session = Depends(get_db)):
    return json_response([
        {
            "id": a.id, "code": a.code, "name": a.name,
            "value_type": a.value_type, "dim_code": a.dim_code,
            "unit": a.unit, "required": a.required,
            "show_in_list": a.show_in_list, "options": a.options,
            "sort_order": a.sort_order, "is_active": a.is_active,
        }
        for a in crud.get_attr_defs(db, active_only=active_only)
    ])


@app.post("/api/attrs", summary="新增车辆字段（不改表结构）", tags=["配置"])
def create_attr(payload: AttrDefIn, db: Session = Depends(get_db)):
    try:
        attr = crud.create_attr_def(db, payload.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return json_response({
        "id": attr.id, "code": attr.code, "name": attr.name,
        "value_type": attr.value_type, "dim_code": attr.dim_code,
        "unit": attr.unit, "required": attr.required,
        "show_in_list": attr.show_in_list, "options": attr.options,
        "sort_order": attr.sort_order, "is_active": attr.is_active,
    })


@app.put("/api/attrs/{attr_id}", summary="修改车辆字段", tags=["配置"])
def update_attr(
    attr_id: int, payload: AttrDefUpdate, db: Session = Depends(get_db),
):
    attr = crud.update_attr_def(
        db, attr_id, payload.model_dump(exclude_unset=True),
    )
    if attr is None:
        raise HTTPException(status_code=404, detail="字段不存在")
    return json_response({
        "id": attr.id, "code": attr.code, "name": attr.name,
        "value_type": attr.value_type, "dim_code": attr.dim_code,
        "unit": attr.unit, "required": attr.required,
        "show_in_list": attr.show_in_list, "options": attr.options,
        "sort_order": attr.sort_order, "is_active": attr.is_active,
    })


@app.post("/api/tree-profiles", summary="新增导航树配置", tags=["配置"])
def create_tree_profile(payload: Dict[str, Any], db: Session = Depends(get_db)):
    try:
        data = crud.create_tree_profile(db, payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return json_response(data)


@app.put("/api/tree-profiles/{code}", summary="修改导航树配置", tags=["配置"])
def update_tree_profile(
    code: str, payload: Dict[str, Any], db: Session = Depends(get_db),
):
    try:
        data = crud.update_tree_profile(db, code, payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if data is None:
        raise HTTPException(status_code=404, detail="导航树配置不存在")
    return json_response(data)


# ═══════════════════════════════════════════════════════════════════════
# 导航树
# ═══════════════════════════════════════════════════════════════════════

@app.get("/api/tree", summary="按配置投影出任意层级的导航树", tags=["导航树"])
def get_tree(
    request: Request,
    profile: Optional[str] = Query(None, description="tree_profiles.code，不传用默认"),
    db: Session = Depends(get_db),
):
    """层级数量 / 顺序 / 来源全部由 ``tree_profiles.levels`` 决定。

    叶子节点 ``kind='segment'``，带 ``segment={vehicle_id, mode_id, ...}``。
    任意字段过滤用 ``?f_<attr_code>=值``。
    """
    t = Timer("tree")
    with t.stage("db"):
        data = crud.get_nav_tree(
            db, profile_code=profile, filters=_filters_from_request(request),
        )
    t.finish(nodes=len(data.get("nodes") or []))
    return json_response(data, t)


# ═══════════════════════════════════════════════════════════════════════
# 车辆
# ═══════════════════════════════════════════════════════════════════════

@app.get("/api/vehicles", summary="车辆扁平列表（动态属性）", tags=["车辆"])
def list_vehicles(
    request: Request,
    limit: int = Query(0, description="0 = 全部"),
    db: Session = Depends(get_db),
):
    rows = crud.list_vehicles_brief(db, filters=_filters_from_request(request))
    return json_response(rows if not limit else rows[:limit])


@app.get("/api/vehicles/{vehicle_id}", summary="车辆详情", tags=["车辆"])
def get_vehicle(vehicle_id: int, db: Session = Depends(get_db)):
    data = crud.get_vehicle_detail(db, vehicle_id)
    if data is None:
        raise HTTPException(status_code=404, detail="车辆不存在")
    return json_response(data)


@app.post("/api/vehicles", summary="新增车辆", tags=["车辆"])
def create_vehicle(payload: VehicleIn, db: Session = Depends(get_db)):
    try:
        data = crud.create_vehicle(db, payload.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return json_response(data)


@app.put("/api/vehicles/{vehicle_id}", summary="修改车辆", tags=["车辆"])
def update_vehicle(
    vehicle_id: int, payload: VehicleUpdateIn, db: Session = Depends(get_db),
):
    try:
        data = crud.update_vehicle(
            db, vehicle_id, payload.model_dump(exclude_unset=True),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if data is None:
        raise HTTPException(status_code=404, detail="车辆不存在")
    return json_response(data)


@app.delete("/api/vehicles/{vehicle_id}", summary="删除车辆", tags=["车辆"])
def delete_vehicle(vehicle_id: int, db: Session = Depends(get_db)):
    if not crud.delete_vehicle(db, vehicle_id):
        raise HTTPException(status_code=404, detail="车辆不存在")
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════════
# 工况 / 曲线名
# ═══════════════════════════════════════════════════════════════════════

@app.get("/api/conditions", summary="工况列表", tags=["辅助"],
         response_model=List[ConditionOut])
def list_conditions(
    vehicle_id: Optional[int] = Query(None),
    mode_id: Optional[int] = Query(None),
    dataset_type: Optional[str] = Query(None, description="dataset_types.code"),
    db: Session = Depends(get_db),
):
    return crud.get_conditions(
        db, vehicle_id=vehicle_id, mode_id=mode_id, dataset_type=dataset_type,
    )


@app.get("/api/curve-names", summary="某工况下的曲线名", tags=["辅助"])
def list_curve_names(
    condition_id: int,
    dataset_type: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    return crud.get_curve_names(db, condition_id, dataset_type=dataset_type)


@app.get("/api/indicator-names", summary="某工况下的指标名", tags=["辅助"])
def list_indicator_names(
    condition_id: Optional[int] = Query(None),
    dataset_type: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    return crud.get_indicator_names(db, condition_id, dataset_type=dataset_type)


# ═══════════════════════════════════════════════════════════════════════
# 数据查询
# ═══════════════════════════════════════════════════════════════════════

@app.get(
    "/api/segments/{vehicle_id}/conditions/{condition_id}/data",
    summary="单个分析单元（车 × 模式）× 工况的数据",
    tags=["数据查询"],
)
def get_segment_data(
    vehicle_id: int,
    condition_id: int,
    mode_id: Optional[int] = Query(None, description="不传 = 该车无模式的数据"),
    dataset_type: Optional[str] = Query(None),
    exp: Optional[str] = Query(None, description="只取指定实验号，逗号分隔"),
    curves: Optional[str] = Query(None, description="只取指定曲线名，逗号分隔"),
    exp_scope: Optional[str] = Query(None, description="avg_first = 平均优先"),
    meta_only: bool = Query(False),
    db: Session = Depends(get_db),
):
    t = Timer("segment_data")
    with t.stage("db"):
        data = crud.get_segment_condition_data(
            db, vehicle_id, condition_id,
            mode_id=mode_id,
            dataset_type=dataset_type,
            exp_nos=_split_csv(exp),
            curve_names=set(_split_csv(curves)) if curves else None,
            meta_only=meta_only,
            exp_scope=_exp_scope_of(exp_scope),
        )
    if data is None:
        t.finish(status=404)
        raise HTTPException(status_code=404, detail="车辆或工况不存在")
    t.finish(exps=len(data.get("experiments") or []))
    return json_response(data, t)


@app.get("/api/compare", summary="多分析单元对比", tags=["数据查询"])
def compare_segments(
    segments: str = Query(..., description="vehicle_id:mode_id，逗号分隔，如 1:2,1:3,5"),
    condition_id: int = Query(..., description="工况 ID"),
    dataset_type: Optional[str] = Query(None),
    exp: Optional[str] = Query(None),
    curves: Optional[str] = Query(None),
    exp_scope: Optional[str] = Query(None),
    meta_only: bool = Query(False),
    db: Session = Depends(get_db),
):
    """一次拿多个 (车 × 模式) 在同一工况下的数据，用于对比画图与指标表格。"""
    segs = _parse_segments(segments)
    picks = set(_split_csv(curves)) if curves else None
    scope = _exp_scope_of(exp_scope)
    if not meta_only:
        _check_compare_scale(db, segs, condition_id, picks, scope, dataset_type)

    t = Timer("compare")
    with t.stage("db"):
        data = crud.get_compare(
            db, segs, condition_id,
            dataset_type=dataset_type,
            exp_nos=_split_csv(exp),
            curve_names=picks,
            meta_only=meta_only,
            exp_scope=scope,
        )
    if data is None:
        t.finish(status=404)
        raise HTTPException(status_code=404, detail="工况不存在")
    t.finish(segments=len(data.get("vehicles") or []), meta_only=meta_only)
    return json_response(data, t)


@app.get(
    "/api/compare/stream",
    summary="多分析单元对比（NDJSON 流式，推荐）",
    tags=["数据查询"],
)
def compare_stream(
    segments: str = Query(..., description="vehicle_id:mode_id，逗号分隔"),
    condition_id: int = Query(..., description="工况 ID"),
    dataset_type: Optional[str] = Query(None),
    exp: Optional[str] = Query(None),
    curves: Optional[str] = Query(None, description="只取指定曲线名（强烈建议传）"),
    exp_scope: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    segs = _parse_segments(segments)
    picks = set(_split_csv(curves)) if curves else None
    scope = _exp_scope_of(exp_scope)
    _check_compare_scale(db, segs, condition_id, picks, scope, dataset_type)
    exp_nos = _split_csv(exp)

    def gen():
        s = SessionLocal()
        t0 = time.perf_counter()
        try:
            for obj in crud.iter_compare_stream(
                s, segs, condition_id,
                dataset_type=dataset_type,
                exp_nos=exp_nos, curve_names=picks, exp_scope=scope,
            ):
                if obj.get("type") == "done":
                    obj["ms"] = round((time.perf_counter() - t0) * 1000, 1)
                yield _json_dumps(obj) + b"\n"
        except Exception as exc:                  # noqa: BLE001
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


# ═══════════════════════════════════════════════════════════════════════
# 指标统计
# ═══════════════════════════════════════════════════════════════════════

@app.get("/api/indicators/all", summary="指标统计（分页扁平）", tags=["统计"])
def get_all_indicators(
    request: Request,
    condition: Optional[str] = Query(None, description="工况名"),
    indicator: Optional[str] = Query(None, description="指标名"),
    average: bool = Query(False, description="true = 只统计平均实验"),
    dataset_type: Optional[str] = Query(None),
    mode_id: Optional[int] = Query(None),
    limit: int = Query(200, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """每行一个「车 × 模式 × 工况 × 实验」，车辆字段是动态属性。

    任意字段过滤用 ``?f_<attr_code>=值``（如 ``f_brand=星河``）。
    """
    t = Timer("indicators_all")
    with t.stage("db"):
        data = crud.get_all_indicators_flat(
            db,
            filters=_filters_from_request(request),
            condition_name=condition,
            indicator_name=indicator,
            average_only=average,
            dataset_type=dataset_type,
            mode_id=mode_id,
            limit=limit,
            offset=offset,
        )
    t.finish(rows=len(data.get("rows") or []), total=data.get("total"))
    return json_response(data, t)


@app.get("/api/indicators/count", summary="指标表格总行数", tags=["统计"])
def count_indicator_rows(
    request: Request,
    condition: Optional[str] = Query(None),
    indicator: Optional[str] = Query(None),
    average: bool = Query(False),
    dataset_type: Optional[str] = Query(None),
    mode_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
):
    data = crud.get_all_indicators_flat(
        db,
        filters=_filters_from_request(request),
        condition_name=condition,
        indicator_name=indicator,
        average_only=average,
        dataset_type=dataset_type,
        mode_id=mode_id,
        limit=1,
        offset=0,
    )
    return {"total": data.get("total", 0)}


# ═══════════════════════════════════════════════════════════════════════
# 上传导入
# ═══════════════════════════════════════════════════════════════════════

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "512"))


def _parse_paths_field(paths: str, files: List[UploadFile]) -> List[str]:
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
        "", description="与 files 一一对应的相对路径 JSON 数组（'车名/模式/工况.json'）"
    ),
    mode: str = Form(
        "replace", description="replace = 先清空再导入；append = 按 slot 覆盖"
    ),
    dry_run: bool = Form(False, description="true = 只解析预览，不写库"),
    exts: Optional[str] = Form(None, description="只认指定后缀，逗号分隔"),
    dataset_type: str = Form(
        "handling_stability",
        description="本次数据按哪种格式解析（dataset_types.code）",
    ),
):
    """接收浏览器上传的数据文件夹，解析后导入。

    目录结构：``<车名>[/<模式>/...]/<工况>.<后缀>``。
    中间目录都是试验模式（可以多层，按 ``/`` 拼接），不同车辆的模式数量不固定。
    车名文件夹可带单号：``极光EV-01(CS-2024-001)``。

    ``vehicles.csv`` 的列按字段 ``code`` 或 ``name`` 匹配，匹配不到的列会
    自动注册成新的车辆字段，不需要提前建表/加字段。
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
    builder = importer.ItemBuilder(exts_tuple, dataset_type=dataset_type)

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
            detail="没有解析出任何可用数据。请确认目录结构是 "
                   "<车名>/[<模式>/]<工况>.<后缀>。"
                   + ("已跳过 %d 个文件。" % len(meta["skipped"]) if meta["skipped"] else "")
                   + ("解析失败 %d 个。" % len(meta["errors"]) if meta["errors"] else ""),
        )

    summary = importer.summarize(items, meta["vmeta"])
    resp: Dict[str, Any] = {
        "ok": True,
        "dry_run": bool(dry_run),
        "mode": mode,
        "dataset_type": dataset_type,
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


@app.get("/api/health", summary="健康检查", tags=["辅助"])
def health():
    return {"status": "ok", "database": describe(), "sqlite": IS_SQLITE}


if __name__ == "__main__":
    import uvicorn

    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.environ["PYTHONPATH"] = _root + os.pathsep + os.environ.get("PYTHONPATH", "")

    uvicorn.run(
        "app.main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=os.getenv("RELOAD", "1").strip().lower() not in ("0", "false", "no"),
    )
