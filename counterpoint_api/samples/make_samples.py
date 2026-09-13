# -*- coding: utf-8 -*-
"""生成示例 MusicXML（score-partwise，divisions=2，4/4，C 大调）。

用法::

    python make_samples.py

紧凑记谱说明：``"C4"`` 为四分音符，``("C4", "w")`` 为全音符，
``("C4", "h", tie="start")`` 带延音开始，``"r"``/``("r","q")`` 为休止。
时值: w=whole h=half q=quarter e=eighth，附点用 ``dot=True``。

该脚本本身也演示本项目期望的合法两声部 MusicXML 写法。
"""

from __future__ import annotations

import os
from xml.sax.saxutils import escape

DURATIONS = {"w": 8, "h": 4, "q": 2, "e": 1}  # divisions=2 下的 <duration>
TYPE_NAMES = {"w": "whole", "h": "half", "q": "quarter", "e": "eighth"}

_STEP_SEMI = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


def parse_pitch(text):
    step = text[0]
    i = 1
    alter = 0
    while i < len(text) and text[i] in "#b":
        alter += 1 if text[i] == "#" else -1
        i += 1
    octave = int(text[i:])
    return step, alter, octave


def note_xml(token, voice="1"):
    """token 可为 'C4'（四分音符）或 (pitch/rest, 时值, 选项)。"""
    if isinstance(token, str):
        head, typ, opts = token, "q", {}
    else:
        head = token[0]
        if len(token) > 1 and isinstance(token[1], str):
            typ, opts = token[1], token[2] if len(token) > 2 else {}
        else:
            typ, opts = "q", token[1] if len(token) > 1 else {}
    length = DURATIONS[typ]
    if opts.get("dot"):
        length = int(length * 1.5)
    pitch = None if head == "r" else head

    attrs = []
    if opts.get("chord"):
        attrs.append("      <chord/>\n")
    if pitch is None:
        attrs.append("      <rest/>\n")
    else:
        step, alter, octave = parse_pitch(pitch)
        pitch_lines = [f"        <step>{step}</step>\n"]
        if alter:
            pitch_lines.append(f"        <alter>{alter}</alter>\n")
        pitch_lines.append(f"        <octave>{octave}</octave>\n")
        attrs.append("      <pitch>\n" + "".join(pitch_lines) + "      </pitch>\n")
    attrs.append(f"      <duration>{length}</duration>\n")
    tie = opts.get("tie")
    if tie in ("start", "stop", "both"):
        if tie in ("start", "both"):
            attrs.append('      <tie type="start"/>\n')
        if tie in ("stop", "both"):
            attrs.append('      <tie type="stop"/>\n')
    attrs.append(f"      <type>{TYPE_NAMES[typ]}</type>\n")
    if opts.get("dot"):
        attrs.append("      <dot/>\n")
    if tie in ("start", "stop", "both"):
        tied_types = []
        if tie in ("start", "both"):
            tied_types.append("start")
        if tie in ("stop", "both"):
            tied_types.append("stop")
        ties = "".join(f'        <tied type="{t}"/>\n' for t in tied_types)
        attrs.append("      <notations>\n" + ties + "      </notations>\n")
    attrs.append(f"      <voice>{voice}</voice>\n")
    return "    <note>\n" + "".join(attrs) + "    </note>\n"


def measure_xml(number, tokens, time_sig=(4, 4), key_fifths=0,
                first=False, divisions=2, clef="G"):
    out = [f'  <measure number="{number}">\n']
    if first:
        out.append("    <attributes>\n")
        out.append(f"      <divisions>{divisions}</divisions>\n")
        out.append("      <key>\n")
        out.append(f"        <fifths>{key_fifths}</fifths>\n")
        out.append("        <mode>major</mode>\n")
        out.append("      </key>\n")
        out.append("      <time>\n")
        out.append(f"        <beats>{time_sig[0]}</beats>\n")
        out.append(f"        <beat-type>{time_sig[1]}</beat-type>\n")
        out.append("      </time>\n")
        out.append("      <clef>\n")
        out.append(f"        <sign>{clef}</sign>"
                   f"<line>{2 if clef == 'G' else 4}</line>\n")
        out.append("      </clef>\n")
        out.append("    </attributes>\n")
    for tok in tokens:
        out.append(note_xml(tok))
    out.append("  </measure>\n")
    return "".join(out)


def part_xml(part_id, measures_list, clef="G", time_sig=(4, 4)):
    body = []
    for i, tokens in enumerate(measures_list, start=1):
        body.append(measure_xml(i, tokens, first=(i == 1), clef=clef,
                                time_sig=time_sig))
    return f'  <part id="{part_id}">\n' + "".join(body) + "  </part>\n"


def document(title, upper_measures, lower_measures, time_sig=(4, 4)):
    header = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE score-partwise PUBLIC "-//Recordare//DTD MusicXML 4.0 Partwise//EN"
  "http://www.musicxml.org/dtds/partwise.dtd">
<score-partwise version="4.0">
  <work><work-title>{escape(title)}</work-title></work>
  <part-list>
    <score-part id="P1"><part-name>Soprano</part-name></score-part>
    <score-part id="P2"><part-name>Bass</part-name></score-part>
  </part-list>
"""
    body = part_xml("P1", upper_measures, clef="G", time_sig=time_sig)
    body += part_xml("P2", lower_measures, clef="F", time_sig=time_sig)
    return header + body + "</score-partwise>\n"


# ---------------------------------------------------------------------------
# 示例 1：干净作业。一~六小节全音符一对；七~十小节含 4-3 延留音与跨小节延音。
# ---------------------------------------------------------------------------

def good_exercise():
    """干净作业：全音符圣咏式 1:1 进行，末段带 4-3 延留音。

    纵向（m1..m10）：m6 P8 M3 M10 m6 m6 M6 [4-3延留] M6 P8，
    强拍全部协和，m8 为有准备、低音保持、级进下行解决的 4-3 延留音
    （跨小节延音），无平行/隐伏五八度（到达 P8/P5 均为反向）、
    无交叉、旋律大跳均在规则内且大跳后级进折回、最高音只出现一次、
    两声部均在默认音域内——分析结果应为 0 条发现。
    """
    upper = [
        [("C5", "w")],                                   # m1  C5/E4  m6
        [("D5", "w")],                                   # m2  D5/D4  P8（反向到达）
        [("E5", "w")],                                   # m3  E5/C4  M3
        [("G5", "w")],                                   # m4  G5/E4  M10
        [("F5", "w")],                                   # m5  F5/D4  M10（G5→F5 级进）
        [("E5", "w")],                                   # m6  E5/G3  M6（F5→E5 级进）
        # m7 准备：C5 对 G3 = P4…不协和；准备点须协和，用 C5/A3=M6
        [("C5", "w", {"tie": "start"})],                 # m7  C5/A3  M6
        [("C5", "h", {"tie": "stop"}), ("B4", "h")],     # m8  强 C5/G3=P4 延留 → B4/G3=M3
        [("A4", "w")],                                   # m9  A4/C4  M6
        [("C5", "w")],                                   # m10 C5/C3  P8（反向收束）
    ]
    lower = [
        [("E4", "w")],
        [("D4", "w")],
        [("C4", "w")],
        [("E4", "w")],
        [("D4", "w")],                                   # E4→D4 级进
        [("G3", "w")],                                   # D4→G3 纯四下行大跳
        [("A3", "w")],                                   # G3→A3 级进折回；C5/A3=M6 准备
        [("G3", "w")],                                   # A3→G3 级进，低音保持 G3 作延留底音
        [("C4", "w")],                                   # G3→C4 纯四上行大跳
        [("C3", "w")],                                   # C4→C3 级进折回，反向到八度
    ]
    return document("示例1-干净的两声部对位（含4-3延留音）", upper, lower)


# ---------------------------------------------------------------------------
# 示例 2：问题作业（平行五、隐伏八、交叉、强不协和、弱不协和跳进、
# 连续大跳、超域、重复最高音等）。修订版示例 3 修复其中一部分。
# ---------------------------------------------------------------------------

def bad_exercise():
    upper = [
        ["C5", "D5", "E5", "E6"],                        # m1 beat2-3 平行五；E6 越界音之一
        ["G5", "A5", "G5", "F5"],                        # m2
        ["F5", "B4", "E5", "D5"],                        # m3 beat3 隐伏八度
        ["E5", "C5", "D5", "C5"],                        # m4 强拍不协和等
        ["B3", "A5", "E6", "E6"],                        # m5 连续同向大跳+超域+最高音第3次
    ]
    lower = [
        ["F3", "G3", "A3", "G3"],                        # m1 D5/G3→E5/A3 两个 P12 同向
        ["C3", "D3", "C3", "B2"],
        ["B2", "G3", "E4", "B3"],                        # m3 B4/G3→E5/E4 同向跳进至八度
        ["A2", "G2", "A2", "G2"],
        ["C3", "C3", "F2", "G2"],
    ]
    return document("示例2-含多类问题的对位作业", upper, lower)


def bad_exercise_revised():
    """修订版：消除平行五、隐伏八、连续大跳、超域与重复最高音；仍残留少量问题。"""
    upper = [
        ["C5", "D5", "E5", "F5"],                        # m1 改低声部打破平行五
        ["E5", "F5", "E5", "D5"],
        ["E5", "C5", "D5", "C5"],                        # m3 改进行，去除隐伏八
        ["E5", "C5", "D5", "C5"],
        ["C5", "G4", "D5", "C5"],                        # m5 无越界/连续大跳
    ]
    lower = [
        ["F3", "G3", "B3", "G3"],
        ["C3", "B2", "C3", "G2"],
        ["B2", "A2", "G2", "A2"],
        ["A2", "G2", "A2", "G2"],
        ["C3", "C3", "F2", "C3"],
    ]
    return document("示例2修订版-部分问题已修复", upper, lower)


# ---------------------------------------------------------------------------
# 示例 4：无法分析的记谱——多声部混写 + 时值缺失
# ---------------------------------------------------------------------------

def broken_notation():
    # 手工拼一谱：P1 含 voice 1 与 voice 2 混写；P2 有音符缺 <duration>
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="4.0">
  <part-list>
    <score-part id="P1"><part-name>Soprano</part-name></score-part>
    <score-part id="P2"><part-name>Bass</part-name></score-part>
  </part-list>
  <part id="P1">
    <measure number="1">
      <attributes>
        <divisions>2</divisions>
        <key><fifths>0</fifths><mode>major</mode></key>
        <time><beats>4</beats><beat-type>4</beat-type></time>
        <clef><sign>G</sign><line>2</line></clef>
      </attributes>
      <note>
        <pitch><step>C</step><octave>5</octave></pitch>
        <duration>4</duration><type>half</type><voice>1</voice>
      </note>
      <note>
        <pitch><step>G</step><octave>4</octave></pitch>
        <duration>4</duration><type>half</type><voice>2</voice>
      </note>
      <note>
        <pitch><step>D</step><octave>5</octave></pitch>
        <duration>4</duration><type>half</type><voice>1</voice>
      </note>
      <note>
        <pitch><step>A</step><octave>4</octave></pitch>
        <duration>4</duration><type>half</type><voice>2</voice>
      </note>
    </measure>
    <measure number="2">
      <note>
        <pitch><step>E</step><octave>5</octave></pitch>
        <duration>8</duration><type>whole</type><voice>1</voice>
      </note>
    </measure>
  </part>
  <part id="P2">
    <measure number="1">
      <note>
        <pitch><step>C</step><octave>3</octave></pitch>
        <duration>8</duration><type>whole</type><voice>1</voice>
      </note>
    </measure>
    <measure number="2">
      <note>
        <rest/>
        <type>whole</type><voice>1</voice>
      </note>
    </measure>
  </part>
</score-partwise>
"""
    return xml


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    files = {
        "good_exercise.musicxml": good_exercise(),
        "bad_exercise.musicxml": bad_exercise(),
        "bad_exercise_revised.musicxml": bad_exercise_revised(),
        "broken_notation.musicxml": broken_notation(),
    }
    for name, content in files.items():
        path = os.path.join(here, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        print("written", path, len(content), "bytes")


if __name__ == "__main__":
    main()
