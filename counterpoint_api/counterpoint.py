# -*- coding: utf-8 -*-
"""两声部对位分析引擎。

输入 :func:`musicxml_io.parse_score` 的还原结果与一份规则配置，
输出 ``findings``（发现列表）。每条发现都携带：

* ``kind`` / ``severity``：类型与严重级别；
* ``measure`` / ``beat``：拍号意义上的小节与拍位（从 1 计）；
* ``notes``：相关音符（声部、小节、音符序号、音高）；
* ``trace``：**判定轨迹**——分析过程中实际比较过的音程、声部进行方向、
  进入/解决音符等，便于教师核对。

检查项
------
1. 声部交叉（可选声部超越）
2. 平行五度 / 八度（一度可配）与隐伏五度 / 八度
3. 强拍不协和（仅允许配置的延留音，检查准备与级进下行解决）
4. 弱拍不协和的进入（级进/跳进、两音同击）与解决（经过音、辅助音、持续音）
5. 连续大跳、大跳未反向级进、超过最大跳进
6. 重复最高音、旋律音域越界

时间单位：四分音符（浮点数）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from . import rulesconfig as rc
from .musicxml_io import format_beat

# ---------------------------------------------------------------------------
# 数据准备
# ---------------------------------------------------------------------------

def analyze(parsed: Dict[str, Any], rules: Dict[str, Any]) -> Dict[str, Any]:
    """执行分析。返回 ``{"findings": [...], "context": {...}}``。"""
    if parsed.get("fatal"):
        return {"findings": [], "context": {"aborted": True,
                                            "reason": "谱面存在 error 级记谱问题，未执行分析。"}}

    measures = parsed["measures"]
    events = parsed["events"]
    roles = parsed["roles"]
    upper_pid = next(pid for pid, role in roles.items() if role == "upper")
    lower_pid = next(pid for pid, role in roles.items() if role == "lower")

    lines = {
        "upper": _build_voice_line(events, upper_pid, measures),
        "lower": _build_voice_line(events, lower_pid, measures),
    }

    ctx: Dict[str, Any] = {
        "measures": measures,
        "lines": lines,
        "upper_pid": upper_pid,
        "lower_pid": lower_pid,
        "beat_grid": _build_beat_grid(measures),
        "checkpoints": [],
    }
    ctx["checkpoints"] = _build_checkpoints(ctx, rules)

    findings: List[Dict[str, Any]] = []
    findings += _check_crossing(ctx, rules)
    findings += _check_parallel_and_hidden(ctx, rules)
    findings += _check_strong_weak_dissonance(ctx, rules)
    for role in ("upper", "lower"):
        findings += _check_melody(ctx, rules, role)
    findings += _check_repeated_highest(ctx, rules)

    findings.sort(key=lambda f: (f.get("time", 0.0), f["kind"]))
    for idx, f in enumerate(findings, start=1):
        f["finding_code"] = f"F{idx:03d}"
        f["fingerprint"] = _fingerprint(f)
        f["severity"] = rules["severity"].get(f["kind"], "warning")
    return {"findings": findings, "context": {"aborted": False}}


# ---------------------------------------------------------------------------
# 声部分子：合并 tie，区分起音与持续
# ---------------------------------------------------------------------------

def _build_voice_line(events: List[Dict[str, Any]], pid: str,
                      measures: List[Dict[str, Any]]) -> Dict[str, Any]:
    """构造一个声部的时间线。

    * segments：实际发声片段（tie 合并后的连续音），含 start/end/pitch/notes；
    * attacks：新起音的片段（tie_stop 不算新起音）；
    * rests：休止片段。
    """
    part_events = [e for e in events if e["part_id"] == pid]
    part_events.sort(key=lambda e: (e["measure_index"], e["_order"]))

    segments: List[Dict[str, Any]] = []
    rests: List[Dict[str, Any]] = []
    open_segment: Optional[Dict[str, Any]] = None  # tie 延续中

    for e in part_events:
        gstart = _global_start(e, measures)
        if e["rest"]:
            if e["duration"] is not None:
                rests.append({"start": gstart, "end": gstart + e["duration"]})
            open_segment = None
            continue
        if e["pitch"] is None or e["duration"] is None:
            open_segment = None
            continue
        if e["tie_stop"] and open_segment is not None and \
                open_segment["pitch"]["midi"] == e["pitch"]["midi"]:
            # 延音延续：不产生新起音，延长当前片段
            open_segment["end"] = gstart + e["duration"]
            open_segment["notes"].append(_note_ref(e))
            if not e["tie_start"]:
                open_segment = None
            continue
        seg = {
            "start": gstart,
            "end": gstart + e["duration"],
            "pitch": e["pitch"],
            "notes": [_note_ref(e)],
            "attacked": True,
            "tied": e["tie_start"],
            "measure_index": e["measure_index"],
        }
        segments.append(seg)
        open_segment = seg if e["tie_start"] else None

    segments.sort(key=lambda s: s["start"])
    return {
        "part_id": pid,
        "segments": segments,
        "attacks": [s for s in segments if s["attacked"]],
        "rests": rests,
    }


def _note_ref(e: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "event_id": e["id"],
        "part_id": e["part_id"],
        "measure": e["measure"],
        "note_index": e["note_index"],
        "pitch": None if e.get("rest") else _pitch_name(e["pitch"]),
        "midi": None if e.get("rest") or not e.get("pitch") else e["pitch"]["midi"],
    }


def _pitch_name(pitch: Dict[str, Any]) -> str:
    step = pitch["step"]
    alter = pitch.get("alter", 0)
    acc = ""
    if alter > 0:
        acc = "#" * alter
    elif alter < 0:
        acc = "b" * (-alter)
    return f"{step}{acc}{pitch['octave']}"


def _global_start(event: Dict[str, Any], measures: List[Dict[str, Any]]) -> float:
    m = measures[event["measure_index"]]
    return m["start"] + event["start"]


# ---------------------------------------------------------------------------
# 拍点与纵向检查点
# ---------------------------------------------------------------------------

def _build_beat_grid(measures: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """每小节按拍号生成拍点（四分音符绝对时间 + 拍号 + 是否强拍）。"""
    grid: List[Dict[str, Any]] = []
    for m in measures:
        ts = m["time_sig"]
        if ts is None:
            continue
        qpb = 4.0 / ts["beat_type"]
        for beat in range(1, ts["beats"] + 1):
            grid.append({
                "time": m["start"] + (beat - 1) * qpb,
                "measure_index": m["index"],
                "beat": float(beat),
            })
    return grid


def _sounding(line: Dict[str, Any], t: float) -> Optional[Dict[str, Any]]:
    for seg in line["segments"]:
        if seg["start"] - 1e-9 <= t < seg["end"] - 1e-9:
            return seg
        if abs(t - seg["start"]) < 1e-9:
            return seg
    return None


def _attack_at(line: Dict[str, Any], t: float) -> Optional[Dict[str, Any]]:
    for seg in line["attacks"]:
        if abs(seg["start"] - t) < 1e-9:
            return seg
    return None


def _build_checkpoints(ctx: Dict[str, Any], rules: Dict[str, Any]) -> List[Dict[str, Any]]:
    """检查点 = 拍点 ∪ 任一声部起音点（两声部都在发声时）。

    弱拍经过音常在反拍进入，因此起音点也纳入；拍点保证“按拍检查”不漏。
    """
    times = {round(g["time"], 6) for g in ctx["beat_grid"]}
    for line in ctx["lines"].values():
        for seg in line["attacks"]:
            times.add(round(seg["start"], 6))

    points: List[Dict[str, Any]] = []
    for t6 in sorted(times):
        t = float(t6)
        up = _sounding(ctx["lines"]["upper"], t)
        lo = _sounding(ctx["lines"]["lower"], t)
        if up is None or lo is None:
            continue
        m_idx, beat = _locate(t, ctx["measures"])
        m = ctx["measures"][m_idx]
        strong = int(beat) if abs(beat - round(beat)) < 1e-9 else None
        is_strong = strong is not None and strong in rc.strong_beats_for(m["time_sig"], rules)
        points.append({
            "time": t,
            "measure_index": m_idx,
            "beat": beat,
            "strong": is_strong,
            "on_beat": abs(beat - round(beat)) < 1e-9,
            "upper": up,
            "lower": lo,
        })
    return points


def _locate(t: float, measures: List[Dict[str, Any]]) -> Tuple[int, float]:
    for m in measures:
        if m["start"] - 1e-9 <= t < m["start"] + m["length"] - 1e-9:
            qpb = 4.0 / m["time_sig"]["beat_type"]
            return m["index"], (t - m["start"]) / qpb + 1.0
    last = measures[-1]
    qpb = 4.0 / last["time_sig"]["beat_type"]
    return last["index"], (t - last["start"]) / qpb + 1.0


def _location(ctx: Dict[str, Any], t: float) -> Dict[str, Any]:
    m_idx, beat = _locate(t, ctx["measures"])
    return {"measure": ctx["measures"][m_idx]["number"], "beat": beat,
            "beat_label": format_beat(beat), "time": round(t, 6)}


def _interval_at(point: Dict[str, Any]) -> Dict[str, Any]:
    return rc.interval_info(point["lower"]["pitch"], point["upper"]["pitch"])


# ---------------------------------------------------------------------------
# 通用 finding 构造
# ---------------------------------------------------------------------------

def _finding(kind: str, ctx: Dict[str, Any], t: float, notes: List[Dict[str, Any]],
             trace: Dict[str, Any], message: str, severity_default: str = "error") -> Dict[str, Any]:
    loc = _location(ctx, t)
    return {
        "kind": kind,
        "severity": severity_default,
        "measure": loc["measure"],
        "beat": loc["beat_label"],
        "time": loc["time"],
        "notes": notes,
        "trace": trace,
        "message": message,
    }


def _seg_note(seg: Dict[str, Any]) -> Dict[str, Any]:
    return seg["notes"][0]


# ---------------------------------------------------------------------------
# 1. 声部交叉 / 超越
# ---------------------------------------------------------------------------

def _check_crossing(ctx: Dict[str, Any], rules: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not rules["voice"]["check_crossing"] and not rules["voice"]["check_overlap"]:
        return []
    out: List[Dict[str, Any]] = []
    prev_point: Optional[Dict[str, Any]] = None
    reported_cross_pairs = set()
    for p in ctx["checkpoints"]:
        up_m, lo_m = p["upper"]["pitch"]["midi"], p["lower"]["pitch"]["midi"]
        pair_key = (id(p["upper"]), id(p["lower"]))
        if rules["voice"]["check_crossing"] and up_m < lo_m:
            if pair_key in reported_cross_pairs:
                prev_point = p
                continue
            reported_cross_pairs.add(pair_key)
            iv = _interval_at(p)
            out.append(_finding(
                "voice_crossing", ctx, p["time"],
                [_seg_note(p["upper"]), _seg_note(p["lower"])],
                {"interval": iv["label"],
                 "upper_midi": up_m, "lower_midi": lo_m,
                 "rule": "高声部音高必须不低于低声部"},
                f"第 {ctx['measures'][p['measure_index']]['number']} 小节第 "
                f"{format_beat(p['beat'])} 拍声部交叉：高声部 {_pitch_name(p['upper']['pitch'])}"
                f" 低于低声部 {_pitch_name(p['lower']['pitch'])}（{iv['label']} 倒置）。"))
        elif rules["voice"].get("check_overlap") and prev_point is not None:
            prev_up = prev_point["upper"]["pitch"]["midi"]
            prev_lo = prev_point["lower"]["pitch"]["midi"]
            if up_m < prev_lo or lo_m > prev_up:
                out.append(_finding(
                    "voice_overlap", ctx, p["time"],
                    [_seg_note(p["upper"]), _seg_note(p["lower"])],
                    {"previous_upper_midi": prev_up, "previous_lower_midi": prev_lo,
                     "current_upper_midi": up_m, "current_lower_midi": lo_m,
                     "rule": "进行后不得越过另一声部前一音"},
                    f"第 {ctx['measures'][p['measure_index']]['number']} 小节第 "
                    f"{format_beat(p['beat'])} 拍声部超越。",
                    severity_default="warning"))
        prev_point = p
    return out


# ---------------------------------------------------------------------------
# 2. 平行 / 隐伏五八度
# ---------------------------------------------------------------------------

def _check_parallel_and_hidden(ctx: Dict[str, Any], rules: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    points = ctx["checkpoints"]
    for prev, cur in zip(points, points[1:]):
        iv_prev, iv_cur = _interval_at(prev), _interval_at(cur)
        if not rc.is_perfect(iv_cur):
            continue
        family = iv_cur["simple_label"]  # P1/P5/P8
        up_move = cur["upper"]["pitch"]["midi"] - prev["upper"]["pitch"]["midi"]
        lo_move = cur["lower"]["pitch"]["midi"] - prev["lower"]["pitch"]["midi"]

        up_attacks = _attack_at(ctx["lines"]["upper"], cur["time"]) is not None
        lo_attacks = _attack_at(ctx["lines"]["lower"], cur["time"]) is not None
        both_attack = up_attacks and lo_attacks
        same_family = iv_prev["simple_label"] == family

        trace_base = {
            "from": {"measure": ctx["measures"][prev["measure_index"]]["number"],
                     "beat": format_beat(prev["beat"]),
                     "interval": iv_prev["label"],
                     "upper": _pitch_name(prev["upper"]["pitch"]),
                     "lower": _pitch_name(prev["lower"]["pitch"])},
            "to": {"measure": ctx["measures"][cur["measure_index"]]["number"],
                   "beat": format_beat(cur["beat"]),
                   "interval": iv_cur["label"],
                   "upper": _pitch_name(cur["upper"]["pitch"]),
                   "lower": _pitch_name(cur["lower"]["pitch"])},
            "upper_motion_semitones": up_move,
            "lower_motion_semitones": lo_move,
        }

        # --- 平行：前一个点与当前点同属五/八，两声部都移动且同向 ----------
        if same_family and up_move != 0 and lo_move != 0 and \
                _sign(up_move) == _sign(lo_move):
            if family == "P5":
                out.append(_finding(
                    "parallel_fifth", ctx, cur["time"],
                    [_seg_note(prev["upper"]), _seg_note(prev["lower"]),
                     _seg_note(cur["upper"]), _seg_note(cur["lower"])],
                    {**trace_base, "rule": "两声部同向移动到连续两个纯五度"},
                    f"第 {trace_base['to']['measure']} 小节第 {trace_base['to']['beat']} 拍"
                    f"出现平行五度（{iv_prev['label']} → {iv_cur['label']}，两声部同向）。"))
            elif family == "P8":
                out.append(_finding(
                    "parallel_octave", ctx, cur["time"],
                    [_seg_note(prev["upper"]), _seg_note(prev["lower"]),
                     _seg_note(cur["upper"]), _seg_note(cur["lower"])],
                    {**trace_base, "rule": "两声部同向移动到连续两个纯八度"},
                    f"第 {trace_base['to']['measure']} 小节第 {trace_base['to']['beat']} 拍"
                    f"出现平行八度（{iv_prev['label']} → {iv_cur['label']}，两声部同向）。"))
            elif family == "P1" and rules["intervals"].get("parallel_unisons_forbidden"):
                out.append(_finding(
                    "parallel_unison", ctx, cur["time"],
                    [_seg_note(prev["upper"]), _seg_note(prev["lower"]),
                     _seg_note(cur["upper"]), _seg_note(cur["lower"])],
                    {**trace_base, "rule": "配置禁止平行一度"},
                    f"第 {trace_base['to']['measure']} 小节第 {trace_base['to']['beat']} 拍"
                    f"出现平行一度。", severity_default="warning"))
            continue

        # --- 隐伏：前点不是同族完全协和，两个声部新起音并同向跳进到达 ------
        if same_family or not both_attack or up_move == 0 or lo_move == 0:
            continue
        su, sl = _sign(up_move), _sign(lo_move)
        if su != sl:
            # 反向到达：按配置豁免（传统允许反向八度）
            if family == "P8" and rules["beat"].get("hidden_allow_contrary_octave", True):
                continue
            if family == "P5":
                continue
        leap_min = rules["beat"]["hidden_leap_min_semitones"]
        leaper = max(abs(up_move), abs(lo_move))
        if leaper < leap_min:
            continue
        if family == "P1" and not rules["beat"].get("hidden_check_unisons", False):
            continue
        kind = "hidden_octave" if family == "P8" else ("hidden_fifth" if family == "P5" else "hidden_unison")
        if kind == "hidden_unison":
            continue
        out.append(_finding(
            kind, ctx, cur["time"],
            [_seg_note(prev["upper"]), _seg_note(prev["lower"]),
             _seg_note(cur["upper"]), _seg_note(cur["lower"])],
            {**trace_base,
             "leap_threshold_semitones": leap_min,
             "largest_motion_semitones": leaper,
             "rule": "两声部同向新起音，至少一声部跳进到达完全协和音程"},
            f"第 {trace_base['to']['measure']} 小节第 {trace_base['to']['beat']} 拍"
            f"出现隐伏{'八度' if family == 'P8' else '五度'}"
            f"（{iv_prev['label']} → {iv_cur['label']}，最大声部移动 {leaper} 半音）。"))
    return out


def _sign(x: float) -> int:
    return (x > 1e-9) - (x < -1e-9)


# ---------------------------------------------------------------------------
# 3/4. 强拍与弱拍不协和
# ---------------------------------------------------------------------------

def _check_strong_weak_dissonance(ctx: Dict[str, Any],
                                  rules: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    points = ctx["checkpoints"]
    # 同一对发声片段（如全音符内每个拍点）的不协和只在对应拍位类别
    # （强/弱）首次出现时评估一次：持续不协和不逐拍重复报告；
    # 合法延留音持续到解决前的弱位也由此豁免。
    # 弱位先进入、持续到强拍的不协和是另一类错误，强位仍单独评估。
    seen_pairs = {"strong": set(), "weak": set()}
    for idx, p in enumerate(points):
        iv = _interval_at(p)
        if rc.is_consonant(iv, rules):
            continue
        bucket = "strong" if p["strong"] else "weak"
        pair_key = (id(p["upper"]), id(p["lower"]))
        if pair_key in seen_pairs[bucket]:
            continue
        seen_pairs[bucket].add(pair_key)
        prev_p = points[idx - 1] if idx > 0 else None
        next_p = points[idx + 1] if idx + 1 < len(points) else None
        if p["strong"]:
            finding = _strong_dissonance(ctx, rules, p, iv, prev_p, next_p)
            if finding:
                out.append(finding)
            else:
                # 合法延留音（强拍不协和、有准备、下行级进解决）：
                # 同一对片段持续到解决前的弱位拍点也不重复报告。
                seen_pairs["weak"].add(pair_key)
        else:
            out.extend(_weak_dissonance(ctx, rules, p, iv, prev_p, next_p))
    return out


def _strong_dissonance(ctx: Dict[str, Any], rules: Dict[str, Any],
                       p: Dict[str, Any], iv: Dict[str, Any],
                       prev_p: Optional[Dict[str, Any]],
                       next_p: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    cfg = rules["dissonance"]
    if not cfg.get("allow_suspension", True):
        return _finding(
            "strong_dissonance", ctx, p["time"],
            [_seg_note(p["upper"]), _seg_note(p["lower"])],
            {"interval": iv["label"], "reason": "suspensions_disallowed",
             "rule": "配置不允许任何强拍不协和（含延留音）"},
            f"第 {ctx['measures'][p['measure_index']]['number']} 小节第 "
            f"{format_beat(p['beat'])} 强强拍不协和（{iv['label']}），规则已禁用延留音。")

    # 延留音：必有一声部在强拍之前就已发声（持续/延音），另一声部新起或也在持续
    active_role = None
    for role in ("upper", "lower"):
        seg = p[role]
        if seg["start"] < p["time"] - 1e-9:
            active_role = role
            break
    other_role = "lower" if active_role == "upper" else "upper"
    trace: Dict[str, Any] = {"interval": iv["label"], "tested_form": "suspension"}

    if active_role is None:
        trace["reason"] = "both_attack_on_strong_beat"
        trace["rule"] = "延留音的不协和音必须在强拍之前已发声（准备）"
        return _finding(
            "strong_dissonance", ctx, p["time"],
            [_seg_note(p["upper"]), _seg_note(p["lower"])],
            trace,
            f"第 {ctx['measures'][p['measure_index']]['number']} 小节第 "
            f"{format_beat(p['beat'])} 强拍两音同时击弦构成不协和（{iv['label']}），"
            "不是延留音。")

    active, other = p[active_role], p[other_role]
    trace["suspension_voice"] = active_role
    trace["sustained_note"] = _seg_note(active)
    trace["explicit_tie"] = bool(active.get("tied")) or \
        any(n["event_id"] != active["notes"][0]["event_id"] for n in active["notes"])

    # 准备：前一拍点该音已发声，且前一点为协和
    prepared = False
    if prev_p is not None and cfg.get("suspension_require_preparation", True):
        prev_seg = prev_p[active_role]
        if prev_seg is active and rc.is_consonant(_interval_at(prev_p), rules):
            prepared = True
        trace["preparation"] = {
            "measure": ctx["measures"][prev_p["measure_index"]]["number"],
            "beat": format_beat(prev_p["beat"]),
            "interval": _interval_at(prev_p)["label"],
            "same_note_sounding": prev_seg is active,
            "consonant": rc.is_consonant(_interval_at(prev_p), rules),
        }
    elif prev_p is not None:
        prepared = True  # 不要求准备时不阻塞

    # 解决：active 声部的下一个新起音，须级进下行；解决点音程协和
    resolution = _next_attack_after(ctx, active_role, active)
    resolved = False
    res_label = None
    if resolution is not None:
        rseg, rpoint = resolution
        move = rseg["pitch"]["midi"] - active["pitch"]["midi"]
        res_iv = rc.interval_info(rpoint["lower"]["pitch"], rpoint["upper"]["pitch"])
        diatonic_steps = _generic_steps(active["pitch"], rseg["pitch"])
        resolved = move < 0 and abs(diatonic_steps) == 1 and rc.is_consonant(res_iv, rules)
        res_label = f"{iv['simple_label'].replace('m', '').replace('M', '').lstrip('PdA')}" \
                    f"-{res_iv['simple_label'].lstrip('mM')}"
        # 用度数构造 4-3 这样的标签
        d_dis = _simple_degree(iv)
        d_res = _simple_degree(res_iv)
        res_label = f"{d_dis}-{d_res}"
        trace["resolution"] = {
            "note": _seg_note(rseg),
            "measure": ctx["measures"][rpoint["measure_index"]]["number"],
            "beat": format_beat(rpoint["beat"]),
            "interval": res_iv["label"],
            "motion_semitones": move,
            "motion_diatonic_steps": diatonic_steps,
            "consonant": rc.is_consonant(res_iv, rules),
            "label": res_label,
        }

    reasons = []
    if cfg.get("suspension_require_preparation", True) and not prepared:
        reasons.append("unprepared")
    if resolution is None:
        reasons.append("unresolved_at_end")
    elif not resolved:
        r = trace["resolution"]
        if r["motion_semitones"] >= 0 or abs(r["motion_diatonic_steps"]) != 1:
            reasons.append("not_step_down")
        elif not r["consonant"]:
            reasons.append("resolves_to_dissonance")
    if resolved and res_label not in cfg.get("suspension_resolutions", []):
        reasons.append(f"resolution_{res_label}_not_configured")

    if not reasons:
        return None  # 合法延留音：不是“问题”
    trace["reasons"] = reasons
    trace["rule"] = "强拍不协和仅允许有准备、级进下行解决到协和的延留音"
    return _finding(
        "strong_dissonance", ctx, p["time"],
        [_seg_note(p["upper"]), _seg_note(p["lower"])],
        trace,
        f"第 {ctx['measures'][p['measure_index']]['number']} 小节第 "
        f"{format_beat(p['beat'])} 强拍不协和（{iv['label']}），"
        f"延留音不成立：{'、'.join(reasons)}。")


def _next_attack_after(ctx: Dict[str, Any], role: str,
                       seg: Dict[str, Any]) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """找声部 role 在 seg 结束处（或之后最近）的下一个起音片段及其检查点。"""
    line = ctx["lines"][role]
    target_t = seg["end"]
    best: Optional[Dict[str, Any]] = None
    for a in line["attacks"]:
        if a["start"] >= seg["end"] - 1e-9 and a is not seg:
            best = a
            break
    if best is None:
        return None
    for p in ctx["checkpoints"]:
        if abs(p["time"] - best["start"]) < 1e-9:
            return best, p
    # 起音点不在检查点（另一声部休止）：构造临时纵向点
    other = "lower" if role == "upper" else "upper"
    other_seg = _sounding(ctx["lines"][other], best["start"])
    if other_seg is None:
        return None
    m_idx, beat = _locate(best["start"], ctx["measures"])
    point = {"time": best["start"], "measure_index": m_idx, "beat": beat,
             "strong": False, "on_beat": True,
             "upper": best if role == "upper" else other_seg,
             "lower": best if role == "lower" else other_seg}
    return best, point


def _generic_steps(p1: Dict[str, Any], p2: Dict[str, Any]) -> int:
    d1 = rc.diatonic_index(p1["step"], p1.get("alter", 0), p1["octave"])
    d2 = rc.diatonic_index(p2["step"], p2.get("alter", 0), p2["octave"])
    return d2 - d1


def _simple_degree(iv: Dict[str, Any]) -> int:
    label = iv["simple_label"]
    parsed = rc.parse_interval_label(label)
    if parsed is None:
        return 0
    degree = parsed[1]
    return degree


def _weak_dissonance(ctx: Dict[str, Any], rules: Dict[str, Any],
                     p: Dict[str, Any], iv: Dict[str, Any],
                     prev_p: Optional[Dict[str, Any]],
                     next_p: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """弱位不协和：判定进入方式与解决。

    一次弱位不协和最多产生两类问题：进入非法（跳进/两音同击/无准备），
    或解决非法（未级进、解决到不协和、悬空）。合法的经过音/辅助音静默。
    """
    cfg = rules["dissonance"]
    findings: List[Dict[str, Any]] = []
    up_attack = _attack_at(ctx["lines"]["upper"], p["time"]) is not None and \
        p["upper"]["start"] == p["time"]
    lo_attack = _attack_at(ctx["lines"]["lower"], p["time"]) is not None and \
        p["lower"]["start"] == p["time"]

    active_roles = [r for r, att in (("upper", up_attack), ("lower", lo_attack)) if att]
    trace: Dict[str, Any] = {
        "interval": iv["label"],
        "measure": ctx["measures"][p["measure_index"]]["number"],
        "beat": format_beat(p["beat"]),
    }

    # 两音同击：不存在“运动中的不协和音”
    if not active_roles:
        # 不协和在两个持续音之间出现——两音都不是新起音，属于撞击型
        trace["reason"] = "dissonance_without_attack"
        trace["rule"] = "弱位不协和必须由级进运动的声部引入"
        findings.append(_finding(
            "weak_dissonance_entry", ctx, p["time"],
            [_seg_note(p["upper"]), _seg_note(p["lower"])], trace,
            f"第 {trace['measure']} 小节第 {trace['beat']} 拍弱位不协和（{iv['label']}），"
            "两个音都不是新进入的音。"))
        return findings

    if len(active_roles) == 2:
        trace["reason"] = "both_voices_attack"
        trace["rule"] = "弱位不协和只能由一个声部级进进入，另一声部保持或作协和进行"
        findings.append(_finding(
            "weak_dissonance_entry", ctx, p["time"],
            [_seg_note(p["upper"]), _seg_note(p["lower"])], trace,
            f"第 {trace['measure']} 小节第 {trace['beat']} 拍弱位两声部同时进入"
            f"不协和音程（{iv['label']}）。"))
        return findings

    role = active_roles[0]
    other = "lower" if role == "upper" else "upper"
    active, passive = p[role], p[other]
    trace["active_voice"] = role
    trace["dissonant_note"] = _seg_note(active)

    # 进入：必须与自己的前一音级进，且前一纵向点协和
    prev_attack = _prev_attack(ctx, role, active)
    entry_step = False
    entry_semitones = None
    if prev_attack is not None:
        entry_semitones = active["pitch"]["midi"] - prev_attack["pitch"]["midi"]
        entry_step = abs(_generic_steps(prev_attack["pitch"], active["pitch"])) == 1
        trace["entry"] = {
            "from_note": _seg_note(prev_attack),
            "motion_semitones": entry_semitones,
            "motion_diatonic_steps": _generic_steps(prev_attack["pitch"], active["pitch"]),
            "by_step": entry_step,
        }
    prev_consonant = prev_p is not None and rc.is_consonant(_interval_at(prev_p), rules)
    trace["preparation_consonant"] = prev_consonant
    # 另一声部是否保持（持续音判定）
    passive_holds = prev_p is not None and prev_p[other] is passive
    trace["other_voice_holds"] = passive_holds

    if prev_attack is None:
        trace["reason"] = "enters_at_very_start"
        findings.append(_mk_weak_entry(ctx, p, iv, trace,
                                       "弱位不协和音是声部第一个音，无进入进行。"))
        return findings
    if not entry_step:
        trace["reason"] = "entered_by_leap"
        trace["rule"] = "弱位不协和必须级进进入"
        findings.append(_mk_weak_entry(
            ctx, p, iv, trace,
            f"第 {trace['measure']} 小节第 {trace['beat']} 拍弱位不协和（{iv['label']}）"
            f"由{_role_cn(role)}声部跳进 {entry_semitones} 半音进入。"))
        return findings
    if not prev_consonant:
        trace["reason"] = "prepared_against_dissonance"
        findings.append(_mk_weak_entry(ctx, p, iv, trace,
                                       f"第 {trace['measure']} 小节第 {trace['beat']} 拍弱位不协和"
                                       "（{0}）的前一纵向音程不协和，不是合法准备。".format(iv['label'])))
        return findings

    # --- 解决：下一个起音必须级进，且解决点协和 ------------------------
    nxt = _next_attack_after(ctx, role, active)
    if nxt is None:
        trace["reason"] = "unresolved_at_end"
        trace["rule"] = "弱位不协和必须级进解决到协和"
        findings.append(_finding(
            "weak_dissonance_resolution", ctx, p["time"],
            [_seg_note(active), _seg_note(passive)], trace,
            f"第 {trace['measure']} 小节第 {trace['beat']} 拍弱位不协和（{iv['label']}）"
            "之后没有解决音（乐曲结束或声部停止）。"))
        return findings

    rseg, rpoint = nxt
    res_move = rseg["pitch"]["midi"] - active["pitch"]["midi"]
    res_steps = _generic_steps(active["pitch"], rseg["pitch"])
    res_iv = rc.interval_info(rpoint["lower"]["pitch"], rpoint["upper"]["pitch"])
    res_consonant = rc.is_consonant(res_iv, rules)
    form = None
    if abs(res_steps) == 1:
        if _sign(res_move) == _sign(entry_semitones):
            form = "passing_tone"
        elif _sign(res_move) == -_sign(entry_semitones):
            form = "neighbor_tone"
    trace["resolution"] = {
        "note": _seg_note(rseg),
        "measure": ctx["measures"][rpoint["measure_index"]]["number"],
        "beat": format_beat(rpoint["beat"]),
        "interval": res_iv["label"],
        "motion_semitones": res_move,
        "motion_diatonic_steps": res_steps,
        "consonant": res_consonant,
        "form": form,
    }

    allowed = True
    reason = None
    if abs(res_steps) != 1:
        allowed = False
        reason = "resolves_by_leap"
    elif not res_consonant:
        allowed = False
        reason = "resolves_to_dissonance"
    elif form == "passing_tone" and not cfg.get("allow_passing_tone", True):
        allowed = False
        reason = "passing_tone_disallowed"
    elif form == "neighbor_tone" and not cfg.get("allow_neighbor_tone", True):
        allowed = False
        reason = "neighbor_tone_disallowed"
    elif form is None:
        allowed = False
        reason = "unclassified_step_motion"

    if passive_holds and form in ("passing_tone", "neighbor_tone"):
        trace["context"] = "pedal"
        if not cfg.get("allow_pedal", False):
            # 另一声部保持时传统仍算经过/辅助音；是否额外标注由配置决定，
            # 默认不罚。
            trace["pedal_note"] = _seg_note(passive)

    if not allowed:
        trace["reason"] = reason
        trace["rule"] = "弱位不协和须级进解决到协和音程（经过音同向、辅助音折返）"
        findings.append(_finding(
            "weak_dissonance_resolution", ctx, p["time"],
            [_seg_note(prev_attack), _seg_note(active), _seg_note(rseg)], trace,
            f"第 {trace['measure']} 小节第 {trace['beat']} 拍弱位不协和（{iv['label']}）"
            f"解决非法：{_reason_cn(reason)}。"))
    return findings


def _mk_weak_entry(ctx: Dict[str, Any], p: Dict[str, Any], iv: Dict[str, Any],
                   trace: Dict[str, Any], message: str) -> Dict[str, Any]:
    return _finding("weak_dissonance_entry", ctx, p["time"],
                    [_seg_note(p["upper"]), _seg_note(p["lower"])], trace, message)


def _prev_attack(ctx: Dict[str, Any], role: str,
                 seg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    line = ctx["lines"][role]
    prev = None
    for a in line["attacks"]:
        if a["start"] < seg["start"] - 1e-9:
            prev = a
        else:
            break
    return prev


def _role_cn(role: str) -> str:
    return "高" if role == "upper" else "低"


def _reason_cn(reason: str) -> str:
    return {
        "resolves_by_leap": "解决为跳进而非级进",
        "resolves_to_dissonance": "解决音程仍不协和",
        "passing_tone_disallowed": "规则禁用经过音",
        "neighbor_tone_disallowed": "规则禁用辅助音",
        "unclassified_step_motion": "级进方向既非经过也非辅助",
    }.get(reason, reason)


# ---------------------------------------------------------------------------
# 5. 旋律：大跳、连续大跳、音域
# ---------------------------------------------------------------------------

def _check_melody(ctx: Dict[str, Any], rules: Dict[str, Any], role: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    mel = rules["melody"]
    line = ctx["lines"][role]
    attacks = line["attacks"]
    pid = line["part_id"]
    rng = mel.get("ranges", {}).get(role)

    # 音域（逐个越界音）
    if rng:
        lo_edge = rc.pitch_to_midi(rng["low"])
        hi_edge = rc.pitch_to_midi(rng["high"])
        for seg in attacks:
            midi = seg["pitch"]["midi"]
            if midi < lo_edge or midi > hi_edge:
                t = seg["start"]
                out.append(_finding(
                    "range_violation", ctx, t, [_seg_note(seg)],
                    {"voice": role, "range": rng,
                     "note_midi": midi,
                     "rule": f"{_role_cn(role)}声部音域限定 {rng['low']}–{rng['high']}"},
                    f"{_role_cn(role)}声部第 {ctx['measures'][seg['measure_index']]['number']}"
                    f"小节音 {_pitch_name(seg['pitch'])} 超出音域 {rng['low']}–{rng['high']}。",
                    severity_default="warning"))

    # 跳进序列（遇休止重置）
    rests = line["rests"]
    prev_leap_info: Optional[Dict[str, Any]] = None  # {"seg","dir","size"}
    prev_seg: Optional[Dict[str, Any]] = None
    for seg in attacks:
        if prev_seg is not None:
            gap_rest = any(r["start"] >= prev_seg["start"] - 1e-9 and
                           r["end"] <= seg["start"] + 1e-9 and
                           r["end"] > r["start"] + 1e-9
                           for r in rests)
            if gap_rest:
                prev_leap_info = None
        if prev_seg is not None:
            move = seg["pitch"]["midi"] - prev_seg["pitch"]["midi"]
            direction = _sign(move)
            size = abs(move)

            if size > mel["max_leap_semitones"]:
                out.append(_finding(
                    "leap_too_large", ctx, seg["start"],
                    [_seg_note(prev_seg), _seg_note(seg)],
                    {"voice": role, "leap_semitones": move,
                     "max_allowed": mel["max_leap_semitones"],
                     "rule": f"单声部跳进不得超过 {mel['max_leap_semitones']} 半音"},
                    f"{_role_cn(role)}声部第 {ctx['measures'][seg['measure_index']]['number']}"
                    f"小节出现 {size} 半音跳进（{_pitch_name(prev_seg['pitch'])}→"
                    f"{_pitch_name(seg['pitch'])}），超过最大跳进 {mel['max_leap_semitones']}。",
                    severity_default="warning"))

            if size >= mel["consecutive_leap_semitones"]:
                if prev_leap_info is not None and prev_leap_info["dir"] == direction:
                    out.append(_finding(
                        "consecutive_leaps", ctx, seg["start"],
                        [_seg_note(prev_leap_info["from"]), _seg_note(prev_seg), _seg_note(seg)],
                        {"voice": role,
                         "first_leap": prev_leap_info["size"],
                         "second_leap": size,
                         "direction": "up" if direction > 0 else "down",
                         "threshold": mel["consecutive_leap_semitones"],
                         "rule": f"同向连续跳进（各 ≥ {mel['consecutive_leap_semitones']} 半音）"},
                        f"{_role_cn(role)}声部第 {ctx['measures'][seg['measure_index']]['number']}"
                        f"小节出现连续同向大跳（{prev_leap_info['size']}+{size} 半音"
                        f"{'上行' if direction > 0 else '下行'}）。",
                        severity_default="warning"))
                prev_leap_info = {"dir": direction, "size": size, "from": prev_seg}

                # 大跳后须反向级进：提前看下一个起音
                if mel.get("leap_must_reverse_by_step", True):
                    following = _next_in_line(attacks, seg)
                    if following is not None:
                        gap_rest2 = any(
                            r["start"] >= seg["start"] - 1e-9 and
                            r["end"] <= following["start"] + 1e-9 and
                            r["end"] > r["start"] + 1e-9
                            for r in rests)
                        if not gap_rest2:
                            fmove = following["pitch"]["midi"] - seg["pitch"]["midi"]
                            fsteps = _generic_steps(seg["pitch"], following["pitch"])
                            ok = _sign(fmove) == -direction and abs(fsteps) == 1
                            if not ok:
                                out.append(_finding(
                                    "leap_not_reversed", ctx, following["start"],
                                    [_seg_note(prev_seg), _seg_note(seg), _seg_note(following)],
                                    {"voice": role, "leap_semitones": move,
                                     "following_motion_semitones": fmove,
                                     "following_diatonic_steps": fsteps,
                                     "rule": "大跳后须以级进反向折回"},
                                    f"{_role_cn(role)}声部大跳 "
                                    f"{_pitch_name(prev_seg['pitch'])}→{_pitch_name(seg['pitch'])}"
                                    f" 之后未作反向级进（落到 {_pitch_name(following['pitch'])}）。",
                                    severity_default="warning"))
            else:
                prev_leap_info = None
        prev_seg = seg
    return out


def _next_in_line(attacks: List[Dict[str, Any]], seg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for i, a in enumerate(attacks):
        if a is seg and i + 1 < len(attacks):
            return attacks[i + 1]
    return None


# ---------------------------------------------------------------------------
# 6. 重复最高音
# ---------------------------------------------------------------------------

def _check_repeated_highest(ctx: Dict[str, Any], rules: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    limit = rules["melody"]["max_highest_note_occurrences"]
    for role in ("upper", "lower"):
        line = ctx["lines"][role]
        attacks = line["attacks"]
        if not attacks:
            continue
        top = max(a["pitch"]["midi"] for a in attacks)
        holders = [a for a in attacks if a["pitch"]["midi"] == top]
        if len(holders) > limit:
            first = holders[0]
            locs = "、".join(f"第{ctx['measures'][a['measure_index']]['number']}小节"
                             f"{_pitch_name(a['pitch'])}" for a in holders)
            out.append(_finding(
                "repeated_highest", ctx, first["start"],
                [_seg_note(a) for a in holders],
                {"voice": role, "highest_pitch": _pitch_name(first["pitch"]),
                 "occurrences": len(holders), "max_allowed": limit,
                 "rule": f"旋律最高音出现不得超过 {limit} 次"},
                f"{_role_cn(role)}声部最高音 {_pitch_name(first['pitch'])} 重复出现 "
                f"{len(holders)} 次（{locs}）。",
                severity_default="warning"))
    return out


# ---------------------------------------------------------------------------
# 指纹（版本比对用）
# ---------------------------------------------------------------------------

def _fingerprint(finding: Dict[str, Any]) -> str:
    import hashlib
    parts = [finding["kind"]]
    for n in finding["notes"]:
        parts.append(f"{n['part_id']}:m{n['measure']}:n{n['note_index']}")
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:10]
