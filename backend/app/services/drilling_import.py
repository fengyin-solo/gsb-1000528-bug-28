"""钻探日志整批导入：解析 → 校验 → 落库 → 结论回写与待办生成，同生共死。

设计要点（对应现场约定）：

* 整批事务：非隔离行任意一行校验不通过，整批退回，业务表一行都不落；
  只有全部通过时，日志台账、缺孔号隔离、钻孔结论回写、偏离待办才一起提交。
* 文件按指纹幂等：以文件全文 SHA-256 为指纹，同一文件重复提交直接返回首次
  结果，不会生成第二条日志、也不会覆盖任何已确认深度。
* 缺孔号先隔离：缺钻孔编号的行进 ``drilling_quarantine`` 隔离表（与台账写入
  同一事务），其余行按现场钻孔编号迁移入台账，并在偏离待办里挂「缺孔号待查」。
* 冲突口径：已确认终孔深度受保护，来件终孔记录与它不一致 → 整批退回；
  历史班次按原上报基准保留，导入永不改写既有日志行。
* 断点续传：上传会话按行偏移量追加，断线重连从首个未确认收到的行继续，
  已确认的行幂等跳过；最终提交仍是一个事务，不会留下半批台账记录。
"""
from __future__ import annotations

import copy
import csv
import hashlib
import io
import threading
from datetime import datetime
from typing import Any

from app.services.drilling_conclusion import (
    DRILLING_LOG_MODULE,
    QUARANTINE_MODULE,
    TERMINAL_TYPES,
    SHIFT_TYPE,
    TERMINAL_TYPE,
    depth_text,
    rebuild as rebuild_conclusions,
    to_float,
)
from app.store import store

IMPORT_FILE_MODULE = "drilling_import_file"

# 表头别名：现场模板可能叫孔号、累计深度等，统一映射到标准列。
HEADER_ALIASES: dict[str, set[str]] = {
    "日志编号": {"日志编号", "日志号", "记录编号", "记录号"},
    "钻孔编号": {"钻孔编号", "钻孔号", "孔号", "现场编号", "施工孔号"},
    "钻进深度": {"钻进深度", "累计深度", "当前深度", "当前孔深", "深度"},
    "回次进尺": {"回次进尺", "本班进尺", "进尺"},
    "岩层描述": {"岩层描述", "岩性描述", "地质描述", "岩性"},
    "水位深度": {"水位深度", "静止水位", "水位"},
    "钻探人员": {"钻探人员", "当班人员", "班组", "施工人员"},
    "记录类型": {"记录类型", "类型", "数据类型", "行类型"},
    "日志状态": {"日志状态", "状态"},
}
OPTIONAL_FIELDS = ["回次进尺", "岩层描述", "水位深度", "钻探人员"]
REQUIRED_HEADERS = ["日志编号", "钻进深度"]


class _PreviewRollback(Exception):
    """预览专用信号：在事务里把结果算完后回滚，不留任何痕迹。"""


class BatchRejected(Exception):
    """整批校验未通过：携带逐行错误，业务表整体退回。"""

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        self.errors = errors
        super().__init__(f"整批退回：{len(errors)} 行校验不通过")


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def fingerprint_of(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def short_batch_no(fingerprint: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"IMP-{stamp}-{fingerprint[:8]}"


class _UploadSession:
    """断点续传会话：保存已确认收到的原始行（含表头），按偏移量追加。"""

    def __init__(self, session_id: str, file_name: str) -> None:
        self.session_id = session_id
        self.file_name = file_name
        self.lines: list[str] = []
        self.created_at = now_text()

    @property
    def received(self) -> int:
        return len(self.lines)

    def content(self) -> str:
        return "\n".join(self.lines)


class DrillingImportService:
    def __init__(self) -> None:
        self._sessions: dict[str, _UploadSession] = {}
        self._lock = threading.RLock()
        self._session_seq = 0

    # ------------------------------------------------------------------ 会话

    def create_session(self, file_name: str) -> dict[str, Any]:
        with self._lock:
            self._session_seq += 1
            session_id = f"s-{self._session_seq}-{datetime.now().strftime('%H%M%S')}"
            session = _UploadSession(session_id, file_name)
            self._sessions[session_id] = session
            return {"session_id": session_id, "file_name": file_name, "received": 0}

    def _get_session(self, session_id: str) -> _UploadSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"上传会话 {session_id} 不存在或已过期，请重新选择文件")
        return session

    def session_status(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            session = self._get_session(session_id)
            return {"session_id": session_id, "file_name": session.file_name, "received": session.received}

    def append_rows(
        self, session_id: str, offset: int, rows: list[str]
    ) -> dict[str, Any]:
        """按偏移量追加行。

        * ``offset == 已收到行数``：正常追加；
        * ``offset < 已收到行数``：断线重传命中已确认区间，幂等忽略，
          调用方按返回的 received 从首个未确认行继续；
        * ``offset > 已收到行数``：中间有缺口，拒绝并要求从 received 续传。
        """
        with self._lock:
            session = self._get_session(session_id)
            if offset > session.received:
                raise IndexError(
                    f"上传存在缺口：服务器已收到 {session.received} 行，"
                    f"却从第 {offset} 行续传，请从第 {session.received} 行继续"
                )
            # 从 offset 起覆盖尾部：断线重传时本批首行通常就是首个未确认行，
            # 它之前已确认的行保留，offset 之后（含上一次传了一半的残缺尾部）
            # 用本批整体替换，既不会重复追加，也能纠正残缺内容。
            session.lines = session.lines[:offset] + list(rows)
            return {
                "session_id": session_id,
                "received": session.received,
                "accepted": len(rows),
                "resume_from": session.received,
            }

    # ------------------------------------------------------------------ 解析

    def _parse_content(self, content: str) -> tuple[list[str], list[dict[str, Any]]]:
        """解析 CSV/TSV 文本为标准字段行；行号从 2 起算（1 是表头）。"""
        text = content.replace("﻿", "").strip("\n")
        if not text.strip():
            raise BatchRejected([{"行号": 0, "原因": "文件为空，没有任何可导入数据"}])

        sample = text[:2048]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;，")
            delimiter = dialect.delimiter
        except csv.Error:
            delimiter = "\t" if sample.count("\t") >= sample.count(",") else ","

        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        raw_rows = list(reader)
        if len(raw_rows) < 2:
            raise BatchRejected([{"行号": 0, "原因": "只有表头没有数据行，整批不予导入"}])

        header_cells = [cell.strip() for cell in raw_rows[0]]
        column_map: dict[str, int] = {}
        for index, title in enumerate(header_cells):
            for canonical, aliases in HEADER_ALIASES.items():
                if title in aliases and canonical not in column_map:
                    column_map[canonical] = index

        missing_headers = [name for name in REQUIRED_HEADERS if name not in column_map]
        if missing_headers:
            raise BatchRejected([
                {"行号": 1, "原因": f"表头缺少必需列：{'、'.join(missing_headers)}"}
            ])

        def cell(row: list[str], field: str) -> str:
            index = column_map.get(field)
            if index is None or index >= len(row):
                return ""
            return row[index].strip()

        parsed: list[dict[str, Any]] = []
        for line_index, raw in enumerate(raw_rows[1:], start=2):
            if not any(cell.strip() for cell in raw):
                continue  # 空行直接跳过，不算数据也不算错误
            record = {
                field: cell(raw, field)
                for field in ["日志编号", "钻孔编号", "钻进深度", *OPTIONAL_FIELDS, "记录类型", "日志状态"]
            }
            record["行号"] = line_index
            parsed.append(record)

        if not parsed:
            raise BatchRejected([{"行号": 0, "原因": "解析后没有任何有效数据行"}])
        return header_cells, parsed

    @staticmethod
    def _is_terminal_record(record: dict[str, Any]) -> bool:
        record_type = record.get("记录类型", "").lower()
        if record_type in {item.lower() for item in TERMINAL_TYPES}:
            return True
        return "终孔" in str(record.get("日志状态", ""))

    # ------------------------------------------------------------------ 校验

    def _plan(self, file_name: str, fingerprint: str, content: str) -> dict[str, Any]:
        """解析并在不落库的前提下给出导入计划与错误清单。

        结论预览借助事务回滚实现：临时落一遍 → 跑结论引擎 → 回滚，
        因此预览看到的结论/待办与真正提交时完全一致。
        """
        _, parsed = self._parse_content(content)

        errors: list[dict[str, Any]] = []
        valid_rows: list[dict[str, Any]] = []
        quarantined_rows: list[dict[str, Any]] = []
        seen_log_nos: set[str] = set()
        ledger = store.rows(DRILLING_LOG_MODULE)
        existing_log_nos = {str(row.get("日志编号") or "").strip() for row in ledger}
        boreholes = store.rows("borehole")
        confirmed_by_code: dict[str, float] = {}
        terminal_in_file: dict[str, list[tuple[int, str, float]]] = {}
        for row in boreholes:
            if row.get("已确认终孔"):
                depth = to_float(row.get("终孔深度"))
                code = str(row.get("钻孔编号") or "").strip()
                if depth is not None and code:
                    confirmed_by_code[code] = depth

        for record in parsed:
            line_no = record["行号"]
            code = record.get("钻孔编号", "").strip()
            log_no = record.get("日志编号", "").strip()

            # 缺孔号：先隔离，不参与硬性校验，不拖垮整批。
            if not code:
                quarantined_rows.append(record)
                continue

            if not log_no:
                errors.append({"行号": line_no, "钻孔编号": code, "原因": "缺少日志编号"})
                continue
            if log_no in seen_log_nos:
                errors.append({"行号": line_no, "日志编号": log_no, "钻孔编号": code,
                               "原因": "文件内日志编号重复，无法确定唯一行"})
                continue
            seen_log_nos.add(log_no)
            if log_no in existing_log_nos:
                errors.append({"行号": line_no, "日志编号": log_no, "钻孔编号": code,
                               "原因": "日志编号已在台账中，导入不允许覆盖既有记录"})
                continue

            depth = to_float(record.get("钻进深度"))
            if depth is None:
                errors.append({"行号": line_no, "日志编号": log_no, "钻孔编号": code,
                               "原因": f"钻进深度「{record.get('钻进深度', '')}」无法识别为米数"})
                continue
            if depth < 0:
                errors.append({"行号": line_no, "日志编号": log_no, "钻孔编号": code,
                               "原因": "钻进深度不能为负数"})
                continue
            record["_深度值"] = depth
            record["_终孔"] = self._is_terminal_record(record)
            valid_rows.append(record)

            if record["_终孔"]:
                terminal_in_file.setdefault(code, []).append((line_no, log_no, depth))
                confirmed = confirmed_by_code.get(code)
                if confirmed is not None and abs(confirmed - depth) > 1e-9:
                    errors.append({
                        "行号": line_no,
                        "日志编号": log_no,
                        "钻孔编号": code,
                        "原因": (
                            f"现场终孔深度 {depth_text(depth)}m 与库内已确认终孔深度 "
                            f"{depth_text(confirmed)}m 冲突，已确认深度不得覆盖"
                        ),
                    })

        # 同一孔在文件里出现互相矛盾的终孔记录时，现场记录本身不自洽，整批退回。
        for code, items in terminal_in_file.items():
            depths = {item[2] for item in items}
            if len(depths) > 1:
                for line_no, log_no, depth in items:
                    others = "、".join(
                        f"第 {other[0]} 行 {depth_text(other[2])}m"
                        for other in items if other[1] != log_no
                    )
                    errors.append({
                        "行号": line_no,
                        "日志编号": log_no,
                        "钻孔编号": code,
                        "原因": f"同孔终孔记录互相矛盾（{others}），无法确定现场终孔基准",
                    })

        # 校验阶段绝不允许半批：把错误抛给上层统一处理（含预览，不记台账）。
        if errors:
            raise BatchRejected(sorted(errors, key=lambda item: item["行号"]))

        preview = self._dry_run(file_name, fingerprint, valid_rows, quarantined_rows)
        return {
            "文件指纹": fingerprint,
            "文件名": file_name,
            "总行数": len(parsed),
            "有效行数": len(valid_rows),
            "隔离行数": len(quarantined_rows),
            "待入库": [self._public_row(row) for row in valid_rows],
            "待隔离": [self._public_row(row) for row in quarantined_rows],
            "结论预览": preview["conclusions"],
            "待办预览": preview["todos"],
            "_valid": valid_rows,
            "_quarantine": quarantined_rows,
        }

    def _dry_run(
        self,
        file_name: str,
        fingerprint: str,
        valid_rows: list[dict[str, Any]],
        quarantined_rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """在事务里临时落库算结论，随后回滚，得到与真实提交一致的预览。"""
        captured: dict[str, Any] = {}
        try:
            with store.transaction():
                self._apply_rows(file_name, fingerprint, valid_rows, quarantined_rows)
                summary = rebuild_conclusions(fingerprint=fingerprint)
                # 结论与待办都是普通 dict，先拷出来再回滚，预览不留任何业务痕迹。
                captured["conclusions"] = copy.deepcopy(summary["结论"])
                captured["todos"] = [
                    dict(row)
                    for row in store.rows("drilling_deviation")
                    if row.get("状态") == "待处理"
                ]
                raise _PreviewRollback
        except _PreviewRollback:
            pass
        return captured

    @staticmethod
    def _public_row(record: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in record.items()
            if not key.startswith("_")
        }

    # ------------------------------------------------------------------ 落库

    def _apply_rows(
        self,
        file_name: str,
        fingerprint: str,
        valid_rows: list[dict[str, Any]],
        quarantined_rows: list[dict[str, Any]],
    ) -> str:
        batch_no = short_batch_no(fingerprint)
        log_table = store.rows(DRILLING_LOG_MODULE)
        next_log_id = max((int(row.get("id", 0)) for row in log_table), default=0) + 1
        for record in valid_rows:
            terminal = record["_终孔"]
            entry = {
                "id": next_log_id,
                "日志编号": record["日志编号"],
                "钻孔编号": record["钻孔编号"],
                "钻进深度": depth_text(record["_深度值"]),
                "上报深度": str(record.get("钻进深度", "")),
                "回次进尺": record.get("回次进尺", ""),
                "岩层描述": record.get("岩层描述", ""),
                "水位深度": record.get("水位深度", ""),
                "钻探人员": record.get("钻探人员", ""),
                "记录类型": TERMINAL_TYPE if terminal else SHIFT_TYPE,
                "日志状态": "终孔" if terminal else "已填写",
                "status": "已审核" if terminal else "已填写",
                "pending": not terminal,
                "abnormal": False,
                "文件指纹": fingerprint,
                "批次号": batch_no,
                "来源文件": file_name,
                "导入时间": now_text(),
            }
            log_table.append(entry)
            next_log_id += 1

        quarantine_table = store.rows(QUARANTINE_MODULE)
        next_q_id = max((int(row.get("id", 0)) for row in quarantine_table), default=0) + 1
        for record in quarantined_rows:
            quarantine_table.append({
                "id": next_q_id,
                "文件指纹": fingerprint,
                "文件名": file_name,
                "批次号": batch_no,
                "行号": record["行号"],
                "日志编号": record.get("日志编号", ""),
                "钻进深度": record.get("钻进深度", ""),
                "回次进尺": record.get("回次进尺", ""),
                "岩层描述": record.get("岩层描述", ""),
                "水位深度": record.get("水位深度", ""),
                "钻探人员": record.get("钻探人员", ""),
                "记录类型": record.get("记录类型", ""),
                "状态": "待补号",
                "pending": True,
                "abnormal": True,
                "隔离时间": now_text(),
            })
            next_q_id += 1
        return batch_no

    def _record_result_outside_transaction(
        self,
        *,
        file_name: str,
        fingerprint: str,
        status: str,
        total: int,
        valid: int,
        quarantined: int,
        errors: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """整批退回时，业务事务已回滚；批次台账在事务外单独留痕，便于审计重报。"""
        table = store.rows(IMPORT_FILE_MODULE)
        existing = next((row for row in table if row.get("文件指纹") == fingerprint), None)
        if existing is None:
            existing = {"id": store.next_id(IMPORT_FILE_MODULE), "文件指纹": fingerprint}
            table.append(existing)
        existing.update({
            "文件名": file_name,
            "状态": status,
            "总行数": total,
            "有效行数": valid,
            "隔离行数": quarantined,
            "错误明细": errors,
            "最近提交时间": now_text(),
            "退回次数": int(existing.get("退回次数", 0)) + 1,
        })
        return existing

    # ------------------------------------------------------------------ 提交

    def _commit_content(
        self, file_name: str, content: str, fingerprint: str | None
    ) -> dict[str, Any]:
        actual = fingerprint_of(content)
        if fingerprint and fingerprint != actual:
            raise ValueError("文件指纹与服务端计算结果不一致，文件可能已被改动")
        fingerprint = actual

        # 同一指纹可能先退回、后成功；幂等只认成功记录，退回留痕不影响重报。
        ledger = store.rows(IMPORT_FILE_MODULE)
        imported = next(
            (row for row in ledger
             if row.get("文件指纹") == fingerprint and row.get("状态") == "已导入"),
            None,
        )
        if imported is not None:
            return {
                "ok": True,
                "幂等跳过": True,
                "状态": "已导入",
                "文件指纹": fingerprint,
                "文件名": imported.get("文件名", file_name),
                "批次号": imported.get("批次号"),
                "新增日志": imported.get("新增日志", 0),
                "隔离行": imported.get("隔离行数", 0),
                "待办总数": imported.get("待办总数"),
                "message": "该文件已成功导入过，按指纹幂等返回首次结果，未重复落库",
            }

        try:
            plan = self._plan(file_name, fingerprint, content)
        except BatchRejected as rejected:
            parsed_count = sum(1 for line in content.splitlines()[1:] if line.strip())
            record = self._record_result_outside_transaction(
                file_name=file_name,
                fingerprint=fingerprint,
                status="已退回",
                total=parsed_count,
                valid=0,
                quarantined=0,
                errors=rejected.errors,
            )
            return {
                "ok": False,
                "状态": "已退回",
                "文件指纹": fingerprint,
                "文件名": file_name,
                "总行数": parsed_count,
                "错误明细": rejected.errors,
                "退回次数": record["退回次数"],
                "message": f"整批退回：{len(rejected.errors)} 行不通过，台账未做任何改动",
            }

        valid_rows = plan["_valid"]
        quarantined_rows = plan["_quarantine"]

        # 注意：批次表必须在事务内重新取。_plan 的预览用嵌套事务试算后回滚，
        # 回滚会把 self._tables 换成深拷贝快照，事务外缓存的列表引用会变成游离对象。
        with store.transaction():
            ledger = store.rows(IMPORT_FILE_MODULE)
            batch_no = self._apply_rows(file_name, fingerprint, valid_rows, quarantined_rows)
            summary = rebuild_conclusions(fingerprint=fingerprint, batch_no=batch_no)
            rejection_count = sum(
                int(row.get("退回次数", 0))
                for row in ledger
                if row.get("文件指纹") == fingerprint and row.get("状态") == "已退回"
            )
            # 同一指纹只留一条台账：成功后移除历史退回留痕，退回次数并入成功记录。
            ledger[:] = [row for row in ledger if row.get("文件指纹") != fingerprint]
            ledger.append({
                "id": store.next_id(IMPORT_FILE_MODULE),
                "文件指纹": fingerprint,
                "文件名": file_name,
                "批次号": batch_no,
                "状态": "已导入",
                "总行数": plan["总行数"],
                "有效行数": len(valid_rows),
                "隔离行数": len(quarantined_rows),
                "新增日志": len(valid_rows),
                "待办总数": summary["待办总数"],
                "待处理待办": summary["待处理待办"],
                "结论孔数": summary["结论孔数"],
                "导入时间": now_text(),
                "错误明细": [],
                "退回次数": rejection_count,
            })

        return {
            "ok": True,
            "幂等跳过": False,
            "状态": "已导入",
            "文件指纹": fingerprint,
            "文件名": file_name,
            "批次号": batch_no,
            "总行数": plan["总行数"],
            "新增日志": len(valid_rows),
            "隔离行": len(quarantined_rows),
            "待办总数": summary["待办总数"],
            "待处理待办": summary["待处理待办"],
            "结论孔数": summary["结论孔数"],
            "结论": {
                code: {
                    "库内结论深度": data["库内结论深度"],
                    "结论来源": data["结论来源"],
                    "比对结论": data["比对结论"],
                }
                for code, data in summary["结论"].items()
            },
            "message": (
                f"整批提交成功：新增 {len(valid_rows)} 条日志、"
                f"隔离 {len(quarantined_rows)} 条缺孔号记录、"
                f"待办 {summary['待处理待办']} 条，三处结论已同批回写"
            ),
        }

    def preview_content(self, file_name: str, content: str, fingerprint: str | None = None) -> dict[str, Any]:
        actual = fingerprint_of(content)
        if fingerprint and fingerprint != actual:
            raise ValueError("文件指纹与服务端计算结果不一致，文件可能已被改动")
        plan = self._plan(file_name, actual, content)
        # 内部落库计划（带解析缓存字段）不外泄，只返回给前端看的预览口径。
        plan.pop("_valid", None)
        plan.pop("_quarantine", None)
        return plan

    def preview_session(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            session = self._get_session(session_id)
            return self.preview_content(session.file_name, session.content())

    def commit_session(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            session = self._get_session(session_id)
            result = self._commit_content(session.file_name, session.content(), None)
            if result.get("状态") == "已导入" and not result.get("幂等跳过"):
                self._sessions.pop(session_id, None)
            return result

    def commit_payload(
        self, file_name: str, content: str, fingerprint: str | None = None
    ) -> dict[str, Any]:
        return self._commit_content(file_name, content, fingerprint)

    # -------------------------------------------------------------- 补录/待办

    def resolve_quarantine(self, quarantine_id: int, borehole_code: str) -> dict[str, Any]:
        """现场补孔号后，把隔离行按现场编号迁移进台账，仍走整批事务。"""
        code = borehole_code.strip()
        if not code:
            raise ValueError("补录必须提供现场钻孔编号")
        with self._lock:
            q_table = store.rows(QUARANTINE_MODULE)
            record = next((row for row in q_table if int(row.get("id", 0)) == quarantine_id), None)
            if record is None:
                raise KeyError(f"隔离记录 {quarantine_id} 不存在")
            if record.get("状态") == "已补录":
                raise ValueError("该隔离行已补录，请勿重复迁移")
            depth = to_float(record.get("钻进深度"))
            with store.transaction():
                log_table = store.rows(DRILLING_LOG_MODULE)
                log_no = record.get("日志编号") or f"Q-{record.get('文件指纹', '')[:8]}-{record.get('行号')}"
                log_table.append({
                    "id": store.next_id(DRILLING_LOG_MODULE),
                    "日志编号": log_no,
                    "钻孔编号": code,
                    "钻进深度": depth_text(depth) if depth is not None else "",
                    "上报深度": str(record.get("钻进深度", "")),
                    "回次进尺": record.get("回次进尺", ""),
                    "岩层描述": record.get("岩层描述", ""),
                    "水位深度": record.get("水位深度", ""),
                    "钻探人员": record.get("钻探人员", ""),
                    "记录类型": SHIFT_TYPE,
                    "日志状态": "已补录",
                    "status": "已填写",
                    "pending": True,
                    "abnormal": False,
                    "来源隔离记录": quarantine_id,
                    "补录时间": now_text(),
                })
                record["状态"] = "已补录"
                record["补录孔号"] = code
                record["pending"] = False
                record["abnormal"] = False
                record["补录时间"] = now_text()
                summary = rebuild_conclusions()
            return {"ok": True, "日志编号": log_no, "钻孔编号": code, **{
                key: summary[key] for key in ("结论孔数", "待办总数", "待处理待办")
            }}

    def list_batches(self) -> list[dict[str, Any]]:
        return list(reversed(store.rows(IMPORT_FILE_MODULE)))

    def list_quarantine(self, only_pending: bool = True) -> list[dict[str, Any]]:
        rows = store.rows(QUARANTINE_MODULE)
        if only_pending:
            rows = [row for row in rows if row.get("状态") != "已补录"]
        return rows

    def list_deviations(self, status: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows("drilling_deviation")
        if status:
            rows = [row for row in rows if row.get("状态") == status]
        return rows

    def set_deviation_status(self, todo_id: int, status: str) -> dict[str, Any]:
        row = store.find("drilling_deviation", todo_id)
        if row is None:
            raise KeyError(f"偏离待办 {todo_id} 不存在")
        if status not in {"待处理", "已处理"}:
            raise ValueError("待办状态只支持 待处理 / 已处理")
        row["状态"] = status
        row["pending"] = status == "待处理"
        if status == "已处理":
            row["处理时间"] = now_text()
        return row
