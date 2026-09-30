"""钻探日志业务规则：状态流转、字段校验与筛选口径都收在这里。

列表/详情回显时统一用库内结论（``_drilling_conclusions``）补齐「终孔深度」等
现场终孔口径：导入落库、钻孔详情、偏离待办三处都从同一份结论取值，不会再出现
列表、导入预览与钻进深度对不上的情况。
"""
from __future__ import annotations

from typing import Any

from app.services import drilling_import as imp
from app.store import store

MODULE = "drilling_log"
REQUIRED_FIELDS = ["日志编号", "钻孔编号", "钻进深度"]
STATUS_ORDER = ["待填写", "已填写", "已审核", "退回补充"]
ACTION_RULES = {"填写日志": "已填写", "提交审核": "已审核", "退回补充": "退回补充"}
NEGATIVE_ACTIONS = []


def with_conclusion(row: dict[str, Any]) -> dict[str, Any]:
    """把同孔库内结论回写到台账行上：列表、详情、导出共用这一份口径。"""
    conclusion = next(
        (item for item in store.rows(imp.TABLE_CONCLUSION)
         if item.get("钻孔编号") == row.get("钻孔编号")),
        None,
    )
    if conclusion is None:
        return row
    enriched = dict(row)
    enriched["终孔深度"] = conclusion.get("终孔深度")
    enriched["结论日志编号"] = conclusion.get("日志编号")
    enriched["终孔日期"] = conclusion.get("终孔日期")
    enriched["终孔结论来源"] = "现场终孔记录"
    return enriched


def import_stats() -> dict[str, int]:
    batches = store.rows(imp.TABLE_BATCH)
    return {
        "导入批次": len(batches),
        "待处理偏离": sum(1 for row in store.rows(imp.TABLE_DEVIATION) if row.get("状态") == "待处理"),
        "缺孔号隔离": sum(1 for row in store.rows(imp.TABLE_QUARANTINE) if row.get("状态") == "待补孔号"),
        "已确认终孔": len(store.rows(imp.TABLE_CONCLUSION)),
    }


class DrillingLogService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("日志编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [with_conclusion(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        return with_conclusion(entry) if entry is not None else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": store.next_id(MODULE)}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"钻探记录 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于钻探日志可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"钻探记录已{action}"
