"""钻孔编录业务规则：状态流转、字段校验与筛选口径都收在这里。

钻孔详情中的终孔口径不自行维护，统一回读库内结论（``_drilling_conclusions``）：
导入现场终孔记录后，日志台账、钻孔详情、偏离待办三处取同一份结论。
"""
from __future__ import annotations

from typing import Any

from app.services import drilling_import as imp
from app.store import store

MODULE = "borehole"
REQUIRED_FIELDS = ["钻孔编号", "勘探区", "孔口坐标"]
STATUS_ORDER = ["待施工", "钻进中", "已终孔", "已封孔", "已废弃"]
ACTION_RULES = {"开始钻进": "钻进中", "登记终孔": "已终孔", "执行封孔": "已封孔"}
NEGATIVE_ACTIONS = []


def _to_depth(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def with_conclusion(row: dict[str, Any]) -> dict[str, Any]:
    """用库内结论回写钻孔详情：终孔深度、结论日志、与设计孔深的偏离率。"""
    enriched = dict(row)
    conclusion = next(
        (item for item in store.rows(imp.TABLE_CONCLUSION)
         if item.get("钻孔编号") == row.get("钻孔编号")),
        None,
    )
    if conclusion is None:
        return enriched
    enriched["终孔深度"] = conclusion.get("终孔深度")
    enriched["结论日志编号"] = conclusion.get("日志编号")
    enriched["终孔日期"] = conclusion.get("终孔日期")
    enriched["终孔结论来源"] = "现场终孔记录"
    design = _to_depth(row.get("设计孔深"))
    final_depth = conclusion.get("终孔深度")
    if design and final_depth is not None:
        enriched["孔深偏离率"] = (float(final_depth) - design) / design
    else:
        enriched["孔深偏离率"] = None
    return enriched


class BoreholeService:
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
            rows = [row for row in rows if keyword in str(row.get("钻孔编号", ""))]
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
            return None, f"钻孔 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于钻孔编录可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"钻孔已{action}"
