"""内存存储事务保证：整批回滚、嵌套保存点、表引用稳定。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.store import store  # noqa: E402


class TransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        store.reset()

    def test_rollback_restores_all_tables(self) -> None:
        log_before = len(store.rows("drilling_log"))
        todo_before = len(store.rows("drilling_deviation"))
        with self.assertRaises(RuntimeError):
            with store.transaction():
                store.rows("drilling_log").append({"id": 999, "钻孔编号": "X"})
                store.rows("drilling_deviation").append({"id": 999, "偏离类型": "X"})
                raise RuntimeError("模拟落库后生成待办前失败")
        self.assertEqual(len(store.rows("drilling_log")), log_before)
        self.assertEqual(len(store.rows("drilling_deviation")), todo_before)

    def test_commit_persists_all_tables(self) -> None:
        with store.transaction():
            store.rows("drilling_log").append({"id": 998, "钻孔编号": "Y"})
            store.rows("drilling_deviation").append({"id": 998, "偏离类型": "Y"})
        self.assertTrue(any(r.get("id") == 998 for r in store.rows("drilling_log")))
        self.assertTrue(any(r.get("id") == 998 for r in store.rows("drilling_deviation")))

    def test_nested_inner_rollback_keeps_outer_changes(self) -> None:
        with store.transaction():
            store.rows("drilling_log").append({"id": 997, "钻孔编号": "OUTER"})
            # 内层事务（预览试算）回滚，不影响外层之前的写入
            with self.assertRaises(ValueError):
                with store.transaction():
                    store.rows("drilling_log").append({"id": 996, "钻孔编号": "INNER"})
                    raise ValueError("预览回滚")
            self.assertTrue(any(r.get("钻孔编号") == "OUTER" for r in store.rows("drilling_log")))
            self.assertFalse(any(r.get("钻孔编号") == "INNER" for r in store.rows("drilling_log")))

    def test_nested_outer_rollback_removes_everything(self) -> None:
        with self.assertRaises(RuntimeError):
            with store.transaction():
                store.rows("drilling_log").append({"id": 995, "钻孔编号": "OUTER"})
                with store.transaction():
                    store.rows("drilling_log").append({"id": 994, "钻孔编号": "INNER"})
                raise RuntimeError("外层最终失败")
        self.assertFalse(any(r.get("id") in (994, 995) for r in store.rows("drilling_log")))

    def test_table_reference_stays_valid_across_nested_rollback(self) -> None:
        """关键回归：事务外/外层缓存的表列表引用，在内层回滚后仍指向活跃表。

        预览事务曾经通过整体替换 self._tables 回滚，导致外层随后写入游离列表，
        出现「日志进了库、批次记录丢失」的半批现象。
        """
        table_ref = store.rows("drilling_import_file")
        with self.assertRaises(ValueError):
            with store.transaction():
                store.rows("drilling_import_file").append({"id": 1, "x": 1})
                raise ValueError
        # 引用仍有效：往里写应当真实可见
        table_ref.append({"id": 2, "文件指纹": "fp", "状态": "已导入"})
        self.assertIn(
            {"id": 2, "文件指纹": "fp", "状态": "已导入"},
            store.rows("drilling_import_file"),
        )

    def test_new_table_created_in_transaction_is_cleared_on_rollback(self) -> None:
        with self.assertRaises(RuntimeError):
            with store.transaction():
                store.rows("brand_new_table").append({"id": 1})
                raise RuntimeError
        self.assertEqual(store.rows("brand_new_table"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
