"""诊断引擎测试：缺考、证据覆盖、不确定性、进步口径与方向相反保留。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reading_app.domain import (  # noqa: E402
    anchor_progress,
    approval_covers,
    build_observations,
    diagnose_student,
    group_summary,
    wilson_interval,
)
from reading_app.models import Difficulty  # noqa: E402
from tests.factories import fresh_store, write_materials  # noqa: E402


class WilsonTests(unittest.TestCase):
    def test_more_items_narrower_interval(self):
        small = wilson_interval(2, 4)
        large = wilson_interval(20, 40)
        self.assertAlmostEqual(small["point"], large["point"])
        self.assertGreater(small["high"] - small["low"],
                           large["high"] - large["low"])

    def test_no_evidence(self):
        self.assertIsNone(wilson_interval(0, 0)["point"])


class AbsenceAndCoverageTests(unittest.TestCase):
    def setUp(self):
        self.store, self.tmp, self.tmpdir = fresh_store()
        self.mat = write_materials(
            self.tmpdir / "m", people=3, forms=3,
            absent={"P001": {"F2"}},  # P001 第二次整卷缺考
        )
        self.store.import_materials(self.mat)

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def test_absence_is_not_zero_and_not_evidence(self):
        obs = build_observations(self.store, "P001")
        f2 = next(o for o in obs["knowledge"] if o.form_id == "F2")
        self.assertEqual(f2.valid_items, 0)
        self.assertEqual(f2.absent_items, 4)
        self.assertIsNone(f2.ratio)
        self.assertTrue(all("缺考" in r for r in f2.missing_reasons))

    def test_absent_student_can_still_be_diagnosed_from_other_forms(self):
        d = diagnose_student(self.store, "P001")
        # 三次里两次有效 -> 不落入证据不足
        self.assertNotEqual(
            d.dimension_verdicts["knowledge"].status,
            Difficulty.INSUFFICIENT)

    def test_blocked_comparison_records_reason(self):
        d = diagnose_student(self.store, "P001")
        blocked = d.dimension_verdicts["knowledge"].comparable_blocked
        self.assertTrue(any(b["left"] == "F1" and b["right"] == "F2" for b in blocked))

    def test_present_student_has_three_observations(self):
        d = diagnose_student(self.store, "P002")
        self.assertEqual(
            len(d.dimension_verdicts["understanding"].observations), 3)


class InsufficientEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.store, self.tmp, self.tmpdir = fresh_store()
        # 只有一次测评 -> 达不到 minimum_observations=2
        self.mat = write_materials(self.tmpdir / "m", people=2, forms=1)
        self.store.import_materials(self.mat)

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def test_single_observation_is_insufficient(self):
        d = diagnose_student(self.store, "P001")
        for v in d.dimension_verdicts.values():
            self.assertEqual(v.status, Difficulty.INSUFFICIENT)
            self.assertTrue(any("少于持续困难所需" in r for r in v.reasons))


class PersistentAndImprovedTests(unittest.TestCase):
    def setUp(self):
        self.store, self.tmp, self.tmpdir = fresh_store()

        def low_then_recover(pid, form, dim, ii):
            # F1/F2 得分率 0.25（持续偏弱），F3 回到 0.75（改善）
            max_score = 4 if form == "F1" else 6
            score = 1 if form in ("F1", "F2") else round(max_score * 0.75)
            return score, None

        self.mat = write_materials(
            self.tmpdir / "m", people=2, forms=3, score_fn=low_then_recover)
        self.store.import_materials(self.mat)

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def test_recovery_marks_improved_not_weak(self):
        d = diagnose_student(self.store, "P001")
        v = d.dimension_verdicts["knowledge"]
        self.assertEqual(v.status, Difficulty.IMPROVED)
        self.assertIn("F1", v.weak_forms); self.assertIn("F2", v.weak_forms)
        self.assertNotIn("F3", v.weak_forms)


class ProgressRequiresApprovalTests(unittest.TestCase):
    def setUp(self):
        self.store, self.tmp, self.tmpdir = fresh_store()
        self.mat = write_materials(
            self.tmpdir / "m", people=2, forms=3,
            approvals=[("L1", "F1", "F2")],  # F2-F3 无批准
        )
        self.store.import_materials(self.mat)

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def test_no_progress_without_approval(self):
        prog = anchor_progress(self.store, "P001", "F2", "F3", 4, "knowledge")
        self.assertIsNone(prog)

    def test_progress_uses_ratio_not_raw_score(self):
        prog = anchor_progress(self.store, "P001", "F1", "F2", 4, "knowledge")
        self.assertIsNotNone(prog)
        self.assertEqual(prog["approval_id"], "L1")
        # 两卷锚题满分不同但得分率口径一致
        self.assertIsInstance(prog["delta"], float)

    def test_withdrawn_approval_blocks(self):
        self.store.set_approval_withdrawn("L1", "2026-04-02", "教研员")
        self.assertIsNone(
            anchor_progress(self.store, "P001", "F1", "F2", 4, "knowledge"))


class DivergenceKeptTests(unittest.TestCase):
    def setUp(self):
        self.store, self.tmp, self.tmpdir = fresh_store()

        def split_scores(pid, form, dim, ii):
            if dim == "knowledge":
                return 4, None          # 知识维度强
            return 1, None              # 理解/反思持续弱

        self.mat = write_materials(
            self.tmpdir / "m", people=2, forms=2, score_fn=split_scores)
        self.store.import_materials(self.mat)

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def test_opposite_directions_are_not_collapsed(self):
        d = diagnose_student(self.store, "P001")
        self.assertIsNotNone(d.divergence)
        self.assertNotEqual(
            d.dimension_verdicts["knowledge"].status,
            d.dimension_verdicts["understanding"].status)


class GroupDenominatorTests(unittest.TestCase):
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
            self.tmpdir / "m", people=3, forms=2, enrolments=enrolments,
            absent={"P002": {"F2"}},
        )
        self.store.import_materials(self.mat)

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def test_transfer_student_counted_once_at_new_school(self):
        # F1 在 2026-04-10，P001 已在 C2
        g = group_summary(self.store, "F1")
        by_school = {r["school_id"]: r for r in g["schools"]}
        self.assertEqual(by_school["C2"]["enrolled_on_day"], 1)
        self.assertEqual(by_school["C1"]["enrolled_on_day"], 2)

    def test_absent_remains_in_denominator(self):
        g = group_summary(self.store, "F2")
        c1 = next(r for r in g["schools"] if r["school_id"] == "C1")
        cell = c1["dimensions"]["knowledge"]
        self.assertEqual(cell["denominator"], 2)   # 在读即入分母
        self.assertEqual(cell["absent"], 1)        # 缺考单列
        self.assertEqual(cell["participated"], 1)


class ApprovalScopeTests(unittest.TestCase):
    def test_anchor_range_and_grade_bounds(self):
        ap = {"scope": "anchor:A1-A6;grade:3-6"}
        self.assertTrue(approval_covers(ap, "A1", 3))
        self.assertTrue(approval_covers(ap, "A6", 6))
        self.assertFalse(approval_covers(ap, "A7", 4))
        self.assertFalse(approval_covers(ap, "A1", 7))

    def test_explicit_anchor_list(self):
        ap = {"scope": "anchor:A1,A3;grade:4-5"}
        self.assertTrue(approval_covers(ap, "A3", 5))
        self.assertFalse(approval_covers(ap, "A2", 5))
        self.assertFalse(approval_covers(ap, "A1", 6))


if __name__ == "__main__":
    unittest.main()
