"""钻探日志接口：维护钻探记录，覆盖填写日志、提交审核、退回补充等动作。

另提供整批导入链路：导入预览、确认提交（整批事务、文件指纹幂等）、缺孔号
隔离清单与放行、偏离待办清单与处理。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from app.schemas import (
    ActionResult,
    DeviationResolvePayload,
    EntryPayload,
    ImportPayload,
    PageResult,
    QuarantineReleasePayload,
)
from app.services import drilling_import as imp
from app.services.drilling_log import DrillingLogService, import_stats
from app.services.drilling_import import BatchRejected

router = APIRouter(prefix="/api/drilling_log", tags=["钻探日志"])

service = DrillingLogService()

LIST_FIELDS = ["日志编号", "钻孔编号", "钻进深度", "回次进尺", "岩层描述", "水位深度", "钻探人员", "日志状态"]
STATUSES = ["待填写", "已填写", "已审核", "退回补充"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按日志编号检索"),
    status: str | None = Query(default=None, description="待填写、已填写、已审核、退回补充"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按日志编号与状态过滤钻探日志列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/import/stats")
def import_overview() -> dict:
    """导入看板：批次数、待处理偏离、缺孔号隔离、已确认终孔都从同一结论口径统计。"""
    return import_stats()


@router.post("/import/preview")
def preview_import(payload: ImportPayload) -> dict:
    """导入预览：与正式提交同一条规划管线，预览结果即落库结果，不会再对不上。"""
    return imp.preview_import(payload.filename, payload.content, resume_from=payload.resume_from)


@router.post("/import/commit")
def commit_import(payload: ImportPayload) -> JSONResponse:
    """确认导入：文件指纹幂等，解析/校验/落库/待办整批事务，任一行不过整批退回。

    校验未通过时返回 422，响应体里带逐行原因，且未写入任何记录（无半批数据）。
    """
    result = imp.commit_import(payload.filename, payload.content, resume_from=payload.resume_from)
    if not result.get("ok"):
        return JSONResponse(status_code=422, content=result)
    return JSONResponse(status_code=200, content=result)


@router.get("/quarantine")
def list_quarantine() -> dict:
    """缺孔号隔离清单：这些行进了隔离表，其余同批记录已正常入台账。"""
    items = imp.list_quarantine()
    return {"total": len(items), "items": items}


@router.post("/quarantine/{entry_id}/release")
def release_quarantine(entry_id: int, payload: QuarantineReleasePayload) -> ActionResult:
    """隔离行补录孔号后放行：仍走整批事务，校验不过继续留在隔离表。"""
    try:
        imp.release_quarantine(entry_id, payload.hole_no)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        return ActionResult(ok=False, message=str(exc))
    except BatchRejected as exc:
        return ActionResult(ok=False, message=f"放行被整批退回：{'；'.join(row['reason'] for row in exc.errors)}")
    return ActionResult(ok=True, message=f"隔离记录 {entry_id} 已补录孔号 {payload.hole_no} 迁入台账")


@router.get("/deviations")
def list_deviations(
    open_only: bool = Query(default=True, description="仅返回待处理的偏离待办"),
) -> dict:
    """偏离待办清单：终孔深度现取库内结论，与日志台账、钻孔详情是同一份。"""
    items = imp.list_deviations(only_open=open_only)
    return {"total": len(items), "items": items}


@router.post("/deviations/{entry_id}/resolve")
def resolve_deviation(entry_id: int, payload: DeviationResolvePayload) -> ActionResult:
    """现场确认后核销一条偏离待办。"""
    try:
        imp.resolve_deviation(entry_id, payload.note)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(ok=True, message=f"偏离待办 {entry_id} 已处理")


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条钻探记录明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"钻探记录 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条钻探记录，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="钻探记录已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条钻探记录执行填写日志、提交审核、退回补充；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/export")
def export_entries() -> dict:
    """导出钻探日志清单：返回当前过滤条件下的全量数据，终孔口径同库内结论。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "drilling_log", "total": total, "items": items}
