"""本地状态库：原始资料镜像 + 教师操作记录，全部落在 SQLite。

原始 materials/data.json 永不被本应用改写；导入只做校验后镜像。
教师确认、暂缓、候补、补充观察与影响事件单独建表，与自动建议明确区分。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

from .models import ConfirmedState

SCHEMA = """
CREATE TABLE IF NOT EXISTS import_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    source_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    imported_on TEXT NOT NULL,
    note TEXT,
    table_counts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schools (
    id TEXT PRIMARY KEY, area TEXT NOT NULL, devices INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS people (
    id TEXT PRIMARY KEY, school_id TEXT NOT NULL, household TEXT NOT NULL,
    grade INTEGER NOT NULL, access_need TEXT NOT NULL, sample_weight REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS enrolments (
    id TEXT PRIMARY KEY, person_id TEXT NOT NULL, school_id TEXT NOT NULL,
    start TEXT NOT NULL, end TEXT
);
CREATE TABLE IF NOT EXISTS forms (
    id TEXT PRIMARY KEY, "on" TEXT NOT NULL, grade_low INTEGER NOT NULL, grade_high INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY, form_id TEXT NOT NULL, dimension TEXT NOT NULL,
    anchor TEXT, max_score INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS responses (
    id TEXT PRIMARY KEY, person_id TEXT NOT NULL, item_id TEXT NOT NULL,
    score REAL, missing_reason TEXT, received_on TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS comparison_approvals (
    id TEXT PRIMARY KEY, left TEXT NOT NULL, right TEXT NOT NULL, scope TEXT NOT NULL,
    approved_on TEXT NOT NULL, withdrawn_on TEXT
);
CREATE TABLE IF NOT EXISTS courses (
    id TEXT PRIMARY KEY, dimension TEXT NOT NULL, grade_low INTEGER NOT NULL,
    grade_high INTEGER NOT NULL, "on" TEXT NOT NULL, start TEXT NOT NULL,
    end TEXT NOT NULL, capacity INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS revisions (
    id TEXT PRIMARY KEY, response_id TEXT NOT NULL, score REAL NOT NULL,
    received_on TEXT NOT NULL, reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observation_policy (
    id TEXT PRIMARY KEY, dimension TEXT NOT NULL, min_items INTEGER NOT NULL,
    concern_ratio REAL NOT NULL, minimum_observations INTEGER NOT NULL
);
-- 席位请求：既有导入安排与教师操作的统一台账（source 区分来源）
CREATE TABLE IF NOT EXISTS seat_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id TEXT NOT NULL,
    course_id TEXT NOT NULL,
    state TEXT NOT NULL,
    source TEXT NOT NULL,                 -- import | teacher | system
    suggested_reason TEXT,
    attended INTEGER NOT NULL DEFAULT 0,
    review_flag INTEGER NOT NULL DEFAULT 0,
    waitlist_position INTEGER,
    created_on TEXT NOT NULL,
    decided_on TEXT,
    actor TEXT,
    note TEXT,
    UNIQUE(person_id, course_id)
);
CREATE TABLE IF NOT EXISTS supp_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id TEXT NOT NULL,
    dimension TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,                -- open | done
    created_on TEXT NOT NULL,
    completed_on TEXT,
    note TEXT
);
CREATE TABLE IF NOT EXISTS impact_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,                  -- revision | approval_withdrawn | reimport
    ref_id TEXT NOT NULL,
    detail TEXT NOT NULL,                -- JSON
    created_on TEXT NOT NULL,
    affected_request_ids TEXT NOT NULL,  -- JSON 数组
    acknowledged INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS decision_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    on_date TEXT NOT NULL,
    detail TEXT NOT NULL                 -- JSON
);
CREATE INDEX IF NOT EXISTS idx_resp_person ON responses(person_id);
CREATE INDEX IF NOT EXISTS idx_resp_item ON responses(item_id);
CREATE INDEX IF NOT EXISTS idx_enrol_person ON enrolments(person_id);
CREATE INDEX IF NOT EXISTS idx_req_person ON seat_requests(person_id);
CREATE INDEX IF NOT EXISTS idx_req_course ON seat_requests(course_id);
CREATE INDEX IF NOT EXISTS idx_rev_resp ON revisions(response_id);
"""

MIRROR_TABLES = (
    "schools", "people", "enrolments", "forms", "items", "responses",
    "comparison_approvals", "courses", "revisions", "observation_policy",
)
# 占用席位的状态：教师确认/调整确认，以及导入的历史确认；候补与暂缓不占席
SEAT_OCCUPYING = (
    ConfirmedState.CONFIRMED.value,
    ConfirmedState.ADJUSTED.value,
)


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self):
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(seat_requests)")}
        if "waitlist_position" not in cols:
            self.conn.execute(
                "ALTER TABLE seat_requests ADD COLUMN waitlist_position INTEGER")

    def close(self):
        self.conn.close()

    # ------------------------------------------------------------------ 导入
    def import_materials(self, materials_dir: str | Path, note: str | None = None,
                         actor: str = "教研员") -> dict:
        """校验并整包导入资料；同编号行做 upsert，返回变化回执。"""
        from inspect_materials import read_materials  # 仓库根目录的资料校验工具

        materials_dir = Path(materials_dir)
        data, receipt = read_materials(materials_dir)
        cur = self.conn.cursor()
        changes: dict[str, dict[str, list[str]]] = {}
        conflicts: list[dict] = []
        # 库内人工管理决定优先的列：重传资料不能静默撤销教研员的撤回
        protected_cells = {
            "comparison_approvals": {"withdrawn_on"},
        }
        for table in MIRROR_TABLES:
            added, updated = [], []
            existing = {r[0] for r in cur.execute(f"SELECT id FROM {table}")}
            protected = protected_cells.get(table, set())
            for row in data[table]:
                rid = row["id"]
                cols = [c for c in row.keys()]
                placeholder = ",".join("?" for _ in cols)
                collist = ",".join(f'"{c}"' for c in cols)
                if rid in existing:
                    old = cur.execute(
                        f"SELECT * FROM {table} WHERE id=?", (rid,)
                    ).fetchone()
                    write_cols = [c for c in cols if not (
                        c in protected and old[c] is not None and row[c] is None)]
                    skipped = [c for c in cols if c not in write_cols]
                    newvals = tuple(row[c] for c in write_cols)
                    oldvals = tuple(old[c] for c in write_cols)
                    if skipped:
                        conflicts.append({
                            "table": table, "id": rid,
                            "field": skipped[0],
                            "kept": old[skipped[0]],
                            "incoming": row[skipped[0]],
                            "reason": "库内人工管理决定优先，资料重传不覆盖",
                        })
                    if newvals != oldvals:
                        sets = ",".join(f'"{c}"=?' for c in write_cols)
                        cur.execute(
                            f"UPDATE {table} SET {sets} WHERE id=?", (*newvals, rid)
                        )
                        updated.append(rid)
                else:
                    cur.execute(
                        f'INSERT INTO {table} ({collist}) VALUES ({placeholder})',
                        tuple(row[c] for c in cols),
                    )
                    added.append(rid)
            if added or updated:
                changes[table] = {"added": added, "updated": updated}

        # 导入的历史安排登记为席位台账（arrangements 不在镜像表清单内）
        arr_added, arr_attendance = [], []
        for row in data["arrangements"]:
            exists = cur.execute(
                "SELECT id, attended FROM seat_requests WHERE person_id=? AND course_id=?",
                (row["person_id"], row["course_id"]),
            ).fetchone()
            if exists is None:
                cur.execute(
                    "INSERT INTO seat_requests (person_id, course_id, state, source, "
                    "attended, created_on, decided_on, actor, note) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (row["person_id"], row["course_id"], ConfirmedState.CONFIRMED.value,
                     "import", 1 if row["attended"] else 0, row["confirmed_on"],
                     row["confirmed_on"], "导入资料", "资料中的教师既有确认"),
                )
                arr_added.append(row["id"])
            elif bool(exists["attended"]) != bool(row["attended"]):
                if exists["attended"] and not row["attended"]:
                    # 已实际发生的课程不能被资料重传撤销成未发生
                    conflicts.append({
                        "table": "arrangements", "id": row["id"],
                        "field": "attended", "kept": True,
                        "incoming": False,
                        "reason": "已实际参加的课程不能被资料重传撤销",
                    })
                else:
                    cur.execute(
                        "UPDATE seat_requests SET attended=? WHERE id=?",
                        (1 if row["attended"] else 0, exists["id"]),
                    )
                    arr_attendance.append(row["id"])
        if arr_added or arr_attendance:
            changes["arrangements"] = {"added": arr_added, "updated": arr_attendance}

        cur.execute(
            "INSERT INTO import_batches (kind, source_path, sha256, imported_on, note, table_counts) "
            "VALUES (?,?,?,?,?,?)",
            ("materials", str(materials_dir), receipt["sha256"], date.today().isoformat(),
             note, json.dumps(receipt["rows"], ensure_ascii=False)),
        )
        batch_id = cur.lastrowid
        cur.execute(
            "INSERT INTO decision_audit (request_id, action, actor, on_date, detail) "
            "VALUES (NULL, 'import_materials', ?, ?, ?)",
            (actor, date.today().isoformat(),
             json.dumps({"batch_id": batch_id, "changes": changes,
                         "conflicts": conflicts}, ensure_ascii=False)),
        )
        self.conn.commit()
        return {"batch_id": batch_id, "sha256": receipt["sha256"],
                "rows": receipt["rows"], "changes": changes,
                "conflicts": conflicts}

    def list_batches(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM import_batches ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------- 基础查询
    def _all(self, table: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(f"SELECT * FROM {table} ORDER BY id")]

    def schools(self): return self._all("schools")
    def people(self): return self._all("people")
    def forms(self): return self._all("forms")
    def items(self): return self._all("items")
    def courses(self): return self._all("courses")
    def enrolments(self): return self._all("enrolments")
    def policies(self): return self._all("observation_policy")

    def person(self, person_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM people WHERE id=?", (person_id,)).fetchone()
        return dict(r) if r else None

    def course(self, course_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM courses WHERE id=?", (course_id,)).fetchone()
        return dict(r) if r else None

    def responses_for(self, person_id: str) -> list[dict]:
        rows = self.conn.execute(
            "WITH latest AS ("
            "  SELECT response_id, score AS revised_score, "
            "    ROW_NUMBER() OVER (PARTITION BY response_id "
            "                       ORDER BY received_on DESC, id DESC) rn, "
            "    COUNT(*) OVER (PARTITION BY response_id) revision_count "
            "  FROM revisions"
            ") "
            "SELECT r.*, l.revised_score, l.revision_count "
            "FROM responses r LEFT JOIN latest l "
            "  ON l.response_id=r.id AND l.rn=1 "
            "WHERE r.person_id=? ORDER BY r.id",
            (person_id,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["effective_score"] = d["revised_score"] if d["revision_count"] else d["score"]
            out.append(d)
        return out

    def effective_score_map(self) -> dict[str, float | None]:
        """全部评分行的生效分：更正存在时取最新更正，否则取原始分。"""
        result = {}
        for r in self.conn.execute("SELECT id, score FROM responses"):
            result[r["id"]] = r["score"]
        for r in self.conn.execute(
            "SELECT response_id, score FROM revisions v WHERE received_on=("
            "  SELECT MAX(received_on) FROM revisions WHERE response_id=v.response_id)"
        ):
            result[r["response_id"]] = r["score"]
        return result

    def revisions(self) -> list[dict]:
        return self._all("revisions")

    # ------------------------------------------------------------- 比较批准
    def approvals(self, include_withdrawn: bool = True) -> list[dict]:
        if include_withdrawn:
            return self._all("comparison_approvals")
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM comparison_approvals WHERE withdrawn_on IS NULL ORDER BY id")]

    def set_approval_withdrawn(self, approval_id: str, on: str, actor: str) -> dict:
        r = self.conn.execute(
            "SELECT * FROM comparison_approvals WHERE id=?", (approval_id,)
        ).fetchone()
        if r is None:
            raise KeyError(f"批准不存在：{approval_id}")
        if r["withdrawn_on"] is not None:
            raise ValueError(f"批准 {approval_id} 已撤回，不能重复撤回")
        self.conn.execute(
            "UPDATE comparison_approvals SET withdrawn_on=? WHERE id=?", (on, approval_id)
        )
        self.conn.execute(
            "INSERT INTO decision_audit (request_id, action, actor, on_date, detail) "
            "VALUES (NULL, 'withdraw_approval', ?, ?, ?)",
            (actor, on, json.dumps({"approval_id": approval_id}, ensure_ascii=False)),
        )
        self.conn.commit()
        return dict(self.conn.execute(
            "SELECT * FROM comparison_approvals WHERE id=?", (approval_id,)).fetchone())

    def active_approval_between(self, left: str, right: str) -> dict | None:
        """仅承认直接的、未撤回的人工批准；不做传递推断。"""
        r = self.conn.execute(
            "SELECT * FROM comparison_approvals WHERE withdrawn_on IS NULL AND "
            "((left=? AND right=?) OR (left=? AND right=?))",
            (left, right, right, left),
        ).fetchone()
        return dict(r) if r else None

    # ------------------------------------------------------------- 在读关系
    def school_on(self, person_id: str, day: str) -> str | None:
        """按当日生效在读关系定位学校；右端不包含。转校生当日只属于新校。"""
        rows = self.conn.execute(
            "SELECT * FROM enrolments WHERE person_id=? AND start<=? "
            "AND (end IS NULL OR ?<end) ORDER BY start",
            (person_id, day, day),
        ).fetchall()
        return rows[-1]["school_id"] if rows else None

    # ------------------------------------------------------------- 席位台账
    def active_requests(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM seat_requests ORDER BY person_id, course_id")]

    def requests_for_person(self, person_id: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM seat_requests WHERE person_id=? ORDER BY id", (person_id,))]

    def requests_for_course(self, course_id: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM seat_requests WHERE course_id=? ORDER BY id", (course_id,))]

    def occupied_seats(self, course_id: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) n FROM seat_requests WHERE course_id=? AND state IN (?,?)",
            (course_id, *SEAT_OCCUPYING),
        ).fetchone()["n"]

    def get_request(self, request_id: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM seat_requests WHERE id=?", (request_id,)).fetchone()
        return dict(r) if r else None

    def find_request(self, person_id: str, course_id: str) -> dict | None:
        r = self.conn.execute(
            "SELECT * FROM seat_requests WHERE person_id=? AND course_id=?",
            (person_id, course_id),
        ).fetchone()
        return dict(r) if r else None

    def add_suggestion(self, person_id: str, course_id: str, reason: str, on: str) -> dict:
        existing = self.find_request(person_id, course_id)
        if existing is not None:
            return existing
        cur = self.conn.execute(
            "INSERT INTO seat_requests (person_id, course_id, state, source, "
            "suggested_reason, attended, review_flag, created_on) VALUES (?,?,?,?,?,0,0,?)",
            (person_id, course_id, ConfirmedState.SUGGESTED.value, "system", reason, on),
        )
        self.conn.commit()
        return self.get_request(cur.lastrowid)

    def update_request_state(self, request_id: int, state: ConfirmedState, actor: str,
                             on: str, note: str | None = None) -> dict:
        req = self.get_request(request_id)
        if req is None:
            raise KeyError(f"安排不存在：{request_id}")
        if req["attended"] and state == ConfirmedState.CANCELLED:
            raise ValueError("课程已实际发生，不能撤销成未发生")
        self.conn.execute(
            "UPDATE seat_requests SET state=?, decided_on=?, actor=?, note=? WHERE id=?",
            (state.value, on, actor, note, request_id),
        )
        self.conn.execute(
            "INSERT INTO decision_audit (request_id, action, actor, on_date, detail) "
            "VALUES (?,?,?,?,?)",
            (request_id, "state:" + state.value, actor, on,
             json.dumps({"note": note}, ensure_ascii=False)),
        )
        self.conn.commit()
        return self.get_request(request_id)

    def mark_review(self, request_id: int, reason: str):
        self.conn.execute(
            "UPDATE seat_requests SET review_flag=1, note=? WHERE id=? AND attended=0",
            (reason, request_id),
        )

    def clear_review(self, request_id: int):
        self.conn.execute(
            "UPDATE seat_requests SET review_flag=0 WHERE id=?", (request_id,)
        )

    def overlapping_confirmed(self, person_id: str, course: dict) -> dict | None:
        """该学生已占席位中与目标课程时段重叠者。"""
        rows = self.conn.execute(
            'SELECT sr.*, c."on" AS c_on, c.start AS c_start, c.end AS c_end, c.id AS cid '
            "FROM seat_requests sr JOIN courses c ON c.id=sr.course_id "
            "WHERE sr.person_id=? AND sr.state IN (?,?)",
            (person_id, *SEAT_OCCUPYING),
        ).fetchall()
        for r in rows:
            if r["cid"] == course["id"]:
                continue
            if r["c_on"] == course["on"] and r["c_start"] < course["end"] \
                    and course["start"] < r["c_end"]:
                return dict(r)
        return None

    # ------------------------------------------------------------- 补充观察
    def add_observation_task(self, person_id: str, dimension: str, reason: str,
                             on: str, note: str | None = None) -> dict:
        cur = self.conn.execute(
            "INSERT INTO supp_observations (person_id, dimension, reason, status, created_on, note) "
            "VALUES (?,?,?,'open',?,?)",
            (person_id, dimension, reason, on, note),
        )
        self.conn.commit()
        return self.get_observation(cur.lastrowid)

    def get_observation(self, obs_id: int) -> dict:
        return dict(self.conn.execute(
            "SELECT * FROM supp_observations WHERE id=?", (obs_id,)).fetchone())

    def list_observations(self, person_id: str | None = None) -> list[dict]:
        if person_id:
            rows = self.conn.execute(
                "SELECT * FROM supp_observations WHERE person_id=? ORDER BY id", (person_id,))
        else:
            rows = self.conn.execute("SELECT * FROM supp_observations ORDER BY id")
        return [dict(r) for r in rows]

    def complete_observation(self, obs_id: int, on: str, note: str | None = None) -> dict:
        obs = self.get_observation(obs_id)
        if obs["status"] == "done":
            raise ValueError("该补充观察已完成，不能重复回录")
        self.conn.execute(
            "UPDATE supp_observations SET status='done', completed_on=?, note=? WHERE id=?",
            (on, note, obs_id),
        )
        self.conn.commit()
        return self.get_observation(obs_id)

    # ------------------------------------------------------------- 影响事件
    def add_impact_event(self, kind: str, ref_id: str, detail: dict,
                         affected_request_ids: list[int], on: str) -> dict:
        # 同一来源对同一批安排已有未知悉事件时不重复登记
        for old in self.conn.execute(
            "SELECT * FROM impact_events WHERE kind=? AND ref_id=? "
            "AND acknowledged=0", (kind, ref_id)):
            if json.loads(old["affected_request_ids"]) == sorted(affected_request_ids):
                d = dict(old)
                d["detail"] = json.loads(d["detail"])
                d["affected_request_ids"] = json.loads(d["affected_request_ids"])
                return d
        cur = self.conn.execute(
            "INSERT INTO impact_events (kind, ref_id, detail, created_on, "
            "affected_request_ids, acknowledged) VALUES (?,?,?,?,?,0)",
            (kind, ref_id, json.dumps(detail, ensure_ascii=False), on,
             json.dumps(affected_request_ids)),
        )
        self.conn.commit()
        return dict(self.conn.execute(
            "SELECT * FROM impact_events WHERE id=?", (cur.lastrowid,)).fetchone())

    def list_impact_events(self, include_acknowledged: bool = True) -> list[dict]:
        q = "SELECT * FROM impact_events"
        if not include_acknowledged:
            q += " WHERE acknowledged=0"
        q += " ORDER BY id"
        out = []
        for r in self.conn.execute(q):
            d = dict(r)
            d["detail"] = json.loads(d["detail"])
            d["affected_request_ids"] = json.loads(d["affected_request_ids"])
            out.append(d)
        return out

    def acknowledge_event(self, event_id: int) -> dict:
        self.conn.execute(
            "UPDATE impact_events SET acknowledged=1 WHERE id=?", (event_id,))
        self.conn.commit()
        r = self.conn.execute("SELECT * FROM impact_events WHERE id=?", (event_id,)).fetchone()
        d = dict(r)
        d["detail"] = json.loads(d["detail"])
        d["affected_request_ids"] = json.loads(d["affected_request_ids"])
        return d

    def audit_trail(self, request_id: int | None = None) -> list[dict]:
        if request_id is None:
            rows = self.conn.execute("SELECT * FROM decision_audit ORDER BY id")
        else:
            rows = self.conn.execute(
                "SELECT * FROM decision_audit WHERE request_id=? ORDER BY id", (request_id,))
        return [dict(r) for r in rows]

    # ------------------------------------------------------------- 出勤回录
    def record_attendance(self, request_id: int, attended: bool, on: str,
                          actor: str = "教师") -> dict:
        req = self.get_request(request_id)
        if req is None:
            raise KeyError(f"安排不存在：{request_id}")
        if not attended and req["attended"]:
            raise ValueError("已实际发生的课程不能回录为未发生")
        self.conn.execute(
            "UPDATE seat_requests SET attended=? WHERE id=?", (1 if attended else 0, request_id)
        )
        self.conn.execute(
            "INSERT INTO decision_audit (request_id, action, actor, on_date, detail) "
            "VALUES (?,?,?,?,?)",
            (request_id, f"attendance:{int(attended)}", actor, on, "{}"),
        )
        self.conn.commit()
        return self.get_request(request_id)
