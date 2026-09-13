# -*- coding: utf-8 -*-
"""测试用极简 MusicXML 构造器：divisions=2，C 大调，4/4，两声部。"""

from __future__ import annotations

DUR = {"w": 8, "h": 4, "q": 2, "e": 1}
TYPE = {"w": "whole", "h": "half", "q": "quarter", "e": "eighth"}


def _note(token):
    # 裸字符串 "C5"/"r" 视为四分音符
    if isinstance(token, str):
        pitch, typ, opts = token, "q", {}
    else:
        pitch, typ = token[0], token[1]
        opts = token[2] if len(token) > 2 else {}
    duration = DUR[typ]
    if opts.get("dot"):
        duration = int(duration * 1.5)
    lines = ["    <note>\n"]
    if pitch == "r":
        lines.append("      <rest/>\n")
    else:
        step = pitch[0]
        i = 1
        alter = 0
        while i < len(pitch) and pitch[i] in "#b":
            alter += 1 if pitch[i] == "#" else -1
            i += 1
        octave = int(pitch[i:])
        lines.append("      <pitch>\n")
        lines.append(f"        <step>{step}</step>\n")
        if alter:
            lines.append(f"        <alter>{alter}</alter>\n")
        lines.append(f"        <octave>{octave}</octave>\n")
        lines.append("      </pitch>\n")
    lines.append(f"      <duration>{duration}</duration>\n")
    tie = opts.get("tie")
    if tie in ("start", "stop"):
        lines.append(f'      <tie type="{tie}"/>\n')
    lines.append(f"      <type>{TYPE[typ]}</type>\n")
    if opts.get("dot"):
        lines.append("      <dot/>\n")
    if tie in ("start", "stop"):
        lines.append("      <notations>\n")
        lines.append(f'        <tied type="{tie}"/>\n')
        lines.append("      </notations>\n")
    lines.append("      <voice>1</voice>\n")
    lines.append("    </note>\n")
    return "".join(lines)


def _measure(number, tokens, attrs=False, clef="G", time_sig=(4, 4),
             key_fifths=0, key_mode="major"):
    out = [f'  <measure number="{number}">\n']
    if attrs:
        out.append("""    <attributes>
      <divisions>2</divisions>
      <key><fifths>%d</fifths><mode>%s</mode></key>
      <time><beats>%d</beats><beat-type>%d</beat-type></time>
      <clef><sign>%s</sign><line>%s</line></clef>
    </attributes>
""" % (key_fifths, key_mode, time_sig[0], time_sig[1],
            clef, 2 if clef == "G" else 4))
    for tok in tokens:
        out.append(_note(tok))
    out.append("  </measure>\n")
    return "".join(out)


def build_doc(upper_measures, lower_measures, time_sig=(4, 4),
              key_fifths=0, key_mode="major"):
    parts = []
    for i, tokens in enumerate(upper_measures, start=1):
        parts.append(_measure(i, tokens, attrs=(i == 1), clef="G",
                              time_sig=time_sig, key_fifths=key_fifths,
                              key_mode=key_mode))
    p1 = '  <part id="P1">\n' + "".join(parts) + "  </part>\n"

    parts = []
    for i, tokens in enumerate(lower_measures, start=1):
        parts.append(_measure(i, tokens, attrs=(i == 1), clef="F",
                              time_sig=time_sig, key_fifths=key_fifths,
                              key_mode=key_mode))
    p2 = '  <part id="P2">\n' + "".join(parts) + "  </part>\n"

    return ("""<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="4.0">
  <part-list>
    <score-part id="P1"><part-name>Soprano</part-name></score-part>
    <score-part id="P2"><part-name>Bass</part-name></score-part>
  </part-list>
""" + p1 + p2 + "</score-partwise>\n").encode("utf-8")
