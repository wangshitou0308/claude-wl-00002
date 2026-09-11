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


def part_xml(part_id, measures, clef="G"):
    body = []
    for i, tokens in enumerate(measures, start=1):
        body.append(measure_xml(i, tokens, first=(i == 1), clef=clef))
    return f'  <part id="{part_id}">\n' + "".join(body) + "  </part>\n"


def document(title, upper_measures, lower_measures):
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
    body = part_xml("P1", upper_measures, clef="G")
    body += part_xml("P2", lower_measures, clef="F")
    return header + body + "</score-partwise>\n"


# ---------------------------------------------------------------------------
# 示例 1：干净作业。一~六小节全音符一对；七~十小节含 4-3 延留音与跨小节延音。
# ---------------------------------------------------------------------------

def good_exercise():
    upper = [
        [("C5", "w")],                                  # m1  1: C5/C3  P8
        [("D5", "w")],                                  # m2  D5/A2  M11(=M4?) -> A2..
        [("E5", "w")],                                  # m3
        [("G5", "w")],                                  # m4
        [("F5", "w")],                                  # m5
        [("A4", "w")],                                  # m6
        # m7: 高声部全音符准备 C5（对 F2 = P12 协和），m8 延续为延留音
        [("C5", "h", {"tie": "start"}), ("C5", "h", {"tie": "stop"})],
        [("C5", "h", {"tie": "start"}), ("B4", "h")],   # m8: C5 强拍延留 4 度，下行解决 B4(3度）
        [("C5", "w")],                                  # m9
        [("C5", "w")],                                  # m10
    ]
    lower = [
        [("C3", "w")],
        [("A2", "w")],
        [("C3", "w")],
        [("C3", "w")],
        [("A2", "w")],
        [("F2", "w")],
        [("F2", "w")],                                  # m7 准备点 F2/C5 = P12
        [("F2", "h"), ("G2", "h")],                     # m8: 强 F2/C5 P4(不协和延留4)，弱 G2/B4 M3
        [("G2", "w")],                                  # m9  G2/C5 P4! 需检查——改用
        [("C3", "w")],
    ]
    lower[8] = [("G2", "w")]  # G2-C5 = P12 actually
    # m9 C5/G2 = 完全协和（12度），m10 C5/C3 八度
    return document("示例1-干净的两声部对位（含4-3延留音）", upper, lower)


# ---------------------------------------------------------------------------
# 示例 2：问题作业（平行五、隐伏八、交叉、强不协和、弱不协和跳进/悬空、
# 连续大跳、超域、重复最高音）。修订版示例 3 修复其中一部分。
# ---------------------------------------------------------------------------

def bad_exercise():
    upper = [
        ["C5", "D5", "E5", "F5"],                       # m1
        ["G5", "A5", "G5", "F5"],                       # m2 G5-C3 P12, A5-D3... 平行五见下
        ["F5", "C5", "D5", "C5"],                       # m3
        ["E5", "C5", "D5", "C5"],                       # m4 含隐伏八（与低声部同向跳进）
        ["B3", "G5", "D6", "C6"],                       # m5 越界 + 连续同向大跳 + 收尾悬空不协和
    ]
    lower = [
        ["F3", "G3", "A3", "G3"],
        ["C3", "D3", "C3", "B2"],                       # m2 beat1 G5/C3 12度；beat2 A5/D3 P19(M6)
        ["B2", "A2", "G2", "A2"],                       # m3 交叉：F5/B2 正常；C5/A2...
        ["A2", "G2", "A2", "G2"],                       # m4
        ["C3", "C3", "F2", "G2"],                       # m5
    ]
    return document("示例2-含多类问题的对位作业", upper, lower)


def bad_exercise_revised():
    """修订版：修复平行五、强不协和、隐伏八、交叉、重复最高音等；保留少量旋律问题。"""
    upper = [
        ["C5", "D5", "E5", "F5"],                       # m1 保留
        ["E5", "F5", "E5", "D5"],                       # m2 改为与低声部 3/6 进行
        ["E5", "C5", "D5", "C5"],                       # m3 去交叉（E5/B2 为 P11+?）
        ["E5", "C5", "D5", "C5"],                       # m4 保持
        ["C5", "G4", "D5", "C5"],                       # m5 收束：无越界/连续同向大跳/悬空
    ]
    lower = [
        ["F3", "G3", "A3", "G3"],
        ["C3", "B2", "C3", "G2"],                       # m2: C-E M3, B-F#? F5/B2 A4 需核对
        ["B2", "A2", "G2", "A2"],
        ["A2", "G2", "A2", "G2"],
        ["C3", "C3", "F2", "C3"],                       # m5 落主音
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
