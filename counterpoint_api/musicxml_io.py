# -*- coding: utf-8 -*-
"""MusicXML 解析器。

只做一件事：**忠实还原** 两声部谱面。按 ``divisions`` 把每个音符/休止
换算到统一的四分音符时间轴上，解析拍号、调号、声部、音高、休止、附点
与跨小节延音（``<tie>`` / ``<tied>``）。

硬性原则
--------
* **不补全**：divisions/拍号/时值缺失，一律作为 ``error`` 级记谱问题报出，
  绝不假设默认值。
* **可定位**：每条问题都带声部 (part_id / role)、小节号、音符序号。
* 出现任何 ``error`` 时调用方必须中止分析；``warning``（如延音未配对、
  未知 type 字符串）不阻塞分析。

支持范围：``score-partwise``、两声部（两个 ``<part>``）、每声部单 ``<voice>``。
不支持 ``score-timewise``、和弦、单声部内多 voice、装饰音、未指定音高记谱。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

_STEP_TO_SEMI = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


class ParseAborted(Exception):
    """XML 根本无法解析（非良构或根元素错误）。"""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _ln(element: ET.Element) -> str:
    """去掉 XML 命名空间后的标签名。"""
    tag = element.tag
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


def _child(element: ET.Element, name: str) -> Optional[ET.Element]:
    for child in element:
        if _ln(child) == name:
            return child
    return None


def _children(element: ET.Element, name: str) -> List[ET.Element]:
    return [c for c in element if _ln(c) == name]


def _text(element: ET.Element, name: str) -> Optional[str]:
    child = _child(element, name)
    if child is None or child.text is None:
        return None
    return child.text.strip()


def _int(element: ET.Element, name: str) -> Optional[int]:
    raw = _text(element, name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _pitch_midi(pitch: Dict[str, Any]) -> int:
    return (pitch["octave"] + 1) * 12 + _STEP_TO_SEMI[pitch["step"]] + pitch.get("alter", 0)


# ---------------------------------------------------------------------------
# 公开入口
# ---------------------------------------------------------------------------

def parse_score(xml_bytes: bytes) -> Dict[str, Any]:
    """解析 score-partwise 文档。

    返回结构（节选）::

        {
          "title": str | None,
          "part_ids": ["P1", "P2"],
          "roles": {"P1": "upper", "P2": "lower"},   # 按谱面顺序
          "divisions_initial": int,
          "measures": [ {number, index, start, length,
                         time_sig: {beats, beat_type}, key: {fifths, mode}} ... ],
          "events": [ {id, part_id, role, measure, measure_index, note_index,
                       start, duration, pitch, rest, dots, type,
                       tie_start, tie_stop, voice} ... ],
          "parse_issues": [ {severity, code, message, part_id, measure, note_index} ... ],
        }

    时间单位统一为 **四分音符**（``start`` / ``duration`` / 小节长度），
    即音符原始 duration 除以当时生效的 divisions。
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ParseAborted(f"XML 解析失败：{exc}") from exc

    root_name = _ln(root)
    issues: List[Dict[str, Any]] = []

    if root_name == "score-timewise":
        raise ParseAborted("仅支持 score-partwise 文档，收到 score-timewise，请先转换。")
    if root_name != "score-partwise":
        raise ParseAborted(f"根元素不是 score-partwise：{root_name!r}")

    title = _extract_title(root)
    part_list = _extract_part_list(root)
    if part_list is None:
        raise ParseAborted("缺少 <part-list>，无法识别声部。")
    if len(part_list) != 2:
        raise ParseAborted(f"本服务只审阅两声部作业，谱面声明了 {len(part_list)} 个声部："
                           f"{', '.join(part_list)}。")

    part_elements: List[Tuple[str, ET.Element]] = []
    for pid in part_list:
        matched = None
        for pel in _children(root, "part"):
            if pel.get("id") == pid:
                matched = pel
                break
        if matched is None:
            raise ParseAborted(f"声部 {pid} 在 part-list 中声明，但找不到对应 <part> 内容。")
        part_elements.append((pid, matched))

    # 分别解析两个声部
    parsed_parts: List[Dict[str, Any]] = []
    for pid, pel in part_elements:
        parsed_parts.append(_parse_part(pid, pel))

    # 声部数量与小节一致性
    measure_lists = [p["measures"] for p in parsed_parts]
    if len({len(m) for m in measure_lists}) != 1:
        counts = {pid: len(p["measures"]) for pid, p in zip(part_list, parsed_parts)}
        issues.append(_issue("error", "measure_count_mismatch",
                             f"两声部小节数不一致：{counts}", None, None, None))

    # 以第一声部为基准建立小节表，并交叉核对拍号
    base = parsed_parts[0]
    measures: List[Dict[str, Any]] = []
    cursor = 0.0
    for idx, m in enumerate(base["measures"]):
        if m["time_sig"] is None:
            issues.append(_issue("error", "missing_time_signature",
                                 "缺少拍号，无法确定小节长度与强弱拍（系统不自行补全）。",
                                 part_list[0], m["number"], None))
            length = 0.0
        else:
            length = m["beats"] * 4.0 / m["beat_type"]
        measures.append({
            "number": m["number"],
            "index": idx,
            "start": cursor,
            "length": length,
            "time_sig": m["time_sig"],
            "key": m["key"],
        })
        cursor += length

    for part_idx, parsed in enumerate(parsed_parts[1:], start=1):
        pid = part_list[part_idx]
        for idx, m in enumerate(parsed["measures"]):
            if idx >= len(measures):
                break
            base_m = base["measures"][idx]
            if m["time_sig"] != base_m["time_sig"]:
                issues.append(_issue("error", "time_signature_conflict",
                                     f"两声部在第 {m['number']} 小节拍号不一致："
                                     f"{base_m['time_sig']} vs {m['time_sig']}",
                                     pid, m["number"], None))
            if m["number"] != base_m["number"]:
                issues.append(_issue("warning", "measure_number_conflict",
                                     f"两声部第 {idx + 1} 小节编号不一致："
                                     f"{base_m['number']} vs {m['number']}",
                                     pid, m["number"], None))

    # 汇总问题与事件，声部角色按谱面顺序
    roles = {part_list[0]: "upper", part_list[1]: "lower"}
    for part_idx, parsed in enumerate(parsed_parts):
        pid = part_list[part_idx]
        for iss in parsed["issues"]:
            iss.setdefault("part_id", pid)
            issues.append(iss)

    events: List[Dict[str, Any]] = []
    divisions_initial = parsed_parts[0].get("divisions_initial")
    for part_idx, parsed in enumerate(parsed_parts):
        pid = part_list[part_idx]
        if parsed.get("divisions_initial") is None:
            issues.append(_issue("error", "missing_divisions",
                                 "声部缺少 <divisions> 声明，无法按 divisions 还原时间位置。",
                                 pid, None, None))
        elif part_idx == 0:
            divisions_initial = parsed["divisions_initial"]
        elif parsed["divisions_initial"] != divisions_initial:
            # 开头 divisions 不同本身不报错（后续各自换算），仅记录
            pass

        for ev in parsed["events"]:
            ev["part_id"] = pid
            ev["role"] = roles[pid]
            events.append(ev)

    events.sort(key=lambda e: (0 if e["role"] == "upper" else 1,
                               e["measure_index"], e["start"], e["_order"]))

    _check_ties(part_list, parsed_parts, issues)

    fatal = any(i["severity"] == "error" for i in issues)
    return {
        "title": title,
        "part_ids": part_list,
        "roles": roles,
        "divisions_initial": divisions_initial,
        "measures": measures,
        "events": events,
        "parse_issues": issues,
        "fatal": fatal,
    }


# ---------------------------------------------------------------------------
# 头部信息
# ---------------------------------------------------------------------------

def _extract_title(root: ET.Element) -> Optional[str]:
    work = _child(root, "work")
    if work is not None:
        title = _text(work, "work-title")
        if title:
            return title
    title = None
    for part_list_el in _children(root, "part-list"):
        break
    identification = _child(root, "identification")
    if identification is not None:
        for typ in _children(identification, "encoding"):
            pass
    # movement-title 是常见的标题位置
    movement = _child(root, "movement-title")
    if movement is not None and movement.text:
        return movement.text.strip()
    if identification is not None:
        for creator in _children(identification, "creator"):
            if creator.get("type", "").lower() == "title" and creator.text:
                return creator.text.strip()
    return None


def _extract_part_list(root: ET.Element) -> Optional[List[str]]:
    part_list = _child(root, "part-list")
    if part_list is None:
        return None
    ids = []
    for score_part in _children(part_list, "score-part"):
        pid = score_part.get("id")
        if pid:
            ids.append(pid)
    return ids


# ---------------------------------------------------------------------------
# 单声部解析
# ---------------------------------------------------------------------------

def _parse_part(pid: str, part_el: ET.Element) -> Dict[str, Any]:
    issues: List[Dict[str, Any]] = []
    events: List[Dict[str, Any]] = []
    measures: List[Dict[str, Any]] = []

    divisions: Optional[int] = None
    divisions_initial: Optional[int] = None
    current_key: Optional[Dict[str, Any]] = None
    current_time: Optional[Dict[str, Any]] = None
    order_counter = 0

    for m_idx, measure in enumerate(_children(part_el, "measure")):
        number_attr = measure.get("number")
        try:
            number = int(number_attr) if number_attr is not None else m_idx + 1
        except ValueError:
            number = m_idx + 1
            issues.append(_issue("warning", "non_numeric_measure_number",
                                 f"小节编号 {number_attr!r} 不是整数，按顺序记作 {number}。",
                                 pid, number, None))

        # 小节内的 attributes（可能在小节中间出现，divisions 只允许在小节边界变化）
        cursor = 0.0  # 本小节内偏移，四分音符单位
        note_index = 0
        seen_note_in_measure = False
        measure_info: Dict[str, Any] = {
            "number": number, "index": m_idx,
            "time_sig": None, "beats": None, "beat_type": None, "key": None,
        }

        for el in measure:
            name = _ln(el)
            if name == "attributes":
                if seen_note_in_measure and _child(el, "divisions") is not None:
                    issues.append(_issue("error", "mid_measure_divisions_change",
                                         "divisions 只允许在小节边界改变；小节中途改变无法可靠还原。",
                                         pid, number, None))
                div = _int(el, "divisions")
                if div is not None:
                    if div <= 0:
                        issues.append(_issue("error", "bad_divisions",
                                             f"<divisions> 必须为正整数，收到 {div}。",
                                             pid, number, None))
                    else:
                        divisions = div
                        if divisions_initial is None:
                            divisions_initial = div
                key_el = _child(el, "key")
                if key_el is not None:
                    fifths = _int(key_el, "fifths")
                    mode = _text(key_el, "mode")
                    if fifths is None:
                        issues.append(_issue("error", "missing_key_fifths",
                                             "<key> 缺少 <fifths>，调号无法识别（系统不自行补全）。",
                                             pid, number, None))
                    else:
                        current_key = {"fifths": fifths, "mode": mode or "major"}
                        measure_info["key"] = current_key
                time_el = _child(el, "time")
                if time_el is not None:
                    beats = _int(time_el, "beats")
                    beat_type = _int(time_el, "beat-type")
                    if beats is None or beat_type is None or beats <= 0 or beat_type <= 0:
                        issues.append(_issue("error", "bad_time_signature",
                                             f"拍号无法识别：beats={beats}, beat-type={beat_type}",
                                             pid, number, None))
                    else:
                        current_time = {"beats": beats, "beat_type": beat_type}
                # 小节开头的调/拍即使沿用上一小节也记录进小节表
                if not seen_note_in_measure:
                    measure_info["time_sig"] = current_time
                    if current_time:
                        measure_info["beats"] = current_time["beats"]
                        measure_info["beat_type"] = current_time["beat_type"]
                    measure_info["key"] = current_key
            elif name == "note":
                seen_note_in_measure = True
                note_index += 1
                order_counter += 1
                ev, advance = _parse_note(el, pid, number, m_idx, note_index,
                                          order_counter, cursor, divisions,
                                          current_time, current_key, issues)
                if ev is not None:
                    events.append(ev)
                    if not ev["chord"]:
                        cursor = ev["start"] + (ev["duration"] or 0.0)
                    # 和弦音与前一音同起，不推进游标
                elif advance is not None:
                    cursor += advance
            elif name == "forward":
                dur = _duration_in_quarters(el, divisions, pid, number, None, issues)
                if dur is not None:
                    cursor += dur
            elif name == "backup":
                dur = _duration_in_quarters(el, divisions, pid, number, None, issues)
                if dur is not None:
                    cursor = max(0.0, cursor - dur)
            # 其它元素（print/direction/barline 等）不影响时间轴

        # 补记：若本小节没有自己的 attributes，小节表取沿用值
        if measure_info["time_sig"] is None:
            measure_info["time_sig"] = current_time
            if current_time:
                measure_info["beats"] = current_time["beats"]
                measure_info["beat_type"] = current_time["beat_type"]
        if measure_info["key"] is None:
            measure_info["key"] = current_key

        # 小节长度校验（四分音符单位）
        if current_time is not None:
            expected = current_time["beats"] * 4.0 / current_time["beat_type"]
            if abs(cursor - expected) > 1e-6:
                issues.append(_issue(
                    "error" if m_idx > 0 else "error",
                    "measure_length_mismatch",
                    f"小节时值总和 {_q(cursor)} 个四分音符，与拍号要求的 "
                    f"{_q(expected)} 个不符（可能漏写音符或时值）。",
                    pid, number, None))
        else:
            issues.append(_issue("error", "missing_time_signature",
                                 f"第 {number} 小节结束时仍无生效拍号，无法校验小节长度。",
                                 pid, number, None))
        measures.append(measure_info)

    # 单声部 voice 一致性：收集到的不同 voice id
    voice_ids = {e["voice"] for e in events if e["voice"] is not None}
    if len(voice_ids) > 1:
        issues.append(_issue("error", "multiple_voices_in_part",
                             f"声部内混写了多个 <voice>：{sorted(voice_ids)}；"
                             "两声部作业必须每个 <part> 只含一个声部。",
                             pid, None, None))

    return {
        "issues": issues,
        "events": events,
        "measures": measures,
        "divisions_initial": divisions_initial,
    }


def _duration_in_quarters(el: ET.Element, divisions: Optional[int],
                          pid: str, number: int, note_index: Optional[int],
                          issues: List[Dict[str, Any]]) -> Optional[float]:
    """读取元素的 <duration> 并按当前 divisions 换算；缺失/非法记为 error。"""
    raw = _int(el, "duration")
    if raw is None:
        issues.append(_issue("error", "missing_duration",
                             "缺少 <duration> 时值，系统不自行补全。",
                             pid, number, note_index))
        return None
    if raw < 0:
        issues.append(_issue("error", "bad_duration",
                             f"<duration> 不能为负数：{raw}", pid, number, note_index))
        return None
    if divisions is None:
        issues.append(_issue("error", "missing_divisions",
                             "音符之前没有 <divisions> 声明，无法还原时间位置。",
                             pid, number, note_index))
        return None
    return raw / divisions


def _parse_note(el: ET.Element, pid: str, number: int, m_idx: int,
                note_index: int, order: int, cursor: float,
                divisions: Optional[int], time_sig: Optional[Dict[str, Any]],
                key: Optional[Dict[str, Any]],
                issues: List[Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], Optional[float]]:
    """解析单个 <note>。返回 (事件或None, 时间推进或None)。"""
    loc = {"part_id": pid, "measure": number, "note_index": note_index}

    # ---- 无法识别/不支持的记谱形式 ------------------------------------
    if _child(el, "grace") is not None:
        issues.append(_issue("error", "grace_note",
                             "装饰音（grace note）无确定时值，两声部作业审阅不支持。",
                             pid, number, note_index))
        return None, 0.0
    if _child(el, "unpitched") is not None:
        issues.append(_issue("error", "unpitched_note",
                             "未指定音高的 <unpitched> 记谱无法用于对位分析。",
                             pid, number, note_index))
        return None, None
    if _child(el, "cue") is not None:
        issues.append(_issue("warning", "cue_note",
                             "提示音（cue note）按普通音符处理，请确认是否为正式声部内容。",
                             pid, number, note_index))

    is_chord = _child(el, "chord") is not None
    if is_chord:
        issues.append(_issue("error", "chord_in_voice",
                             "单声部内出现 <chord/>（同一时刻多个音），属多声部混写。",
                             pid, number, note_index))

    voice_el = _child(el, "voice")
    voice_id = voice_el.text.strip() if voice_el is not None and voice_el.text else None
    if voice_id is None:
        issues.append(_issue("warning", "missing_voice_id",
                             "音符缺少 <voice> 标识；因声部内未出现多 voice，按唯一声部处理。",
                             pid, number, note_index))

    # ---- 休止 vs 音高 -------------------------------------------------
    rest_el = _child(el, "rest")
    pitch_el = _child(el, "pitch")
    pitch: Optional[Dict[str, Any]] = None
    is_rest = rest_el is not None

    if is_rest:
        if pitch_el is not None:
            issues.append(_issue("error", "rest_with_pitch",
                                 "休止符同时带有 <pitch>，记谱无法识别。",
                                 pid, number, note_index))
    elif pitch_el is None:
        issues.append(_issue("error", "note_without_pitch_or_rest",
                             "音符既无 <pitch> 也无 <rest>，记谱无法识别（系统不自行补全）。",
                             pid, number, note_index))
    else:
        step = _text(pitch_el, "step")
        octave = _int(pitch_el, "octave")
        alter = _int(pitch_el, "alter")
        if step not in _STEP_TO_SEMI or octave is None:
            issues.append(_issue("error", "bad_pitch",
                                 f"音高无法识别：step={step!r}, octave={octave}",
                                 pid, number, note_index))
        else:
            pitch = {"step": step, "alter": alter or 0, "octave": octave}
            pitch["midi"] = _pitch_midi(pitch)
            if alter is None:
                # 调号决定的变音不写 alter 是正常的，这里只标注，不报警
                pitch["alter_explicit"] = False
            else:
                pitch["alter_explicit"] = True

    # ---- 时值 ---------------------------------------------------------
    type_el = _child(el, "type")
    note_type = type_el.text.strip() if type_el is not None and type_el.text else None
    known_types = {"whole", "half", "quarter", "eighth", "16th", "32nd", "64th",
                   "breve", "long"}
    if note_type is not None and note_type not in known_types:
        issues.append(_issue("warning", "unknown_note_type",
                             f"音符类型 {note_type!r} 不在已知记谱类型中；"
                             "时间位置仍以 <duration>/divisions 为准。",
                             pid, number, note_index))

    dots = len(_children(el, "dot"))

    if divisions is None:
        issues.append(_issue("error", "missing_divisions",
                             "音符之前没有 <divisions> 声明，无法按 divisions 还原时间位置。",
                             pid, number, note_index))
        duration_q: Optional[float] = None
        raw_dur = _int(el, "duration")
        if raw_dur is None and not is_chord:
            issues.append(_issue("error", "missing_duration",
                                 "音符缺少 <duration> 时值，系统不自行补全。",
                                 pid, number, note_index))
    else:
        raw_dur = _int(el, "duration")
        if raw_dur is None:
            if not is_chord:
                issues.append(_issue("error", "missing_duration",
                                     "音符缺少 <duration> 时值，系统不自行补全。",
                                     pid, number, note_index))
            duration_q = None
        elif raw_dur < 0:
            issues.append(_issue("error", "bad_duration",
                                 f"<duration> 不能为负数：{raw_dur}",
                                 pid, number, note_index))
            duration_q = None
        else:
            duration_q = raw_dur / divisions

    # ---- 延音 tie / tied ----------------------------------------------
    tie_start = False
    tie_stop = False
    for tie in _children(el, "tie"):
        kind = tie.get("type")
        if kind == "start":
            tie_start = True
        elif kind == "stop":
            tie_stop = True
        elif kind not in (None, "continue"):
            issues.append(_issue("warning", "unknown_tie_type",
                                 f"<tie type={kind!r}> 无法识别，按忽略处理。",
                                 pid, number, note_index))
    for notation in _children(el, "notation"):
        for tied in _children(notation, "tied"):
            kind = tied.get("type")
            if kind == "start":
                tie_start = True
            elif kind == "stop":
                tie_stop = True
    if (tie_start or tie_stop) and is_rest:
        issues.append(_issue("error", "tie_on_rest",
                             "休止符上出现延音线（tie），记谱无法识别。",
                             pid, number, note_index))

    start = cursor  # chord 不推进时间，由调用方依据 duration 推进；chord 音与前一音同起
    ev: Dict[str, Any] = {
        "id": f"{pid}-m{m_idx + 1}-n{note_index}",
        "measure": number,
        "measure_index": m_idx,
        "note_index": note_index,
        "start": start,
        "duration": duration_q,
        "pitch": pitch,
        "rest": is_rest,
        "dots": dots,
        "type": note_type,
        "tie_start": tie_start,
        "tie_stop": tie_stop,
        "voice": voice_id,
        "chord": is_chord,
        "_order": order,
    }
    ev.update(loc)
    return ev, None


# ---------------------------------------------------------------------------
# 延音配对
# ---------------------------------------------------------------------------

def _check_ties(part_list: List[str], parsed_parts: List[Dict[str, Any]],
                issues: List[Dict[str, Any]]) -> None:
    """同一声部内按时间顺序配对 tie start/stop；不配对的发 warning。"""
    for pid, parsed in zip(part_list, parsed_parts):
        pitched = [e for e in parsed["events"] if e["pitch"] is not None]
        pitched.sort(key=lambda e: (e["measure_index"], e["_order"]))
        open_tie: Optional[Dict[str, Any]] = None
        for ev in pitched:
            if ev["tie_stop"]:
                if open_tie is None:
                    issues.append(_issue("warning", "tie_stop_without_start",
                                         "延音结束（tie stop）找不到开始音。",
                                         pid, ev["measure"], ev["note_index"]))
                else:
                    if open_tie["pitch"]["midi"] != ev["pitch"]["midi"]:
                        issues.append(_issue("warning", "tie_pitch_mismatch",
                                             f"延音线两端音高不一致："
                                             f"{open_tie['pitch']['step']}{open_tie['pitch']['octave']}"
                                             f" -> {ev['pitch']['step']}{ev['pitch']['octave']}",
                                             pid, ev["measure"], ev["note_index"]))
                    open_tie = None
            if ev["tie_start"]:
                if open_tie is not None:
                    issues.append(_issue("warning", "tie_start_without_stop",
                                         "前一个延音开始后未闭合又出现新的开始。",
                                         pid, ev["measure"], ev["note_index"]))
                open_tie = ev
        if open_tie is not None:
            issues.append(_issue("warning", "tie_unclosed",
                                 "延音开始（tie start）后找不到结束音。",
                                 pid, open_tie["measure"], open_tie["note_index"]))


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _issue(severity: str, code: str, message: str,
           part_id: Optional[str], measure: Optional[int],
           note_index: Optional[int]) -> Dict[str, Any]:
    out = {"severity": severity, "code": code, "message": message}
    if part_id is not None:
        out["part_id"] = part_id
    if measure is not None:
        out["measure"] = measure
    if note_index is not None:
        out["note_index"] = note_index
    return out


def _q(value: float) -> str:
    """四分音符数的可读格式（去掉多余小数）。"""
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.3f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------------------
# 供分析引擎使用的派生工具
# ---------------------------------------------------------------------------

def beat_of(start_quarter: float, measure: Dict[str, Any]) -> float:
    """把四分音符绝对时间换算成小节内拍号意义上的拍点（从 1 计）。"""
    ts = measure["time_sig"]
    quarter_per_beat = 4.0 / ts["beat_type"]
    return (start_quarter - measure["start"]) / quarter_per_beat + 1.0


def format_beat(beat: float) -> str:
    """拍点显示：1、2.5、3 ⅓ 这种；简单分数用 1/3 风格。"""
    if abs(beat - round(beat)) < 1e-9:
        return str(int(round(beat)))
    # 常见的三分拍
    thirds = round(beat * 3) / 3
    if abs(beat - thirds) < 1e-9:
        n = round(beat * 3)
        whole, rem = divmod(n, 3)
        if rem == 0:
            return str(whole)
        return f"{whole}+{rem}/3" if whole else f"{rem}/3"
    return f"{beat:.2f}".rstrip("0").rstrip(".")
