# -*- coding: utf-8 -*-
"""规则配置：强弱拍、协和音程、旋律音域、最大跳进等。

一份 *规则配置 (RuleSet)* 是不可变的 JSON 结构，可被作业选用或复制后另存。
本模块负责：

* 提供默认配置 :data:`DEFAULT_RULES`（C 调严格对位教学常用值）；
* 校验用户提交的配置 :func:`validate_rules`；
* 合并用户覆盖项并给出规则版本摘要 :func:`build_rules`；
* 音程语义（度数、性质、协和判定），供分析引擎调用。

音程标签采用 ``"P8"``、``"m3"``、``"d5"`` 这样的短记法：
``P/m/M/A/d`` + 单音程度数（1–7）；复音程默认折算单音程，可在配置中关闭。
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 音名换算
# ---------------------------------------------------------------------------

#: 白名字号 C D E F G A B（对位按自然音级计度数）
_STEPS = {"C": 0, "D": 1, "E": 2, "F": 3, "G": 4, "A": 5, "B": 6}

#: 各自然音程（按度数索引 1..8）在大小调体系下的半音数基准（小/纯）
_SEMITONE_TABLE = {
    1: 0,   # 纯一度
    2: 1,   # 小二度
    3: 3,   # 小三度
    4: 5,   # 纯四度
    5: 7,   # 纯五度
    6: 8,   # 小六度
    7: 10,  # 小七度
    8: 12,  # 纯八度
}

#: 协和音程家族（strict counterpoint 默认）
DEFAULT_CONSONANT_INTERVALS: List[str] = ["P1", "m3", "M3", "P5", "m6", "M6", "P8"]

#: 完全协和音程家族（平行/隐伏五八度判定使用）
PERFECT_FAMILIES = ("P1", "P5", "P8")

#: 默认强弱拍（拍号字符串 -> 强拍列表，拍号从 1 计）
DEFAULT_STRONG_BEATS: Dict[str, List[int]] = {
    "2/4": [1],
    "3/4": [1],
    "4/4": [1, 3],
    "2/2": [1],
    "3/2": [1],
    "6/8": [1, 4],
    "9/8": [1, 4, 7],
    "12/8": [1, 4, 7, 10],
}

#: 默认旋律音域（按声部角色：upper=高声部 / lower=低声部）
DEFAULT_RANGES: Dict[str, Dict[str, str]] = {
    "upper": {"low": "E4", "high": "A5"},
    "lower": {"low": "C3", "high": "E4"},
}

#: 内置默认规则。键名稳定，即对外 API 字段。
DEFAULT_RULES: Dict[str, Any] = {
    "name": "严格对位默认规则",
    "description": "Fux 风格两声部严格对位教学默认值：五八度禁止同向/平行，弱拍不协和限经过音与辅助音，延留音须级进下行解决。",
    "beat": {
        # 未在 strong_beats 中显式声明的拍号，按单拍子首拍、复拍子按 3 个单位音组分拍
        "default_strong_beats": DEFAULT_STRONG_BEATS,
        # 隐伏五八度中，任一声部做“跳进”的阈值（半音数）
        "hidden_leap_min_semitones": 4,
        # 反向到达的五/八度是否豁免隐伏判定（传统上反向八度允许）
        "hidden_allow_contrary_octave": True,
        "hidden_check_unisons": False,
    },
    "intervals": {
        "consonant": DEFAULT_CONSONANT_INTERVALS,
        # 复音程（>八度）折算为单音程后判定协和
        "reduce_compound": True,
        # 平行一度是否与平行五八度同罚
        "parallel_unisons_forbidden": False,
    },
    "melody": {
        # 单声部最大跳进（半音数）
        "max_leap_semitones": 12,
        # 连续两次同向跳进均达到此值时记为“连续大跳”
        "consecutive_leap_semitones": 7,
        # 大跳之后须反向级进（不满足时另记一条）
        "leap_must_reverse_by_step": True,
        "ranges": DEFAULT_RANGES,
        # 同一声部最高音出现超过该次数即记“重复最高音”（2=允许出现两次）
        "max_highest_note_occurrences": 2,
    },
    "dissonance": {
        # 强拍允许的不协和：仅延留音（suspension）
        "allow_suspension": True,
        # 延留音准备：延留音必须在前一拍已发声且前一拍为协和
        "suspension_require_preparation": True,
        # 允许的延留音解决标签（如 4-3、7-6、9-8）
        "suspension_resolutions": ["4-3", "7-6", "9-8"],
        # 弱拍不协和允许的形态
        "allow_passing_tone": True,
        "allow_neighbor_tone": True,
        # 持续音（另一声部保持时进入的弱拍不协和）
        "allow_pedal": False,
    },
    "voice": {
        # 声部交叉检查
        "check_crossing": True,
        # 声部超越（前一纵向点不交叉、当前点越过）额外检查，默认关闭
        "check_overlap": False,
    },
    "species": {
        # 首小节允许对位声部以休止符开始（如第二类/第四类的半休止）
        "allow_first_measure_rest": True,
        # 首小节休止允许的最大长度（四分音符数）
        "first_measure_rest_max_quarters": 2.0,
        # 终止小节允许对位声部用全音符收束（不按类别节奏要求）
        "final_measure_whole_note": True,
        # 倒数第二小节允许灵活节奏（接近终止时可打破类别节奏型）
        "penultimate_measure_flexible": True,
        # 第五类要求全曲至少两种不同时值（混合节奏）
        "species5_require_mixed_values": True,
        # 第五类允许八分音符
        "species5_allow_eighths": True,
    },
    # 各类发现的严重级别，可被教师覆盖
    "severity": {
        "voice_crossing": "error",
        "voice_overlap": "warning",
        "parallel_fifth": "error",
        "parallel_octave": "error",
        "parallel_unison": "warning",
        "hidden_fifth": "error",
        "hidden_octave": "error",
        "strong_dissonance": "error",
        "weak_dissonance_entry": "error",
        "weak_dissonance_resolution": "error",
        "consecutive_leaps": "warning",
        "leap_not_reversed": "warning",
        "leap_too_large": "warning",
        "repeated_highest": "warning",
        "range_violation": "warning",
        # 对位类别（species）专属发现
        "species_note_count": "error",
        "species_attack_position": "error",
        "species_note_value": "error",
        "species_tie_missing": "error",
        "species_tie_unexpected": "warning",
        "species5_rhythm_monotony": "warning",
        "start_interval_imperfect": "error",
        "final_interval_not_octave": "error",
        "cadence_motion": "error",
        "leading_tone": "error",
    },
}


# ---------------------------------------------------------------------------
# 配置合并与校验
# ---------------------------------------------------------------------------

def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """递归合并：override 的叶子值覆盖 base；dict 递归，其它整体替换。"""
    out = copy.deepcopy(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def validate_rules(rules: Any) -> List[str]:
    """校验规则结构，返回错误消息列表（空列表表示通过）。

    校验保持克制：只检查语义会直接影响分析的字段，未知字段保留不报错，
    便于不同教学法扩展配置。
    """
    errors: List[str] = []
    if not isinstance(rules, dict):
        return ["规则配置必须是 JSON 对象"]

    beats = rules.get("beat", {})
    if isinstance(beats, dict):
        strong = beats.get("default_strong_beats", {})
        if not isinstance(strong, dict):
            errors.append("beat.default_strong_beats 必须是 拍号->强拍数组 的对象")
        else:
            for sig, lst in strong.items():
                if not (isinstance(lst, list) and all(isinstance(x, int) and x >= 1 for x in lst)):
                    errors.append(f"拍号 {sig} 的强拍必须是从 1 开始的整数数组")
        leap = beats.get("hidden_leap_min_semitones")
        if leap is not None and not (isinstance(leap, int) and leap >= 1):
            errors.append("beat.hidden_leap_min_semitones 必须是 >=1 的整数")

    iv = rules.get("intervals", {})
    if isinstance(iv, dict):
        cons = iv.get("consonant")
        if cons is not None:
            if not isinstance(cons, list) or not cons:
                errors.append("intervals.consonant 必须是非空数组")
            else:
                for label in cons:
                    if not isinstance(label, str) or parse_interval_label(label) is None:
                        errors.append(f"无法识别的协和音程标签: {label!r}（形如 P1/m3/M6/d5）")

    mel = rules.get("melody", {})
    if isinstance(mel, dict):
        for key in ("max_leap_semitones", "consecutive_leap_semitones"):
            val = mel.get(key)
            if val is not None and not (isinstance(val, int) and val >= 1):
                errors.append(f"melody.{key} 必须是 >=1 的整数")
        occ = mel.get("max_highest_note_occurrences")
        if occ is not None and not (isinstance(occ, int) and occ >= 1):
            errors.append("melody.max_highest_note_occurrences 必须是 >=1 的整数")
        ranges = mel.get("ranges", {})
        if ranges is not None:
            if not isinstance(ranges, dict):
                errors.append("melody.ranges 必须是 upper/lower -> {low,high} 的对象")
            else:
                for voice, rng in ranges.items():
                    if not isinstance(rng, dict):
                        errors.append(f"melody.ranges.{voice} 必须包含 low/high")
                        continue
                    for edge in ("low", "high"):
                        if edge in rng and pitch_to_midi(str(rng[edge])) is None:
                            errors.append(f"melody.ranges.{voice}.{edge} 音高无法识别: {rng[edge]!r}")
                    lo, hi = rng.get("low"), rng.get("high")
                    if lo is not None and hi is not None:
                        if pitch_to_midi(str(lo)) > pitch_to_midi(str(hi)):
                            errors.append(f"melody.ranges.{voice}: low 高于 high")

    sp = rules.get("species", {})
    if isinstance(sp, dict):
        for key in ("allow_first_measure_rest", "final_measure_whole_note",
                    "penultimate_measure_flexible", "species5_require_mixed_values",
                    "species5_allow_eighths"):
            val = sp.get(key)
            if val is not None and not isinstance(val, bool):
                errors.append(f"species.{key} 必须是布尔值")
        rest = sp.get("first_measure_rest_max_quarters")
        if rest is not None and not (isinstance(rest, (int, float))
                                     and not isinstance(rest, bool) and rest >= 0):
            errors.append("species.first_measure_rest_max_quarters 必须是 >=0 的数")

    sev = rules.get("severity", {})
    if sev is not None and not isinstance(sev, dict):
        errors.append("severity 必须是 类型->error/warning/info 的对象")
    elif isinstance(sev, dict):
        for kind, level in sev.items():
            if level not in ("error", "warning", "info"):
                errors.append(f"severity.{kind} 只允许 error/warning/info")
    return errors


def build_rules(override: Optional[Dict[str, Any]] = None,
                name: Optional[str] = None) -> Dict[str, Any]:
    """以默认规则为底，合并覆盖项，附加 name 与版本指纹。

    :raises ValueError: 配置校验失败
    """
    merged = _deep_merge(DEFAULT_RULES, override or {})
    errors = validate_rules(merged)
    if errors:
        raise ValueError("规则配置无效: " + "; ".join(errors))
    if name:
        merged["name"] = name
    merged["version"] = rule_fingerprint(merged)
    return merged


def rule_fingerprint(rules: Dict[str, Any]) -> str:
    """对规则内容做 SHA1 前 12 位，作为规则版本号。

    name/description/version 不参与指纹——改名字不产生新版本。
    """
    payload = {k: v for k, v in rules.items() if k not in ("name", "description", "version")}
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# 音高与音程
# ---------------------------------------------------------------------------

def pitch_to_midi(text: str) -> Optional[int]:
    """把 ``C4``/``Bb3``/``F#5`` 这样的音名转 MIDI 号，失败返回 None。"""
    text = text.strip()
    if len(text) < 2 or text[0].upper() not in _STEPS:
        return None
    step = _STEPS[text[0].upper()]
    i = 1
    alter = 0
    while i < len(text) and text[i] in "#b":
        alter += 1 if text[i] == "#" else -1
        i += 1
    if i >= len(text):
        return None
    try:
        octave = int(text[i:])
    except ValueError:
        return None
    # C 大调音阶半音偏移
    nat = [0, 2, 4, 5, 7, 9, 11][step]
    return (octave + 1) * 12 + nat + alter


def midi_to_pitch(midi: int) -> str:
    """MIDI 号转带变音记号的音名（升号体系），如 60 -> C4。"""
    names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    octave = midi // 12 - 1
    return f"{names[midi % 12]}{octave}"


def diatonic_index(step: str, alter: int, octave: int) -> int:
    """自然音级序号（C0=0），用于算度数。"""
    return (octave * 7) + _STEPS[step]


def interval_info(low_pitch: Dict[str, Any], high_pitch: Dict[str, Any]) -> Dict[str, Any]:
    """计算两个音高之间的音程信息。

    返回字段：``semitones``（半音数，恒 >=0）、``generic``（度数 1/2/...）、
    ``quality``（P/m/M/A/d/dd…）、``label``（``M6`` 等）、
    ``compound``（是否复音程）、``simple_label``（折算单音程标签）。

    入参为解析器产生的音高 dict（step/alter/octave）。
    """
    m1 = (low_pitch["octave"] + 1) * 12 + {"C": 0, "D": 2, "E": 4, "F": 5,
                                           "G": 7, "A": 9, "B": 11}[low_pitch["step"]] + low_pitch.get("alter", 0)
    m2 = (high_pitch["octave"] + 1) * 12 + {"C": 0, "D": 2, "E": 4, "F": 5,
                                            "G": 7, "A": 9, "B": 11}[high_pitch["step"]] + high_pitch.get("alter", 0)
    if m2 < m1:
        m1, m2 = m2, m1
        low_pitch, high_pitch = high_pitch, low_pitch
    semitones = m2 - m1

    d1 = diatonic_index(low_pitch["step"], 0, low_pitch["octave"])
    d2 = diatonic_index(high_pitch["step"], 0, high_pitch["octave"])
    generic = d2 - d1 + 1  # 1=unison

    # 折算单音程度数（1..7，八度保留为 8）
    g = generic
    while g > 8:
        g -= 7

    simple_semitones = semitones % 12
    if g == 8:
        simple_semitones = 12

    quality, simple_label = _classify(g, simple_semitones)
    compound = generic > 8
    if compound:
        # 复音程标签按实际度数标注（性质与折算单音程相同）
        label = f"{quality}{generic}"
    else:
        label = simple_label
    return {
        "semitones": semitones,
        "generic": generic,
        "quality": quality,
        "label": label,
        "simple_label": simple_label,
        "compound": compound,
    }


def _classify(g: int, semitones: int) -> Tuple[str, str]:
    """按单音程度数 (1..8) 与半音数判定性质，返回 (quality, label)。

    纯音程族（1/4/5/8）基准为纯；大小族（2/3/6/7）基准为小，大半音数 +1。
    """
    base = _SEMITONE_TABLE[g]
    diff = semitones - base
    perfect = g in (1, 4, 5, 8)
    if perfect:
        if diff == 0:
            quality = "P"
        elif diff > 0:
            quality = "A" * diff
        else:
            quality = "d" * (-diff)
        return quality, f"{quality}{g}"
    # 大小族
    if diff == 0:
        quality = "m"
    elif diff == 1:
        quality = "M"
    elif diff >= 2:
        quality = "A" * (diff - 1)
    else:
        quality = "d" * (-diff)
    return quality, f"{quality}{g}"


def parse_interval_label(label: str) -> Optional[Tuple[str, int]]:
    """解析 ``M3`` 这样的标签为 (性质前缀, 度数)；非法返回 None。"""
    if not isinstance(label, str) or len(label) < 2:
        return None
    i = 0
    while i < len(label) and label[i] in "PmMAd":
        i += 1
    if i == 0 or i == len(label):
        return None
    try:
        degree = int(label[i:])
    except ValueError:
        return None
    if degree < 1:
        return None
    return label[:i], degree


def is_consonant(iv: Dict[str, Any], rules: Dict[str, Any]) -> bool:
    """按配置判断音程信息是否协和。"""
    cfg = rules["intervals"]
    target = iv["simple_label"] if cfg.get("reduce_compound", True) else iv["label"]
    allowed = set(cfg["consonant"])
    if target in allowed:
        return True
    # 配置写单音程、谱面是复音程（或反之）时按折算再匹配一次
    if cfg.get("reduce_compound", True):
        for label in allowed:
            parsed = parse_interval_label(label)
            if parsed and parsed[1] > 7 and iv["label"] == label:
                return True
    return False


def is_perfect(iv: Dict[str, Any]) -> bool:
    return iv["simple_label"] in PERFECT_FAMILIES


# ---------------------------------------------------------------------------
# 调号与音级
# ---------------------------------------------------------------------------

#: 大调 / 自然小调音阶（相对主音的半音偏移，级数 1..7）
_MAJOR_SCALE = [0, 2, 4, 5, 7, 9, 11]
_NATURAL_MINOR_SCALE = [0, 2, 3, 5, 7, 8, 10]

_MAJOR_NAMES = {0: "C", 1: "G", 2: "D", 3: "A", 4: "E", 5: "B", 6: "F#",
                7: "C#", -1: "F", -2: "Bb", -3: "Eb", -4: "Ab", -5: "Db",
                -6: "Gb", -7: "Cb"}
_MINOR_NAMES = {0: "a", 1: "e", 2: "b", 3: "f#", 4: "c#", 5: "g#", 6: "d#",
                7: "a#", -1: "d", -2: "g", -3: "c", -4: "f", -5: "bb",
                -6: "eb", -7: "ab"}


def is_minor_mode(mode: Optional[str]) -> bool:
    return bool(mode) and str(mode).strip().lower().startswith("minor")


def key_tonic_pc(fifths: int, mode: Optional[str] = None) -> int:
    """调号 -> 主音音高类别（0=C … 11=B）。小调取关系小调主音。"""
    base = (fifths * 7) % 12
    if is_minor_mode(mode):
        base = (base + 9) % 12
    return base


def key_label(key: Optional[Dict[str, Any]]) -> Optional[str]:
    """调号可读标签，如 ``C 大调`` / ``a 小调``；无调号返回 None。"""
    if not key:
        return None
    fifths = key.get("fifths")
    if fifths is None:
        return None
    if is_minor_mode(key.get("mode")):
        name = _MINOR_NAMES.get(fifths, f"{fifths:+d}")
        return f"{name} 小调"
    name = _MAJOR_NAMES.get(fifths, f"{fifths:+d}")
    return f"{name} 大调"


def scale_degree(pitch: Dict[str, Any], key: Optional[Dict[str, Any]]
                 ) -> Optional[Tuple[int, int]]:
    """音高在调内的音级。返回 ``(级数 1-7, 变化半音)``，无法确定调时返回 None。

    变化半音：0=自然级，+1=升高半音（如小调导音 #7），-1=降低半音。
    """
    if not key or key.get("fifths") is None:
        return None
    tonic = key_tonic_pc(key["fifths"], key.get("mode"))
    scale = _NATURAL_MINOR_SCALE if is_minor_mode(key.get("mode")) else _MAJOR_SCALE
    rel = (pitch["midi"] - tonic) % 12
    for idx, semi in enumerate(scale):
        if rel == semi:
            return (idx + 1, 0)
    for idx, semi in enumerate(scale):
        if rel == (semi + 1) % 12:
            return (idx + 1, 1)
        if rel == (semi - 1) % 12:
            return (idx + 1, -1)
    return None


def degree_label(degree: int, alter: int = 0) -> str:
    """音级标签：``1``、``#7``、``b3``。"""
    prefix = "#" * alter if alter > 0 else ("b" * (-alter) if alter < 0 else "")
    return f"{prefix}{degree}"


# ---------------------------------------------------------------------------
# 拍号强弱拍
# ---------------------------------------------------------------------------

def strong_beats_for(time_sig: Dict[str, int], rules: Dict[str, Any]) -> List[int]:
    """返回某拍号下的强拍（从 1 计）。

    先查规则显式表；未配置时按教学惯例推断：
    单拍子首拍强；复拍子（每拍为附点单位，分母为 8 且分子是 3 的倍数）
    按每 3 个八分音符一组。
    """
    sig = f"{time_sig['beats']}/{time_sig['beat_type']}"
    table = rules["beat"]["default_strong_beats"]
    if sig in table:
        return list(table[sig])
    numerator, denominator = time_sig["beats"], time_sig["beat_type"]
    if denominator == 8 and numerator % 3 == 0 and numerator > 3:
        return list(range(1, numerator + 1, 3))
    return [1]
