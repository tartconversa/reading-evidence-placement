"""诊断引擎：证据覆盖、测量不确定性、分维度判断与群体口径。

本层只做计算，不落库、不产生教师动作。所有阈值取自 observation_policy
（校内演练口径），结果自带依据，可逐条追问。
"""
from __future__ import annotations

import math
from collections import defaultdict

from .models import (
    DIMENSIONS,
    DIMENSION_CN,
    ConfirmedState,
    Difficulty,
    DimensionVerdict,
    ObservationScore,
    StudentDiagnosis,
)

# Wilson 95% 分位数（双侧 z=1.96）
Z_95 = 1.96


# --------------------------------------------------------------- 测量不确定性
def wilson_interval(earned: float, available: float) -> dict:
    """把维度得分率视为二项比例，用 Wilson 区间表达测量不确定性。

    以原始分折算为“加权试验”（available 为满分、earned 为得分），
    题量越小区间越宽；区间用于提示可信度，不改变关注线判断本身。
    """
    if available <= 0:
        return {"point": None, "low": None, "high": None, "n": 0, "method": "wilson95"}
    n = max(available, 1e-9)
    phat = earned / n
    z2 = Z_95 * Z_95
    denom = 1 + z2 / n
    center = (phat + z2 / (2 * n)) / denom
    margin = Z_95 * math.sqrt((phat * (1 - phat) + z2 / (4 * n)) / n) / denom
    return {
        "point": round(phat, 4),
        "low": round(max(0.0, center - margin), 4),
        "high": round(min(1.0, center + margin), 4),
        "n": round(n, 2),
        "method": "wilson95",
    }


# --------------------------------------------------------------- 观察汇总
def build_observations(store, person_id: str) -> dict[str, list[ObservationScore]]:
    """按 测评×维度 汇总有效作答；缺考不计零，单列缺失原因。"""
    items = {i["id"]: i for i in store.items()}
    forms = {f["id"]: f for f in store.forms()}
    rows = store.responses_for(person_id)
    # (form, dimension) -> 聚合
    bucket: dict[tuple[str, str], dict] = {}
    for r in rows:
        item = items[r["item_id"]]
        key = (item["form_id"], item["dimension"])
        b = bucket.setdefault(key, {"earned": 0.0, "available": 0.0,
                                    "valid": 0, "max_items": 0,
                                    "absent": 0, "reasons": []})
        b["max_items"] += 1
        if r["effective_score"] is None:
            if r["missing_reason"] == "absent":
                b["absent"] += 1
                b["reasons"].append(f"{item['id']}:缺考")
            else:
                b["reasons"].append(f"{item['id']}:评分缺失")
            continue
        b["valid"] += 1
        b["earned"] += float(r["effective_score"])
        b["available"] += float(item["max_score"])
    out: dict[str, list[ObservationScore]] = defaultdict(list)
    for (form_id, dim), b in bucket.items():
        out[dim].append(ObservationScore(
            form_id=form_id,
            form_on=forms[form_id]["on"],
            dimension=dim,
            valid_items=b["valid"],
            max_items=b["max_items"],
            earned=b["earned"],
            available=b["available"],
            absent_items=b["absent"],
            missing_reasons=b["reasons"],
        ))
    for dim in out:
        out[dim].sort(key=lambda o: o.form_on)
    return out


# --------------------------------------------------------------- 批准与进步
def _parse_anchor_list(spec: str) -> set[str]:
    """解析 A1-A6 或 A1,A3 形式的锚点清单。"""
    wanted: set[str] = set()
    for name in (x.strip() for x in spec.split(",") if x.strip()):
        if "-" in name:
            head, _, tail = name.partition("-")
            base = "".join(ch for ch in head if not ch.isdigit())
            lo = int("".join(ch for ch in head if ch.isdigit()))
            hi = int("".join(ch for ch in tail if ch.isdigit()))
            wanted.update(f"{base}{i}" for i in range(lo, hi + 1))
        else:
            wanted.add(name)
    return wanted


def approval_covers(approval: dict, anchor: str, grade: int) -> bool:
    """解析 scope（anchor:A1-A6;grade:3-6），判断是否覆盖某锚点与年级。"""
    anchor_ok = grade_ok = False
    for part in approval["scope"].split(";"):
        kind, _, val = (x.strip() for x in part.partition(":"))
        if kind == "anchor":
            anchor_ok = anchor in _parse_anchor_list(val)
        elif kind == "grade":
            lo, _, hi = val.partition("-")
            grade_ok = int(lo) <= grade <= int(hi)
    return anchor_ok and grade_ok


def anchor_progress(store, person_id: str, left_form: str, right_form: str,
                    grade: int, dimension: str) -> dict | None:
    """两次测评在有效批准下，按本维度共同锚题得分率计算变化。

    无有效批准返回 None（调用方据此记录“不能比较”）；
    锚题满分随卷不同，只比较比例，绝不做分数等值假设。
    """
    approval = store.active_approval_between(left_form, right_form)
    if approval is None:
        return None
    # 两卷本维度共有的、且批准覆盖的锚点
    left_by_anchor, right_by_anchor = {}, {}
    for it in store.items():
        if it["anchor"] and it["dimension"] == dimension \
                and it["form_id"] in (left_form, right_form) \
                and approval_covers(approval, it["anchor"], grade):
            (left_by_anchor if it["form_id"] == left_form else right_by_anchor)[it["anchor"]] = it
    common = sorted(set(left_by_anchor) & set(right_by_anchor))
    if not common:
        return None
    # 学生在这些锚题上的作答
    person_rows = defaultdict(dict)
    for r in store.responses_for(person_id):
        person_rows[r["item_id"]] = r
    le = la = re_ = ra = 0.0
    used, skipped = [], []
    for anchor in common:
        li, ri = left_by_anchor[anchor], right_by_anchor[anchor]
        lr, rr = person_rows.get(li["id"]), person_rows.get(ri["id"])
        if lr is None or rr is None or lr["effective_score"] is None \
                or rr["effective_score"] is None:
            skipped.append(anchor)
            continue
        le += float(lr["effective_score"]); la += float(li["max_score"])
        re_ += float(rr["effective_score"]); ra += float(ri["max_score"])
        used.append(anchor)
    if not used or la == 0 or ra == 0:
        return None
    left_ratio, right_ratio = le / la, re_ / ra
    return {
        "approval_id": approval["id"],
        "left": left_form,
        "right": right_form,
        "anchors": used,
        "anchors_skipped_missing": skipped,
        "left_ratio": round(left_ratio, 4),
        "right_ratio": round(right_ratio, 4),
        "delta": round(right_ratio - left_ratio, 4),
    }


# --------------------------------------------------------------- 分维度判断
def verdict_for_dimension(store, person: dict, dimension: str,
                          observations: list[ObservationScore]) -> DimensionVerdict:
    policy = next(p for p in store.policies() if p["dimension"] == dimension)
    forms_order = sorted(store.forms(), key=lambda f: f["on"])
    form_ids = [f["id"] for f in forms_order]
    obs_by_form = {o.form_id: o for o in observations}
    grade = person["grade"]

    # 有效观察：题数达到 min_items（整卷缺考自然被排除，不视为能力证据）
    valid_obs = [o for o in observations if o.valid_items >= policy["min_items"]]
    weak_forms, weak_ratios = [], {}
    for o in valid_obs:
        ratio = o.ratio
        weak_ratios[o.form_id] = ratio
        if ratio is not None and ratio < policy["concern_ratio"]:
            weak_forms.append(o.form_id)

    # 相邻测评的进步对比（仅在直接有效批准下，按本维度锚题）
    progress, blocked, approvals_used = None, [], []
    dim_pairs = []
    for a, b in zip(form_ids, form_ids[1:]):
        prog = anchor_progress(store, person["id"], a, b, grade, dimension)
        if prog is None:
            # 区分：学生在两卷均无有效锚题作答 vs. 仅缺有效联系
            approval = store.active_approval_between(a, b)
            blocked.append({
                "left": a, "right": b,
                "reason": "无有效比较联系（未批准或已撤回）" if approval is None
                          else "批准范围内无共同有效锚题作答",
            })
        else:
            approvals_used.append(prog["approval_id"])
            dim_pairs.append(prog)
    if dim_pairs:
        progress = dim_pairs[-1]  # 最近一次同维度有效对比

    # 最近一次有效观察及其不确定性
    latest = valid_obs[-1] if valid_obs else None
    uncertainty = wilson_interval(latest.earned, latest.available) if latest else None

    reasons: list[str] = []
    if not valid_obs:
        status = Difficulty.INSUFFICIENT
        reasons.append(f"没有题数达到 {policy['min_items']} 题的有效观察，不能判定能力")
        for o in observations:
            reasons.append(f"{o.form_id} 有效题 {o.valid_items}/{o.max_items}，"
                           f"缺失：{'; '.join(o.missing_reasons) or '无'}")
    elif len(valid_obs) < policy["minimum_observations"]:
        status = Difficulty.INSUFFICIENT
        reasons.append(
            f"有效观察 {len(valid_obs)} 次，少于持续困难所需 "
            f"{policy['minimum_observations']} 次，先补观察而非定级")
        if weak_forms:
            reasons.append("单次表现已低于关注线，仅作当前偏弱提示")
    elif weak_forms and len(weak_forms) >= policy["minimum_observations"]:
        # 持续困难：达到观察次数要求的多次偏弱；若最近一次已回到关注线上，转为已有改善
        if latest and latest.form_id not in weak_forms:
            status = Difficulty.IMPROVED
            reasons.append(
                f"曾在 {('、'.join(weak_forms))} 低于关注线 {policy['concern_ratio']:.2f}，"
                f"最近 {latest.form_id} 得分率 {latest.ratio:.2f} 回到关注线上")
            if progress:
                reasons.append(
                    f"有效批准 {progress['approval_id']} 下锚题得分率 "
                    f"{progress['left_ratio']:.2f}→{progress['right_ratio']:.2f}，"
                    f"变化 {progress['delta']:+.2f}")
        else:
            status = Difficulty.PERSISTENT
            reasons.append(
                f"{len(weak_forms)} 次观察低于关注线 {policy['concern_ratio']:.2f}："
                f"{'、'.join(weak_forms)}，达到持续困难口径 "
                f"（≥{policy['minimum_observations']} 次）")
    elif weak_forms:
        status = Difficulty.WEAK
        reasons.append(
            f"当前观察 {('、'.join(weak_forms))} 得分率低于关注线 "
            f"{policy['concern_ratio']:.2f}，但偏弱次数未达持续困难口径")
    else:
        status = Difficulty.MAINTAINED
        reasons.append(f"各次有效观察均不低于关注线 {policy['concern_ratio']:.2f}")
        if progress and progress["delta"] < 0:
            reasons.append(
                f"注意：有效批准 {progress['approval_id']} 下锚题得分率 "
                f"变化 {progress['delta']:+.2f}，虽未破线但有下行迹象")

    has_pair = progress is not None
    return DimensionVerdict(
        dimension=dimension,
        status=status,
        concern_threshold=policy["concern_ratio"],
        observations=observations,
        weak_forms=weak_forms,
        weak_count=len(weak_forms),
        has_progress_pair=has_pair,
        progress_from=progress["left"] if progress else None,
        progress_to=progress["right"] if progress else None,
        progress_delta=progress["delta"] if progress else None,
        comparable_blocked=blocked,
        uncertainty=uncertainty,
        reasons=reasons,
        policy_snapshot=dict(policy),
        approvals_used=sorted(set(approvals_used)),
    )


def diagnose_student(store, person_id: str) -> StudentDiagnosis:
    person = store.person(person_id)
    if person is None:
        raise KeyError(f"学生不存在：{person_id}")
    grouped = build_observations(store, person_id)
    verdicts = {}
    for dim in DIMENSIONS:
        verdicts[dim] = verdict_for_dimension(store, person, dim, grouped.get(dim, []))

    # 知识与理解方向相反：保留差异，不压等级
    divergence = None
    k, u = verdicts["knowledge"], verdicts["understanding"]
    k_weak = k.status in (Difficulty.WEAK, Difficulty.PERSISTENT)
    u_weak = u.status in (Difficulty.WEAK, Difficulty.PERSISTENT)
    if k_weak != u_weak:
        divergence = {
            "knowledge_status": k.status.value,
            "understanding_status": u.status.value,
            "note": "阅读知识与文本理解方向相反，保留分维度差异，不合并为单一等级",
        }

    coverage = {}
    for dim, v in verdicts.items():
        coverage[dim] = {
            "observations": [
                {
                    "form_id": o.form_id, "form_on": o.form_on,
                    "valid_items": o.valid_items, "max_items": o.max_items,
                    "ratio": o.ratio, "absent_items": o.absent_items,
                    "missing_reasons": o.missing_reasons,
                } for o in v.observations
            ],
            "uncertainty": v.uncertainty,
            "blocked_comparisons": v.comparable_blocked,
        }
    return StudentDiagnosis(
        person_id=person_id,
        grade=person["grade"],
        roster_school=person["school_id"],
        dimension_verdicts=verdicts,
        divergence=divergence,
        evidence_coverage=coverage,
    )


def diagnosis_to_dict(d: StudentDiagnosis) -> dict:
    return {
        "person_id": d.person_id,
        "grade": d.grade,
        "roster_school": d.roster_school,
        "divergence": d.divergence,
        "evidence_coverage": d.evidence_coverage,
        "dimensions": {
            dim: {
                "status": v.status.value,
                "status_cn": v.status.cn,
                "concern_threshold": v.concern_threshold,
                "weak_forms": v.weak_forms,
                "weak_count": v.weak_count,
                "has_progress_pair": v.has_progress_pair,
                "progress": None if not v.has_progress_pair else {
                    "from": v.progress_from, "to": v.progress_to,
                    "delta": v.progress_delta,
                },
                "blocked_comparisons": v.comparable_blocked,
                "uncertainty": v.uncertainty,
                "approvals_used": v.approvals_used,
                "policy_snapshot": v.policy_snapshot,
                "reasons": v.reasons,
            }
            for dim, v in d.dimension_verdicts.items()
        },
    }


# --------------------------------------------------------------- 群体比较口径
def group_summary(store, form_id: str) -> dict:
    """按测评当日在读关系构成群体；转校生只进当日所在校，缺考者留在分母。

    补交成绩不会改变分母：分母来自当日在读名册，而非收到的评分行。
    """
    form = next(f for f in store.forms() if f["id"] == form_id)
    day = form["on"]
    items = {i["id"]: i for i in store.items()
             if i["form_id"] == form_id}
    dim_items = defaultdict(list)
    for it in items.values():
        dim_items[it["dimension"]].append(it)
    score_map = store.effective_score_map()
    resp_by_person_item = defaultdict(dict)
    for r in store._all("responses"):
        if r["item_id"] in items:
            resp_by_person_item[r["person_id"]][r["item_id"]] = r

    schools = {s["id"]: 0 for s in store.schools()}
    per_school = {sid: {dim: {"denominator": 0, "participated": 0, "absent": 0,
                              "earned": 0.0, "available": 0.0}
                        for dim in DIMENSIONS} for sid in schools}
    for p in store.people():
        sid = store.school_on(p["id"], day)
        if sid is None:
            continue  # 当日无在读关系，不进入任何学校分母
        schools[sid] += 1
        person_resp = resp_by_person_item.get(p["id"], {})
        for dim, its in dim_items.items():
            cell = per_school[sid][dim]
            cell["denominator"] += 1  # 当日在读即入分母，含缺考与补交
            absent = all(
                person_resp.get(i["id"], {}).get("missing_reason") == "absent"
                or i["id"] not in person_resp
                for i in its
            )
            if absent:
                cell["absent"] += 1
                continue
            used = False
            for i in its:
                r = person_resp.get(i["id"])
                if r is None:
                    continue
                eff = score_map.get(r["id"])
                if eff is None:
                    continue
                cell["earned"] += float(eff)
                cell["available"] += float(i["max_score"])
                used = True
            if used:
                cell["participated"] += 1
    out = {"form_id": form_id, "form_on": day, "schools": []}
    for sid, total in schools.items():
        row = {"school_id": sid, "enrolled_on_day": total, "dimensions": {}}
        for dim in DIMENSIONS:
            c = per_school[sid][dim]
            row["dimensions"][dim] = {
                **c,
                "mean_ratio": round(c["earned"] / c["available"], 4)
                if c["available"] else None,
            }
        out["schools"].append(row)
    return out


# --------------------------------------------------------------- 证据变化复议
def revalidate_requests(store, *, revision_ids=None, approval_id=None,
                        on_date=None) -> list[int]:
    """评分更正或撤回联系后，重判尚未开始的已确认安排。

    已实际参加（attended=1）的课程是已发生事实，不撤销、不标复议；
    已确认未开始的安排只打 review_flag 提示教师复议，绝不静默腾出名额。
    返回被标记的席位请求编号。
    """
    affected: set[int] = set()
    relevant_people: set[str] = set()
    if revision_ids:
        resp_ids = {r["response_id"] for r in store.revisions() if r["id"] in revision_ids}
        for r in store._all("responses"):
            if r["id"] in resp_ids:
                relevant_people.add(r["person_id"])
    if approval_id:
        # 撤回批准影响所有学生（其进步证据可能消失），范围限定在未开始的确认
        relevant_people = {p["id"] for p in store.people()}

    for req in store.active_requests():
        if req["attended"]:
            continue  # 已发生的课程不可撤销
        if req["state"] not in (ConfirmedState.CONFIRMED.value,
                                ConfirmedState.ADJUSTED.value):
            continue
        if revision_ids and req["person_id"] not in relevant_people:
            continue
        reason = ("评分更正后诊断依据变化，请复议" if revision_ids
                  else f"比较联系 {approval_id} 撤回后进步依据变化，请复议")
        store.mark_review(req["id"], reason)
        affected.add(req["id"])
    return sorted(affected)
