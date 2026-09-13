# -*- coding: utf-8 -*-
"""多轮修订链核心：谱面对齐与 finding 追踪。

对齐（:func:`align_scores`）
---------------------------
两版解析结果按 **声部 → 小节 → 拍位** 三级核对：

* 声部：``part_ids`` 及其顺序必须一致；
* 小节：小节数必须一致，且逐小节拍号（``beats``/``beat_type``）一致；
* 拍位：满足前两条后，逐声部把音符按 ``(小节, 起始拍位)`` 配对——
  同一起点的音符一一对应（单声部每时刻至多一个音符，配对无歧义），
  仅一侧存在的音符记为新增/删除；
* 位置移动：槽位配对后，同一小节内某音高在旧版恰消失一次、在新版
  恰出现一次（含被误记为音高变化的换位）时，重新配对为 moved；
  跨小节移动与多候选情形不予猜测，保持删除/新增原样。

任一硬性条件不满足即 ``ok=False``（调用方拒绝加入链）。调号变化不阻断，
但记入 ``warnings``。

追踪（:func:`track_findings`）
-----------------------------
以音符对齐结果追踪上一轮每条 finding 在本轮的去向，状态机：

* ``carried``       原样保留：类型相同，相关音符的声部、时间区间、音高全未变；
* ``moved``         位置移动：类型相同，音符拍位/时值/序号变化，音高未变；
* ``pitch_changed`` 音高变化：类型相同、位置对应，但至少一个音符音高（或休止状态）变化；
* ``kind_changed``  类型变化：同一批音符对应到不同类型的 finding；
* ``resolved``      已解决：本轮无对应 finding（含相关音符被删除）；
* ``new``           新出现：本轮没有来源的 finding；
* ``ambiguous``     歧义：一项对应多个候选，**并列保留，不凭相似度自动归属**。

候选判定分两级，两阶段进行：

1. **精确键优先**：本轮 finding 的音符集合与映射键完全相同者优先；
2. **交集兜底**：无精确匹配时退到「有交集」的候选，但已被其他上一轮
   finding 精确匹配的本轮 finding 不再充当交集候选（其归属已经确定）。

交集候选或精确候选不止一个时一律标歧义，不凭相似度自动归属。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

#: 追踪状态（首轮为 initial）
TRACE_STATUSES = ("initial", "carried", "moved", "pitch_changed",
                  "kind_changed", "resolved", "new", "ambiguous")

#: 复核状态：none 无需复核 / inherited 裁定已沿用 / pending 待复核 / reviewed 已复核
REVIEW_STATES = ("none", "inherited", "pending", "reviewed")

#: 进入待复核的追踪状态：除「原样保留」与「首轮记录」外的一切变化，
#: 教师都需要确认（含已解决与新出现，确认后才算完成本轮审阅）。
PENDING_STATUSES = ("moved", "pitch_changed", "kind_changed",
                    "ambiguous", "resolved", "new")

STATUS_LABELS_ZH = {
    "initial": "首轮记录",
    "carried": "原样保留",
    "moved": "位置移动",
    "pitch_changed": "音高变化",
    "kind_changed": "类型变化",
    "resolved": "已解决",
    "new": "新出现",
    "ambiguous": "歧义（多候选）",
}


# ---------------------------------------------------------------------------
# 谱面对齐
# ---------------------------------------------------------------------------

def align_scores(prev: Dict[str, Any], curr: Dict[str, Any]) -> Dict[str, Any]:
    """对齐两版解析结果（:func:`musicxml_io.parse_score` 的返回）。

    返回结构::

        {
          "ok": bool,
          "checks": [{"level": ..., "ok": bool, "detail": str}],
          "warnings": [str],
          "note_map": {prev_event_id: {"curr": curr_event_id | None,
                                       "change": "same|pitch|duration|pitch+duration|rest_changed|removed",
                                       ...}},
          "added": [curr_event_id, ...],
          "summary": {"paired": int, "same": int, "pitch_changed": int,
                      "duration_changed": int, "added": int, "removed": int},
        }
    """
    checks: List[Dict[str, Any]] = []
    warnings: List[str] = []

    # ---- 声部 -------------------------------------------------------------
    prev_parts, curr_parts = prev["part_ids"], curr["part_ids"]
    parts_ok = prev_parts == curr_parts
    checks.append({
        "level": "part",
        "ok": parts_ok,
        "detail": "声部一致：%s" % "、".join(prev_parts) if parts_ok
                  else f"声部不一致：上一版 {prev_parts}，本版 {curr_parts}",
    })

    # ---- 小节数 ------------------------------------------------------------
    prev_ms, curr_ms = prev["measures"], curr["measures"]
    count_ok = len(prev_ms) == len(curr_ms)
    checks.append({
        "level": "measure_count",
        "ok": count_ok,
        "detail": f"小节数一致（{len(prev_ms)} 小节）" if count_ok
                  else f"小节数不一致：上一版 {len(prev_ms)} 小节，本版 {len(curr_ms)} 小节",
    })

    # ---- 逐小节拍号 ---------------------------------------------------------
    sig_ok = True
    sig_detail = ""
    if count_ok:
        bad = []
        for pm, cm in zip(prev_ms, curr_ms):
            if pm["time_sig"] != cm["time_sig"]:
                sig_ok = False
                bad.append(f"第 {pm['number']} 小节 "
                           f"{_sig(pm['time_sig'])} → {_sig(cm['time_sig'])}")
            if pm.get("key") != cm.get("key"):
                warnings.append(
                    f"第 {pm['number']} 小节调号变化："
                    f"{_key(pm.get('key'))} → {_key(cm.get('key'))}")
        sig_detail = "逐小节拍号一致" if sig_ok else "拍号不一致：" + "；".join(bad)
    else:
        sig_ok = False
        sig_detail = "小节数不一致，跳过拍号核对"
    checks.append({"level": "time_signature", "ok": sig_ok, "detail": sig_detail})

    ok = all(c["ok"] for c in checks)

    # ---- 拍位：逐声部音符配对 ------------------------------------------------
    note_map: Dict[str, Dict[str, Any]] = {}
    added: List[str] = []
    summary = {"paired": 0, "same": 0, "pitch_changed": 0, "moved": 0,
               "duration_changed": 0, "added": 0, "removed": 0}
    if ok:
        for pid in prev_parts:
            prev_events = _index_events(prev["events"], pid)
            curr_events = _index_events(curr["events"], pid)
            pairs: List[Tuple[Dict[str, Any], Dict[str, Any], str]] = []
            removed: List[Dict[str, Any]] = []
            part_added: List[Dict[str, Any]] = []
            for key in sorted(set(prev_events) | set(curr_events)):
                pe, ce = prev_events.get(key), curr_events.get(key)
                if pe is not None and ce is not None:
                    pairs.append((pe, ce, _note_change(pe, ce)))
                elif pe is not None:
                    removed.append(pe)
                else:
                    part_added.append(ce)
            # 同小节内“失而复得”的同音高音符重新配对为位置移动，
            # 避免把换位/移动误记为音高变化或增删
            pairs, removed, part_added = _repair_moved_notes(
                pairs, removed, part_added)

            for pe, ce, change in pairs:
                entry: Dict[str, Any] = {
                    "curr": ce["id"],
                    "change": change,
                    "part_id": pid,
                    "prev_measure": pe["measure"],
                    "curr_measure": ce["measure"],
                    "prev_note_index": pe["note_index"],
                    "curr_note_index": ce["note_index"],
                    "prev_start": pe["start"],
                    "curr_start": ce["start"],
                    "prev_pitch": _pitch_label(pe),
                    "curr_pitch": _pitch_label(ce),
                    "prev_duration": pe["duration"],
                    "curr_duration": ce["duration"],
                }
                note_map[pe["id"]] = entry
                summary["paired"] += 1
                if change == "same":
                    summary["same"] += 1
                elif change == "moved":
                    summary["moved"] += 1
                elif "pitch" in change or change == "rest_changed":
                    summary["pitch_changed"] += 1
                else:
                    summary["duration_changed"] += 1
            for pe in removed:
                note_map[pe["id"]] = {
                    "curr": None, "change": "removed",
                    "part_id": pid, "prev_measure": pe["measure"],
                    "prev_note_index": pe["note_index"],
                    "prev_start": pe["start"],
                    "prev_pitch": _pitch_label(pe),
                    "prev_duration": pe["duration"],
                }
                summary["removed"] += 1
            for ce in part_added:
                added.append(ce["id"])
                summary["added"] += 1

    return {"ok": ok, "checks": checks, "warnings": warnings,
            "note_map": note_map, "added": added, "summary": summary}


def _repair_moved_notes(
        pairs: List[Tuple[Dict[str, Any], Dict[str, Any], str]],
        removed: List[Dict[str, Any]],
        added: List[Dict[str, Any]]
        ) -> Tuple[List[Tuple[Dict[str, Any], Dict[str, Any], str]],
                   List[Dict[str, Any]], List[Dict[str, Any]]]:
    """同小节内“失而复得”的同音高音符重新配对为位置移动（moved）。

    槽位配对只能看到「同一拍位上的前后音符」：一个音从第 4 拍移到第 3 拍
    （或与别的音换位）时，会被误记为两处音高变化（或一删一增）。本函数在
    槽位配对之后检查：同一小节内某个音高在旧版**恰消失一次**、在新版
    **恰出现一次**时，把这两个事件重新配对为 moved。

    * 只限同一小节（跨小节的移动不予猜测，保持删除/新增原样）；
    * 消失与出现都必须唯一，否则不配对（不凭相似度自动归属）；
    * 被拆散的槽位对，其另一端回到删除/新增池，不再参与本轮配对。
    """
    lost: Dict[Tuple[int, int], List[Tuple[str, int, Dict[str, Any]]]] = {}
    gained: Dict[Tuple[int, int], List[Tuple[str, int, Dict[str, Any]]]] = {}

    def _lose(source: str, idx: int, ev: Dict[str, Any]) -> None:
        if ev.get("pitch") is None:
            return
        lost.setdefault((ev["measure_index"], ev["pitch"]["midi"]), []).append(
            (source, idx, ev))

    def _gain(source: str, idx: int, ev: Dict[str, Any]) -> None:
        if ev.get("pitch") is None:
            return
        gained.setdefault((ev["measure_index"], ev["pitch"]["midi"]), []).append(
            (source, idx, ev))

    for i, (pe, ce, change) in enumerate(pairs):
        if change in ("pitch", "pitch+duration"):
            _lose("pair", i, pe)
            _gain("pair", i, ce)
        elif change == "rest_changed":
            _lose("pair", i, pe)   # note→rest：音高消失
            _gain("pair", i, ce)   # rest→note：音高出现
    for j, pe in enumerate(removed):
        _lose("removed", j, pe)
    for j, ce in enumerate(added):
        _gain("added", j, ce)

    moved: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for key in sorted(set(lost) & set(gained)):
        ls, gs = lost[key], gained[key]
        if len(ls) == 1 and len(gs) == 1:
            moved.append((ls[0][2], gs[0][2]))
    if not moved:
        return pairs, removed, added

    moved_prev = {pe["id"] for pe, _ in moved}
    moved_curr = {ce["id"] for _, ce in moved}
    # 标记被 moved 端点拆散的槽位对
    dissolved = set()
    for i, (pe, ce, _change) in enumerate(pairs):
        if pe["id"] in moved_prev or ce["id"] in moved_curr:
            dissolved.add(i)

    new_pairs: List[Tuple[Dict[str, Any], Dict[str, Any], str]] = []
    out_removed: List[Dict[str, Any]] = []
    out_added: List[Dict[str, Any]] = []
    for i, (pe, ce, change) in enumerate(pairs):
        if i in dissolved:
            # 被拆散的对：未被 moved 消费的一端回到删除/新增池
            if pe["id"] not in moved_prev:
                out_removed.append(pe)
            if ce["id"] not in moved_curr:
                out_added.append(ce)
            continue
        new_pairs.append((pe, ce, change))
    for pe in removed:
        if pe["id"] not in moved_prev:
            out_removed.append(pe)
    for ce in added:
        if ce["id"] not in moved_curr:
            out_added.append(ce)
    for pe, ce in moved:
        new_pairs.append((pe, ce, "moved"))
    new_pairs.sort(key=lambda p: (p[0]["measure_index"], p[0]["start"]))
    return new_pairs, out_removed, out_added


def _index_events(events: List[Dict[str, Any]], part_id: str
                  ) -> Dict[Tuple[int, float], Dict[str, Any]]:
    """按 (小节序号, 小节内起始拍位) 索引一个声部的全部事件（含休止）。"""
    out: Dict[Tuple[int, float], Dict[str, Any]] = {}
    for e in events:
        if e["part_id"] != part_id:
            continue
        out[(e["measure_index"], round(e["start"], 6))] = e
    return out


def _note_change(prev_ev: Dict[str, Any], curr_ev: Dict[str, Any]) -> str:
    """比较两个已按拍位配对的音符，返回变化类别。"""
    if prev_ev["rest"] != curr_ev["rest"]:
        return "rest_changed"
    pitch_same = True
    if not prev_ev["rest"]:
        pitch_same = (prev_ev["pitch"] and curr_ev["pitch"]
                      and prev_ev["pitch"]["midi"] == curr_ev["pitch"]["midi"])
    dur_same = abs((prev_ev["duration"] or 0) - (curr_ev["duration"] or 0)) < 1e-9
    if pitch_same and dur_same:
        return "same"
    if not pitch_same and not dur_same:
        return "pitch+duration"
    return "duration" if pitch_same else "pitch"


def _pitch_label(event: Dict[str, Any]) -> Optional[str]:
    if event["rest"] or not event.get("pitch"):
        return None
    p = event["pitch"]
    acc = ""
    if p.get("alter", 0) > 0:
        acc = "#" * p["alter"]
    elif p.get("alter", 0) < 0:
        acc = "b" * (-p["alter"])
    return f"{p['step']}{acc}{p['octave']}"


def _sig(ts: Optional[Dict[str, Any]]) -> str:
    return f"{ts['beats']}/{ts['beat_type']}" if ts else "无拍号"


def _key(key: Optional[Dict[str, Any]]) -> str:
    if not key:
        return "无调号"
    return f"fifths={key.get('fifths')} mode={key.get('mode')}"


# ---------------------------------------------------------------------------
# finding 追踪
# ---------------------------------------------------------------------------

def track_findings(prev_findings: List[Dict[str, Any]],
                   curr_findings: List[Dict[str, Any]],
                   alignment: Dict[str, Any]
                   ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """按音符对齐结果追踪上一轮 finding 到本轮。

    ``prev_findings`` / ``curr_findings`` 为带 ``id``、``kind``、``notes``
    （每项含 ``event_id``）的 dict。返回 ``(traces, summary)``；
    每条 trace 含 ``status``、``prev_finding_id``、``curr_finding_id``、
    ``evidence``（判定依据，逐轮时间线直接展示）。
    """
    note_map = alignment["note_map"]
    curr_by_id = {f["id"]: f for f in curr_findings}
    # 本轮 finding 的音符键索引：event_id -> [finding_id]
    curr_note_index: Dict[str, List[str]] = {}
    curr_keys: Dict[int, frozenset] = {}
    for f in curr_findings:
        key = frozenset(n["event_id"] for n in f["notes"])
        curr_keys[f["id"]] = key
        for eid in key:
            curr_note_index.setdefault(eid, []).append(f["id"])

    # ---- 第一阶段：计算每条上一轮 finding 的映射键与精确匹配 ----------------
    prepared: List[Dict[str, Any]] = []
    exact_claimed: set = set()  # 已被精确匹配的本轮 finding
    for pf in prev_findings:
        removed_notes: List[Dict[str, Any]] = []
        mapped_ids: List[str] = []
        note_alignment: List[Dict[str, Any]] = []
        for n in pf["notes"]:
            entry = note_map.get(n["event_id"])
            if entry is None or entry["curr"] is None:
                removed_notes.append({
                    "event_id": n["event_id"], "part_id": n["part_id"],
                    "measure": n["measure"], "note_index": n["note_index"],
                    "pitch": n.get("pitch"),
                })
                note_alignment.append({"prev": n["event_id"], "curr": None,
                                       "change": "removed"})
            else:
                mapped_ids.append(entry["curr"])
                na: Dict[str, Any] = {"prev": n["event_id"],
                                      "curr": entry["curr"],
                                      "change": entry["change"]}
                for field in ("prev_measure", "curr_measure",
                              "prev_note_index", "curr_note_index",
                              "prev_start", "curr_start",
                              "prev_pitch", "curr_pitch",
                              "prev_duration", "curr_duration"):
                    if field in entry:
                        na[field] = entry[field]
                note_alignment.append(na)
        mapped_key = frozenset(mapped_ids)
        exact_ids = [] if removed_notes else [
            f["id"] for f in curr_findings if curr_keys[f["id"]] == mapped_key]
        exact_claimed.update(exact_ids)
        prepared.append({"pf": pf, "removed_notes": removed_notes,
                         "note_alignment": note_alignment,
                         "mapped_key": mapped_key, "exact_ids": exact_ids})

    # ---- 第二阶段：逐条归类 --------------------------------------------------
    traces: List[Dict[str, Any]] = []
    claimed_curr: set = set()  # 被任何候选集引用的本轮 finding

    for item in prepared:
        pf = item["pf"]
        note_alignment = item["note_alignment"]
        mapped_key = item["mapped_key"]

        if item["removed_notes"]:
            traces.append({
                "status": "resolved",
                "prev_finding_id": pf["id"],
                "curr_finding_id": None,
                "evidence": {
                    "rule": "相关音符在本版中被删除/替换，原问题不再成立",
                    "removed_notes": item["removed_notes"],
                    "note_alignment": note_alignment,
                },
            })
            continue

        # 候选分两级：精确键优先；交集兜底时排除已被精确匹配的本轮 finding。
        if item["exact_ids"]:
            cand_ids = item["exact_ids"]
            match_level = "exact"
        else:
            cand_ids = []
            for eid in mapped_key:
                for fid in curr_note_index.get(eid, []):
                    if fid not in exact_claimed and fid not in cand_ids:
                        cand_ids.append(fid)
            match_level = "overlap"
        claimed_curr.update(cand_ids)

        if not cand_ids:
            traces.append({
                "status": "resolved",
                "prev_finding_id": pf["id"],
                "curr_finding_id": None,
                "evidence": {
                    "rule": "本轮分析中不存在引用相关音符的 finding，原问题已消除",
                    "note_alignment": note_alignment,
                },
            })
            continue

        if len(cand_ids) > 1:
            traces.append({
                "status": "ambiguous",
                "prev_finding_id": pf["id"],
                "curr_finding_id": None,
                "evidence": {
                    "rule": "一项对应多个候选，按并列歧义处理，不凭相似度自动归属",
                    "match_level": match_level,
                    "note_alignment": note_alignment,
                    "candidates": [_candidate_brief(curr_by_id[i], mapped_key,
                                                  curr_keys[i]) for i in cand_ids],
                },
            })
            continue

        cf = curr_by_id[cand_ids[0]]
        exact_key = match_level == "exact"
        same_kind = cf["kind"] == pf["kind"]
        if not same_kind:
            status = "kind_changed"
            rule = (f"相关音符对应的 finding 类型由 {pf['kind']} 变为 {cf['kind']}"
                    if exact_key else
                    f"相关音符部分对应到不同类型的 finding（{pf['kind']} → {cf['kind']}）")
        elif not exact_key:
            status = "moved"
            rule = "类型相同，但 finding 的音符构成发生变化（增删了相关音符）"
        else:
            changes = {a["change"] for a in note_alignment}
            if changes == {"same"}:
                status = "carried"
                rule = "类型相同，相关音符的声部、时间区间与音高均未变"
            elif changes <= {"same", "duration", "moved"}:
                status = "moved"
                rule = "类型相同、音高未变，但相关音符的位置（拍位/时值/序号）变化"
            else:
                status = "pitch_changed"
                rule = "类型相同、位置对应，但至少一个相关音符的音高/休止状态变化"
        traces.append({
            "status": status,
            "prev_finding_id": pf["id"],
            "curr_finding_id": cf["id"],
            "evidence": {
                "rule": rule,
                "note_alignment": note_alignment,
                "prev_kind": pf["kind"],
                "curr_kind": cf["kind"],
                "key_exact": exact_key,
            },
        })

    # 本轮无来源的 finding → new
    for cf in curr_findings:
        if cf["id"] in claimed_curr:
            continue
        traces.append({
            "status": "new",
            "prev_finding_id": None,
            "curr_finding_id": cf["id"],
            "evidence": {
                "rule": "本轮新出现的 finding，上一版没有可对应的来源",
                "curr_kind": cf["kind"],
            },
        })

    summary = {"prev_total": len(prev_findings), "curr_total": len(curr_findings)}
    for st in TRACE_STATUSES:
        summary[st] = sum(1 for t in traces if t["status"] == st)
    return traces, summary


def _candidate_brief(finding: Dict[str, Any], mapped_key: frozenset,
                     own_key: frozenset) -> Dict[str, Any]:
    return {
        "finding_id": finding["id"],
        "kind": finding["kind"],
        "measure": finding.get("measure"),
        "beat": finding.get("beat"),
        "message": finding.get("message"),
        "shared_notes": len(own_key & mapped_key),
        "total_notes": len(own_key),
        "key_exact": own_key == mapped_key,
    }
