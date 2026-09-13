# -*- coding: utf-8 -*-
"""分析编排：连接存储层、解析器、规则引擎与比对逻辑。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from . import counterpoint, musicxml_io, rulesconfig as rc
from .species import SPECIES_NAMES, VALID_SPECIES
from .storage import Database


class ServiceError(Exception):
    """面向接口的业务错误（携带 HTTP 状态码）。"""

    def __init__(self, status: int, message: str, details: Any = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details


# ---------------------------------------------------------------------------
# 规则集
# ---------------------------------------------------------------------------

def ensure_default_rule_set(db: Database) -> int:
    rules = rc.build_rules()
    return db.upsert_rule_set(rules["name"], rules["version"], rules, built_in=True)


def create_rule_set(db: Database, payload: Dict[str, Any]) -> Dict[str, Any]:
    name = payload.get("name") or "自定义规则"
    base_id = payload.get("base_rule_set_id")
    overrides = payload.get("rules", payload)
    if base_id is not None:
        row = db.get_rule_set(int(base_id))
        if row is None:
            raise ServiceError(404, f"基础规则集 {base_id} 不存在")
        base_rules = json.loads(row["rules_json"])
        merged = rc._deep_merge(base_rules, payload.get("overrides", {}))
    else:
        # 从默认规则起步，payload 中允许直接给完整规则覆盖
        merged = rc._deep_merge(rc.DEFAULT_RULES, payload.get("overrides", payload.get("rules", {})))
    merged["name"] = name
    errors = rc.validate_rules(merged)
    if errors:
        raise ServiceError(400, "规则配置无效", errors)
    merged["version"] = rc.rule_fingerprint(merged)
    rid = db.upsert_rule_set(name, merged["version"], merged)
    return rule_set_dict(db.get_rule_set(rid))


def rule_set_dict(row: sqlite3.Row) -> Dict[str, Any]:
    rules = json.loads(row["rules_json"])
    return {
        "id": row["id"],
        "name": row["name"],
        "version": row["version"],
        "built_in": bool(row["built_in"]),
        "created_at": row["created_at"],
        "rules": rules,
    }


# ---------------------------------------------------------------------------
# 原谱
# ---------------------------------------------------------------------------

def upload_score(db: Database, xml_text: str, filename: Optional[str] = None) -> Dict[str, Any]:
    if not xml_text or not xml_text.strip():
        raise ServiceError(400, "MusicXML 内容为空")
    digest = musicxml_hash(xml_text)
    # 先做一次解析，返回的结构问题随 score 记录可见（创建分析时会阻断）
    try:
        parsed = musicxml_io.parse_score(xml_text.encode("utf-8"))
    except musicxml_io.ParseAborted as exc:
        raise ServiceError(400, f"MusicXML 无法解析：{exc}")
    score_id = db.insert_score(xml_text, parsed.get("title") or filename,
                               parsed["part_ids"], digest)
    return {
        "id": score_id,
        "title": parsed.get("title"),
        "part_ids": parsed["part_ids"],
        "digest": digest,
        "parse_issue_count": len(parsed["parse_issues"]),
        "fatal": parsed["fatal"],
    }


def musicxml_hash(xml_text: str) -> str:
    import hashlib
    return hashlib.sha1(xml_text.encode("utf-8")).hexdigest()


def score_dict(row: sqlite3.Row, include_xml: bool = False) -> Dict[str, Any]:
    out = {
        "id": row["id"],
        "title": row["title"],
        "part_ids": json.loads(row["part_ids"]),
        "digest": row["digest"],
        "created_at": row["created_at"],
    }
    if include_xml:
        out["musicxml"] = row["xml"]
    return out


# ---------------------------------------------------------------------------
# 分析
# ---------------------------------------------------------------------------

def create_analysis(db: Database, payload: Dict[str, Any]) -> Dict[str, Any]:
    score_id = payload.get("score_id")
    if score_id is None:
        raise ServiceError(400, "缺少 score_id")
    score_row = db.get_score(int(score_id))
    if score_row is None:
        raise ServiceError(404, f"原谱 {score_id} 不存在")

    rule_set_id = payload.get("rule_set_id")
    if rule_set_id is None:
        rule_set_id = ensure_default_rule_set(db)
    rs_row = db.get_rule_set(int(rule_set_id))
    if rs_row is None:
        raise ServiceError(404, f"规则集 {rule_set_id} 不存在")
    rules = json.loads(rs_row["rules_json"])

    parsed = musicxml_io.parse_score(score_row["xml"].encode("utf-8"))

    # ---- 对位类别与定旋律声部（教师创建分析时指定；不擅自改类）----------
    species, cantus_role, cantus_part = _resolve_species(payload, parsed)
    status = "parse_error" if parsed["fatal"] else "ok"

    findings: List[Dict[str, Any]] = []
    if not parsed["fatal"]:
        result = counterpoint.analyze(parsed, rules, species=species,
                                      cantus_role=cantus_role)
        findings = result["findings"]

    summary = _summarize(findings, parsed["parse_issues"])
    analysis_id = db.insert_analysis(
        score_row["id"], rs_row["id"], rules, status,
        parsed["parse_issues"], findings, summary,
        species=species, cantus_part=cantus_part)

    return analysis_dict(db, analysis_id, include_findings=True)


def _resolve_species(payload: Dict[str, Any], parsed: Dict[str, Any]
                     ) -> Tuple[Optional[int], Optional[str], Optional[str]]:
    """解析并校验 species / cantus 参数。

    返回 ``(species, cantus_role, cantus_part)``；未指定时为全 None。
    类别非法、定旋律声部不存在、只给其一，均抛 400——绝不擅自改类。
    """
    species = payload.get("species")
    cantus = payload.get("cantus", payload.get("cantus_part"))
    if species is None and cantus is None:
        return None, None, None
    if species is None or cantus is None:
        raise ServiceError(
            400, "请同时指定对位类别 species（1–5）与定旋律声部 cantus"
                 "（upper / lower / part_id）")
    if isinstance(species, bool) or not isinstance(species, int) \
            or species not in VALID_SPECIES:
        raise ServiceError(
            400, f"对位类别非法：{species!r}；类别必须是 1–5 的整数："
                 + "、".join(f"{k}={v}" for k, v in SPECIES_NAMES.items()))
    part_ids = parsed["part_ids"]
    roles = parsed["roles"]
    if cantus in ("upper", "lower"):
        cantus_role = cantus
        cantus_part = next(pid for pid, r in roles.items() if r == cantus)
    elif cantus in part_ids:
        cantus_part = cantus
        cantus_role = roles[cantus]
    else:
        raise ServiceError(
            400, f"定旋律声部不存在：{cantus!r}；本谱声部为 {part_ids}"
                 f"（upper={part_ids[0]}，lower={part_ids[1]}）")
    return species, cantus_role, cantus_part


def _summarize(findings: List[Dict[str, Any]],
               parse_issues: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_kind: Dict[str, int] = {}
    by_severity: Dict[str, int] = {}
    for f in findings:
        by_kind[f["kind"]] = by_kind.get(f["kind"], 0) + 1
        by_severity[f["severity"]] = by_severity.get(f["severity"], 0) + 1
    return {
        "total": len(findings),
        "by_kind": by_kind,
        "by_severity": by_severity,
        "parse_errors": sum(1 for i in parse_issues if i["severity"] == "error"),
        "parse_warnings": sum(1 for i in parse_issues if i["severity"] == "warning"),
    }


def analysis_dict(db: Database, analysis_id: int,
                  include_findings: bool = False) -> Dict[str, Any]:
    row = db.get_analysis(analysis_id)
    if row is None:
        raise ServiceError(404, f"分析 {analysis_id} 不存在")
    score = db.get_score(row["score_id"])
    rs = db.get_rule_set(row["rule_set_id"])
    out = {
        "id": row["id"],
        "score_id": row["score_id"],
        "score_title": score["title"] if score else None,
        "rule_set_id": row["rule_set_id"],
        "rule_set_name": rs["name"] if rs else None,
        "rule_version": row["rules_snapshot"] and json.loads(row["rules_snapshot"]).get("version"),
        "species": row["species"],
        "species_name": SPECIES_NAMES.get(row["species"]) if row["species"] else None,
        "cantus_part": row["cantus_part"],
        "cantus_role": _cantus_role(score, row["cantus_part"]),
        "status": row["status"],
        "parse_issues": json.loads(row["parse_issues_json"]),
        "summary": json.loads(row["summary_json"]),
        "created_at": row["created_at"],
    }
    if include_findings:
        rows = db.query_findings(analysis_id)
        out["findings"] = [finding_dict(db, r) for r in rows]
    return out


def _cantus_role(score_row: Optional[sqlite3.Row],
                 cantus_part: Optional[str]) -> Optional[str]:
    if score_row is None or not cantus_part:
        return None
    part_ids = json.loads(score_row["part_ids"])
    if part_ids and part_ids[0] == cantus_part:
        return "upper"
    if len(part_ids) > 1 and part_ids[1] == cantus_part:
        return "lower"
    return None


def finding_dict(db: Database, row: sqlite3.Row) -> Dict[str, Any]:
    out = {
        "id": row["id"],
        "analysis_id": row["analysis_id"],
        "fingerprint": row["fingerprint"],
        "kind": row["kind"],
        "severity": row["severity"],
        "measure": row["measure"],
        "beat": row["beat"],
        "notes": json.loads(row["notes_json"]),
        "trace": json.loads(row["trace_json"]),
        "message": row["message"],
    }
    verdict = db.latest_verdict(row["id"])
    if verdict is not None:
        out["verdict"] = {
            "decision": verdict["decision"],
            "comment": verdict["comment"],
            "teacher": verdict["teacher"],
            "at": verdict["created_at"],
        }
    else:
        out["verdict"] = None
    return out


def list_findings(db: Database, analysis_id: int,
                  query: Dict[str, List[str]]) -> List[Dict[str, Any]]:
    row = db.get_analysis(analysis_id)
    if row is None:
        raise ServiceError(404, f"分析 {analysis_id} 不存在")
    kinds = query.get("kind") or None
    severities = query.get("severity") or None
    measure = None
    if query.get("measure"):
        try:
            measure = int(query["measure"][0])
        except ValueError:
            raise ServiceError(400, "measure 必须是整数")
    part_id: Optional[str] = None
    role = (query.get("role") or [None])[0]
    if role:
        if role not in ("upper", "lower"):
            raise ServiceError(400, "role 只允许 upper / lower")
        score = db.get_score(row["score_id"])
        part_ids = json.loads(score["part_ids"])  # 谱面顺序：上声部、下声部
        part_id = part_ids[0] if role == "upper" else part_ids[1]
    explicit_part = (query.get("part") or [None])[0]
    if explicit_part:
        part_id = explicit_part
    verdict = (query.get("verdict") or [None])[0]
    rows = db.query_findings(analysis_id, kinds=kinds, severities=severities,
                             measure=measure, part_id=part_id, verdict=verdict)
    return [finding_dict(db, r) for r in rows]


# ---------------------------------------------------------------------------
# 裁定
# ---------------------------------------------------------------------------

def record_verdict(db: Database, payload: Dict[str, Any]) -> Dict[str, Any]:
    finding_id = payload.get("finding_id")
    decision = payload.get("decision")
    if finding_id is None or decision is None:
        raise ServiceError(400, "缺少 finding_id 或 decision")
    from .storage import VALID_DECISIONS
    if decision not in VALID_DECISIONS:
        raise ServiceError(400, f"decision 必须是 {VALID_DECISIONS} 之一")
    finding = db.get_finding(int(finding_id))
    if finding is None:
        raise ServiceError(404, f"发现 {finding_id} 不存在")
    vid = db.add_verdict(int(finding_id), finding["analysis_id"], decision,
                         payload.get("comment"), payload.get("teacher"))
    vrow = db.get_verdict(vid)
    return {
        "id": vrow["id"],
        "finding_id": vrow["finding_id"],
        "analysis_id": vrow["analysis_id"],
        "decision": vrow["decision"],
        "comment": vrow["comment"],
        "teacher": vrow["teacher"],
        "created_at": vrow["created_at"],
    }


# ---------------------------------------------------------------------------
# 两版比对
# ---------------------------------------------------------------------------

KIND_LABELS_ZH = {
    "voice_crossing": "声部交叉",
    "voice_overlap": "声部超越",
    "parallel_fifth": "平行五度",
    "parallel_octave": "平行八度",
    "parallel_unison": "平行一度",
    "hidden_fifth": "隐伏五度",
    "hidden_octave": "隐伏八度",
    "strong_dissonance": "强拍不协和",
    "weak_dissonance_entry": "弱拍不协和-进入",
    "weak_dissonance_resolution": "弱拍不协和-解决",
    "consecutive_leaps": "连续大跳",
    "leap_not_reversed": "大跳未反向级进",
    "leap_too_large": "超过最大跳进",
    "repeated_highest": "重复最高音",
    "range_violation": "音域越界",
    "species_note_count": "对位音数量与类别不符",
    "species_attack_position": "起音位置与类别不符",
    "species_note_value": "音符时值与类别不符",
    "species_tie_missing": "第四类缺少切分延音",
    "species_tie_unexpected": "第四类强拍新起音",
    "species5_rhythm_monotony": "第五类节奏不混合",
    "start_interval_imperfect": "起始非完全协和",
    "final_interval_not_octave": "终止非一度/八度",
    "cadence_motion": "终止进行非反向级进",
    "leading_tone": "导音处理不当",
}


def compare_analyses(db: Database, base_id: int, revised_id: int,
                     persist: bool = True) -> Dict[str, Any]:
    if base_id == revised_id:
        raise ServiceError(400, "必须选择两版不同的分析进行比较")
    base = db.get_analysis(base_id)
    revised = db.get_analysis(revised_id)
    if base is None:
        raise ServiceError(404, f"基线分析 {base_id} 不存在")
    if revised is None:
        raise ServiceError(404, f"修订分析 {revised_id} 不存在")

    base_rows = {r["fingerprint"]: r for r in db.query_findings(base_id)}
    rev_rows = {r["fingerprint"]: r for r in db.query_findings(revised_id)}

    added_fp = set(rev_rows) - set(base_rows)
    removed_fp = set(base_rows) - set(rev_rows)
    kept_fp = set(base_rows) & set(rev_rows)

    def brief(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "fingerprint": row["fingerprint"],
            "kind": row["kind"],
            "kind_zh": KIND_LABELS_ZH.get(row["kind"], row["kind"]),
            "severity": row["severity"],
            "measure": row["measure"],
            "beat": row["beat"],
            "message": row["message"],
            "notes": json.loads(row["notes_json"]),
        }

    result = {
        "base_analysis_id": base_id,
        "revised_analysis_id": revised_id,
        "base_score_id": base["score_id"],
        "revised_score_id": revised["score_id"],
        "context": {
            "base": _analysis_context(base),
            "revised": _analysis_context(revised),
        },
        "counts": {
            "base_total": len(base_rows),
            "revised_total": len(rev_rows),
            "added": len(added_fp),
            "removed": len(removed_fp),
            "unchanged": len(kept_fp),
            "net_change": len(rev_rows) - len(base_rows),
        },
        "added": [brief(rev_rows[f]) for f in sorted(added_fp)],
        "removed": [brief(base_rows[f]) for f in sorted(removed_fp)],
        "unchanged": [brief(rev_rows[f]) for f in sorted(kept_fp)],
        "parse_status": {"base": base["status"], "revised": revised["status"]},
    }
    if persist:
        cid = db.insert_comparison(base_id, revised_id, result)
        result["id"] = cid
    return result


def _analysis_context(row: sqlite3.Row) -> Dict[str, Any]:
    """比对结果中保留的课型参数：类别、定旋律声部与规则版本。"""
    snapshot = json.loads(row["rules_snapshot"]) if row["rules_snapshot"] else {}
    return {
        "species": row["species"],
        "species_name": SPECIES_NAMES.get(row["species"]) if row["species"] else None,
        "cantus_part": row["cantus_part"],
        "rule_version": snapshot.get("version"),
    }


def comparison_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {"id": row["id"],
            "base_analysis_id": row["base_analysis_id"],
            "revised_analysis_id": row["revised_analysis_id"],
            "created_at": row["created_at"],
            **json.loads(row["result_json"])}


def analysis_export(db: Database, analysis_id: int) -> Dict[str, Any]:
    """下载用 JSON：分析全貌 + 规则版本 + 裁定。"""
    row = db.get_analysis(analysis_id)
    if row is None:
        raise ServiceError(404, f"分析 {analysis_id} 不存在")
    score = db.get_score(row["score_id"])
    verdicts = [
        {"finding_id": v["finding_id"], "decision": v["decision"],
         "comment": v["comment"], "teacher": v["teacher"],
         "created_at": v["created_at"]}
        for v in db.all_verdicts(analysis_id)
    ]
    return {
        "analysis": analysis_dict(db, analysis_id, include_findings=True),
        "score_meta": score_dict(score),
        "rules_version": json.loads(row["rules_snapshot"]).get("version"),
        "rules": json.loads(row["rules_snapshot"]),
        "verdicts": verdicts,
        "kind_labels_zh": KIND_LABELS_ZH,
    }
