"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

导入落库要求「整批事务」：``store.transaction()`` 在进入时给所有表拍快照，
业务过程中任意一步抛错就把整批改动回滚到快照，只有不抛错走出 with 块才提交。
快照按栈管理、支持嵌套（预览事务会套在提交事务里做「试算后回滚」），回滚时
逐表原地恢复内容、保持表对象身份稳定，事务外缓存的列表引用不会失效。
"""
from __future__ import annotations

import copy
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tables: dict[str, list[dict[str, Any]]] = self._seed_tables()
        # 事务快照栈：支持嵌套事务（预览事务套在提交事务里做试算回滚）。
        self._snapshots: list[dict[str, list[dict[str, Any]]]] = []

    def _seed_tables(self) -> dict[str, list[dict[str, Any]]]:
        return {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}

    def reset(self) -> None:
        """清空运行期数据并恢复种子数据，供测试与运维重置使用。"""
        with self._lock:
            self._tables = self._seed_tables()
            self._snapshots.clear()

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, module: str) -> int:
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """整批事务：块内异常 → 回滚到进入前快照；正常退出 → 一起提交。

        * 外层事务抛错：全部表回滚，不留半批记录；
        * 内层事务抛错：只回滚内层进入点之后的改动，外层已有改动不受影响；
        * 回滚逐表原地恢复，``store.rows(module)`` 拿到的列表对象始终有效。
        """
        with self._lock:
            snapshot = copy.deepcopy(self._tables)
            self._snapshots.append(snapshot)
            try:
                yield
            except BaseException:
                # 恢复快照里存在的表内容；事务中新建的表清空但保留键。
                for name, snapshot_rows in snapshot.items():
                    table = self._tables.setdefault(name, [])
                    table.clear()
                    table.extend(copy.deepcopy(snapshot_rows))
                for name in list(self._tables):
                    if name not in snapshot:
                        self._tables[name].clear()
                self._snapshots.pop()
                raise
            else:
                self._snapshots.pop()

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
