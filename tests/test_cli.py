"""CLI 端到端测试：用真实命令解析路径验证本地操作入口。"""
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reading_app.cli import main  # noqa: E402
from reading_app.store import Store  # noqa: E402
from tests.factories import fresh_store, write_materials  # noqa: E402


def low_score(pid, form, dim, ii):
    max_score = 4 if form == "F1" else 6
    return 1, None


class CliEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.store, self.tmp, self.tmpdir = fresh_store()
        self.db = str(Path(self.tmp.name) / "state.sqlite3")
        courses = [
            {"id": "K1", "dimension": "understanding", "grade_low": 3,
             "grade_high": 6, "on": "2026-09-28", "start": "15:00",
             "end": "16:00", "capacity": 1},
            {"id": "K2", "dimension": "knowledge", "grade_low": 3,
             "grade_high": 6, "on": "2026-09-29", "start": "15:00",
             "end": "16:00", "capacity": 2},
        ]
        self.mat = write_materials(
            self.tmpdir / "m", people=3, forms=3, courses=courses,
            score_fn=low_score,
            arrangements=[{"id": "B1", "person_id": "P001", "course_id": "K1",
                           "confirmed_on": "2026-09-20", "attended": False}],
        )

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def run_cli(self, *argv) -> dict | list:
        buf = io.StringIO()
        with redirect_stdout(buf):
            main(["--db", self.db, "--json", *argv])
        return json.loads(buf.getvalue())

    def test_full_lifecycle(self):
        # 导入 -> 规划 -> 诊断 -> 确认/暂缓 -> 撤回 -> 影响 -> 出勤
        self.run_cli("import", str(self.mat))
        plan = self.run_cli("plan", "--on", "2026-09-26")
        self.assertGreaterEqual(len(plan["waitlist"]), 1)
        diagnosis = self.run_cli("diagnose", "P002")
        self.assertEqual(
            diagnosis["dimensions"]["understanding"]["status"],
            "persistent_difficulty")

        # 教师确认一条系统建议
        suggestion = next(s for s in plan["suggestions"])
        accepted = self.run_cli("accept", str(suggestion["request_id"]),
                                "--actor", "王老师")
        self.assertEqual(accepted["request"]["state"], "confirmed")
        self.assertEqual(accepted["request"]["actor"], "王老师")

        # 暂缓另一条同课程之外的建议（同课程可能已满）
        other = next((s for s in plan["suggestions"][1:]
                      if s["course_id"] != suggestion["course_id"]), None)
        if other is not None:
            deferred = self.run_cli("defer", str(other["request_id"]),
                                    "--note", "等补充观察")
            self.assertEqual(deferred["request"]["state"], "deferred")

        # 撤回批准 -> 受影响的未开始确认被标记复议，且产生影响事件
        result = self.run_cli("withdraw-approval", "L1", "--actor", "教研员")
        self.assertIn("impact_event_id", result)
        impacts = self.run_cli("impacts")
        self.assertTrue(any(e["kind"] == "approval_withdrawn" for e in impacts))

        # explain 能交代口径与批准
        st = Store(self.db)
        explained_dim = st.course(
            st.get_request(suggestion["request_id"])["course_id"])["dimension"]
        st.close()
        explained = self.run_cli("explain", str(suggestion["request_id"]))
        self.assertEqual(explained["dimension"], explained_dim)
        self.assertIn("policy_snapshot", explained["evidence_used"])
        self.assertIn("uncertainty", explained["evidence_used"])

        # 教师已知悉影响事件
        event_id = impacts[0]["id"]
        acked = self.run_cli("ack-impact", str(event_id))
        self.assertEqual(acked["acknowledged"], 1)

    def test_attended_history_survives_replan(self):
        self.run_cli("import", str(self.mat))
        self.run_cli("plan", "--on", "2026-09-26")
        # B1 已确认未开始，先回录实际参加
        b1 = Store(self.db).find_request("P001", "K1")
        self.run_cli("attend", str(b1["id"]))
        # 已参加后不能撤销
        with self.assertRaises(SystemExit):
            self.run_cli("cancel", str(b1["id"]))
        # 重新规划不会删除已发生事实
        self.run_cli("plan", "--on", "2026-12-01")
        st = Store(self.db)
        self.assertTrue(st.find_request("P001", "K1")["attended"])
        st.close()

    def test_supplementary_observation_flow(self):
        # 单测评 -> 证据不足 -> 补充观察 -> 回录完成
        mat2 = write_materials(self.tmpdir / "m2", people=2, forms=1)
        self.run_cli("import", str(mat2))
        self.run_cli("plan", "--on", "2026-09-26")
        obs = self.run_cli("observations", "--person", "P001")
        self.assertTrue(obs)
        done = self.run_cli("complete-observation", str(obs[0]["id"]),
                            "--note", "已完成课堂观察")
        self.assertEqual(done["status"], "done")
        # 重复回录被拒绝
        with self.assertRaises(ValueError):
            self.run_cli("complete-observation", str(obs[0]["id"]))

    def test_group_command_denominators(self):
        self.run_cli("import", str(self.mat))
        g = self.run_cli("group", "F1")
        self.assertEqual(g["form_id"], "F1")
        for row in g["schools"]:
            for cell in row["dimensions"].values():
                self.assertGreaterEqual(cell["denominator"], cell["participated"])

    def test_audit_trail_separates_sources(self):
        self.run_cli("import", str(self.mat))
        self.run_cli("plan", "--on", "2026-09-26")
        audit = self.run_cli("audit")
        actions = {a["action"] for a in audit}
        self.assertIn("import_materials", actions)


if __name__ == "__main__":
    unittest.main()
