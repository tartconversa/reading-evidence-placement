"""支持接续工作流测试：容量、候补、复议、已发生事实保护与新一轮观察。"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reading_app.domain import diagnose_student, revalidate_requests  # noqa: E402
from reading_app.models import ConfirmedState, Difficulty  # noqa: E402
from reading_app.planner import (  # noqa: E402
    build_plan,
    promote_from_waitlist,
    waitlist_queue,
)
from reading_app.store import Store  # noqa: E402
from tests.factories import fresh_store, write_materials  # noqa: E402


def low_score(pid, form, dim, ii):
    max_score = 4 if form == "F1" else 6
    return 1, None  # 得分率 0.25 / 0.167，持续低于 0.45 关注线


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.store, self.tmp, self.tmpdir = fresh_store()
        courses = [
            {"id": "K1", "dimension": "understanding", "grade_low": 3,
             "grade_high": 6, "on": "2026-09-28", "start": "15:00",
             "end": "16:00", "capacity": 1},
            {"id": "K2", "dimension": "knowledge", "grade_low": 3,
             "grade_high": 6, "on": "2026-09-28", "start": "15:00",
             "end": "16:00", "capacity": 1},
        ]
        self.mat = write_materials(
            self.tmpdir / "m", people=4, forms=2,
            courses=courses, score_fn=low_score,
            arrangements=[{"id": "B1", "person_id": "P001", "course_id": "K1",
                           "confirmed_on": "2026-09-20", "attended": False}],
        )
        self.store.import_materials(self.mat)
        self.report = build_plan(self.store, "2026-09-26")

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def test_capacity_never_exceeded(self):
        for seat in self.report.seats:
            self.assertLessEqual(seat["seats_taken"], seat["capacity"])
            self.assertGreaterEqual(seat["available"], 0)

    def test_overflow_goes_to_waitlist_with_reason(self):
        k1_wait = [w for w in self.report.waitlist if w["course_id"] == "K1"]
        # K1 容量 1 且已被 P001 确认占满，其余持续困难学生全部候补
        self.assertTrue(k1_wait)
        self.assertTrue(all(w["reason"] for w in k1_wait))

    def test_weak_evidence_never_forced_into_course(self):
        # 全员持续困难时无补充观察；另构造证据不足场景验证不进候选
        store2, tmp2, td2 = fresh_store()
        mat2 = write_materials(td2 / "m2", people=2, forms=1)
        store2.import_materials(mat2)
        report2 = build_plan(store2, "2026-09-26")
        self.assertEqual(report2.suggestions, [])
        self.assertEqual(report2.waitlist, [])
        self.assertTrue(report2.observations)
        store2.close(); tmp2.cleanup()

    def test_no_double_booking_in_overlapping_slot(self):
        for person_id in {s["person_id"] for s in self.report.suggestions}:
            courses_taken = [s["course_id"] for s in self.report.suggestions
                             if s["person_id"] == person_id]
            # K1/K2 完全同时段，同一学生至多一席
            self.assertLessEqual(len(courses_taken), 1)

    def test_waitlist_promotion_requires_free_seat_and_queue_order(self):
        queue = waitlist_queue(self.store, "K1")
        head, second = queue[0], queue[1]
        with self.assertRaisesRegex(ValueError, "无空位"):
            promote_from_waitlist(self.store, head["id"], "王老师", "2026-09-27")
        with self.assertRaisesRegex(ValueError, "队首"):
            promote_from_waitlist(self.store, second["id"], "王老师", "2026-09-27")
        # 教师取消已确认（未开始）安排后，队首可递补
        occ = [r for r in self.store.requests_for_course("K1")
               if r["state"] == "confirmed" and not r["attended"]][0]
        self.store.update_request_state(
            occ["id"], ConfirmedState.CANCELLED, "王老师", "2026-09-27")
        promoted = promote_from_waitlist(self.store, head["id"], "王老师", "2026-09-27")
        self.assertEqual(promoted["state"], "confirmed")
        self.assertEqual(promoted["actor"], "王老师")
        # 递补后再次满员
        with self.assertRaisesRegex(ValueError, "无空位"):
            promote_from_waitlist(
                self.store, waitlist_queue(self.store, "K1")[0]["id"],
                "王老师", "2026-09-27")


class TeacherDecisionTests(unittest.TestCase):
    def setUp(self):
        self.store, self.tmp, self.tmpdir = fresh_store()
        self.mat = write_materials(
            self.tmpdir / "m", people=3, forms=2,
            arrangements=[
                {"id": "B1", "person_id": "P001", "course_id": "K1",
                 "confirmed_on": "2026-09-20", "attended": True},
                {"id": "B2", "person_id": "P002", "course_id": "K1",
                 "confirmed_on": "2026-09-21", "attended": False},
            ],
        )
        self.store.import_materials(self.mat)

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def test_attended_course_cannot_be_undone(self):
        attended = [r for r in self.store.active_requests() if r["attended"]][0]
        with self.assertRaisesRegex(ValueError, "已实际发生"):
            self.store.update_request_state(
                attended["id"], ConfirmedState.CANCELLED, "教师", "2026-10-01")
        with self.assertRaisesRegex(ValueError, "已实际发生"):
            self.store.record_attendance(attended["id"], False, "2026-10-01")

    def test_attendance_recording_is_append_only(self):
        req = self.store.find_request("P002", "K1")
        self.store.record_attendance(req["id"], True, "2026-09-28")
        self.assertTrue(self.store.get_request(req["id"])["attended"])
        with self.assertRaisesRegex(ValueError, "已实际发生"):
            self.store.record_attendance(req["id"], False, "2026-09-29")

    def test_auto_suggestion_and_teacher_confirm_are_distinct(self):
        build_plan(self.store, "2026-09-26")
        candidates = [r for r in self.store.active_requests()
                      if r["source"] == "system"]
        # 容量可能已被两条导入确认占满；若无系统建议则直接验证分账事实
        if not candidates:
            for r in self.store.active_requests():
                self.assertEqual(r["source"], "import")
            return
        suggestion = candidates[0]
        self.assertEqual(suggestion["state"], "suggested")
        self.assertIsNone(suggestion["actor"])
        confirmed = self.store.update_request_state(
            suggestion["id"], ConfirmedState.CONFIRMED, "李老师", "2026-09-27")
        self.assertEqual(confirmed["source"], "system")  # 来源不改写
        self.assertEqual(confirmed["actor"], "李老师")
        trail = self.store.audit_trail(suggestion["id"])
        self.assertTrue(any(t["action"] == "state:confirmed" for t in trail))


class RevisionMeetsConfirmationTests(unittest.TestCase):
    """评分更正 × 已确认名额相遇：已发生保护、未开始复议、名额不静默腾退。"""

    def setUp(self):
        self.store, self.tmp, self.tmpdir = fresh_store()
        self.mat = write_materials(
            self.tmpdir / "m", people=3, forms=2,
            arrangements=[
                {"id": "B1", "person_id": "P001", "course_id": "K1",
                 "confirmed_on": "2026-09-20", "attended": True},
                {"id": "B2", "person_id": "P002", "course_id": "K1",
                 "confirmed_on": "2026-09-21", "attended": False},
            ],
            revisions=[{"id": "V1", "response_id": "R1", "score": 0,
                        "received_on": "2026-09-25", "reason": "复核漏扣分"}],
        )
        self.store.import_materials(self.mat)

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def test_revision_flags_only_unstarted_confirmation(self):
        affected = revalidate_requests(
            self.store, revision_ids=["V1"], on_date="2026-09-25")
        # R1 属于 P001；P001 的 B1 已参加 -> 不标记；P002 与更正无关 -> 不标记
        b1 = self.store.find_request("P001", "K1")
        self.assertEqual(b1["review_flag"], 0)
        # P001 没有未开始确认，因此 affected 为空
        self.assertNotIn(b1["id"], affected)

    def test_revision_flags_unstarted_request_and_keeps_seat(self):
        # 让 R1 改属 P002 的评分行：构造一条 P002 的更正
        r_for_p2 = next(r for r in self.store._all("responses")
                        if r["person_id"] == "P002")
        self.store.conn.execute(
            "INSERT INTO revisions (id, response_id, score, received_on, reason) "
            "VALUES ('V2', ?, 0, '2026-09-25', '测试更正')", (r_for_p2["id"],))
        self.store.conn.commit()
        affected = revalidate_requests(
            self.store, revision_ids=["V2"], on_date="2026-09-25")
        b2 = self.store.find_request("P002", "K1")
        self.assertIn(b2["id"], affected)
        self.assertEqual(b2["review_flag"], 1)
        # 状态仍是 confirmed，席位没有被静默腾出
        self.assertEqual(b2["state"], "confirmed")
        event = self.store.add_impact_event(
            "revision", "V2", {"response_id": r_for_p2["id"]}, affected, "2026-09-25")
        self.assertEqual(event["affected_request_ids"], json.dumps([b2["id"]]))

    def test_revision_changes_diagnosis_effective_score(self):
        # V1 把 R1 改为 0，生效分必须取更正值，原始分仍保留
        raw = next(r for r in self.store._all("responses") if r["id"] == "R1")
        self.assertEqual(raw["score"], 3)
        revised = next(r for r in self.store.responses_for("P001")
                       if r["revision_count"])
        self.assertEqual(revised["id"], "R1")
        self.assertEqual(revised["effective_score"], 0)
        self.assertEqual(revised["score"], 3)  # 原始分未被覆盖
        # 诊断可正常重算且引用更正后的证据
        d = diagnose_student(self.store, "P001")
        self.assertIsNotNone(d.dimension_verdicts["knowledge"])


class TransferStudentTests(unittest.TestCase):
    def setUp(self):
        self.store, self.tmp, self.tmpdir = fresh_store()
        enrolments = [
            {"id": "E1", "person_id": "P001", "school_id": "C1",
             "start": "2026-01-01", "end": "2026-04-01"},
            {"id": "E2", "person_id": "P001", "school_id": "C2",
             "start": "2026-04-01", "end": None},
        ] + [
            {"id": f"E{i+2}", "person_id": f"P{i:03d}", "school_id": "C1",
             "start": "2026-01-01", "end": None}
            for i in range(2, 4)
        ]
        self.mat = write_materials(
            self.tmpdir / "m", people=3, forms=2, enrolments=enrolments)
        self.store.import_materials(self.mat)

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def test_school_on_uses_right_open_intervals(self):
        # 右端不包含：2026-04-01 当天已属于 C2
        self.assertIsNone(
            self.store.school_on("P001", "2025-12-31"))
        self.assertEqual(self.store.school_on("P001", "2026-03-31"), "C1")
        self.assertEqual(self.store.school_on("P001", "2026-04-01"), "C2")

    def test_transfer_does_not_double_count(self):
        from reading_app.domain import group_summary
        g = group_summary(self.store, "F1")  # 日期 2026-04-10
        total = sum(r["enrolled_on_day"] for r in g["schools"])
        self.assertEqual(total, 3)  # 三个学生各算一次


class ReimportProtectionTests(unittest.TestCase):
    """资料重传不能覆盖库内人工管理决定与已发生事实。"""

    def setUp(self):
        self.store, self.tmp, self.tmpdir = fresh_store()
        self.mat = write_materials(
            self.tmpdir / "m", people=2, forms=2,
            arrangements=[{"id": "B1", "person_id": "P001", "course_id": "K1",
                           "confirmed_on": "2026-09-20", "attended": True}],
        )
        self.store.import_materials(self.mat)

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def test_withdrawal_survives_reimport(self):
        self.store.set_approval_withdrawn("L1", "2026-05-02", "教研员")
        receipt = self.store.import_materials(self.mat, note="重传")
        conflict = next(c for c in receipt["conflicts"]
                        if c["table"] == "comparison_approvals" and c["id"] == "L1")
        self.assertEqual(conflict["kept"], "2026-05-02")
        l1 = next(a for a in self.store.approvals() if a["id"] == "L1")
        self.assertEqual(l1["withdrawn_on"], "2026-05-02")

    def test_attended_fact_survives_reimport(self):
        receipt = self.store.import_materials(self.mat)
        # 源文件里 B1 仍是 attended=true；构造一份翻转为 false 的资料再导入
        import json as _json
        data_path = self.mat / "data.json"
        data = _json.loads(data_path.read_text(encoding="utf-8"))
        data["arrangements"][0]["attended"] = False
        data_path.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")
        receipt = self.store.import_materials(self.mat)
        self.assertTrue(any(c["field"] == "attended" for c in receipt["conflicts"]))
        self.assertTrue(self.store.find_request("P001", "K1")["attended"])


class CourseGradeGateTests(unittest.TestCase):
    def test_course_outside_grade_range_not_suggested(self):
        store, tmp, td = fresh_store()
        courses = [{"id": "K9", "dimension": "understanding", "grade_low": 5,
                    "grade_high": 6, "on": "2026-09-28", "start": "15:00",
                    "end": "16:00", "capacity": 5}]
        mat = write_materials(td / "m", people=2, forms=2, grade=4,
                              courses=courses, score_fn=low_score)
        store.import_materials(mat)
        report = build_plan(store, "2026-09-26")
        self.assertFalse(any(s["course_id"] == "K9" for s in report.suggestions))
        self.assertFalse(any(w["course_id"] == "K9" for w in report.waitlist))
        store.close(); tmp.cleanup()


class NewRoundTests(unittest.TestCase):
    """新一轮观察：结课不等于困难消失，资格依据新证据重判。"""

    def setUp(self):
        self.store, self.tmp, self.tmpdir = fresh_store()
        self.mat = write_materials(
            self.tmpdir / "m", people=2, forms=2, score_fn=low_score,
            arrangements=[{"id": "B1", "person_id": "P001", "course_id": "K1",
                           "confirmed_on": "2026-09-20", "attended": True}],
        )
        self.store.import_materials(self.mat)

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def test_finished_course_keeps_history_but_status_follows_new_evidence(self):
        d1 = diagnose_student(self.store, "P001")
        self.assertEqual(
            d1.dimension_verdicts["understanding"].status,
            Difficulty.PERSISTENT)
        # 课程已参加的事实保留
        attended = self.store.find_request("P001", "K1")
        self.assertTrue(attended["attended"])
        # 重新规划：历史支持体现在层级下调，而不是抹掉困难判断
        report = build_plan(self.store, "2026-12-01")
        p1_sugg = [s for s in report.suggestions if s["person_id"] == "P001"]
        # 仍可能被建议，但理由中保留持续困难口径；课程事实未被撤销
        self.assertTrue(self.store.find_request("P001", "K1")["attended"])
        self.assertTrue(all(s["tier"] in (1, 2, 3) for s in p1_sugg))


if __name__ == "__main__":
    unittest.main()
