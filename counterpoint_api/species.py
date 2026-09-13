# -*- coding: utf-8 -*-
"""第一至第五类对位（Fux species）类别校验。

在通用两声部检查之外，按教师创建分析时指定的「定旋律声部 + 对位类别」
核对类别专属要求：

* **起音位置**：每个定旋律音内，对位声部新起音必须落在类别规定的时点
  （第一类对齐定旋律、第二类每音两个、第三类每音四个、第四类弱位起音、
  第五类落在拍点网格上）；
* **对位音数量**：第一类 1:1、第二类 2:1、第三类 4:1、第四类每音两个
  半音符位（一延留一起音）、第五类自由；
* **第四类切分延留**：弱位起音必须以延音（tie）进入下一小节的强位，
  强位不得另起新音；
* **第五类混合节奏**：全曲至少两种时值；
* **终止式**（结合调号）：起始完全协和、终止一度或八度、倒数第二音到
  终止音反向级进、导音（第七级）升高并级进上行解决到主音。

谱面节奏无法满足所选类别时，逐小节/音符报出 ``species_*`` 发现并定位，
**系统不擅自更改类别**。首小节休止、终止小节全音符收束、倒数第二小节
灵活处理均由规则集 ``species`` 段配置。

每条发现的 ``trace`` 携带：调号、两声部音级、纵向音程、实际/期望节奏
比例与判定过程。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from . import counterpoint as engine
from . import rulesconfig as rc
from .musicxml_io import format_beat

EPS = 1e-9

#: 类别编号 -> 中文名
SPECIES_NAMES = {
    1: "第一类（一音对一音）",
    2: "第二类（二音对一音）",
    3: "第三类（四音对一音）",
    4: "第四类（切分延留）",
    5: "第五类（混合节奏）",
}
VALID_SPECIES = tuple(SPECIES_NAMES)

#: 每个定旋律音内期望的对位起音位置（占定旋律音时值的比例）。
#: 第四类新起音在弱位（后半），强位由延留音占据，单独核对。
_ATTACK_OFFSETS = {
    1: [0.0],
    2: [0.0, 0.5],
    3: [0.0, 0.25, 0.5, 0.75],
    4: [0.5],
}

#: 类别要求的对位音符时值（占定旋律音时值比例）
_NOTE_VALUE = {1: 1.0, 2: 0.5, 3: 0.25}

#: 节奏比例标签（对位音 : 定旋律音）
_RATIO_LABEL = {1: "1:1", 2: "2:1", 3: "4:1", 4: "2:1（切分）", 5: "自由混合"}

_VALUE_NAMES = {1.0: "全音符", 0.5: "二分音符", 0.25: "四分音符", 0.125: "八分音符"}


def _value_name(ratio: float) -> str:
    return _VALUE_NAMES.get(round(ratio, 6), f"{ratio:g} 倍时值")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def check(ctx: Dict[str, Any], rules: Dict[str, Any], species: int,
          cantus_role: str) -> List[Dict[str, Any]]:
    """按类别校验。``ctx`` 为 :func:`counterpoint.analyze` 构造的上下文。"""
    cp_role = "lower" if cantus_role == "upper" else "upper"
    cf_line = ctx["lines"][cantus_role]
    cp_line = ctx["lines"][cp_role]
    cfg = rules.get("species", {})

    findings: List[Dict[str, Any]] = []
    cf_segments = cf_line["segments"]
    if not cf_segments:
        return findings

    n = len(cf_segments)
    for i, cf_seg in enumerate(cf_segments):
        is_first = i == 0
        is_final = i == n - 1
        is_penultimate = i == n - 2
        if is_final and cfg.get("final_measure_whole_note", True):
            findings += _check_final_measure(ctx, rules, species, cf_seg,
                                             cp_line, cp_role, cantus_role)
            continue
        if is_penultimate and cfg.get("penultimate_measure_flexible", True):
            continue  # 终止前一小节允许灵活节奏（和声检查仍在最后统一做）
        if species == 5:
            findings += _check_species5_measure(ctx, rules, cfg, cf_seg,
                                                cp_line, cp_role, cantus_role)
            continue
        # 第四类：本小节的弱位起音是否必须系延音进入下一小节
        require_tie = species == 4 and (
            i + 1 <= n - 2 or not cfg.get("final_measure_whole_note", True))
        findings += _check_species_measure(
            ctx, rules, cfg, species, cf_seg, cp_line, cp_role, cantus_role,
            is_first=is_first, require_tie=require_tie)

    if species == 5 and cfg.get("species5_require_mixed_values", True):
        findings += _check_species5_variety(ctx, rules, cf_segments, cp_line,
                                            cp_role, cantus_role)

    findings += _check_harmonic_frame(ctx, rules, cf_line, cp_line,
                                      cantus_role, cp_role)
    return findings


# ---------------------------------------------------------------------------
# 公共：trace 基础信息（调号 / 音级 / 纵向音程 / 节奏比例）
# ---------------------------------------------------------------------------

def _key(ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for m in ctx["measures"]:
        if m.get("key"):
            return m["key"]
    return None


def _degrees(key: Optional[Dict[str, Any]], cf_seg: Dict[str, Any],
             cp_seg: Optional[Dict[str, Any]]) -> Dict[str, Optional[str]]:
    out: Dict[str, Optional[str]] = {"cantus": None, "counterpoint": None}
    if key:
        d_cf = rc.scale_degree(cf_seg["pitch"], key)
        if d_cf:
            out["cantus"] = rc.degree_label(*d_cf)
        if cp_seg is not None:
            d_cp = rc.scale_degree(cp_seg["pitch"], key)
            if d_cp:
                out["counterpoint"] = rc.degree_label(*d_cp)
    return out


def _base_trace(ctx: Dict[str, Any], rules: Dict[str, Any], species: int,
                cf_seg: Dict[str, Any], cp_seg: Optional[Dict[str, Any]],
                cp_role: str, cantus_role: str) -> Dict[str, Any]:
    key = _key(ctx)
    trace: Dict[str, Any] = {
        "species": species or None,
        "species_name": SPECIES_NAMES.get(species),
        "cantus_role": cantus_role,
        "counterpoint_role": cp_role,
        "key": rc.key_label(key),
        "scale_degrees": _degrees(key, cf_seg, cp_seg),
        "rhythm_ratio": {"expected": _RATIO_LABEL.get(species)},
    }
    if cp_seg is not None:
        low, high = (cf_seg, cp_seg) if cantus_role == "lower" else (cp_seg, cf_seg)
        trace["vertical_interval"] = rc.interval_info(low["pitch"], high["pitch"])["label"]
    return trace


def _cf_note(cf_seg: Dict[str, Any]) -> Dict[str, Any]:
    return engine._seg_note(cf_seg)


# ---------------------------------------------------------------------------
# 第一至第四类：逐定旋律音核对
# ---------------------------------------------------------------------------

def _check_species_measure(ctx: Dict[str, Any], rules: Dict[str, Any],
                           cfg: Dict[str, Any], species: int,
                           cf_seg: Dict[str, Any], cp_line: Dict[str, Any],
                           cp_role: str, cantus_role: str,
                           is_first: bool, require_tie: bool
                           ) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    s, e = cf_seg["start"], cf_seg["end"]
    length = e - s
    attacks = [a for a in cp_line["attacks"] if s - EPS <= a["start"] < e - EPS]

    # ---- 首小节休止例外：允许对位声部先休止再进入 ----------------------
    rest_until = s
    if is_first and cfg.get("allow_first_measure_rest", True):
        max_rest = float(cfg.get("first_measure_rest_max_quarters", 2.0))
        for r in cp_line["rests"]:
            if abs(r["start"] - s) < EPS and r["end"] > r["start"] + EPS:
                rest_until = max(rest_until, min(r["end"], s + max_rest))
                if r["end"] - s > max_rest + EPS:
                    trace = _base_trace(ctx, rules, species, cf_seg, None,
                                        cp_role, cantus_role)
                    trace["rule"] = (f"首小节休止不得超过 "
                                     f"{_q(max_rest)} 个四分音符（species."
                                     f"first_measure_rest_max_quarters）")
                    trace["rest_quarters"] = round(r["end"] - s, 6)
                    out.append(engine._finding(
                        "species_note_value", ctx, s, [_cf_note(cf_seg)], trace,
                        f"第 {ctx['measures'][cf_seg['measure_index']]['number']} "
                        f"小节对位声部首小节休止 {_q(r['end'] - s)} 个四分音符，"
                        f"超过规则允许的 {_q(max_rest)} 个。"))

    offsets = [o for o in _ATTACK_OFFSETS[species]
               if s + o * length >= rest_until - EPS]
    expected_times = [s + o * length for o in offsets]

    # ---- 起音位置 ------------------------------------------------------
    for t in expected_times:
        if not any(abs(a["start"] - t) < EPS for a in attacks):
            cp_here = engine._sounding(cp_line, t)
            trace = _base_trace(ctx, rules, species, cf_seg, cp_here,
                                cp_role, cantus_role)
            trace["rule"] = (f"{SPECIES_NAMES[species]}要求每个定旋律音内在 "
                             f"{_offsets_label(species)} 起音")
            trace["missing_attack_offset_ratio"] = round((t - s) / length, 6)
            out.append(engine._finding(
                "species_attack_position", ctx, t,
                [_cf_note(cf_seg)] + ([engine._seg_note(cp_here)] if cp_here else []),
                trace,
                f"第 {ctx['measures'][cf_seg['measure_index']]['number']} 小节第 "
                f"{format_beat(engine._locate(t, ctx['measures'])[1])} 拍缺少"
                f"{SPECIES_NAMES[species]}要求的对位起音。"))
    for a in attacks:
        if not any(abs(a["start"] - t) < EPS for t in expected_times):
            trace = _base_trace(ctx, rules, species, cf_seg, a, cp_role, cantus_role)
            trace["rule"] = (f"{SPECIES_NAMES[species]}的起音只能落在 "
                             f"{_offsets_label(species)}")
            trace["actual_offset_ratio"] = round((a["start"] - s) / length, 6)
            out.append(engine._finding(
                "species_attack_position", ctx, a["start"],
                [engine._seg_note(a), _cf_note(cf_seg)], trace,
                f"第 {ctx['measures'][a['measure_index']]['number']} 小节第 "
                f"{format_beat(engine._locate(a['start'], ctx['measures'])[1])} "
                f"拍的对位起音位置不符合{SPECIES_NAMES[species]}。"))

    # ---- 对位音数量 ----------------------------------------------------
    if len(attacks) != len(expected_times):
        cp_here = attacks[0] if attacks else engine._sounding(cp_line, s)
        trace = _base_trace(ctx, rules, species, cf_seg, cp_here, cp_role, cantus_role)
        trace["rule"] = (f"{SPECIES_NAMES[species]}每个定旋律音应对应 "
                         f"{len(_ATTACK_OFFSETS[species])} 个对位音"
                         f"（{_RATIO_LABEL[species]}）")
        trace["expected_attacks"] = len(expected_times)
        trace["actual_attacks"] = len(attacks)
        trace["rhythm_ratio"]["actual"] = f"{len(attacks)}:1"
        notes = [_cf_note(cf_seg)] + [engine._seg_note(a) for a in attacks]
        out.append(engine._finding(
            "species_note_count", ctx, s, notes, trace,
            f"第 {ctx['measures'][cf_seg['measure_index']]['number']} 小节定旋律音 "
            f"{engine._pitch_name(cf_seg['pitch'])} 对应 {len(attacks)} 个对位音，"
            f"{SPECIES_NAMES[species]}要求 {_RATIO_LABEL[species]}"
            f"（本处应为 {len(expected_times)} 个）。"))

    # ---- 音符时值（第一至第三类）---------------------------------------
    if species in _NOTE_VALUE:
        expected_dur = _NOTE_VALUE[species] * length
        for a in attacks:
            actual_dur = a["end"] - a["start"]
            if abs(actual_dur - expected_dur) > EPS:
                trace = _base_trace(ctx, rules, species, cf_seg, a,
                                    cp_role, cantus_role)
                trace["rule"] = (f"{SPECIES_NAMES[species]}的对位音应为"
                                 f"{_value_name(_NOTE_VALUE[species])}")
                trace["expected_value"] = _value_name(_NOTE_VALUE[species])
                trace["actual_value_quarters"] = round(actual_dur, 6)
                out.append(engine._finding(
                    "species_note_value", ctx, a["start"],
                    [engine._seg_note(a), _cf_note(cf_seg)], trace,
                    f"第 {ctx['measures'][a['measure_index']]['number']} 小节对位音 "
                    f"{engine._pitch_name(a['pitch'])} 时值 {_q(actual_dur)} 个四分"
                    f"音符，{SPECIES_NAMES[species]}要求"
                    f"{_value_name(_NOTE_VALUE[species])}"
                    f"（{_q(expected_dur)} 个四分音符）。"))

    # ---- 第四类：切分延留 ----------------------------------------------
    if species == 4:
        out += _check_species4_ties(ctx, rules, cfg, cf_seg, cp_line,
                                    cp_role, cantus_role, s, e, length,
                                    attacks, is_first, require_tie)
    return out


def _check_species4_ties(ctx: Dict[str, Any], rules: Dict[str, Any],
                         cfg: Dict[str, Any], cf_seg: Dict[str, Any],
                         cp_line: Dict[str, Any], cp_role: str,
                         cantus_role: str, s: float, e: float, length: float,
                         attacks: List[Dict[str, Any]], is_first: bool,
                         require_tie: bool) -> List[Dict[str, Any]]:
    """第四类：强位必须是延音延续（非新起音）；弱位起音必须系延音。"""
    out: List[Dict[str, Any]] = []
    if not is_first:
        sounding = engine._sounding(cp_line, s)
        if sounding is not None and sounding["start"] >= s - EPS:
            # 强位出现新起音：切分链断裂
            trace = _base_trace(ctx, rules, 4, cf_seg, sounding, cp_role, cantus_role)
            trace["rule"] = "第四类对位强位应由前一小节弱位的延音（tie）延续，不得新起音"
            out.append(engine._finding(
                "species_tie_unexpected", ctx, s,
                [engine._seg_note(sounding), _cf_note(cf_seg)], trace,
                f"第 {ctx['measures'][cf_seg['measure_index']]['number']} 小节强拍"
                f"对位声部新起音 {engine._pitch_name(sounding['pitch'])}，"
                "第四类要求此处为前一小节弱位延留过来的音。",
                severity_default="warning"))
    for a in attacks:
        if abs(a["start"] - (s + length / 2.0)) < EPS and require_tie and not a["tied"]:
            trace = _base_trace(ctx, rules, 4, cf_seg, a, cp_role, cantus_role)
            trace["rule"] = "第四类对位弱位起音必须以延音（tie）进入下一小节强位"
            out.append(engine._finding(
                "species_tie_missing", ctx, a["start"],
                [engine._seg_note(a), _cf_note(cf_seg)], trace,
                f"第 {ctx['measures'][a['measure_index']]['number']} 小节弱位对位音 "
                f"{engine._pitch_name(a['pitch'])} 未系延音，无法形成第四类的"
                "切分延留。"))
    return out


# ---------------------------------------------------------------------------
# 终止小节：全音符收束例外
# ---------------------------------------------------------------------------

def _check_final_measure(ctx: Dict[str, Any], rules: Dict[str, Any],
                         species: int, cf_seg: Dict[str, Any],
                         cp_line: Dict[str, Any], cp_role: str,
                         cantus_role: str) -> List[Dict[str, Any]]:
    """终止小节：对位声部应以一个全音符收束（规则可关）。"""
    out: List[Dict[str, Any]] = []
    s, e = cf_seg["start"], cf_seg["end"]
    length = e - s
    attacks = [a for a in cp_line["attacks"] if s - EPS <= a["start"] < e - EPS]
    ok = (len(attacks) == 1 and abs(attacks[0]["start"] - s) < EPS
          and attacks[0]["end"] >= e - EPS)
    if not ok:
        cp_here = attacks[0] if attacks else engine._sounding(cp_line, s)
        trace = _base_trace(ctx, rules, species, cf_seg, cp_here, cp_role, cantus_role)
        trace["rule"] = "终止小节对位声部应以一个全音符收束（species.final_measure_whole_note）"
        trace["actual_attacks"] = len(attacks)
        trace["rhythm_ratio"]["actual"] = f"{len(attacks)}:1"
        notes = [_cf_note(cf_seg)] + [engine._seg_note(a) for a in attacks]
        out.append(engine._finding(
            "species_note_value", ctx, s, notes, trace,
            f"终止小节（第 {ctx['measures'][cf_seg['measure_index']]['number']} "
            f"小节）对位声部应以一个全音符收束，实际 {len(attacks)} 个起音。"))
    return out


# ---------------------------------------------------------------------------
# 第五类：混合节奏
# ---------------------------------------------------------------------------

def _check_species5_measure(ctx: Dict[str, Any], rules: Dict[str, Any],
                            cfg: Dict[str, Any], cf_seg: Dict[str, Any],
                            cp_line: Dict[str, Any], cp_role: str,
                            cantus_role: str) -> List[Dict[str, Any]]:
    """第五类：起音须落在拍点网格上（允许八分音符时为八分网格）。"""
    out: List[Dict[str, Any]] = []
    s, e = cf_seg["start"], cf_seg["end"]
    length = e - s
    grid = 8 if cfg.get("species5_allow_eighths", True) else 4
    attacks = [a for a in cp_line["attacks"] if s - EPS <= a["start"] < e - EPS]
    for a in attacks:
        offset_ratio = (a["start"] - s) / length if length > EPS else 0.0
        on_grid = abs(offset_ratio * grid - round(offset_ratio * grid)) < 1e-6
        if not on_grid:
            trace = _base_trace(ctx, rules, 5, cf_seg, a, cp_role, cantus_role)
            trace["rule"] = ("第五类对位起音须落在"
                             f"{'八分' if grid == 8 else '四分'}音符网格上")
            trace["actual_offset_ratio"] = round(offset_ratio, 6)
            out.append(engine._finding(
                "species_attack_position", ctx, a["start"],
                [engine._seg_note(a), _cf_note(cf_seg)], trace,
                f"第 {ctx['measures'][a['measure_index']]['number']} 小节对位起音"
                f"不在{'八分' if grid == 8 else '四分'}音符网格上，"
                "第五类混合节奏仍需对齐拍点。"))
        dur = a["end"] - a["start"]
        if dur < length / grid - EPS or dur > length + EPS:
            trace = _base_trace(ctx, rules, 5, cf_seg, a, cp_role, cantus_role)
            trace["rule"] = "第五类对位音符时值不得短于网格单位或超过定旋律音"
            trace["actual_value_quarters"] = round(dur, 6)
            out.append(engine._finding(
                "species_note_value", ctx, a["start"],
                [engine._seg_note(a), _cf_note(cf_seg)], trace,
                f"第 {ctx['measures'][a['measure_index']]['number']} 小节对位音 "
                f"{engine._pitch_name(a['pitch'])} 时值 {_q(dur)} 个四分音符，"
                "超出第五类允许范围。"))
    return out


def _check_species5_variety(ctx: Dict[str, Any], rules: Dict[str, Any],
                            cf_segments: List[Dict[str, Any]],
                            cp_line: Dict[str, Any], cp_role: str,
                            cantus_role: str) -> List[Dict[str, Any]]:
    """第五类要求全曲混合至少两种时值。"""
    durations = {round(a["end"] - a["start"], 6) for a in cp_line["attacks"]}
    if len(durations) >= 2 or not cp_line["attacks"]:
        return []
    first = cp_line["attacks"][0]
    cf_seg = cf_segments[0]
    trace = _base_trace(ctx, rules, 5, cf_seg, first, cp_role, cantus_role)
    trace["rule"] = "第五类（华丽对位）应混合使用多种时值（species.species5_require_mixed_values）"
    trace["distinct_values"] = sorted(durations)
    trace["rhythm_ratio"]["actual"] = "单一时值"
    return [engine._finding(
        "species5_rhythm_monotony", ctx, first["start"],
        [engine._seg_note(first), _cf_note(cf_seg)], trace,
        "第五类对位全曲只使用了一种时值，不符合混合节奏（华丽对位）的要求。",
        severity_default="warning")]


# ---------------------------------------------------------------------------
# 和声框架：起始 / 终止 / 反向级进 / 导音
# ---------------------------------------------------------------------------

def _check_harmonic_frame(ctx: Dict[str, Any], rules: Dict[str, Any],
                          cf_line: Dict[str, Any], cp_line: Dict[str, Any],
                          cantus_role: str, cp_role: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    key = _key(ctx)
    cp_attacks = cp_line["attacks"]
    cf_segments = cf_line["segments"]
    if not cp_attacks or not cf_segments:
        return out

    # ---- 起始完全协和 ---------------------------------------------------
    first = cp_attacks[0]
    cf_seg = engine._sounding(cf_line, first["start"])
    if cf_seg is not None:
        low, high = (cf_seg, first) if cantus_role == "lower" else (first, cf_seg)
        iv = rc.interval_info(low["pitch"], high["pitch"])
        perfect_ok = iv["simple_label"] in ("P1", "P5", "P8")
        if cp_role == "lower" and iv["simple_label"] == "P5":
            perfect_ok = False  # 对位在下方时五度等效四度，不作起始
        if not perfect_ok:
            trace = _base_trace(ctx, rules, 0, cf_seg, first, cp_role, cantus_role)
            trace.pop("species", None)
            trace.pop("species_name", None)
            trace.pop("rhythm_ratio", None)
            trace["vertical_interval"] = iv["label"]
            trace["counterpoint_position"] = "below" if cp_role == "lower" else "above"
            trace["rule"] = ("起始音程必须为完全协和（P1/P5/P8；"
                             "对位声部在定旋律下方时不用五度）")
            out.append(engine._finding(
                "start_interval_imperfect", ctx, first["start"],
                [engine._seg_note(first), engine._seg_note(cf_seg)], trace,
                f"第 {ctx['measures'][first['measure_index']]['number']} 小节起始"
                f"纵向音程 {iv['label']} 不是允许的完全协和音程。"))

    # ---- 终止一度或八度 --------------------------------------------------
    last = cp_attacks[-1]
    cf_last = engine._sounding(cf_line, last["start"])
    if cf_last is not None:
        low, high = (cf_last, last) if cantus_role == "lower" else (last, cf_last)
        iv = rc.interval_info(low["pitch"], high["pitch"])
        if iv["simple_label"] not in ("P1", "P8"):
            trace = _base_trace(ctx, rules, 0, cf_last, last, cp_role, cantus_role)
            trace.pop("species", None)
            trace.pop("species_name", None)
            trace.pop("rhythm_ratio", None)
            trace["vertical_interval"] = iv["label"]
            trace["rule"] = "终止音程必须为一度或八度"
            out.append(engine._finding(
                "final_interval_not_octave", ctx, last["start"],
                [engine._seg_note(last), engine._seg_note(cf_last)], trace,
                f"终止小节（第 {ctx['measures'][cf_last['measure_index']]['number']} "
                f"小节）纵向音程 {iv['label']} 不是一度或八度。"))

    # ---- 倒数第二音到终止音：反向级进 + 导音 ------------------------------
    if len(cp_attacks) >= 2 and len(cf_segments) >= 2:
        cp_pen, cp_fin = cp_attacks[-2], cp_attacks[-1]
        cf_pen, cf_fin = cf_segments[-2], cf_segments[-1]
        out += _check_cadence_motion(ctx, rules, key, cp_pen, cp_fin,
                                     cf_pen, cf_fin, cantus_role, cp_role)
        out += _check_leading_tone(ctx, rules, key, cp_pen, cp_fin,
                                   cf_pen, cf_fin, cantus_role, cp_role)
    return out


def _check_cadence_motion(ctx: Dict[str, Any], rules: Dict[str, Any],
                          key: Optional[Dict[str, Any]],
                          cp_pen: Dict[str, Any], cp_fin: Dict[str, Any],
                          cf_pen: Dict[str, Any], cf_fin: Dict[str, Any],
                          cantus_role: str, cp_role: str) -> List[Dict[str, Any]]:
    cp_move = cp_fin["pitch"]["midi"] - cp_pen["pitch"]["midi"]
    cf_move = cf_fin["pitch"]["midi"] - cf_pen["pitch"]["midi"]
    cp_steps = engine._generic_steps(cp_pen["pitch"], cp_fin["pitch"])
    cf_steps = engine._generic_steps(cf_pen["pitch"], cf_fin["pitch"])
    contrary = cp_move != 0 and cf_move != 0 and (cp_move > 0) != (cf_move > 0)

    problems: List[str] = []
    if abs(cp_steps) != 1:
        problems.append("对位声部未级进")
    if abs(cf_steps) != 1:
        problems.append("定旋律未级进")
    if not contrary:
        problems.append("两声部未反向进行")
    if not problems:
        return []

    low, high = (cf_fin, cp_fin) if cantus_role == "lower" else (cp_fin, cf_fin)
    iv = rc.interval_info(low["pitch"], high["pitch"])
    trace = {
        "key": rc.key_label(key),
        "scale_degrees": {
            "cantus_penultimate": _deg_label(key, cf_pen),
            "cantus_final": _deg_label(key, cf_fin),
            "counterpoint_penultimate": _deg_label(key, cp_pen),
            "counterpoint_final": _deg_label(key, cp_fin),
        },
        "vertical_interval": iv["label"],
        "cantus_motion": {"semitones": cf_move, "diatonic_steps": cf_steps},
        "counterpoint_motion": {"semitones": cp_move, "diatonic_steps": cp_steps},
        "contrary_motion": contrary,
        "problems": problems,
        "rule": "倒数第二音到终止音两声部须反向级进",
    }
    return [engine._finding(
        "cadence_motion", ctx, cp_fin["start"],
        [engine._seg_note(cp_pen), engine._seg_note(cp_fin),
         engine._seg_note(cf_pen), engine._seg_note(cf_fin)], trace,
        f"终止进行不符合反向级进：{'、'.join(problems)}"
        f"（对位 {engine._pitch_name(cp_pen['pitch'])}→"
        f"{engine._pitch_name(cp_fin['pitch'])}，定旋律 "
        f"{engine._pitch_name(cf_pen['pitch'])}→"
        f"{engine._pitch_name(cf_fin['pitch'])}）。")]


def _check_leading_tone(ctx: Dict[str, Any], rules: Dict[str, Any],
                        key: Optional[Dict[str, Any]],
                        cp_pen: Dict[str, Any], cp_fin: Dict[str, Any],
                        cf_pen: Dict[str, Any], cf_fin: Dict[str, Any],
                        cantus_role: str, cp_role: str) -> List[Dict[str, Any]]:
    """导音处理：倒数第二音为第七级时，须升高（小调）并级进上行解决到主音。"""
    if not key:
        return []
    out: List[Dict[str, Any]] = []
    tonic_pc = rc.key_tonic_pc(key["fifths"], key.get("mode"))
    leading_pc = (tonic_pc + 11) % 12
    for role, pen, fin in ((cp_role, cp_pen, cp_fin),
                           (cantus_role, cf_pen, cf_fin)):
        deg = rc.scale_degree(pen["pitch"], key)
        if deg is None or deg[0] != 7:
            continue
        issues: List[str] = []
        pc = pen["pitch"]["midi"] % 12
        if pc != leading_pc:
            issues.append("第七级未升高（与主音非半音关系）")
        fin_deg = rc.scale_degree(fin["pitch"], key)
        move = fin["pitch"]["midi"] - pen["pitch"]["midi"]
        steps = engine._generic_steps(pen["pitch"], fin["pitch"])
        resolves = (fin_deg is not None and fin_deg[0] == 1
                    and move > 0 and abs(steps) == 1)
        if not resolves:
            issues.append("导音未级进上行解决到主音")
        if not issues:
            continue
        trace = {
            "key": rc.key_label(key),
            "voice": role,
            "scale_degrees": {
                "penultimate": _deg_label(key, pen),
                "final": _deg_label(key, fin),
            },
            "penultimate_pc": pc,
            "leading_tone_pc": leading_pc,
            "resolution_semitones": move,
            "resolution_diatonic_steps": steps,
            "issues": issues,
            "rule": "导音（第七级）须升高半音并级进上行解决到主音",
        }
        out.append(engine._finding(
            "leading_tone", ctx, pen["start"],
            [engine._seg_note(pen), engine._seg_note(fin)], trace,
            f"第 {ctx['measures'][pen['measure_index']]['number']} 小节"
            f"{'高' if role == 'upper' else '低'}声部导音处理不当："
            f"{'、'.join(issues)}（{engine._pitch_name(pen['pitch'])}→"
            f"{engine._pitch_name(fin['pitch'])}，{rc.key_label(key)}）。"))
    return out


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _deg_label(key: Optional[Dict[str, Any]], seg: Dict[str, Any]) -> Optional[str]:
    if not key:
        return None
    deg = rc.scale_degree(seg["pitch"], key)
    return rc.degree_label(*deg) if deg else None


def _offsets_label(species: int) -> str:
    names = {0.0: "定旋律音起点", 0.25: "第二个四分位", 0.5: "中点（弱位）",
             0.75: "第四个四分位"}
    return "、".join(names[o] for o in _ATTACK_OFFSETS[species])


def _q(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.3f}".rstrip("0").rstrip(".")
