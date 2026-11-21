"""本地命令行入口：导入、批准管理、诊断、群体口径、席位与回录。

约定：
  * 状态库默认写入 state/reading.sqlite3，原始 materials/ 永不改写；
  * 自动建议（system）与教师确认（teacher/import）在输出中分别标注；
  * 所有写操作记录 actor 与日期，进入 decision_audit。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from .domain import (
    DIMENSION_CN,
    DIMENSIONS,
    diagnosis_to_dict,
    group_summary,
    revalidate_requests,
)
from .models import ConfirmedState
from .planner import _seats_taken, build_plan, promote_from_waitlist, report_to_dict
from .store import Store

DEFAULT_DB = Path("state/reading.sqlite3")


# ----------------------------------------------------------------- 输出工具
def emit(payload, as_json: bool):
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return
    if isinstance(payload, str):
        print(payload)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def cn_state(state: str) -> str:
    try:
        return ConfirmedState(state).cn
    except ValueError:
        return state


# ----------------------------------------------------------------- 各子命令
def cmd_import(store: Store, args) -> dict:
    return store.import_materials(args.materials, note=args.note, actor=args.actor)


def cmd_batches(store: Store, args) -> list[dict]:
    return store.list_batches()


def cmd_approval_list(store: Store, args) -> list[dict]:
    return store.approvals(include_withdrawn=args.all)


def cmd_approval_withdraw(store: Store, args) -> dict:
    affected = revalidate_requests(store, approval_id=args.approval_id,
                                   on_date=args.on)
    approval = store.set_approval_withdrawn(args.approval_id, args.on, args.actor)
    detail = {"approval_id": args.approval_id, "scope": approval["scope"]}
    event = store.add_impact_event(
        "approval_withdrawn", args.approval_id, detail, affected, args.on)
    return {"approval": approval,
            "review_flagged_requests": affected,
            "impact_event_id": event["id"],
            "note": "已确认未开始的安排仅提示复议，名额未被静默腾出；已参加课程不受影响"}


def cmd_diagnose(store: Store, args) -> dict:
    from .domain import diagnose_student
    d = diagnose_student(store, args.person)
    payload = diagnosis_to_dict(d)
    payload["requests"] = [
        {k: r[k] for k in ("id", "course_id", "state", "source", "review_flag",
                           "attended", "suggested_reason")}
        for r in store.requests_for_person(args.person)
    ]
    return payload


def cmd_group(store: Store, args) -> dict:
    return group_summary(store, args.form)


def cmd_plan(store: Store, args) -> dict:
    report = build_plan(store, args.on)
    return report_to_dict(report)


def _resolve_request(store, key: str) -> int:
    if key.isdigit():
        return int(key)
    pid, _, cid = key.partition(":")
    if not cid:
        raise SystemExit("安排定位需用请求编号或 学生编号:课程编号")
    req = store.find_request(pid, cid)
    if req is None:
        raise SystemExit(f"找不到安排 {key}")
    return req["id"]


def _teacher_decision(store, args, state: ConfirmedState) -> dict:
    request_id = _resolve_request(store, args.request)
    req = store.get_request(request_id)
    course = store.course(req["course_id"])
    if state in (ConfirmedState.CONFIRMED, ConfirmedState.ADJUSTED):
        if req["attended"]:
            raise SystemExit("该课程已实际参加，无需再确认")
        clash = store.overlapping_confirmed(req["person_id"], course)
        if clash is not None and clash["id"] != request_id:
            raise SystemExit(
                f"时段冲突：学生 {req['person_id']} 已确认课程 "
                f"{clash['course_id']}（{clash['c_on']} {clash['c_start']}-{clash['c_end']}），"
                "同一重叠时段不能占两席")
        # 当前请求若已是建议/确认，本身已占一席；容量检查只针对“新增占席”
        already_holding = req["state"] in (
            ConfirmedState.CONFIRMED.value, ConfirmedState.ADJUSTED.value,
            ConfirmedState.SUGGESTED.value)
        if not already_holding and \
                _seats_taken(store, course["id"]) >= course["capacity"]:
            raise SystemExit(f"课程 {course['id']} 名额已满（建议与确认均占席），请走候补递补流程")
    updated = store.update_request_state(request_id, state, args.actor,
                                         args.on, args.note)
    store.clear_review(request_id)
    return {"request": updated, "note": "教师决定已与自动建议分账记录"}


def cmd_accept(store, args) -> dict:
    return _teacher_decision(store, args, ConfirmedState.CONFIRMED)


def cmd_adjust(store, args) -> dict:
    return _teacher_decision(store, args, ConfirmedState.ADJUSTED)


def cmd_defer(store, args) -> dict:
    return _teacher_decision(store, args, ConfirmedState.DEFERRED)


def cmd_cancel(store, args) -> dict:
    request_id = _resolve_request(store, args.request)
    req = store.get_request(request_id)
    if req["attended"]:
        raise SystemExit("课程已实际发生，不能撤销成未发生；只能保留事实并记录后续说明")
    updated = store.update_request_state(request_id, ConfirmedState.CANCELLED,
                                         args.actor, args.on, args.note)
    store.clear_review(request_id)
    return {"request": updated, "note": "撤销仅限尚未开始的安排；如腾出名额请用 promote 递补候补"}


def cmd_promote(store, args) -> dict:
    request_id = _resolve_request(store, args.request)
    return {"request": promote_from_waitlist(store, request_id, args.actor, args.on)}


def cmd_requests(store, args) -> list[dict]:
    rows = store.active_requests()
    if args.person:
        rows = [r for r in rows if r["person_id"] == args.person]
    if args.course:
        rows = [r for r in rows if r["course_id"] == args.course]
    return rows


def cmd_attend(store, args) -> dict:
    request_id = _resolve_request(store, args.request)
    return {"request": store.record_attendance(
        request_id, args.attended, args.on, args.actor)}


def cmd_observations(store, args) -> list[dict]:
    return store.list_observations(args.person)


def cmd_observation_complete(store, args) -> dict:
    return store.complete_observation(args.id, args.on, args.note)


def cmd_impacts(store, args) -> list[dict]:
    return store.list_impact_events(include_acknowledged=args.all)


def cmd_impact_ack(store, args) -> dict:
    return store.acknowledge_event(args.id)


def cmd_audit(store, args) -> list[dict]:
    request_id = _resolve_request(store, args.request) if args.request else None
    return store.audit_trail(request_id)


def cmd_explain(store, args) -> dict:
    """追问一次建议：采用了哪些测评、批准与当时口径。"""
    request_id = _resolve_request(store, args.request)
    req = store.get_request(request_id)
    course = store.course(req["course_id"])
    from .domain import diagnose_student
    diagnosis = diagnosis_to_dict(diagnose_student(store, req["person_id"]))
    dim_block = diagnosis["dimensions"][course["dimension"]]
    return {
        "request_id": request_id,
        "person_id": req["person_id"],
        "course_id": req["course_id"],
        "state": req["state"],
        "state_cn": cn_state(req["state"]),
        "source": req["source"],
        "suggested_reason": req["suggested_reason"],
        "dimension": course["dimension"],
        "dimension_cn": DIMENSION_CN[course["dimension"]],
        "evidence_used": dim_block,
        "note": "自动建议来自当前证据重算；教师确认历史见 audit 子命令",
    }


# ----------------------------------------------------------------- 装配
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reading-app",
        description="阅读素养诊断与支持接续（校内演练口径，非国家标准）")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 状态库路径")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("import", help="校验并整包导入资料目录")
    p.add_argument("materials", help="资料目录（含 data.json/contract.json）")
    p.add_argument("--note"); p.add_argument("--actor", default="教研员")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("batches", help="查看导入批次与指纹")
    p.set_defaults(func=cmd_batches)

    p = sub.add_parser("approvals", help="列出比较批准")
    p.add_argument("--all", action="store_true", help="含已撤回")
    p.set_defaults(func=cmd_approval_list)

    p = sub.add_parser("withdraw-approval", help="撤回比较联系并重判受影响安排")
    p.add_argument("approval_id"); p.add_argument("--on", default=date.today().isoformat())
    p.add_argument("--actor", default="教研员")
    p.set_defaults(func=cmd_approval_withdraw)

    p = sub.add_parser("diagnose", help="查看学生分维度诊断与证据")
    p.add_argument("person")
    p.set_defaults(func=cmd_diagnose)

    p = sub.add_parser("group", help="某测评按当日在读构成的群体口径")
    p.add_argument("form")
    p.set_defaults(func=cmd_group)

    p = sub.add_parser("plan", help="重算自动建议、候补与补充观察")
    p.add_argument("--on", default=date.today().isoformat())
    p.set_defaults(func=cmd_plan)

    def add_decision(name, func, help_text):
        q = sub.add_parser(name, help=help_text)
        q.add_argument("request", help="请求编号，或 学生编号:课程编号")
        q.add_argument("--actor", default="教师")
        q.add_argument("--on", default=date.today().isoformat())
        q.add_argument("--note")
        q.set_defaults(func=func)
        return q

    add_decision("accept", cmd_accept, "接受建议并确认名额")
    add_decision("adjust", cmd_adjust, "调整后确认（仍记为教师决定）")
    add_decision("defer", cmd_defer, "暂缓建议")
    add_decision("cancel", cmd_cancel, "撤销尚未开始的安排")

    p = sub.add_parser("promote", help="名额腾出后递补候补")
    p.add_argument("request"); p.add_argument("--actor", default="教师")
    p.add_argument("--on", default=date.today().isoformat())
    p.set_defaults(func=cmd_promote)

    p = sub.add_parser("requests", help="查看席位台账")
    p.add_argument("--person"); p.add_argument("--course")
    p.set_defaults(func=cmd_requests)

    p = sub.add_parser("attend", help="回录课程实际参加情况")
    p.add_argument("request")
    p.add_argument("--attended", dest="attended", action="store_true", default=True)
    p.add_argument("--absent", dest="attended", action="store_false")
    p.add_argument("--on", default=date.today().isoformat())
    p.add_argument("--actor", default="教师")
    p.set_defaults(func=cmd_attend)

    p = sub.add_parser("observations", help="查看补充观察任务")
    p.add_argument("--person")
    p.set_defaults(func=cmd_observations)

    p = sub.add_parser("complete-observation", help="回录补充观察已完成")
    p.add_argument("id", type=int); p.add_argument("--on", default=date.today().isoformat())
    p.add_argument("--note")
    p.set_defaults(func=cmd_observation_complete)

    p = sub.add_parser("impacts", help="查看更正/撤回对在办安排的影响")
    p.add_argument("--all", action="store_true", help="含已知悉事件")
    p.set_defaults(func=cmd_impacts)

    p = sub.add_parser("ack-impact", help="标记影响事件已知悉")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_impact_ack)

    p = sub.add_parser("audit", help="操作审计轨迹")
    p.add_argument("--request", help="可选：限定某个安排")
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("explain", help="追问建议采用的测评、批准与口径")
    p.add_argument("request")
    p.set_defaults(func=cmd_explain)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    store = Store(args.db)
    try:
        payload = args.func(store, args)
        emit(payload, args.json)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
