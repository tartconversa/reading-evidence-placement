"""领域常量与数据结构。

口径全部为项目内的校内演练口径，不声称复现国家标准。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

DIMENSIONS = ("knowledge", "understanding", "reflection")
DIMENSION_CN = {
    "knowledge": "阅读知识",
    "understanding": "文本理解",
    "reflection": "反思评价",
}


class Difficulty(str, Enum):
    """分维度判断；不允许把三个维度压成一个总等级。"""

    INSUFFICIENT = "insufficient_evidence"  # 尚无足够证据
    WEAK = "current_weakness"  # 当前表现偏弱（单次或持续）
    PERSISTENT = "persistent_difficulty"  # 持续困难
    IMPROVED = "improved"  # 已有改善证据
    MAINTAINED = "maintained"  # 表现稳定（未触发关注线）

    @property
    def cn(self) -> str:
        return {
            "insufficient_evidence": "证据不足",
            "current_weakness": "当前偏弱",
            "persistent_difficulty": "持续困难",
            "improved": "已有改善",
            "maintained": "表现稳定",
        }[self.value]


class ConfirmedState(str, Enum):
    SUGGESTED = "suggested"  # 自动建议，教师尚未处理
    CONFIRMED = "confirmed"  # 教师确认
    ADJUSTED = "adjusted"  # 教师调整后确认
    DEFERRED = "deferred"  # 教师暂缓
    WAITLISTED = "waitlisted"  # 候补（系统裁决，非教师确认）
    REVIEW = "review_needed"  # 证据变化后需教师复议
    OBSERVATION = "supplementary_observation"  # 补充观察安排
    CANCELLED = "cancelled"  # 教师撤销（仅限尚未开始的安排）

    @property
    def cn(self) -> str:
        return {
            "suggested": "自动建议",
            "confirmed": "教师确认",
            "adjusted": "调整确认",
            "deferred": "教师暂缓",
            "waitlisted": "候补",
            "review_needed": "待复议",
            "supplementary_observation": "补充观察",
            "cancelled": "已撤销",
        }[self.value]


# 自动建议/裁决与教师人工动作的明确分界
TEACHER_DECISIONS = {
    ConfirmedState.CONFIRMED,
    ConfirmedState.ADJUSTED,
    ConfirmedState.DEFERRED,
    ConfirmedState.CANCELLED,
}


@dataclass(frozen=True)
class ObservationScore:
    """一次测评中某维度的有效作答汇总。"""

    form_id: str
    form_on: str
    dimension: str
    valid_items: int          # 有有效原始分的题数
    max_items: int            # 本卷本维度题目总数
    earned: float             # 有效题得分合计
    available: float          # 有效题满分合计
    absent_items: int         # 缺考题数（score 为 null 且 missing_reason=absent）
    missing_reasons: list[str] = field(default_factory=list)

    @property
    def ratio(self) -> float | None:
        return self.earned / self.available if self.available else None


@dataclass(frozen=True)
class DimensionVerdict:
    dimension: str
    status: Difficulty
    concern_threshold: float          # 本口径关注线（比例）
    observations: list[ObservationScore]
    weak_forms: list[str]             # 触发关注线的测评
    weak_count: int
    has_progress_pair: bool           # 是否存在有效批准下的进步对比
    progress_from: str | None
    progress_to: str | None
    progress_delta: float | None      # 锚题得分率差
    comparable_blocked: list[dict]    # 因无有效联系而不能比较的相邻测评
    uncertainty: dict | None          # 最近一次有效观察的测量不确定性
    reasons: list[str]                # 可解释的判断依据
    policy_snapshot: dict             # 当时采用的口径
    approvals_used: list[str]         # 采用的批准编号


@dataclass(frozen=True)
class StudentDiagnosis:
    person_id: str
    grade: int
    roster_school: str
    dimension_verdicts: dict[str, DimensionVerdict]
    divergence: dict | None           # 知识与理解方向相反时保留差异
    evidence_coverage: dict           # 覆盖与缺失原因汇总


@dataclass(frozen=True)
class CourseOption:
    course_id: str
    dimension: str
    grade_low: int
    grade_high: int
    on: str
    start: str
    end: str
    capacity: int
    occupied: int                     # 已确认/调整确认占用（已上课程同样占位）
    available: int

    def overlaps(self, other: "CourseOption") -> bool:
        """同一日期、时间区间相交即视为重叠时段。"""
        if self.on != other.on:
            return False
        return self.start < other.end and other.start < self.end
