"""支持规划：建议生成、容量裁决、候补与补充观察。

自动建议（system 来源）与教师确认（teacher/import 来源）分账管理；
候补是系统在容量不足时的有理由排序，不等于教师确认。
"""
from __future__ import annotations

from dataclasses import dataclass

from .domain import diagnose_student
from .models import (
    DIMENSIONS,
    DIMENSION_CN,
    ConfirmedState,
    Difficulty,
)

# 优先级层：数字越小越优先。层间体现“持续困难 vs 已获得支持”的差别。
TIER_PERSISTENT = 1      # 持续困难，且本时段尚未获得同维度支持
TIER_WEAK = 2            # 当前偏弱（含改善前仍有缺口的单次偏弱）
TIER_PERSISTENT_SUPPORTED = 3  # 持续困难但已有同维度在身支持
TIER_IMPROVED = 4        # 已有改善证据，仅在有名额时接续

PLACEMENT_STATUSES = {
    Difficulty.PERSISTENT, Difficulty.WEAK, Difficulty.IMPROVED,
}
SUPP_OBS_STATUSES = {Difficulty.INSUFFICIENT}


@dataclass
class PlanningReport:
    suggestions: list[dict]
    waitlist: list[dict]
    observations: list[dict]
    blockers: list[dict]
    seats: list[dict]
    open_observations_total: int = 0


def _has_same_dimension_support(store, person_id: str, dimension: str) -> bool:
    """该学生是否已有同维度、占席位的安排（含已参加的历史课程）。"""
    for req in store.requests_for_person(person_id):
        if req["state"] not in (ConfirmedState.CONFIRMED.value,
                                ConfirmedState.ADJUSTED.value):
            continue
        course = store.course(req["course_id"])
        if course and course["dimension"] == dimension:
            return True
    return False


def _student_requests(store, person_id: str) -> list[dict]:
    return store.requests_for_person(person_id)


def build_plan(store, on_date: str) -> PlanningReport:
    """全量重算自动建议与候补；教师已决定的记录原样保留。

    证据不足的学生只产生补充观察，不会为填课程被归为低能力。
    """
    # 清理上一轮纯系统记录（建议与候补），教师/导入记录不动
    store.conn.execute(
        "DELETE FROM seat_requests WHERE source='system' AND state IN (?,?)",
        (ConfirmedState.SUGGESTED.value, ConfirmedState.WAITLISTED.value),
    )

    suggestions, waitlist, observations, blockers = [], [], [], []
    people = store.people()
    courses = store.courses()
    course_by_id = {c["id"]: c for c in courses}

    # 候选：(tier, person_id, course_id, 理由)
    candidates: list[tuple[int, str, str, str]] = []
    for person in people:
        pid = person["id"]
        diagnosis = diagnose_student(store, pid)
        existing = _student_requests(store, pid)
        decided_courses = {r["course_id"] for r in existing
                          if r["state"] in (ConfirmedState.CONFIRMED.value,
                                            ConfirmedState.ADJUSTED.value,
                                            ConfirmedState.DEFERRED.value)}
        for dim in DIMENSIONS:
            verdict = diagnosis.dimension_verdicts[dim]
            if verdict.status in SUPP_OBS_STATUSES:
                # 证据弱：先补充观察，不进入课程候选
                open_obs = [o for o in store.list_observations(pid)
                            if o["dimension"] == dim and o["status"] == "open"]
                if not open_obs:
                    reason = (f"{DIMENSION_CN[dim]}证据不足（"
                              + "；".join(verdict.reasons) + "），先安排补充观察")
                    task = store.add_observation_task(pid, dim, reason, on_date)
                    observations.append(task)
                continue
            if verdict.status not in PLACEMENT_STATUSES:
                continue
            supported = _has_same_dimension_support(store, pid, dim)
            # 适配该维度、学段且未被教师处理过的课程
            for course in courses:
                if course["dimension"] != dim:
                    continue
                if not (course["grade_low"] <= person["grade"] <= course["grade_high"]):
                    continue
                if course["id"] in decided_courses:
                    continue
                if verdict.status == Difficulty.PERSISTENT:
                    tier = TIER_PERSISTENT_SUPPORTED if supported else TIER_PERSISTENT
                elif verdict.status == Difficulty.WEAK:
                    tier = TIER_WEAK
                else:
                    tier = TIER_IMPROVED
                reason = (
                    f"{DIMENSION_CN[dim]}判断为{verdict.status.cn}："
                    + "；".join(verdict.reasons)
                )
                candidates.append((tier, pid, course["id"], reason))

    # 同学生重叠时段只保留最高优先候选，其余记入 blockers
    candidates.sort(key=lambda x: (x[0], x[1], x[2]))
    chosen: list[tuple[int, str, str, str]] = []
    seen_student_courses: dict[str, list[dict]] = {}
    for tier, pid, cid, reason in candidates:
        course = course_by_id[cid]
        clash = None
        for kept_course in seen_student_courses.get(pid, []):
            if kept_course["on"] == course["on"] and \
                    kept_course["start"] < course["end"] and \
                    course["start"] < kept_course["end"]:
                clash = kept_course
                break
        if clash:
            blockers.append({
                "person_id": pid, "course_id": cid,
                "reason": f"与已选课程 {clash['id']} 时段重叠，同一学生不能占两席",
            })
            continue
        chosen.append((tier, pid, cid, reason))
        seen_student_courses.setdefault(pid, []).append(course)

    # 容量裁决：按优先级层、层内按稳定编号排序
    chosen.sort(key=lambda x: (x[0], x[1], x[2]))
    occupied = {c["id"]: store.occupied_seats(c["id"]) for c in courses}
    waitlist_counter = {c["id"]: 0
                        for c in courses}
    for tier, pid, cid, reason in chosen:
        course = course_by_id[cid]
        # 已确认的重叠席位（教师侧）再兜一次底
        clash = store.overlapping_confirmed(pid, course)
        if clash is not None:
            blockers.append({
                "person_id": pid, "course_id": cid,
                "reason": f"与教师已确认课程 {clash['course_id']} 时段重叠",
            })
            continue
        if occupied[cid] < course["capacity"]:
            req = store.add_suggestion(pid, cid, reason, on_date)
            occupied[cid] += 1
            suggestions.append({
                "request_id": req["id"], "person_id": pid, "course_id": cid,
                "tier": tier, "reason": reason,
            })
        else:
            waitlist_counter[cid] += 1
            position = waitlist_counter[cid]
            cur = store.conn.execute(
                "INSERT INTO seat_requests (person_id, course_id, state, source, "
                "suggested_reason, attended, review_flag, waitlist_position, created_on) "
                "VALUES (?,?,?,?,?,0,0,?,?)",
                (pid, cid, ConfirmedState.WAITLISTED.value, "system", reason, position, on_date),
            )
            waitlist.append({
                "request_id": cur.lastrowid, "person_id": pid, "course_id": cid,
                "tier": tier, "position": position,
                "reason": _waitlist_reason(tier, course),
            })
    store.conn.commit()

    seats = []
    for c in courses:
        confirmed = store.occupied_seats(c["id"])
        taken = _seats_taken(store, c["id"])
        seats.append({
            "course_id": c["id"], "capacity": c["capacity"],
            "confirmed_occupied": confirmed,
            "suggested_locked": taken - confirmed,
            "seats_taken": taken,
            "available": c["capacity"] - taken,
        })
    open_total = store.conn.execute(
        "SELECT COUNT(*) n FROM supp_observations WHERE status='open'").fetchone()["n"]
    return PlanningReport(suggestions, waitlist, observations, blockers, seats,
                          open_observations_total=open_total)


def _waitlist_reason(tier: int, course: dict) -> str:
    base = {
        TIER_PERSISTENT: "持续困难，容量不足进入候补，优先于单次偏弱",
        TIER_WEAK: "当前偏弱，容量不足进入候补",
        TIER_PERSISTENT_SUPPORTED: "持续困难但已有同维度支持，让位于尚未获得支持者",
        TIER_IMPROVED: "已有改善证据，容量不足时排在最后",
    }[tier]
    return f"{DIMENSION_CN[course['dimension']]}课程 {course['id']}：{base}"


def _seats_taken(store, course_id: str) -> int:
    """已占席位：教师确认/调整确认 + 尚未被教师处理的系统建议。

    候补与暂缓不占席；建议虽非教师确认，但在本轮裁决中已锁定名额，
    否则候补递补会绕过容量上限。
    """
    row = store.conn.execute(
        "SELECT COUNT(*) n FROM seat_requests WHERE course_id=? AND state IN (?,?,?)",
        (course_id, ConfirmedState.CONFIRMED.value, ConfirmedState.ADJUSTED.value,
         ConfirmedState.SUGGESTED.value),
    ).fetchone()
    return row["n"]


def waitlist_queue(store, course_id: str) -> list[dict]:
    """某课程的候补队列，按持久化候补序号。"""
    return [dict(r) for r in store.conn.execute(
        "SELECT * FROM seat_requests WHERE course_id=? AND state=? "
        "ORDER BY waitlist_position, id",
        (course_id, ConfirmedState.WAITLISTED.value))]


def promote_from_waitlist(store, request_id: int, actor: str, on_date: str) -> dict:
    """教师复议后腾出名额时递补；只允许队首递补，容量与重叠仍强校验。"""
    req = store.get_request(request_id)
    if req is None:
        raise KeyError(f"候补记录不存在：{request_id}")
    if req["state"] != ConfirmedState.WAITLISTED.value:
        raise ValueError("该记录不是候补状态，不能递补")
    queue = waitlist_queue(store, req["course_id"])
    if queue and queue[0]["id"] != request_id:
        raise ValueError(
            f"该候补不是队首（队首为请求 {queue[0]['id']}，"
            f"学生 {queue[0]['person_id']}），不能跳过队首递补")
    course = store.course(req["course_id"])
    if _seats_taken(store, course["id"]) >= course["capacity"]:
        raise ValueError(f"课程 {course['id']} 仍无空位（建议与确认均占席），不能递补")
    clash = store.overlapping_confirmed(req["person_id"], course)
    if clash is not None:
        raise ValueError(f"与已确认课程 {clash['course_id']} 时段重叠，不能递补")
    return store.update_request_state(
        request_id, ConfirmedState.CONFIRMED, actor, on_date,
        "候补递补的教师确认")


def report_to_dict(report: PlanningReport) -> dict:
    return {
        "suggestions": report.suggestions,
        "waitlist": report.waitlist,
        "observations_new": report.observations,
        "open_observations_total": report.open_observations_total,
        "blockers": report.blockers,
        "seats": report.seats,
    }
