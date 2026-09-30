"""钻探日志整批导入的端到端测试：用 TestClient 跑真实 HTTP 路由。

覆盖场景：
1. 整批事务——任一非隔离行不通过，台账、待办、钻孔详情全部不动；
2. 指纹幂等——重复提交同一文件不落第二遍；
3. 缺孔号隔离——缺号行进隔离表并生成待办，其余行正常迁移；
4. 现场终孔优先 / 历史班次原基准保留 / 三处结论一致；
5. 已确认终孔深度不得被来件覆盖（整批退回）；
6. 断点续传——偏移追加、重传幂等、缺口拒绝，中断不产生半批；
7. 隔离行补号迁移；待办关闭后重算不复活。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.services.drilling_import import fingerprint_of  # noqa: E402
from app.store import store  # noqa: E402


def csv_content(*rows: str) -> str:
    header = "日志编号,钻孔编号,钻进深度,记录类型,岩层描述"
    return "\n".join([header, *rows])


class DrillingImportTests(unittest.TestCase):
    def setUp(self) -> None:
        store.reset()
        self.client = TestClient(app)

    # ------------------------------------------------------------ 基础落库

    def test_commit_all_success_together(self) -> None:
        content = csv_content(
            "DL-1,ZK-900,10,班次,砂岩",
            "DL-2,ZK-900,22,终孔,泥岩",
        )
        response = self.client.post(
            "/api/drilling_import/commit", json={"file_name": "a.csv", "content": content}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["新增日志"], 2)
        self.assertEqual(data["隔离行"], 0)

        ledger = self.client.get("/api/drilling_log?keyword=ZK-900").json()["items"]
        self.assertEqual(len(ledger), 2)
        # 三处取同一份：台账行结论深度、钻孔（未登记不适用，看待办）、待办
        for row in ledger:
            self.assertEqual(row["库内结论深度"], "22")
            self.assertEqual(row["结论来源"], "现场终孔记录")
        # 终孔优先：班次行保留自己上报的 10，结论是终孔的 22
        shift = next(row for row in ledger if row["日志编号"] == "DL-1")
        terminal = next(row for row in ledger if row["日志编号"] == "DL-2")
        self.assertEqual(shift["钻进深度"], "10")
        self.assertEqual(terminal["钻进深度"], "22")

    def test_preview_does_not_persist_anything(self) -> None:
        before = self.client.get("/api/drilling_log").json()["total"]
        content = csv_content("DL-1,ZK-901,5,班次,", "DL-2,ZK-901,12,终孔,")
        response = self.client.post(
            "/api/drilling_import/preview", json={"file_name": "p.csv", "content": content}
        )
        self.assertEqual(response.status_code, 200)
        preview = response.json()
        self.assertEqual(preview["有效行数"], 2)
        self.assertEqual(preview["结论预览"]["ZK-901"]["库内结论深度"], "12")
        self.assertNotIn("_valid", preview)
        self.assertNotIn("_quarantine", preview)
        after = self.client.get("/api/drilling_log").json()["total"]
        self.assertEqual(before, after)
        self.assertEqual(self.client.get("/api/drilling_import/batches").json()["total"], 0)

    # ------------------------------------------------------------ 整批退回

    def test_any_invalid_row_rejects_entire_batch(self) -> None:
        logs_before = self.client.get("/api/drilling_log").json()["total"]
        todos_before = self.client.get("/api/drilling_import/deviations").json()["total"]
        batches_before = self.client.get("/api/drilling_import/batches").json()["total"]
        content = csv_content(
            "DL-1,ZK-902,10,班次,",
            "DL-2,ZK-902,深,终孔,",   # 深度无法识别 → 整批退回
            "DL-3,ZK-902,11,班次,",
        )
        response = self.client.post(
            "/api/drilling_import/commit", json={"file_name": "bad.csv", "content": content}
        )
        self.assertEqual(response.status_code, 422)
        detail = response.json()["detail"]
        self.assertEqual(detail["kind"], "batch_rejected")
        self.assertTrue(any("无法识别" in e["原因"] for e in detail["errors"]))
        # 业务表完全不动，不允许只导入一半
        self.assertEqual(self.client.get("/api/drilling_log").json()["total"], logs_before)
        self.assertEqual(
            self.client.get("/api/drilling_import/deviations").json()["total"], todos_before
        )
        # 批次留痕只记退回，不含业务数据
        batches = self.client.get("/api/drilling_import/batches").json()["items"]
        self.assertEqual(len(batches), batches_before + 1)
        self.assertEqual(batches[0]["状态"], "已退回")

        # 修正后以同一文件名重报可以成功（指纹变了是新批次，旧退回留痕并入）
        fixed = csv_content("DL-1,ZK-902,10,班次,", "DL-2,ZK-902,12,终孔,", "DL-3,ZK-902,11,班次,")
        response = self.client.post(
            "/api/drilling_import/commit", json={"file_name": "bad.csv", "content": fixed}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["新增日志"], 3)

    def test_duplicate_log_no_in_file_rejects_batch(self) -> None:
        content = csv_content("DL-1,ZK-903,10,班次,", "DL-1,ZK-903,12,终孔,")
        response = self.client.post(
            "/api/drilling_import/commit", json={"file_name": "dup.csv", "content": content}
        )
        self.assertEqual(response.status_code, 422)
        self.assertTrue(any("重复" in e["原因"] for e in response.json()["detail"]["errors"]))

    # ------------------------------------------------------------ 指纹幂等

    def test_fingerprint_idempotency(self) -> None:
        content = csv_content("DL-1,ZK-904,8,班次,", "DL-2,ZK-904,16,终孔,")
        payload = {"file_name": "id.csv", "content": content, "fingerprint": fingerprint_of(content)}
        first = self.client.post("/api/drilling_import/commit", json=payload).json()
        second = self.client.post("/api/drilling_import/commit", json=payload).json()
        self.assertFalse(first["幂等跳过"])
        self.assertTrue(second["幂等跳过"])
        self.assertEqual(first["批次号"], second["批次号"])
        logs = self.client.get("/api/drilling_log?keyword=ZK-904").json()["total"]
        self.assertEqual(logs, 2)

    def test_fingerprint_mismatch_rejected(self) -> None:
        content = csv_content("DL-1,ZK-905,8,班次,")
        response = self.client.post(
            "/api/drilling_import/commit",
            json={"file_name": "x.csv", "content": content, "fingerprint": "deadbeef"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("指纹", response.json()["detail"])

    # ------------------------------------------------------------ 缺孔号隔离

    def test_missing_borehole_code_quarantined_rest_migrate(self) -> None:
        content = csv_content(
            "DL-1,ZK-906,9,班次,",
            "DL-2,,18,终孔,",        # 缺孔号：隔离，不拖垮整批
            "DL-3,ZK-906,18,终孔,",
        )
        response = self.client.post(
            "/api/drilling_import/commit", json={"file_name": "q.csv", "content": content}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["新增日志"], 2)
        self.assertEqual(data["隔离行"], 1)

        quarantine = self.client.get("/api/drilling_import/quarantine").json()
        self.assertEqual(quarantine["total"], 1)
        self.assertEqual(quarantine["items"][0]["日志编号"], "DL-2")
        self.assertEqual(quarantine["items"][0]["状态"], "待补号")

        todos = self.client.get("/api/drilling_import/deviations").json()["items"]
        self.assertTrue(any(t["偏离类型"] == "缺孔号待查" for t in todos))

        # 现场补号后整批迁入台账
        qid = quarantine["items"][0]["id"]
        response = self.client.post(
            f"/api/drilling_import/quarantine/{qid}/resolve", json={"钻孔编号": "ZK-906"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/drilling_import/quarantine").json()["total"], 0)
        self.assertEqual(self.client.get("/api/drilling_log?keyword=ZK-906").json()["total"], 3)

    # -------------------------------------------------- 终孔冲突 / 已确认保护

    def _seed_confirmed_borehole(self, code: str, depth: str, design: str) -> None:
        store.rows("borehole").append({
            "id": store.next_id("borehole"),
            "钻孔编号": code,
            "勘探区": "测试矿区",
            "孔口坐标": "X0,Y0",
            "设计孔深": design,
            "终孔深度": depth,
            "已确认终孔": True,
            "status": "已终孔",
            "pending": False,
            "abnormal": False,
        })

    def test_confirmed_terminal_depth_is_protected(self) -> None:
        self._seed_confirmed_borehole("ZK-907", "20.00", "20")
        logs_before = self.client.get("/api/drilling_log").json()["total"]
        content = csv_content("DL-1,ZK-907,10,班次,", "DL-2,ZK-907,21,终孔,")
        response = self.client.post(
            "/api/drilling_import/commit", json={"file_name": "c.csv", "content": content}
        )
        self.assertEqual(response.status_code, 422)
        errors = response.json()["detail"]["errors"]
        self.assertTrue(any("已确认" in e["原因"] for e in errors))
        # 整批退回：连不冲突的班次行也不能进
        self.assertEqual(self.client.get("/api/drilling_log").json()["total"], logs_before)
        hole = next(
            row for row in store.rows("borehole") if row["钻孔编号"] == "ZK-907"
        )
        self.assertEqual(hole["终孔深度"], "20.00")

    def test_terminal_wins_and_deviation_todo_created(self) -> None:
        self._seed_confirmed_borehole("ZK-908", "30.00", "20")  # 偏离设计 10m
        content = csv_content("DL-1,ZK-908,8,班次,", "DL-2,ZK-908,30,终孔,")
        response = self.client.post(
            "/api/drilling_import/commit", json={"file_name": "t.csv", "content": content}
        )
        self.assertEqual(response.status_code, 200)

        # 三处同一份结论
        hole = next(row for row in store.rows("borehole") if row["钻孔编号"] == "ZK-908")
        self.assertEqual(hole["库内结论深度"], "30")
        # 已确认深度的展示原值（30.00）受保护，不被二次格式化覆盖
        self.assertEqual(hole["终孔深度"], "30.00")
        self.assertEqual(hole["比对结论"], "偏离设计")
        ledger = self.client.get("/api/drilling_log?keyword=ZK-908").json()["items"]
        for row in ledger:
            self.assertEqual(row["库内结论深度"], "30")
        todos = self.client.get("/api/drilling_import/deviations").json()["items"]
        deviation = next(t for t in todos if t["偏离类型"] == "孔深偏离设计")
        self.assertEqual(deviation["结论深度"], "30")
        self.assertEqual(deviation["设计深度"], "20")

    def test_shift_rows_keep_original_baseline_without_terminal(self) -> None:
        self._seed_confirmed_borehole("ZK-909", "", "50")
        content = csv_content("DL-1,ZK-909,10,班次,", "DL-2,ZK-909,14,班次,")
        self.client.post(
            "/api/drilling_import/commit", json={"file_name": "s.csv", "content": content}
        )
        ledger = self.client.get("/api/drilling_log?keyword=ZK-909").json()["items"]
        depths = sorted(row["钻进深度"] for row in ledger)
        self.assertEqual(depths, ["10", "14"])  # 原值不被改写
        for row in ledger:
            self.assertEqual(row["库内结论深度"], "14")
            self.assertEqual(row["结论来源"], "历史班次上报")
        hole = next(row for row in store.rows("borehole") if row["钻孔编号"] == "ZK-909")
        self.assertEqual(hole["库内结论深度"], "14")
        # 没有终孔 → 不产生孔深偏离设计待办
        todos = self.client.get(
            "/api/drilling_import/deviations?status_filter=待处理"
        ).json()["items"]
        self.assertFalse(any(t["偏离类型"] == "孔深偏离设计" for t in todos))

    # ------------------------------------------------------------ 断点续传

    def test_resume_from_first_unconfirmed_row(self) -> None:
        created = self.client.post(
            "/api/drilling_import/sessions", json={"file_name": "r.csv"}
        ).json()
        sid = created["session_id"]
        header = "日志编号,钻孔编号,钻进深度,记录类型"
        rows = ["DL-1,ZK-910,10,班次", "DL-2,ZK-910,20,终孔"]
        all_lines = [header, *rows]

        # 第一次只传了表头+首行
        ack = self.client.post(
            f"/api/drilling_import/sessions/{sid}/rows",
            json={"offset": 0, "rows": all_lines[:2]},
        ).json()
        self.assertEqual(ack["received"], 2)

        # 断线重传：从 0 重发全部，已确认的表头/首行与未确认尾部一起被本批覆盖，
        # 不产生重复行，最终完整文件为 3 行。
        ack = self.client.post(
            f"/api/drilling_import/sessions/{sid}/rows",
            json={"offset": 0, "rows": all_lines},
        ).json()
        self.assertEqual(ack["accepted"], 3)
        self.assertEqual(ack["resume_from"], 3)

        # 跳行续传（有缺口）必须拒绝
        gap = self.client.post(
            f"/api/drilling_import/sessions/{sid}/rows",
            json={"offset": 9, "rows": ["DL-x,ZK,1,班次"]},
        )
        self.assertEqual(gap.status_code, 409)

        # 传输期间没有任何台账记录
        self.assertEqual(self.client.get("/api/drilling_log?keyword=ZK-910").json()["total"], 0)

        committed = self.client.post(
            f"/api/drilling_import/sessions/{sid}/commit"
        ).json()
        self.assertEqual(committed["新增日志"], 2)
        self.assertEqual(
            self.client.get("/api/drilling_log?keyword=ZK-910").json()["total"], 2
        )
        # 会话已结束
        self.assertEqual(
            self.client.get(f"/api/drilling_import/sessions/{sid}").status_code, 404
        )

    def test_connection_drop_leaves_no_half_batch(self) -> None:
        """传到一半时：库里没有半批日志；对残缺文件提交会整批退回且不留记录。"""
        created = self.client.post(
            "/api/drilling_import/sessions", json={"file_name": "half.csv"}
        ).json()
        sid = created["session_id"]
        header = "日志编号,钻孔编号,钻进深度,记录类型"

        # 只确认收到了表头 + 一条残缺行（深度缺失），后续行因断线从未到达
        self.client.post(
            f"/api/drilling_import/sessions/{sid}/rows",
            json={"offset": 0, "rows": [header, "DL-2,ZK-911,,终孔"]},
        )
        # 传输进行期间台账必须是干净的，不能出现先落一半的行
        self.assertEqual(self.client.get("/api/drilling_log?keyword=ZK-911").json()["total"], 0)

        # 现场拿残缺内容强行提交（缺少深度值），整批退回
        bad = self.client.post(f"/api/drilling_import/sessions/{sid}/commit")
        self.assertEqual(bad.status_code, 422)
        self.assertEqual(self.client.get("/api/drilling_log?keyword=ZK-911").json()["total"], 0)

        # 连接恢复后从首个未确认行（偏移 1）续传：残缺尾部被本批 2 行整体替换
        ack = self.client.post(
            f"/api/drilling_import/sessions/{sid}/rows",
            json={"offset": 1, "rows": ["DL-1,ZK-911,10,班次", "DL-2,ZK-911,20,终孔"]},
        ).json()
        self.assertEqual(ack["accepted"], 2)
        self.assertEqual(ack["resume_from"], 3)
        committed = self.client.post(f"/api/drilling_import/sessions/{sid}/commit")
        self.assertEqual(committed.status_code, 200)
        self.assertEqual(committed.json()["新增日志"], 2)
        self.assertEqual(self.client.get("/api/drilling_log?keyword=ZK-911").json()["total"], 2)

    # ------------------------------------------------------------ 待办稳定性

    def test_closed_todo_not_resurrected_by_rebuild(self) -> None:
        content = csv_content("DL-1,ZK-912,40,终孔,")
        self._seed_confirmed_borehole("ZK-912", "40", "20")
        self.client.post(
            "/api/drilling_import/commit", json={"file_name": "z.csv", "content": content}
        )
        todos = self.client.get("/api/drilling_import/deviations").json()["items"]
        todo = next(t for t in todos if t["偏离类型"] == "孔深偏离设计")
        closed = self.client.post(
            f"/api/drilling_import/deviations/{todo['id']}/status", json={"status": "已处理"}
        )
        self.assertEqual(closed.status_code, 200)

        # 再来一批同孔班次数据触发结论重算，已关闭待办保持关闭
        more = csv_content("DL-3,ZK-912,40,班次,")
        self.client.post(
            "/api/drilling_import/commit", json={"file_name": "z2.csv", "content": more}
        )
        fresh = next(
            t for t in self.client.get("/api/drilling_import/deviations").json()["items"]
            if t["偏离类型"] == "孔深偏离设计"
        )
        self.assertEqual(fresh["状态"], "已处理")
        self.assertFalse(fresh["pending"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
