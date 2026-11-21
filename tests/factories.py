"""测试辅助：在临时目录构造合成资料包与状态库。

所有数据均为测试内即时合成，与 materials/ 中的演练资料相互独立。
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from reading_app.store import Store

DIMENSIONS = ("knowledge", "understanding", "reflection")


def write_materials(directory: Path, *, people=4, forms=2, items_per_dim=4,
                    approvals=None, withdrawn=None, courses=None,
                    arrangements=None, revisions=None,
                    enrolments=None, schools=None, score_fn=None,
                    absent=None, grade=4) -> Path:
    """生成一份结构符合契约的最小资料包。

    score_fn(person_id, form_id, dimension, item_index) -> (score, missing_reason)
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    schools = schools or [{"id": "C1", "area": "城区", "devices": 10},
                          {"id": "C2", "area": "乡村", "devices": 6}]
    persons = [{
        "id": f"P{i:03d}", "school_id": "C1", "household": f"H{i:03d}",
        "grade": grade, "access_need": "text", "sample_weight": 1.0,
    } for i in range(1, people + 1)]
    if enrolments is None:
        enrolments = [{"id": f"E{i}", "person_id": f"P{i:03d}", "school_id": "C1",
                       "start": "2026-01-01", "end": None}
                      for i in range(1, people + 1)]
    form_rows = [{"id": f"F{i}", "on": f"2026-0{3+i}-10", "grade_low": 3,
                  "grade_high": 6} for i in range(1, forms + 1)]
    items, responses = [], []
    rid = 0
    for fi in range(1, forms + 1):
        for dim in DIMENSIONS:
            for ii in range(1, items_per_dim + 1):
                # 每维度前两题做共同锚题
                anchor = f"A{ DIMENSIONS.index(dim)*2 + (1 if ii <= 2 else 0) }" \
                    if ii <= 2 else None
                if ii <= 2:
                    anchor = {"knowledge": "A3", "understanding": "A1",
                              "reflection": "A5"}[dim]
                item_id = f"F{fi}-{dim}-{ii}"
                max_score = 4 if fi == 1 else 6
                items.append({"id": item_id, "form_id": f"F{fi}", "dimension": dim,
                              "anchor": anchor, "max_score": max_score})
                for pi in range(1, people + 1):
                    rid += 1
                    pid = f"P{pi:03d}"
                    is_absent = absent and pid in absent and f"F{fi}" in absent[pid]
                    if is_absent:
                        score, reason = None, "absent"
                    elif score_fn:
                        score, reason = score_fn(pid, f"F{fi}", dim, ii)
                    else:
                        score, reason = 3, None
                    responses.append({"id": f"R{rid}", "person_id": pid,
                                      "item_id": item_id, "score": score,
                                      "missing_reason": reason,
                                      "received_on": form_rows[fi - 1]["on"]})
    approvals = approvals if approvals is not None else (
        [("L1", "F1", "F2")] if forms >= 2 else [])
    if not approvals:
        # 单测评时无法形成真实联系，放一条自反占位满足契约非空要求
        approvals = [("L0", "F1", "F1")]
    approval_rows = [{
        "id": aid, "left": l, "right": r,
        "scope": "anchor:A1-A6;grade:3-6",
        "approved_on": "2026-04-01",
        "withdrawn_on": "2026-05-01" if withdrawn and aid in withdrawn else None,
    } for aid, l, r in approvals]
    if courses is None:
        courses = [{"id": "K1", "dimension": "understanding", "grade_low": 3,
                    "grade_high": 6, "on": "2026-09-28", "start": "15:00",
                    "end": "16:00", "capacity": 2}]
    if arrangements is None:
        arrangements = []
    # 契约要求每张表非空：无安排时给一条与课程无关的最小记录
    if not arrangements:
        arrangements = [{"id": "B1", "person_id": persons[0]["id"],
                         "course_id": courses[0]["id"],
                         "confirmed_on": "2026-09-20", "attended": False}]
    revisions = revisions or []
    if not revisions:
        # 占位更正：指向首条评分行，分值不变，仅满足契约非空要求
        revisions = [{"id": "V0", "response_id": "R1", "score": 3,
                      "received_on": "2026-12-31", "reason": "占位：无实质更正"}]
    policy = [{"id": f"D{i+1}", "dimension": dim, "min_items": 4,
               "concern_ratio": 0.45, "minimum_observations": 2}
              for i, dim in enumerate(DIMENSIONS)]
    data = {
        "schools": schools, "people": persons, "enrolments": enrolments,
        "forms": form_rows, "items": items, "responses": responses,
        "comparison_approvals": approval_rows, "courses": courses,
        "arrangements": arrangements, "revisions": revisions,
        "observation_policy": policy,
    }
    contract = json.loads(
        (Path(__file__).resolve().parents[1] / "materials" / "contract.json")
        .read_text(encoding="utf-8"))
    (directory / "data.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")
    (directory / "contract.json").write_text(
        json.dumps(contract, ensure_ascii=False), encoding="utf-8")
    return directory


def fresh_store() -> tuple[Store, tempfile.TemporaryDirectory, Path]:
    tmp = tempfile.TemporaryDirectory()
    db_path = Path(tmp.name) / "state.sqlite3"
    return Store(db_path), tmp, Path(tmp.name)
