"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

导入等跨表写操作必须通过 ``store.transaction(...)`` 包成整批事务：进入事务时
对相关表做快照，任何一步抛异常都会按快照回滚，保证「要么一起成功、要么一起
回滚」，不会留下只导入一半的半批记录。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from copy import deepcopy
from typing import Any, Iterator

from app.seed import SEED_ROWS

# 导入链路自用的内部表（批次登记、缺孔号隔离、库内结论、偏离待办）。
# 下划线开头，不进业务模块清单与概览统计。
INTERNAL_TABLES = (
    "_import_batches",
    "_drilling_quarantine",
    "_drilling_conclusions",
    "_drilling_deviations",
)


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        for name in INTERNAL_TABLES:
            self._tables[name] = []
        # 内存仓库没有数据库锁，导入整批提交期间用可重入锁挡住并发写。
        self._lock = threading.RLock()

    def module_names(self) -> list[str]:
        return sorted(name for name in self._tables if not name.startswith("_"))

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
    def transaction(self, *tables: str) -> Iterator[None]:
        """整批事务：对涉及的表逐表快照，异常时整批回滚，成功时一并落库。

        事务体内对 ``self.rows(...)`` 里字典的增删改都会在异常时恢复到进入前的
        状态；未列入 ``tables`` 的表不允许在事务里写（读不受影响）。
        """
        touched = [table for table in dict.fromkeys(tables)]
        with self._lock:
            snapshots = {table: deepcopy(self.rows(table)) for table in touched}
            try:
                yield
            except BaseException:
                for table, snapshot in snapshots.items():
                    self._tables[table] = snapshot
                raise

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
