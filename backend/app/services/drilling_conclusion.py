"""库内结论引擎：台账、钻孔详情、偏离待办三处共用同一份结论。

口径（与业务约定一一对应）：

1. 现场终孔记录优先：同一钻孔只要有终孔记录，库内结论深度取终孔深度；
2. 历史班次按原上报基准保留：没有终孔记录时，结论取各班次上报深度的最大值，
   班次行自身的上报深度永不被改写；
3. 结论只在一个地方计算（:func:`evaluate`），日志台账、钻孔详情、偏离待办
   都从同一份结论字典取值，杜绝三处对不上；
4. 偏离判定（孔深偏离设计、回次深度异常、未登记钻孔、缺孔号待查）与结论
   回写在同一事务内完成，不会出现结论更新了、待办还是旧的这种半批状态。
"""
from __future__ import annotations

import math
from typing import Any

from app.store import store

DRILLING_LOG_MODULE = "drilling_log"
BOREHOLE_MODULE = "borehole"
DEVIATION_MODULE = "drilling_deviation"
QUARANTINE_MODULE = "drilling_quarantine"

# 孔深与设计值偏差超过该米数才进偏离待办，量测级误差不打扰现场。
DEPTH_TOLERANCE = 0.5

TERMINAL_TYPES = {"终孔", "终孔记录", "现场终孔", "现场终孔记录", "final"}
SHIFT_TYPE = "班次"
TERMINAL_TYPE = "终孔"

SOURCE_TERMINAL = "现场终孔记录"
SOURCE_SHIFT = "历史班次上报"


def to_float(value: Any) -> float | None:
    """把「12.5」「12.50m」这类深度值解析成数字；无法计量（样例占位文本、
    空值）时返回 None，而不是抛异常打断整批。"""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    # 只有文本以数字开头才按深度解析；「钻探日志样例1」这类占位描述不以数字
    # 开头，不能误当成 1m，否则会凭空生成「未登记钻孔」待办。
    if not (text[0].isdigit() or text[0] == "."):
        return None
    digits: list[str] = []
    for char in text:
        if char.isdigit() or char == ".":
            digits.append(char)
        elif digits:
            break
    if not digits or digits == ["."]:
        return None
    try:
        number = float("".join(digits))
    except ValueError:
        return None
    if not math.isfinite(number):
        return None
    return number


def depth_text(value: float | None) -> str | None:
    """深度统一展示口径：整数米不带小数点，其余保留两位。"""
    if value is None:
        return None
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def is_terminal(row: dict[str, Any]) -> bool:
    record_type = str(row.get("记录类型") or "").strip().lower()
    if record_type in {item.lower() for item in TERMINAL_TYPES}:
        return True
    return bool(str(row.get("终孔深度") or "").strip())


def _ordered_shift_rows(log_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    shifts = [row for row in log_rows if not is_terminal(row)]
    return sorted(shifts, key=lambda row: (int(row.get("id", 0)),))


def evaluate() -> dict[str, dict[str, Any]]:
    """计算当前库内全部钻孔的结论。结论字典是三处回写的唯一数据源。"""
    log_rows = store.rows(DRILLING_LOG_MODULE)
    borehole_rows = store.rows(BOREHOLE_MODULE)
    quarantine_rows = store.rows(QUARANTINE_MODULE)

    borehole_by_code = {
        str(row.get("钻孔编号") or "").strip(): row
        for row in borehole_rows
        if str(row.get("钻孔编号") or "").strip()
    }

    grouped: dict[str, list[dict[str, Any]]] = {}
    ignored_placeholder = 0
    for row in log_rows:
        code = str(row.get("钻孔编号") or "").strip()
        if not code:
            continue
        # 深度无法计量的行（历史演示占位文本）不参与结论与偏离计算，
        # 也不应被当成「未登记钻孔」的证据；真实导入的行深度都是可计量米数。
        if to_float(row.get("钻进深度")) is None:
            ignored_placeholder += 1
            continue
        grouped.setdefault(code, []).append(row)

    conclusions: dict[str, dict[str, Any]] = {}
    codes = set(grouped) | set(borehole_by_code)
    for code in sorted(codes):
        rows = grouped.get(code, [])
        borehole = borehole_by_code.get(code)
        terminal_rows = [row for row in rows if is_terminal(row)]
        shift_rows = _ordered_shift_rows(rows)
        terminal_depths = [
            depth for depth in (to_float(row.get("钻进深度")) for row in terminal_rows)
            if depth is not None
        ]
        shift_depths = [
            depth for depth in (to_float(row.get("钻进深度")) for row in shift_rows)
            if depth is not None
        ]

        confirmed_depth: float | None = None
        confirmed_log_id: int | None = None
        if borehole is not None and bool(borehole.get("已确认终孔")):
            confirmed_depth = to_float(borehole.get("终孔深度"))
            for row in terminal_rows:
                row_depth = to_float(row.get("钻进深度"))
                if row_depth is not None and confirmed_depth is not None and abs(
                    row_depth - confirmed_depth
                ) < 1e-9:
                    confirmed_log_id = int(row.get("id", 0))
                    break
        if confirmed_depth is None and terminal_depths:
            confirmed_depth = max(terminal_depths)
            for row in terminal_rows:
                if to_float(row.get("钻进深度")) == confirmed_depth:
                    confirmed_log_id = int(row.get("id", 0))
                    break

        if confirmed_depth is not None:
            conclusion_depth = confirmed_depth
            source = SOURCE_TERMINAL
        elif shift_depths:
            conclusion_depth = max(shift_depths)
            source = SOURCE_SHIFT
        else:
            conclusion_depth = None
            source = None

        design_depth = to_float(borehole.get("设计孔深")) if borehole else None
        deviation = (
            conclusion_depth - design_depth
            if conclusion_depth is not None and design_depth is not None
            else None
        )
        if deviation is None or (not terminal_rows and confirmed_depth is None):
            compare = "待终孔" if design_depth is not None else "暂无设计基准"
        elif abs(deviation) <= DEPTH_TOLERANCE:
            compare = "符合设计"
        else:
            compare = "偏离设计"

        # 回次深度异常：同孔班次上报深度回落，或班次报得比已确认终孔还深。
        footage_issues: list[str] = []
        previous: float | None = None
        previous_log: str | None = None
        for row in shift_rows:
            depth = to_float(row.get("钻进深度"))
            if depth is None:
                continue
            log_no = str(row.get("日志编号") or "")
            if previous is not None and depth + DEPTH_TOLERANCE < previous:
                footage_issues.append(
                    f"班次 {log_no} 深度 {depth_text(depth)}m 低于上一班次 "
                    f"{previous_log} 的 {depth_text(previous)}m"
                )
            if confirmed_depth is not None and depth - DEPTH_TOLERANCE > confirmed_depth:
                footage_issues.append(
                    f"班次 {log_no} 上报 {depth_text(depth)}m 超过已确认终孔深度 "
                    f"{depth_text(confirmed_depth)}m"
                )
            previous, previous_log = depth, log_no

        pending_quarantine = [
            row for row in quarantine_rows if not row.get("钻孔编号") and row.get("状态") != "已补录"
        ]

        conclusions[code] = {
            "钻孔编号": code,
            "库内结论深度": depth_text(conclusion_depth),
            "结论深度值": conclusion_depth,
            "结论来源": source,
            "终孔记录编号": next(
                (str(row.get("日志编号")) for row in terminal_rows
                 if to_float(row.get("钻进深度")) == confirmed_depth),
                None,
            ),
            "终孔记录ID": confirmed_log_id,
            "班次记录数": len(shift_rows),
            "设计孔深": depth_text(design_depth),
            "设计深度值": design_depth,
            "偏差值": depth_text(deviation) if deviation is not None else None,
            "比对结论": compare,
            "是否未登记孔": borehole is None,
            "回次异常": footage_issues,
            "缺孔号隔离数": len(pending_quarantine),
        }
    return conclusions


def _todo_key(kind: str, code: str, extra: str = "") -> str:
    return f"{kind}:{code}:{extra}" if extra else f"{kind}:{code}"


def rebuild(*, fingerprint: str | None = None, batch_no: str | None = None) -> dict[str, Any]:
    """按当前库内数据重算结论并整批回写：日志台账 → 钻孔详情 → 偏离待办。

    必须在 :func:`app.store.Store.transaction` 内调用，三处写入同生共死。
    待办按稳定键做幂等 upsert，现场在清单里关闭过的待办不会因重算复活或丢失。
    """
    conclusions = evaluate()
    log_rows = store.rows(DRILLING_LOG_MODULE)
    borehole_rows = store.rows(BOREHOLE_MODULE)
    deviation_rows = store.rows(DEVIATION_MODULE)
    quarantine_rows = store.rows(QUARANTINE_MODULE)

    prior_by_key = {str(row.get("待办键")): row for row in deviation_rows}
    seen_keys: set[str] = set()
    # 用单子列表包一层，让嵌套函数也能自增待办 id。
    next_todo_id = [max((int(row.get("id", 0)) for row in deviation_rows), default=0) + 1]

    def upsert_todo(
        key: str,
        *,
        code: str,
        kind: str,
        detail: str,
        conclusion: dict[str, Any] | None,
        related_log: str | None = None,
    ) -> None:
        seen_keys.add(key)
        prior = prior_by_key.get(key)
        if prior is not None:
            prior["偏离说明"] = detail
            prior["结论深度"] = conclusion["库内结论深度"] if conclusion else None
            prior["设计深度"] = conclusion["设计孔深"] if conclusion else None
            prior["关联日志编号"] = related_log
            if fingerprint:
                prior["最近指纹"] = fingerprint
            return
        deviation_rows.append({
            "id": next_todo_id[0],
            "待办键": key,
            "钻孔编号": code,
            "偏离类型": kind,
            "偏离说明": detail,
            "结论深度": conclusion["库内结论深度"] if conclusion else None,
            "设计深度": conclusion["设计孔深"] if conclusion else None,
            "关联日志编号": related_log,
            "状态": "待处理",
            "pending": True,
            "abnormal": True,
            "来源批次": batch_no,
            "最近指纹": fingerprint,
        })
        next_todo_id[0] += 1


    # 1) 回写日志台账（班次行的钻进深度保持原上报基准，只挂结论字段）
    for row in log_rows:
        code = str(row.get("钻孔编号") or "").strip()
        conclusion = conclusions.get(code)
        row["库内结论深度"] = conclusion["库内结论深度"] if conclusion else None
        row["结论来源"] = conclusion["结论来源"] if conclusion else None

    # 2) 回写钻孔详情（已确认终孔深度受保护，不被班次或旧值覆盖）
    for row in borehole_rows:
        code = str(row.get("钻孔编号") or "").strip()
        conclusion = conclusions.get(code)
        if conclusion is None:
            continue
        row["库内结论深度"] = conclusion["库内结论深度"]
        row["结论来源"] = conclusion["结论来源"]
        row["库内偏差"] = conclusion["偏差值"]
        row["比对结论"] = conclusion["比对结论"]
        if conclusion["结论来源"] == SOURCE_TERMINAL and conclusion["结论深度值"] is not None:
            current = to_float(row.get("终孔深度"))
            if row.get("已确认终孔") and current == conclusion["结论深度值"]:
                # 已确认终孔：深度展示值原样保留（例如 30.00 不被改写成 30），
                # 来件只能印证不能覆盖。
                pass
            elif current is None or abs(current - conclusion["结论深度值"]) > 1e-9:
                row["终孔深度"] = conclusion["库内结论深度"]
            row["已确认终孔"] = True
            row["确认指纹"] = fingerprint or row.get("确认指纹")
            row["确认批次"] = batch_no or row.get("确认批次")

    # 3) 生成/刷新偏离待办
    for code, conclusion in conclusions.items():
        depth = conclusion["结论深度值"]
        design = conclusion["设计深度值"]
        if (
            conclusion["结论来源"] == SOURCE_TERMINAL
            and depth is not None
            and design is not None
            and abs(depth - design) > DEPTH_TOLERANCE
        ):
            sign = "+" if depth >= design else "-"
            upsert_todo(
                _todo_key("孔深偏离设计", code),
                code=code,
                kind="孔深偏离设计",
                detail=(
                    f"终孔深度 {conclusion['库内结论深度']}m，设计 "
                    f"{conclusion['设计孔深']}m，偏差 {sign}{conclusion['偏差值']}m"
                ),
                conclusion=conclusion,
                related_log=conclusion["终孔记录编号"],
            )
        for index, issue in enumerate(conclusion["回次异常"]):
            upsert_todo(
                _todo_key("回次深度异常", code, str(index)),
                code=code,
                kind="回次深度异常",
                detail=issue,
                conclusion=conclusion,
            )
        if conclusion["是否未登记孔"] and depth is not None:
            upsert_todo(
                _todo_key("未登记钻孔", code),
                code=code,
                kind="未登记钻孔",
                detail=f"现场编号 {code} 有可计量的钻探日志（{depth_text(depth)}m）但钻孔台账未登记，请核对后补建孔号",
                conclusion=conclusion,
            )

    for row in quarantine_rows:
        if row.get("状态") == "已补录":
            continue
        key = _todo_key("缺孔号待查", str(row.get("文件指纹") or ""), str(row.get("行号") or ""))
        upsert_todo(
            key,
            code="（缺孔号）",
            kind="缺孔号待查",
            detail=(
                f"{row.get('文件名', '来件')} 第 {row.get('行号')} 行缺钻孔编号，"
                f"日志编号 {row.get('日志编号') or '未知'}，已隔离待现场补号"
            ),
            conclusion=None,
            related_log=str(row.get("日志编号") or "") or None,
        )

    # 已不存在偏离情形的旧待办整体清除；仍存在但被现场关闭的保留关闭状态。
    store.rows(DEVIATION_MODULE)[:] = [
        row for row in deviation_rows if str(row.get("待办键")) in seen_keys
    ]

    open_count = sum(1 for row in deviation_rows if row.get("状态") == "待处理")
    return {
        "结论孔数": len(conclusions),
        "待办总数": len(deviation_rows),
        "待处理待办": open_count,
        "结论": conclusions,
    }
