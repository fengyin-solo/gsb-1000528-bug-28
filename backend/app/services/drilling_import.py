"""钻探日志整批导入：文件指纹幂等 + 解析/校验/落库/待办生成整批事务。

口径（与现场约定一致，列表/预览/落库三处共用同一份规划结果）：

* 整批事务：任何一行不通过，整批退回；解析、校验、台账落库、缺孔号隔离、
  库内结论回写、偏离待办生成要么一起成功，要么一起回滚，不留半批记录。
* 文件按内容 SHA-256 指纹幂等：同一文件重复提交直接返回首次结果，不重复落库。
* 冲突规则：来件与既有日志冲突时以现场终孔记录为准（终孔行覆盖同号台账）；
  历史班次记录按原上报基准保留（同号班次行跳过，不覆盖）。
* 缺孔号不阻断整批：缺孔号的行先进隔离表，其余记录按现场编号迁移入台账；
  隔离行补齐孔号后可放行，放行同样走整批事务。
* 断点续传：从未成功行继续；已经确认进台账的行只做跳过，绝不覆盖已确认深度。
"""
from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from app.store import store

MODULE_LOG = "drilling_log"
MODULE_BOREHOLE = "borehole"
TABLE_BATCH = "_import_batches"
TABLE_QUARANTINE = "_drilling_quarantine"
TABLE_CONCLUSION = "_drilling_conclusions"
TABLE_DEVIATION = "_drilling_deviations"

# 整批事务涉及的全部表：任一步失败，这六张表一起回滚。
TRANSACTION_TABLES = (
    MODULE_LOG,
    MODULE_BOREHOLE,
    TABLE_QUARANTINE,
    TABLE_CONCLUSION,
    TABLE_DEVIATION,
    TABLE_BATCH,
)

LOG_FIELDS = ["日志编号", "钻孔编号", "钻进深度", "回次进尺", "岩层描述", "水位深度", "钻探人员", "日志状态"]
FINAL_MARKERS = {"终孔", "终孔记录", "现场终孔", "是", "true", "1", "yes", "y"}
DEVIATION_TOLERANCE = 0.05  # 终孔深度相对设计孔深偏离超过 5% 进偏离待办

ACTION_INSERT = "新增入台账"
ACTION_FINAL_OVERRIDE = "终孔覆盖"
ACTION_KEEP_BASELINE = "保留原上报基准"
ACTION_RESUME_SKIP = "断点跳过已确认行"
ACTION_QUARANTINE = "隔离待补孔号"
ACTION_TODO_MISSING = "生成未登记孔号待办"
ACTION_TODO_DEVIATION = "生成孔深偏离待办"


class BatchRejected(Exception):
    """整批校验未通过：携带逐行原因，调用方整批退回、不写任何表。"""

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        self.errors = errors
        super().__init__(f"整批退回：{len(errors)} 行未通过校验")


@dataclass
class RawRow:
    line: int
    values: dict[str, str]


@dataclass
class PlannedRow:
    line: int
    log_no: str
    hole_no: str
    depth: float
    is_final: bool
    values: dict[str, str]
    action: str = ""
    reason: str = ""
    quarantine: bool = False


@dataclass
class Plan:
    rows: list[PlannedRow] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    todos: list[dict[str, Any]] = field(default_factory=list)


# ------------------------------------------------------------------ 解析

def file_fingerprint(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def parse_file(filename: str, content: str) -> tuple[list[RawRow], list[dict[str, Any]]]:
    """解析 CSV/JSON 来件为逐行字典；解析期问题作为整批错误返回，不抛半批数据。"""
    errors: list[dict[str, Any]] = []
    text = content.lstrip("﻿")
    name = (filename or "").lower()
    rows: list[RawRow] = []
    if name.endswith(".json") or text.lstrip().startswith(("{", "[")):
        import json

        try:
            payload = json.loads(text)
        except ValueError as exc:
            return [], [{"line": 0, "日志编号": "", "reason": f"JSON 无法解析：{exc}"}]
        if isinstance(payload, dict):
            payload = payload.get("rows") or payload.get("items") or []
        if not isinstance(payload, list):
            return [], [{"line": 0, "日志编号": "", "reason": "JSON 顶层必须是记录数组（或含 rows/items 的对象）"}]
        for index, item in enumerate(payload, start=1):
            if not isinstance(item, dict):
                errors.append({"line": index, "日志编号": "", "reason": "该行不是对象，无法映射字段"})
                continue
            rows.append(RawRow(index, {str(k): ("" if v is None else str(v)).strip() for k, v in item.items()}))
        return rows, errors

    # CSV：优先用嗅探到的方言，嗅探失败退回逗号。
    try:
        sample = text[:4096]
        dialect: Any = csv.excel
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            pass
        reader = csv.reader(io.StringIO(text), dialect)
        header = next(reader, None)
        if not header:
            return [], [{"line": 0, "日志编号": "", "reason": "CSV 没有表头，至少需要「日志编号、钻进深度」两列"}]
        header = [cell.strip() for cell in header]
        for line, cells in enumerate(reader, start=2):
            if not any(cell.strip() for cell in cells):
                continue  # 空行跳过，不算记录也不算错误
            values = {header[i]: cells[i].strip() if i < len(cells) else "" for i in range(len(header))}
            rows.append(RawRow(line, values))
    except csv.Error as exc:
        errors.append({"line": 0, "日志编号": "", "reason": f"CSV 无法解析：{exc}"})
    return rows, errors


# ------------------------------------------------------------------ 数值/工具

def _to_depth(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        depth = float(text)
    except ValueError:
        return None
    return depth


def _is_final(values: dict[str, str]) -> bool:
    for key in ("记录类型", "是否终孔", "终孔标记"):
        if str(values.get(key, "")).strip().lower() in FINAL_MARKERS:
            return True
    return str(values.get("日志状态", "")).strip() in ("已终孔", "终孔")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ------------------------------------------------------------------ 规划（纯计算）

class _Target:
    """规划/落库的统一状态视图：正式提交用实时表，预览用深拷贝快照。"""

    def rows(self, table: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    def next_id(self, table: str) -> int:
        return max((int(row.get("id", 0)) for row in self.rows(table)), default=0) + 1


class _LiveTarget(_Target):
    def rows(self, table: str) -> list[dict[str, Any]]:
        return store.rows(table)


class _SnapshotTarget(_Target):
    def __init__(self) -> None:
        from copy import deepcopy

        self._tables = {name: deepcopy(store.rows(name)) for name in TRANSACTION_TABLES}

    def rows(self, table: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(table, [])


def _confirmed_hole_depths(target: _Target) -> dict[str, float]:
    """台账中各孔已确认的最大深度——断点续传与深度回退校验都以此为底线。"""
    depths: dict[str, float] = {}
    for row in target.rows(MODULE_LOG):
        hole = str(row.get("钻孔编号") or "").strip()
        depth = _to_depth(row.get("钻进深度"))
        if hole and depth is not None:
            depths[hole] = max(depth, depths.get(hole, depth))
    return depths


def _find_log(target: _Target, log_no: str) -> dict[str, Any] | None:
    for row in target.rows(MODULE_LOG):
        if str(row.get("日志编号") or "").strip() == log_no:
            return row
    return None


def build_plan(
    raw_rows: list[RawRow],
    parse_errors: list[dict[str, Any]],
    *,
    fingerprint: str,
    filename: str,
    resume_from: int = 0,
    target: _Target | None = None,
) -> Plan:
    """把来件逐行规划成台账动作；硬错误整批收集，缺孔号只标记隔离。"""
    target = target or _LiveTarget()
    plan = Plan(errors=list(parse_errors))
    seen_logs: set[str] = set()
    hole_max = _confirmed_hole_depths(target)

    for raw in raw_rows:
        values = {str(k).strip(): str(v).strip() for k, v in raw.values.items() if str(k).strip()}
        log_no = values.get("日志编号", "")
        hole_no = values.get("钻孔编号", "")
        depth = _to_depth(values.get("钻进深度"))
        prefix = {"line": raw.line, "日志编号": log_no}

        if not log_no:
            plan.errors.append({**prefix, "reason": "缺少必填「日志编号」"})
        elif log_no in seen_logs:
            plan.errors.append({**prefix, "reason": f"日志编号 {log_no} 在本批内重复"})
        if depth is None:
            plan.errors.append({**prefix, "reason": "「钻进深度」缺失或不是数值"})
        elif depth < 0:
            plan.errors.append({**prefix, "reason": f"钻进深度 {depth:g} 不能为负"})

        # 缺孔号：软处理，进入隔离，不作废整批。
        if not hole_no:
            plan.rows.append(PlannedRow(raw.line, log_no, "", depth if depth is not None and depth >= 0 else 0.0,
                                        _is_final(values), values,
                                        action=ACTION_QUARANTINE, reason="缺钻孔编号，先隔离待补", quarantine=True))
            if log_no:
                seen_logs.add(log_no)
            continue

        if not log_no or depth is None or depth < 0:
            if log_no:
                seen_logs.add(log_no)
            continue
        seen_logs.add(log_no)

        existing = _find_log(target, log_no)
        is_final = _is_final(values)
        # 深度回退校验只针对「新的非终孔班次记录」：既有日志走冲突策略（终孔
        # 覆盖/班次保留），新的现场终孔记录以现场为准允许纠正深度，均不按回退处理。
        if existing is None and not is_final and depth < hole_max.get(hole_no, -1.0):
            plan.errors.append({
                **prefix,
                "reason": f"钻进深度 {depth:g} 低于孔 {hole_no} 已确认深度 {hole_max[hole_no]:g}，不得回退覆盖",
            })
            continue
        if not is_final or existing is not None:
            hole_max[hole_no] = max(depth, hole_max.get(hole_no, depth))

        if raw.line <= resume_from and existing is not None:
            # 中断重连：该行此前已经确认进台账，跳过且绝不改写。
            action, reason = ACTION_RESUME_SKIP, f"第 {raw.line} 行在中断前已确认，按已确认深度保留"
        elif existing is not None and is_final:
            action, reason = ACTION_FINAL_OVERRIDE, "现场终孔记录为准，覆盖同号历史记录"
        elif existing is not None:
            action, reason = ACTION_KEEP_BASELINE, "历史班次按原上报基准保留，来件班次不覆盖"
        else:
            action, reason = ACTION_INSERT, "按现场编号迁入台账"
        plan.rows.append(PlannedRow(raw.line, log_no, hole_no, depth, is_final, values, action=action, reason=reason))

    return plan


# ------------------------------------------------------------------ 落库（事务内）

def _apply_log_row(target: _Target, item: PlannedRow, fingerprint: str) -> dict[str, Any] | None:
    """把规划行写入日志台账，返回被写入/覆盖的台账行；跳过类动作返回 None。"""
    if item.quarantine or item.action in (ACTION_KEEP_BASELINE, ACTION_RESUME_SKIP):
        return None
    rows = target.rows(MODULE_LOG)
    existing = _find_log(target, item.log_no)
    payload = {key: item.values.get(key, "") for key in LOG_FIELDS if key != "日志状态"}
    if item.is_final:
        status, pending = "已审核", False
        payload["日志状态"] = "已终孔"
    else:
        raw_status = item.values.get("日志状态")
        status = raw_status if raw_status in ("待填写", "已填写", "已审核", "退回补充") else "已填写"
        pending = status != "已审核"
        payload["日志状态"] = status
    payload["记录来源"] = "现场终孔" if item.is_final else "班次上报"

    if existing is not None:  # 仅终孔覆盖会走到这里
        existing.update(payload)
        existing["status"] = status
        existing["pending"] = pending
        existing["abnormal"] = False
        existing["终孔标记"] = item.is_final
        existing["来源批次指纹"] = fingerprint
        return existing

    entry: dict[str, Any] = {"id": target.next_id(MODULE_LOG)}
    entry.update(payload)
    entry["status"] = status
    entry["pending"] = pending
    entry["abnormal"] = False
    entry["终孔标记"] = item.is_final
    entry["来源批次指纹"] = fingerprint
    rows.append(entry)
    return entry


def _upsert_conclusion(target: _Target, item: PlannedRow, fingerprint: str) -> dict[str, Any]:
    """库内结论按钻孔编号唯一落一份：日志台账、钻孔详情、偏离待办都读它。"""
    rows = target.rows(TABLE_CONCLUSION)
    conclusion = next((row for row in rows if row.get("钻孔编号") == item.hole_no), None)
    if conclusion is None:
        conclusion = {"id": target.next_id(TABLE_CONCLUSION)}
        rows.append(conclusion)
    conclusion.update({
        "钻孔编号": item.hole_no,
        "日志编号": item.log_no,
        "终孔深度": item.depth,
        "终孔日期": item.values.get("终孔日期") or item.values.get("日期") or _now()[:10],
        "钻探人员": item.values.get("钻探人员", ""),
        "岩层描述": item.values.get("岩层描述", ""),
        "来源批次指纹": fingerprint,
        "更新时间": _now(),
    })
    return conclusion


def _find_borehole(target: _Target, hole_no: str) -> dict[str, Any] | None:
    for row in target.rows(MODULE_BOREHOLE):
        if str(row.get("钻孔编号") or "").strip() == hole_no:
            return row
    return None


def _sync_deviations(target: _Target, item: PlannedRow) -> list[dict[str, Any]]:
    """依据同一份库内结论生成/核销偏离待办；返回本批新增的待办。"""
    created: list[dict[str, Any]] = []
    deviations = target.rows(TABLE_DEVIATION)
    conclusion = next(
        (row for row in target.rows(TABLE_CONCLUSION) if row.get("钻孔编号") == item.hole_no),
        None,
    )
    borehole = _find_borehole(target, item.hole_no)

    def open_todo(kind: str, detail: str, rate: float | None) -> dict[str, Any]:
        # 同孔同类型只保留一条未处理待办（幂等）。
        active = next(
            (row for row in deviations
             if row.get("钻孔编号") == item.hole_no and row.get("类型") == kind and row.get("状态") == "待处理"),
            None,
        )
        if active is not None:
            active.update({
                "终孔深度": conclusion["终孔深度"] if conclusion else item.depth,
                "设计孔深": _to_depth(borehole.get("设计孔深")) if borehole else None,
                "偏离率": rate,
                "说明": detail,
                "更新时间": _now(),
            })
            return active
        todo = {
            "id": target.next_id(TABLE_DEVIATION),
            "钻孔编号": item.hole_no,
            "类型": kind,
            "状态": "待处理",
            "终孔深度": conclusion["终孔深度"] if conclusion else item.depth,
            "设计孔深": _to_depth(borehole.get("设计孔深")) if borehole else None,
            "偏离率": rate,
            "日志编号": item.log_no,
            "说明": detail,
            "pending": True,
            "abnormal": True,
            "创建时间": _now(),
        }
        deviations.append(todo)
        created.append(todo)
        return todo

    if borehole is None:
        open_todo("钻孔编号未登记", f"终孔日志 {item.log_no} 的孔号 {item.hole_no} 在钻孔台账中不存在", None)
        return created

    design = _to_depth(borehole.get("设计孔深"))
    if design and design > 0:
        rate = abs(item.depth - design) / design
        if rate > DEVIATION_TOLERANCE:
            open_todo(
                "孔深偏离超阈值",
                f"终孔深度 {item.depth:g} 与设计孔深 {design:g} 偏离 {rate:.1%}，超过 {DEVIATION_TOLERANCE:.0%} 阈值",
                rate,
            )
        else:
            # 新结论回到阈值内：自动核销该孔未处理的偏离待办。
            for row in deviations:
                if row.get("钻孔编号") == item.hole_no and row.get("类型") == "孔深偏离超阈值" and row.get("状态") == "待处理":
                    row["状态"] = "已核销"
                    row["pending"] = False
                    row["abnormal"] = False
                    row["处理时间"] = _now()
                    row["处理说明"] = f"现场终孔深度 {item.depth:g} 回到允许偏离范围内"
    return created


def apply_plan(target: _Target, plan: Plan, *, fingerprint: str, filename: str,
               register_batch: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """在事务体内执行规划：台账、隔离、结论、待办同批落库，并登记批次指纹。"""
    counters = {"新增": 0, "终孔覆盖": 0, "保留跳过": 0, "断点跳过": 0, "隔离": 0, "待办新增": 0}
    final_holes: set[str] = set()
    applied_logs: list[str] = []
    max_line = 0

    for item in plan.rows:
        max_line = max(max_line, item.line)
        if item.quarantine:
            target.rows(TABLE_QUARANTINE).append({
                "id": target.next_id(TABLE_QUARANTINE),
                "来源批次指纹": fingerprint,
                "来源文件": filename,
                "行号": item.line,
                "values": dict(item.values),
                "原因": item.reason,
                "状态": "待补孔号",
                "pending": True,
                "abnormal": True,
                "隔离时间": _now(),
            })
            counters["隔离"] += 1
            continue

        written = _apply_log_row(target, item, fingerprint)
        if item.action == ACTION_INSERT:
            counters["新增"] += 1
        elif item.action == ACTION_FINAL_OVERRIDE:
            counters["终孔覆盖"] += 1
        elif item.action == ACTION_KEEP_BASELINE:
            counters["保留跳过"] += 1
        elif item.action == ACTION_RESUME_SKIP:
            counters["断点跳过"] += 1
        if written is not None:
            applied_logs.append(item.log_no)
        if item.is_final and item.action != ACTION_RESUME_SKIP:
            final_holes.add(item.hole_no)

    # 结论回写与待办生成：只对本批落库的终孔孔号执行，三处取同一份结论。
    todos_added: list[dict[str, Any]] = []
    for item in plan.rows:
        if item.hole_no in final_holes and item.is_final and not item.quarantine \
                and item.action not in (ACTION_KEEP_BASELINE, ACTION_RESUME_SKIP):
            _upsert_conclusion(target, item, fingerprint)
            todos_added.extend(_sync_deviations(target, item))
    counters["待办新增"] = len(todos_added)

    batch = {
        "指纹": fingerprint,
        "文件名": filename,
        "总行数": len(plan.rows),
        "新增": counters["新增"],
        "终孔覆盖": counters["终孔覆盖"],
        "保留跳过": counters["保留跳过"],
        "断点跳过": counters["断点跳过"],
        "隔离": counters["隔离"],
        "待办新增": counters["待办新增"],
        "已应用日志编号": applied_logs,
        "已确认行号": max_line,
        "提交时间": _now(),
        "状态": "已提交",
    }
    if register_batch is not None:
        record = {"id": target.next_id(TABLE_BATCH), **batch}
        register_batch(record)
    return batch


# ------------------------------------------------------------------ 对外编排

def _row_results(plan: Plan) -> list[dict[str, Any]]:
    return [
        {
            "行号": item.line,
            "日志编号": item.log_no,
            "钻孔编号": item.hole_no or "（缺号隔离）",
            "钻进深度": item.depth,
            "是否终孔": item.is_final,
            "动作": item.action,
            "说明": item.reason,
        }
        for item in plan.rows
    ]


def _todo_projection(target: _Target) -> list[dict[str, Any]]:
    result = []
    conclusions = {row["钻孔编号"]: row for row in target.rows(TABLE_CONCLUSION)}
    for row in target.rows(TABLE_DEVIATION):
        if row.get("状态") != "待处理":
            continue
        conclusion = conclusions.get(row.get("钻孔编号"))
        result.append({
            "钻孔编号": row.get("钻孔编号"),
            "类型": row.get("类型"),
            "终孔深度": conclusion.get("终孔深度") if conclusion else row.get("终孔深度"),
            "设计孔深": row.get("设计孔深"),
            "偏离率": row.get("偏离率"),
            "日志编号": conclusion.get("日志编号") if conclusion else row.get("日志编号"),
            "说明": row.get("说明"),
        })
    return result


def preview_import(filename: str, content: str, *, resume_from: int = 0) -> dict[str, Any]:
    """导入预览：在深拷贝快照上跑与正式提交完全相同的规划，杜绝预览与落库对不上。"""
    fingerprint = file_fingerprint(content)
    existing = find_batch(fingerprint)
    if existing is not None:
        return {"ok": True, "reused": True, "fingerprint": fingerprint, "message": "该文件已成功导入，重复提交直接返回首次结果",
                "summary": _public_batch(existing), "rows": [], "errors": [], "todos": [], "quarantine": []}

    raw_rows, parse_errors = parse_file(filename, content)
    snapshot = _SnapshotTarget()
    plan = build_plan(raw_rows, parse_errors, fingerprint=fingerprint, filename=filename,
                      resume_from=resume_from, target=snapshot)
    if not plan.rows and not raw_rows:
        plan.errors.append({"line": 0, "日志编号": "", "reason": "文件内没有可导入的记录行"})

    response: dict[str, Any] = {
        "ok": not plan.errors,
        "reused": False,
        "fingerprint": fingerprint,
        "filename": filename,
        "总行数": len(raw_rows),
        "rows": _row_results(plan),
        "errors": plan.errors,
        "quarantine": [
            {"行号": item.line, "日志编号": item.log_no, "说明": item.reason, "values": item.values}
            for item in plan.rows if item.quarantine
        ],
        "todos": [],
    }
    if plan.errors:
        response["message"] = f"校验未通过，整批退回：{len(plan.errors)} 行有问题，未写入任何记录"
        return response

    # 只在零错误时才在快照上模拟落库，给出与提交一致的计数与待办预览。
    batch = apply_plan(snapshot, plan, fingerprint=fingerprint, filename=filename)
    response.update({
        "message": "预览通过，确认后整批提交；任一行不通过都会整批退回",
        "summary": {key: batch[key] for key in
                    ("总行数", "新增", "终孔覆盖", "保留跳过", "断点跳过", "隔离", "待办新增")},
        "todos": _todo_projection(snapshot),
    })
    return response


def commit_import(filename: str, content: str, *, resume_from: int = 0) -> dict[str, Any]:
    """正式导入：指纹幂等 + 六表整批事务，失败按快照回滚，绝不留半批记录。"""
    fingerprint = file_fingerprint(content)
    existing = find_batch(fingerprint)
    if existing is not None:
        return {"ok": True, "reused": True, "fingerprint": fingerprint,
                "message": "文件指纹已存在，返回首次导入结果，未重复落库",
                "summary": _public_batch(existing), "rows": [], "errors": [], "todos": [], "quarantine": []}

    raw_rows, parse_errors = parse_file(filename, content)
    target = _LiveTarget()
    plan = build_plan(raw_rows, parse_errors, fingerprint=fingerprint, filename=filename,
                      resume_from=resume_from, target=target)
    if not plan.rows and not raw_rows:
        plan.errors.append({"line": 0, "日志编号": "", "reason": "文件内没有可导入的记录行"})
    if plan.errors:
        # 不进事务、不落任何表，整批退回。
        return {"ok": False, "reused": False, "fingerprint": fingerprint,
                "message": f"校验未通过，整批退回：{len(plan.errors)} 行有问题",
                "rows": _row_results(plan), "errors": plan.errors,
                "todos": [], "quarantine": []}

    with store.transaction(*TRANSACTION_TABLES):
        batch_summary = apply_plan(
            target, plan, fingerprint=fingerprint, filename=filename,
            register_batch=lambda record: store.rows(TABLE_BATCH).append(record),
        )
        record = next(row for row in store.rows(TABLE_BATCH) if row.get("指纹") == fingerprint)

    return {"ok": True, "reused": False, "fingerprint": fingerprint, "batch_id": record["id"],
            "message": "整批导入成功：台账、隔离、库内结论与偏离待办已同批提交",
            "summary": _public_batch(record), "rows": _row_results(plan), "errors": [],
            "todos": _todo_projection(target),
            "resume": {"已确认行号": batch_summary["已确认行号"], "指纹": fingerprint}}


def find_batch(fingerprint: str) -> dict[str, Any] | None:
    return next((row for row in store.rows(TABLE_BATCH) if row.get("指纹") == fingerprint), None)


def _public_batch(record: dict[str, Any]) -> dict[str, Any]:
    return {key: record.get(key) for key in
            ("文件名", "总行数", "新增", "终孔覆盖", "保留跳过", "断点跳过", "隔离", "待办新增", "已确认行号", "提交时间", "状态")}


# ------------------------------------------------------------------ 隔离放行 / 待办核销

def list_quarantine() -> list[dict[str, Any]]:
    return [row for row in store.rows(TABLE_QUARANTINE) if row.get("状态") == "待补孔号"]


def release_quarantine(entry_id: int, hole_no: str) -> dict[str, Any]:
    """隔离行补齐孔号后放行：复用同一套规划与整批事务，不单独开口子写半条。"""
    hole_no = hole_no.strip()
    if not hole_no:
        raise ValueError("补录的钻孔编号不能为空")
    target = _LiveTarget()
    with store.transaction(*TRANSACTION_TABLES):
        entry = next((row for row in target.rows(TABLE_QUARANTINE)
                      if int(row.get("id", 0)) == entry_id and row.get("状态") == "待补孔号"), None)
        if entry is None:
            raise KeyError(f"隔离记录 {entry_id} 不存在或已放行")
        values = dict(entry.get("values") or {})
        values["钻孔编号"] = hole_no
        raw = RawRow(int(entry.get("行号", 0)), values)
        plan = build_plan([raw], [], fingerprint=str(entry.get("来源批次指纹") or ""),
                          filename=str(entry.get("来源文件") or ""), target=target)
        if plan.errors:
            raise BatchRejected(plan.errors)
        # 隔离放行不另起批次登记（批次指纹沿用原批次），只做台账/结论/待办同批落库。
        apply_plan(target, plan, fingerprint=str(entry.get("来源批次指纹") or f"quarantine-{entry_id}"),
                   filename=str(entry.get("来源文件") or "隔离放行"), register_batch=None)
        entry["状态"] = "已放行"
        entry["放行孔号"] = hole_no
        entry["pending"] = False
        entry["abnormal"] = False
        entry["放行时间"] = _now()
    return {"ok": True, "message": f"隔离记录 {entry_id} 已按孔号 {hole_no} 迁入台账"}


def list_deviations(only_open: bool = True) -> list[dict[str, Any]]:
    """偏离待办清单：终孔深度始终现取库内结论，保证三处同一份。"""
    conclusions = {row["钻孔编号"]: row for row in store.rows(TABLE_CONCLUSION)}
    result = []
    for row in sorted(store.rows(TABLE_DEVIATION), key=lambda item: int(item.get("id", 0))):
        if only_open and row.get("状态") != "待处理":
            continue
        conclusion = conclusions.get(row.get("钻孔编号"))
        result.append({
            "id": row.get("id"),
            "钻孔编号": row.get("钻孔编号"),
            "类型": row.get("类型"),
            "状态": row.get("状态"),
            "终孔深度": conclusion.get("终孔深度") if conclusion else row.get("终孔深度"),
            "结论日志编号": conclusion.get("日志编号") if conclusion else row.get("日志编号"),
            "设计孔深": row.get("设计孔深"),
            "偏离率": row.get("偏离率"),
            "说明": row.get("说明"),
            "创建时间": row.get("创建时间"),
        })
    return result


def resolve_deviation(entry_id: int, note: str | None = None) -> dict[str, Any]:
    with store.transaction(TABLE_DEVIATION):
        entry = next((row for row in store.rows(TABLE_DEVIATION) if int(row.get("id", 0)) == entry_id), None)
        if entry is None:
            raise KeyError(f"偏离待办 {entry_id} 不存在")
        if entry.get("状态") != "待处理":
            raise ValueError(f"偏离待办 {entry_id} 已处于{entry.get('状态')}状态，无需重复处理")
        entry["状态"] = "已处理"
        entry["pending"] = False
        entry["abnormal"] = False
        entry["处理时间"] = _now()
        entry["处理说明"] = (note or "").strip() or "现场确认偏离可接受"
    return {"ok": True, "message": f"偏离待办 {entry_id} 已处理"}
