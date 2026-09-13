# -*- coding: utf-8 -*-
"""分析编排：连接存储层、解析器、规则引擎与比对逻辑。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from . import counterpoint, musicxml_io, revision, rulesconfig as rc
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
                     ) -> Tuple[int, str, str]:
    """解析并校验 species / cantus 参数（**必填**）。

    返回 ``(species, cantus_role, cantus_part)``。类别非法、定旋律声部
    不存在、参数缺失，均抛 400——绝不擅自改类或默认猜测。
    """
    species = payload.get("species")
    cantus = payload.get("cantus")
    missing = []
    if species is None:
        missing.append("species（对位类别 1–5）")
    if cantus is None:
        missing.append("cantus（定旋律声部 upper / lower / part_id）")
    if missing:
        raise ServiceError(
            400, "创建分析必须指定 " + " 与 ".join(missing)
                 + "；系统不擅自选择类别或声部")
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


# ---------------------------------------------------------------------------
# 修订链
# ---------------------------------------------------------------------------

def create_chain(db: Database, payload: Dict[str, Any]) -> Dict[str, Any]:
    """以一份分析为起点建链。链内冻结 species / cantus_part / 规则版本。"""
    analysis_id = payload.get("analysis_id")
    if analysis_id is None:
        raise ServiceError(400, "缺少 analysis_id")
    row = db.get_analysis(int(analysis_id))
    if row is None:
        raise ServiceError(404, f"分析 {analysis_id} 不存在")
    if row["status"] != "ok":
        raise ServiceError(400, "分析存在解析错误，不能作为修订链起点",
                           {"analysis_id": analysis_id, "status": row["status"]})
    if db.analysis_chain(row["id"]) is not None:
        raise ServiceError(400, f"分析 {analysis_id} 已属于某条修订链，不能重复入链")
    if row["species"] is None or not row["cantus_part"]:
        raise ServiceError(400, "分析缺少 species / cantus_part，无法建链")

    rule_version = json.loads(row["rules_snapshot"]).get("version")
    title = payload.get("title") or f"修订链（分析 #{row['id']} 起）"
    chain_id = db.insert_chain(title, row["species"], row["cantus_part"],
                               row["rule_set_id"], rule_version)
    revision_id = db.insert_revision(chain_id, 1, row["id"], row["score_id"],
                                     alignment=None, tracking_summary=None)
    # 首轮：每条 finding 记一条 initial 追踪，作为后续轮的基准
    now_rows = []
    for f in db.query_findings(row["id"]):
        now_rows.append((chain_id, revision_id, 1, "initial", None, f["id"],
                         {"rule": "链起点：首轮分析的发现，作为后续轮次追踪基准",
                          "kind": f["kind"]},
                         "none", None))
    if now_rows:
        db.insert_traces(now_rows)
    return chain_dict(db, chain_id)


def append_revision(db: Database, chain_id: int,
                    payload: Dict[str, Any]) -> Dict[str, Any]:
    """向链尾追加一轮分析。

    校验链内一致性（species / cantus_part / 规则版本），并对最新一轮的
    谱面做声部-小节-拍位对齐；无法对齐时拒绝加入。随后按音符对齐结果
    追踪上一轮 finding，写出本轮追踪记录与裁定沿用/待复核状态。
    """
    chain = db.get_chain(chain_id)
    if chain is None:
        raise ServiceError(404, f"修订链 {chain_id} 不存在")
    analysis_id = payload.get("analysis_id")
    if analysis_id is None:
        raise ServiceError(400, "缺少 analysis_id")
    analysis = db.get_analysis(int(analysis_id))
    if analysis is None:
        raise ServiceError(404, f"分析 {analysis_id} 不存在")
    if analysis["status"] != "ok":
        raise ServiceError(400, "分析存在解析错误，不能加入修订链",
                           {"analysis_id": analysis_id, "status": analysis["status"]})
    if db.analysis_chain(analysis["id"]) is not None:
        raise ServiceError(400, f"分析 {analysis_id} 已属于某条修订链，不能重复入链")

    # ---- 链内一致性：species / cantus_part / 规则版本 ----------------------
    mismatches = []
    if analysis["species"] != chain["species"]:
        mismatches.append({
            "field": "species",
            "chain": chain["species"],
            "analysis": analysis["species"],
            "detail": f"链为{SPECIES_NAMES.get(chain['species'])}，"
                      f"该分析为{SPECIES_NAMES.get(analysis['species'])}",
        })
    if analysis["cantus_part"] != chain["cantus_part"]:
        mismatches.append({
            "field": "cantus_part",
            "chain": chain["cantus_part"],
            "analysis": analysis["cantus_part"],
            "detail": f"链内定旋律声部为 {chain['cantus_part']}，"
                      f"该分析为 {analysis['cantus_part']}",
        })
    analysis_rule_version = json.loads(analysis["rules_snapshot"]).get("version")
    if analysis_rule_version != chain["rule_version"]:
        mismatches.append({
            "field": "rule_version",
            "chain": chain["rule_version"],
            "analysis": analysis_rule_version,
            "detail": "规则版本不一致；请用与链相同的规则集创建分析",
        })
    if mismatches:
        raise ServiceError(400, "分析与修订链的课型参数不一致，拒绝加入", mismatches)

    # ---- 与最新一轮做谱面对齐 ----------------------------------------------
    prev_rev = db.latest_revision(chain_id)
    prev_analysis = db.get_analysis(prev_rev["analysis_id"])
    prev_score = db.get_score(prev_analysis["score_id"])
    curr_score = db.get_score(analysis["score_id"])
    prev_parsed = musicxml_io.parse_score(prev_score["xml"].encode("utf-8"))
    curr_parsed = musicxml_io.parse_score(curr_score["xml"].encode("utf-8"))
    alignment = revision.align_scores(prev_parsed, curr_parsed)
    if not alignment["ok"]:
        raise ServiceError(
            400, "两版谱面无法按声部、小节、拍位对齐，拒绝加入修订链",
            {"checks": alignment["checks"],
             "prev_score_id": prev_score["id"],
             "curr_score_id": curr_score["id"]})

    # ---- finding 追踪 -------------------------------------------------------
    prev_findings = _tracking_findings(db, prev_analysis["id"])
    curr_findings = _tracking_findings(db, analysis["id"])
    traces, summary = revision.track_findings(prev_findings, curr_findings,
                                              alignment)

    seq = prev_rev["seq"] + 1
    revision_id = db.insert_revision(chain_id, seq, analysis["id"],
                                     analysis["score_id"], alignment, summary)

    # ---- 裁定沿用与待复核 ----------------------------------------------------
    prev_verdicts = {f["id"]: db.latest_verdict(f["id"]) for f in prev_findings}
    trace_rows = []
    inherited: List[Dict[str, Any]] = []
    for t in traces:
        review_state = "none"
        verdict_source = None
        pv = prev_verdicts.get(t["prev_finding_id"]) if t["prev_finding_id"] else None
        if t["status"] == "carried" and pv is not None:
            # finding 及关联音符未变：沿用教师裁定
            new_vid = db.add_verdict(
                t["curr_finding_id"], analysis["id"], pv["decision"],
                f"[沿用第{seq - 1}轮裁定] {pv['comment'] or ''}".strip(),
                pv["teacher"])
            review_state = "inherited"
            verdict_source = _verdict_source(t["prev_finding_id"], pv, applied=True)
            inherited.append({"trace_prev_finding_id": t["prev_finding_id"],
                              "curr_finding_id": t["curr_finding_id"],
                              "verdict_id": new_vid,
                              "decision": pv["decision"]})
        elif t["status"] in revision.PENDING_STATUSES:
            # 其他情况（含已解决与新出现）：进入待复核，保留来源裁定供教师参考
            review_state = "pending"
            if pv is not None:
                verdict_source = _verdict_source(t["prev_finding_id"], pv,
                                                 applied=False)
        trace_rows.append((chain_id, revision_id, seq, t["status"],
                           t["prev_finding_id"], t["curr_finding_id"],
                           t["evidence"], review_state, verdict_source))
    if trace_rows:
        db.insert_traces(trace_rows)

    return {
        "chain_id": chain_id,
        "revision_id": revision_id,
        "seq": seq,
        "analysis_id": analysis["id"],
        "score_id": analysis["score_id"],
        "alignment": {
            "checks": alignment["checks"],
            "warnings": alignment["warnings"],
            "summary": alignment["summary"],
        },
        "tracking_summary": summary,
        "verdicts_inherited": inherited,
        "traces": [trace_dict(db, r) for r in
                   db.query_traces(chain_id, seq=seq)],
    }


def _tracking_findings(db: Database, analysis_id: int) -> List[Dict[str, Any]]:
    """供追踪算法使用的 finding 简表（含 id/kind/notes）。"""
    return [{
        "id": r["id"], "kind": r["kind"], "measure": r["measure"],
        "beat": r["beat"], "message": r["message"],
        "notes": json.loads(r["notes_json"]),
    } for r in db.query_findings(analysis_id)]


def _verdict_source(prev_finding_id: int, verdict: sqlite3.Row,
                    applied: bool) -> Dict[str, Any]:
    return {
        "prev_finding_id": prev_finding_id,
        "applied": applied,
        "verdict": {"id": verdict["id"], "decision": verdict["decision"],
                    "comment": verdict["comment"], "teacher": verdict["teacher"],
                    "at": verdict["created_at"]},
    }


def trace_dict(db: Database, row: sqlite3.Row) -> Dict[str, Any]:
    out = {
        "id": row["id"],
        "chain_id": row["chain_id"],
        "revision_id": row["revision_id"],
        "seq": row["seq"],
        "status": row["status"],
        "status_zh": revision.STATUS_LABELS_ZH.get(row["status"], row["status"]),
        "prev_finding_id": row["prev_finding_id"],
        "curr_finding_id": row["curr_finding_id"],
        "evidence": json.loads(row["evidence_json"]),
        "review_state": row["review_state"],
        "verdict_source": (json.loads(row["verdict_source_json"])
                           if row["verdict_source_json"] else None),
        "review_decision": row["review_decision"],
        "review_comment": row["review_comment"],
        "review_teacher": row["review_teacher"],
        "reviewed_at": row["reviewed_at"],
        "created_at": row["created_at"],
    }
    for side, col in (("prev_finding", row["prev_finding_id"]),
                      ("curr_finding", row["curr_finding_id"])):
        if col is None:
            out[side] = None
            continue
        f = db.get_finding(col)
        out[side] = None if f is None else {
            "id": f["id"], "kind": f["kind"],
            "kind_zh": KIND_LABELS_ZH.get(f["kind"], f["kind"]),
            "severity": f["severity"], "measure": f["measure"],
            "beat": f["beat"], "message": f["message"],
            "notes": json.loads(f["notes_json"]),
        }
    return out


def _revision_brief(db: Database, row: sqlite3.Row) -> Dict[str, Any]:
    analysis = db.get_analysis(row["analysis_id"])
    return {
        "id": row["id"],
        "seq": row["seq"],
        "analysis_id": row["analysis_id"],
        "score_id": row["score_id"],
        "analysis_status": analysis["status"] if analysis else None,
        "findings_total": (json.loads(analysis["summary_json"])["total"]
                           if analysis else None),
        "tracking_summary": (json.loads(row["tracking_summary_json"])
                             if row["tracking_summary_json"] else None),
        "created_at": row["created_at"],
    }


def chain_dict(db: Database, chain_id: int,
               include_revisions: bool = True) -> Dict[str, Any]:
    row = db.get_chain(chain_id)
    if row is None:
        raise ServiceError(404, f"修订链 {chain_id} 不存在")
    revisions = db.list_revisions(chain_id)
    pending = db.query_traces(chain_id, review_states=["pending"])
    out = {
        "id": row["id"],
        "title": row["title"],
        "species": row["species"],
        "species_name": SPECIES_NAMES.get(row["species"]),
        "cantus_part": row["cantus_part"],
        "rule_set_id": row["rule_set_id"],
        "rule_version": row["rule_version"],
        "created_at": row["created_at"],
        "revision_count": len(revisions),
        "pending_review_count": len(pending),
    }
    if include_revisions:
        out["revisions"] = [_revision_brief(db, r) for r in revisions]
    return out


def chain_timeline(db: Database, chain_id: int) -> Dict[str, Any]:
    """修订时间线：逐轮列出追踪结果与判定依据。"""
    chain = chain_dict(db, chain_id, include_revisions=False)
    rounds = []
    for rev in db.list_revisions(chain_id):
        traces = [trace_dict(db, t)
                  for t in db.query_traces(chain_id, seq=rev["seq"])]
        entry = _revision_brief(db, rev)
        entry["alignment"] = (json.loads(rev["alignment_json"])
                              if rev["alignment_json"] else None)
        if entry["alignment"] is not None:
            # 时间线保留对齐检查与统计，完整音符映射见下载接口
            entry["alignment"] = {
                "checks": entry["alignment"]["checks"],
                "warnings": entry["alignment"]["warnings"],
                "summary": entry["alignment"]["summary"],
            }
        entry["traces"] = traces
        rounds.append(entry)
    return {"chain": chain, "rounds": rounds,
            "status_labels_zh": revision.STATUS_LABELS_ZH}


def list_reviews(db: Database, chain_id: int,
                 query: Dict[str, List[str]]) -> Dict[str, Any]:
    """待复核筛选：按复核状态（默认 pending）与轮次过滤追踪记录。"""
    if db.get_chain(chain_id) is None:
        raise ServiceError(404, f"修订链 {chain_id} 不存在")
    states = query.get("state") or ["pending"]
    if states == ["all"]:
        states = None
    elif any(s not in revision.REVIEW_STATES for s in states):
        raise ServiceError(400, f"state 只允许 {revision.REVIEW_STATES} 或 all")
    seq = None
    if query.get("seq"):
        try:
            seq = int(query["seq"][0])
        except ValueError:
            raise ServiceError(400, "seq 必须是整数")
    rows = db.query_traces(chain_id, seq=seq, review_states=states)
    return {"chain_id": chain_id,
            "filter": {"state": states or "all", "seq": seq},
            "count": len(rows),
            "reviews": [trace_dict(db, r) for r in rows]}


def submit_review(db: Database, chain_id: int,
                  payload: Dict[str, Any]) -> Dict[str, Any]:
    """复核一条待复核追踪：可同时对本轮 finding 记录裁定。

    * 有本轮 finding 的条目（new/moved/pitch_changed/kind_changed）：
      ``decision`` 会作为裁定写入该 finding；
    * 已解决（resolved）条目没有本轮 finding，``decision`` 连同备注、
      教师一起记录在追踪记录本身（review_decision 等字段）；
    * 歧义（ambiguous）条目可用 ``chosen_finding_id`` 在候选中指定归属，
      指定后裁定落在该候选上。
    """
    if db.get_chain(chain_id) is None:
        raise ServiceError(404, f"修订链 {chain_id} 不存在")
    trace_id = payload.get("trace_id")
    if trace_id is None:
        raise ServiceError(400, "缺少 trace_id")
    trace = db.get_trace(int(trace_id))
    if trace is None or trace["chain_id"] != chain_id:
        raise ServiceError(404, f"追踪记录 {trace_id} 不在链 {chain_id} 中")
    if trace["review_state"] != "pending":
        raise ServiceError(400, f"追踪记录 {trace_id} 不在待复核状态"
                                f"（当前 {trace['review_state']}）")

    chosen = payload.get("chosen_finding_id")
    if chosen is not None:
        chosen = int(chosen)
        if trace["status"] != "ambiguous":
            raise ServiceError(400, "只有歧义（ambiguous）条目支持 chosen_finding_id")
        evidence = json.loads(trace["evidence_json"])
        cand_ids = [c["finding_id"] for c in evidence.get("candidates", [])]
        if chosen not in cand_ids:
            raise ServiceError(400, f"finding {chosen} 不在候选列表 {cand_ids} 中")

    decision = payload.get("decision")
    if decision is not None:
        from .storage import VALID_DECISIONS
        if decision not in VALID_DECISIONS:
            raise ServiceError(400, f"decision 必须是 {VALID_DECISIONS} 之一")
    verdict = None
    if decision is not None:
        target = chosen if chosen is not None else trace["curr_finding_id"]
        if target is None and trace["status"] == "ambiguous":
            raise ServiceError(400, "歧义条目请先给 chosen_finding_id 再裁定")
        if target is not None:
            # 有本轮 finding：裁定写入该 finding（resolved 无本轮 finding，
            # 裁定只记录在追踪记录上）
            verdict = record_verdict(db, {
                "finding_id": target, "decision": decision,
                "comment": payload.get("comment"),
                "teacher": payload.get("teacher")})
    db.mark_trace_reviewed(trace["id"], curr_finding_id=chosen,
                           decision=decision,
                           comment=payload.get("comment"),
                           teacher=payload.get("teacher"))
    out = trace_dict(db, db.get_trace(trace["id"]))
    if verdict is not None:
        out["verdict_recorded"] = verdict
    return out


def compare_chains(db: Database, payload: Dict[str, Any]) -> Dict[str, Any]:
    """链间比较：逐轮统计对照 + 两链最新一轮的 finding 增减。"""
    id_a, id_b = payload.get("chain_id_a"), payload.get("chain_id_b")
    if id_a is None or id_b is None:
        raise ServiceError(400, "缺少 chain_id_a / chain_id_b")
    if int(id_a) == int(id_b):
        raise ServiceError(400, "必须选择两条不同的链进行比较")
    chain_a, chain_b = db.get_chain(int(id_a)), db.get_chain(int(id_b))
    if chain_a is None:
        raise ServiceError(404, f"修订链 {id_a} 不存在")
    if chain_b is None:
        raise ServiceError(404, f"修订链 {id_b} 不存在")

    def rounds(chain_id: int) -> List[Dict[str, Any]]:
        return [_revision_brief(db, r) for r in db.list_revisions(chain_id)]

    latest_a = db.latest_revision(chain_a["id"])
    latest_b = db.latest_revision(chain_b["id"])
    latest_cmp = compare_analyses(db, latest_a["analysis_id"],
                                  latest_b["analysis_id"], persist=False)
    return {
        "chain_a": {**chain_dict(db, chain_a["id"], include_revisions=False),
                    "rounds": rounds(chain_a["id"])},
        "chain_b": {**chain_dict(db, chain_b["id"], include_revisions=False),
                    "rounds": rounds(chain_b["id"])},
        "compatible": bool(
            chain_a["species"] == chain_b["species"]
            and chain_a["cantus_part"] == chain_b["cantus_part"]
            and chain_a["rule_version"] == chain_b["rule_version"]),
        "latest_comparison": latest_cmp,
    }


def chain_export(db: Database, chain_id: int) -> Dict[str, Any]:
    """下载用 JSON：链全貌 + 逐轮对齐/追踪依据 + 裁定历史。"""
    timeline = chain_timeline(db, chain_id)
    # 下载版补齐每轮完整对齐结果（含音符映射）
    for rev_row, entry in zip(db.list_revisions(chain_id), timeline["rounds"]):
        entry["alignment"] = (json.loads(rev_row["alignment_json"])
                              if rev_row["alignment_json"] else None)
        entry["verdicts"] = [
            {"finding_id": v["finding_id"], "decision": v["decision"],
             "comment": v["comment"], "teacher": v["teacher"],
             "created_at": v["created_at"]}
            for v in db.all_verdicts(rev_row["analysis_id"])
        ]
    timeline["kind_labels_zh"] = KIND_LABELS_ZH
    return timeline
