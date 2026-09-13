# -*- coding: utf-8 -*-
"""SQLite 持久层。

保存内容
--------
* 原谱 MusicXML（``scores``，不可变）；
* 规则配置及其版本指纹（``rule_sets``，同一 name+version 去重）；
* 分析任务与发现（``analyses`` / ``findings``，分析时冻结规则快照）；
* 教师裁定（``verdicts``，追加式，保留裁定历史）；
* 两版比对（``comparisons``，保存问题增减快照）。

所有时间戳为 UTC ISO8601。连接在 :class:`Database` 上下文内使用。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    part_ids TEXT NOT NULL,            -- JSON 数组
    xml TEXT NOT NULL,                 -- 原始 MusicXML 文本
    digest TEXT NOT NULL UNIQUE,       -- sha1 原文摘要
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rule_sets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    version TEXT NOT NULL,             -- 规则内容指纹
    rules_json TEXT NOT NULL,
    built_in INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(name, version)
);

CREATE TABLE IF NOT EXISTS analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    score_id INTEGER NOT NULL REFERENCES scores(id),
    rule_set_id INTEGER NOT NULL REFERENCES rule_sets(id),
    rules_snapshot TEXT NOT NULL,      -- 分析时冻结的规则 JSON
    status TEXT NOT NULL,              -- ok / parse_error
    species INTEGER,                   -- 对位类别 1-5（未指定为 NULL）
    cantus_part TEXT,                  -- 定旋律声部 part_id
    parse_issues_json TEXT NOT NULL,   -- 解析期问题（error/warning）
    summary_json TEXT NOT NULL,        -- {total, by_kind, by_severity}
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id INTEGER NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
    fingerprint TEXT NOT NULL,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL,
    measure INTEGER,
    beat TEXT,
    time_q REAL,
    notes_json TEXT NOT NULL,
    trace_json TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_findings_analysis ON findings(analysis_id);
CREATE INDEX IF NOT EXISTS idx_findings_kind ON findings(analysis_id, kind);
CREATE INDEX IF NOT EXISTS idx_findings_severity ON findings(analysis_id, severity);

CREATE TABLE IF NOT EXISTS verdicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    finding_id INTEGER NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
    analysis_id INTEGER NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
    decision TEXT NOT NULL,            -- confirmed / rejected / deferred
    comment TEXT,
    teacher TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_verdicts_finding ON verdicts(finding_id);

CREATE TABLE IF NOT EXISTS comparisons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    base_analysis_id INTEGER NOT NULL REFERENCES analyses(id),
    revised_analysis_id INTEGER NOT NULL REFERENCES analyses(id),
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

VALID_DECISIONS = ("confirmed", "rejected", "deferred")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """SQLite 连接封装，按需传文件路径，``:memory:`` 用于测试。

    文件库每实例一个连接（服务端每请求新建）；内存库默认使用进程内
    共享的单一连接（带 ``check_same_thread=False``），否则不同连接会
    看到彼此独立的空内存库。
    """

    _memory_shared: Optional["Database"] = None

    def __init__(self, path: str = "counterpoint.db"):
        self.path = path
        if path == ":memory:":
            if Database._memory_shared is not None:
                self._shared = True
                self.conn = Database._memory_shared.conn
                return
            self._shared = False
            self.conn = sqlite3.connect(":memory:", check_same_thread=False)
            Database._memory_shared = self
        else:
            self._shared = False
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """老库补列：analyses 增加对位类别与定旋律声部。"""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(analyses)")}
        if "species" not in cols:
            self.conn.execute("ALTER TABLE analyses ADD COLUMN species INTEGER")
        if "cantus_part" not in cols:
            self.conn.execute("ALTER TABLE analyses ADD COLUMN cantus_part TEXT")

    def close(self) -> None:
        # 共享内存连接由进程持有，单次 close 不真正关闭
        if self._shared or self.path == ":memory:":
            return
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @classmethod
    def reset_shared_memory(cls) -> None:
        """丢弃共享内存库（测试隔离用）。"""
        if cls._memory_shared is not None:
            try:
                cls._memory_shared.conn.close()
            except sqlite3.Error:
                pass
            cls._memory_shared = None

    def wipe(self) -> None:
        """清空全部业务表（测试隔离用）。"""
        for table in ("verdicts", "findings", "analyses", "comparisons",
                      "rule_sets", "scores"):
            self.conn.execute(f"DELETE FROM {table}")
        self.conn.commit()

    # ------------------------------------------------------------------
    # 原谱
    # ------------------------------------------------------------------

    def insert_score(self, xml: str, title: Optional[str],
                     part_ids: List[str], digest: str) -> int:
        cur = self.conn.execute(
            "SELECT id FROM scores WHERE digest = ?", (digest,))
        row = cur.fetchone()
        if row:
            return int(row["id"])
        cur = self.conn.execute(
            "INSERT INTO scores (title, part_ids, xml, digest, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (title, json.dumps(part_ids, ensure_ascii=False), xml, digest, utc_now()))
        self.conn.commit()
        return int(cur.lastrowid)

    def get_score(self, score_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM scores WHERE id = ?", (score_id,)).fetchone()

    def list_scores(self) -> List[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM scores ORDER BY id DESC"))

    # ------------------------------------------------------------------
    # 规则集
    # ------------------------------------------------------------------

    def upsert_rule_set(self, name: str, version: str, rules: Dict[str, Any],
                        built_in: bool = False) -> int:
        cur = self.conn.execute(
            "SELECT id FROM rule_sets WHERE name = ? AND version = ?",
            (name, version))
        row = cur.fetchone()
        if row:
            return int(row["id"])
        cur = self.conn.execute(
            "INSERT INTO rule_sets (name, version, rules_json, built_in, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, version, json.dumps(rules, ensure_ascii=False),
             1 if built_in else 0, utc_now()))
        self.conn.commit()
        return int(cur.lastrowid)

    def get_rule_set(self, rule_set_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM rule_sets WHERE id = ?", (rule_set_id,)).fetchone()

    def list_rule_sets(self) -> List[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM rule_sets ORDER BY built_in DESC, id ASC"))

    def copy_rule_set(self, rule_set_id: int, new_name: str,
                      overrides: Optional[Dict[str, Any]] = None) -> int:
        """复制规则集：基于旧版本内容合并覆盖项，产生新版本行。"""
        from . import rulesconfig as rc
        row = self.get_rule_set(rule_set_id)
        if row is None:
            raise KeyError(f"规则集 {rule_set_id} 不存在")
        rules = json.loads(row["rules_json"])
        if overrides:
            rules = rc._deep_merge(rules, overrides)
        rules["name"] = new_name
        rules["version"] = rc.rule_fingerprint(rules)
        return self.upsert_rule_set(new_name, rules["version"], rules)

    # ------------------------------------------------------------------
    # 分析与发现
    # ------------------------------------------------------------------

    def insert_analysis(self, score_id: int, rule_set_id: int,
                        rules_snapshot: Dict[str, Any], status: str,
                        parse_issues: List[Dict[str, Any]],
                        findings: List[Dict[str, Any]],
                        summary: Dict[str, Any],
                        species: Optional[int] = None,
                        cantus_part: Optional[str] = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO analyses (score_id, rule_set_id, rules_snapshot, status, "
            "species, cantus_part, parse_issues_json, summary_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (score_id, rule_set_id,
             json.dumps(rules_snapshot, ensure_ascii=False), status,
             species, cantus_part,
             json.dumps(parse_issues, ensure_ascii=False),
             json.dumps(summary, ensure_ascii=False), utc_now()))
        analysis_id = int(cur.lastrowid)
        self.conn.executemany(
            "INSERT INTO findings (analysis_id, fingerprint, kind, severity, measure, "
            "beat, time_q, notes_json, trace_json, message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(analysis_id, f["fingerprint"], f["kind"], f["severity"],
              f.get("measure"), f.get("beat"), f.get("time"),
              json.dumps(f["notes"], ensure_ascii=False),
              json.dumps(f["trace"], ensure_ascii=False), f["message"])
             for f in findings])
        self.conn.commit()
        return analysis_id

    def get_analysis(self, analysis_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM analyses WHERE id = ?", (analysis_id,)).fetchone()

    def list_analyses(self, score_id: Optional[int] = None) -> List[sqlite3.Row]:
        if score_id is not None:
            return list(self.conn.execute(
                "SELECT * FROM analyses WHERE score_id = ? ORDER BY id DESC",
                (score_id,)))
        return list(self.conn.execute("SELECT * FROM analyses ORDER BY id DESC"))

    def query_findings(self, analysis_id: int,
                       kinds: Optional[Iterable[str]] = None,
                       severities: Optional[Iterable[str]] = None,
                       measure: Optional[int] = None,
                       part_id: Optional[str] = None,
                       verdict: Optional[str] = None) -> List[sqlite3.Row]:
        sql = "SELECT f.* FROM findings f WHERE f.analysis_id = ?"
        params: List[Any] = [analysis_id]
        if kinds:
            sql += f" AND f.kind IN ({','.join('?' for _ in kinds)})"
            params.extend(kinds)
        if severities:
            sql += f" AND f.severity IN ({','.join('?' for _ in severities)})"
            params.extend(severities)
        if measure is not None:
            sql += " AND f.measure = ?"
            params.append(measure)
        if part_id:
            # notes_json 是数组，任一相关音符属于该声部即命中
            sql += (" AND EXISTS (SELECT 1 FROM json_each(f.notes_json) ne "
                    "WHERE json_extract(ne.value, '$.part_id') = ?)")
            params.append(part_id)
        if verdict:
            sql += (" AND (SELECT v.decision FROM verdicts v WHERE v.finding_id = f.id "
                    "ORDER BY v.id DESC LIMIT 1) = ?")
            params.append(verdict)
        sql += " ORDER BY f.time_q, f.id"
        return list(self.conn.execute(sql, params))

    def get_finding(self, finding_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()

    def latest_verdict(self, finding_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM verdicts WHERE finding_id = ? ORDER BY id DESC LIMIT 1",
            (finding_id,)).fetchone()

    def all_verdicts(self, analysis_id: int) -> List[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM verdicts WHERE analysis_id = ? ORDER BY id",
            (analysis_id,)))

    def add_verdict(self, finding_id: int, analysis_id: int, decision: str,
                    comment: Optional[str], teacher: Optional[str]) -> int:
        if decision not in VALID_DECISIONS:
            raise ValueError(f"decision 必须是 {VALID_DECISIONS} 之一")
        cur = self.conn.execute(
            "INSERT INTO verdicts (finding_id, analysis_id, decision, comment, "
            "teacher, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (finding_id, analysis_id, decision, comment, teacher, utc_now()))
        self.conn.commit()
        return int(cur.lastrowid)

    def get_verdict(self, verdict_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM verdicts WHERE id = ?", (verdict_id,)).fetchone()

    # ------------------------------------------------------------------
    # 比对
    # ------------------------------------------------------------------

    def insert_comparison(self, base_id: int, revised_id: int,
                          result: Dict[str, Any]) -> int:
        cur = self.conn.execute(
            "INSERT INTO comparisons (base_analysis_id, revised_analysis_id, "
            "result_json, created_at) VALUES (?, ?, ?, ?)",
            (base_id, revised_id, json.dumps(result, ensure_ascii=False), utc_now()))
        self.conn.commit()
        return int(cur.lastrowid)

    def get_comparison(self, comparison_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM comparisons WHERE id = ?", (comparison_id,)).fetchone()
