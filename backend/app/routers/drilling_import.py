"""钻探日志整批导入接口。

路由口径（和业务约定对应）：

* ``POST   /sessions``              建立断点续传会话
* ``POST   /sessions/{id}/rows``    按偏移量追加上传行，断线从首个未确认行继续
* ``GET    /sessions/{id}``         查询已确认收到的行数（续传定位用）
* ``POST   /sessions/{id}/preview`` 导入预览（事务内试算后回滚，不留数据）
* ``POST   /sessions/{id}/commit``  整批提交（解析、校验、落库、待办生成同一事务）
* ``POST   /preview``               跳过会话，直接对全文做整批预览
* ``POST   /commit``                跳过会话，直接整批提交
* ``GET    /batches``               批次留痕（成功/退回均在案，按指纹幂等）
* ``GET    /quarantine``            缺孔号隔离清单
* ``POST   /quarantine/{id}/resolve`` 隔离行补孔号后整批迁入台账
* ``GET    /deviations``            偏离待办清单
* ``POST   /deviations/{id}/status``  现场确认后关闭待办
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.schemas import (
    ImportCommitPayload,
    ImportSessionCreate,
    ImportSessionRows,
    QuarantineResolvePayload,
    TodoStatusPayload,
)
from app.services.drilling_import import (
    BatchRejected,
    DrillingImportService,
    fingerprint_of,
)

router = APIRouter(prefix="/api/drilling_import", tags=["钻探日志导入"])

service = DrillingImportService()


def _reject_semantic(error: ValueError | KeyError | IndexError, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail=str(error))


def _rejected_detail(error: BatchRejected) -> dict:
    return {
        "kind": "batch_rejected",
        "message": f"整批退回：{len(error.errors)} 行不通过，台账未做任何改动",
        "errors": error.errors,
    }


@router.post("/sessions")
def create_session(payload: ImportSessionCreate) -> dict:
    """建立断点续传会话；只登记文件名，不落任何业务数据。"""
    return service.create_session(payload.file_name)


@router.post("/sessions/{session_id}/rows")
def append_rows(session_id: str, payload: ImportSessionRows) -> dict:
    """按偏移量追加行；重复区间幂等忽略，缺口区间直接拒绝并指明续传位置。"""
    try:
        return service.append_rows(session_id, payload.offset, payload.rows)
    except KeyError as error:
        raise _reject_semantic(error, status=404)
    except IndexError as error:
        raise _reject_semantic(error, status=409)


@router.get("/sessions/{session_id}")
def session_status(session_id: str) -> dict:
    """返回已确认收到的行数，前端断线重连后据此定位续传起点。"""
    try:
        return service.session_status(session_id)
    except KeyError as error:
        raise _reject_semantic(error, status=404)


@router.post("/sessions/{session_id}/preview")
def preview_session(session_id: str) -> dict:
    """对已上传的完整文件做整批预览：任意一行不通过都会带逐行错误整体退回。"""
    try:
        return service.preview_session(session_id)
    except KeyError as error:
        raise _reject_semantic(error, status=404)
    except BatchRejected as error:
        raise HTTPException(status_code=422, detail={
            "kind": "batch_rejected",
            "message": f"整批退回：{len(error.errors)} 行不通过，台账未做任何改动",
            "errors": error.errors,
        })


@router.post("/sessions/{session_id}/commit", status_code=200)
def commit_session(session_id: str) -> dict:
    """整批提交；返回 422 表示整批退回，业务表一行未动（不会留下半批）。"""
    try:
        result = service.commit_session(session_id)
    except KeyError as error:
        raise _reject_semantic(error, status=404)
    except BatchRejected as error:
        raise HTTPException(status_code=422, detail=_rejected_detail(error))
    if not result.get("ok", True):
        raise HTTPException(status_code=422, detail={
            "kind": "batch_rejected",
            "message": result.get("message", "整批退回，台账未做任何改动"),
            "errors": result.get("错误明细", []),
            "文件指纹": result.get("文件指纹"),
        })
    return result


@router.post("/preview")
def preview_content(payload: ImportCommitPayload) -> dict:
    """直接送全文预览，便于小文件一步到位；内部同样是事务试算后回滚。"""
    fingerprint = payload.fingerprint or fingerprint_of(payload.content)
    try:
        return service.preview_content(payload.file_name, payload.content, fingerprint)
    except ValueError as error:
        raise _reject_semantic(error)
    except BatchRejected as error:
        raise HTTPException(status_code=422, detail={
            "kind": "batch_rejected",
            "message": f"整批退回：{len(error.errors)} 行不通过，台账未做任何改动",
            "errors": error.errors,
        })


@router.post("/commit", status_code=200)
def commit_content(payload: ImportCommitPayload) -> dict:
    """直接送全文整批提交；文件指纹幂等，重复文件返回首次结果不落第二遍。"""
    fingerprint = payload.fingerprint or fingerprint_of(payload.content)
    try:
        result = service.commit_payload(payload.file_name, payload.content, fingerprint)
    except ValueError as error:
        raise _reject_semantic(error)
    except BatchRejected as error:
        raise HTTPException(status_code=422, detail=_rejected_detail(error))
    if not result.get("ok", True):
        raise HTTPException(status_code=422, detail={
            "kind": "batch_rejected",
            "message": result.get("message", "整批退回，台账未做任何改动"),
            "errors": result.get("错误明细", []),
            "文件指纹": result.get("文件指纹"),
        })
    return result


@router.get("/batches")
def list_batches() -> dict:
    """批次留痕：成功的批次与退回记录都在，方便现场按指纹核对重报。"""
    items = service.list_batches()
    return {"total": len(items), "items": items}


@router.get("/quarantine")
def list_quarantine(include_resolved: bool = False) -> dict:
    """缺孔号隔离清单：默认只看待补号行，补录完成的可按需一起看。"""
    items = service.list_quarantine(only_pending=not include_resolved)
    return {"total": len(items), "items": items}


@router.post("/quarantine/{quarantine_id}/resolve")
def resolve_quarantine(quarantine_id: int, payload: QuarantineResolvePayload) -> dict:
    """现场补孔号后，把隔离行按现场编号整批迁入台账并重算三处结论。"""
    try:
        return service.resolve_quarantine(quarantine_id, payload.borehole_code)
    except KeyError as error:
        raise _reject_semantic(error, status=404)
    except ValueError as error:
        raise _reject_semantic(error)


@router.get("/deviations")
def list_deviations(status_filter: str | None = None) -> dict:
    """偏离待办清单：与日志台账、钻孔详情共用同一份库内结论。"""
    items = service.list_deviations(status_filter)
    return {"total": len(items), "items": items}


@router.post("/deviations/{todo_id}/status")
def set_deviation_status(todo_id: int, payload: TodoStatusPayload) -> dict:
    """现场核对后关闭待办（或误关后重开）；重算结论不会复活已关闭的待办。"""
    try:
        return service.set_deviation_status(todo_id, payload.status)
    except KeyError as error:
        raise _reject_semantic(error, status=404)
    except ValueError as error:
        raise _reject_semantic(error)
