# -*- coding: utf-8 -*-
"""核心测试：解析器、规则引擎、存储、服务编排（不启动 HTTP）。

直接运行：``python -m counterpoint_api.tests.test_core``
或用 unittest：``python -m unittest counterpoint_api.tests.test_core -v``
"""

import json
import os
import unittest

from counterpoint_api import counterpoint, musicxml_io, rulesconfig as rc
from counterpoint_api.storage import Database
from counterpoint_api import service

SAMPLE_DIR = os.path.join(os.path.dirname(__file__), "..", "samples")


def read_sample(name):
    with open(os.path.join(SAMPLE_DIR, name), "rb") as fh:
        return fh.read()


class TestInterval(unittest.TestCase):
    def test_basic_intervals(self):
        def iv(a, b):
            pa = {"step": a[0], "alter": 0, "octave": int(a[1])}
            pb = {"step": b[0], "alter": 0, "octave": int(b[1])}
            return rc.interval_info(pa, pb)

        self.assertEqual(iv("C4", "G4")["label"], "P5")
        self.assertEqual(iv("C4", "C5")["label"], "P8")
        self.assertEqual(iv("C4", "E4")["label"], "M3")
        self.assertEqual(iv("E4", "C5")["label"], "m6")
        self.assertEqual(iv("B3", "F4")["label"], "d5")
        self.assertEqual(iv("F4", "B4")["label"], "A4")
        self.assertEqual(iv("C4", "F4")["label"], "P4")
        self.assertTrue(iv("C3", "E4")["compound"])
        self.assertEqual(iv("C3", "E4")["simple_label"], "M3")

    def test_consonance(self):
        rules = rc.build_rules()

        def cons(a, b):
            pa = {"step": a[0], "alter": 0, "octave": int(a[1])}
            pb = {"step": b[0], "alter": 0, "octave": int(b[1])}
            return rc.is_consonant(rc.interval_info(pa, pb), rules)

        for a, b in [("C4", "G4"), ("C4", "E4"), ("C4", "A4"), ("D4", "F4")]:
            self.assertTrue(cons(a, b), f"{a}-{b} 应协和")
        for a, b in [("C4", "F4"), ("B3", "F4"), ("C4", "D4"), ("F4", "B4")]:
            self.assertFalse(cons(a, b), f"{a}-{b} 应不协和")

    def test_strong_beats(self):
        rules = rc.build_rules()
        self.assertEqual(rc.strong_beats_for({"beats": 4, "beat_type": 4}, rules), [1, 3])
        self.assertEqual(rc.strong_beats_for({"beats": 6, "beat_type": 8}, rules), [1, 4])
        self.assertEqual(rc.strong_beats_for({"beats": 3, "beat_type": 4}, rules), [1])


class TestParser(unittest.TestCase):
    def test_good_sample_parses_clean(self):
        parsed = musicxml_io.parse_score(read_sample("good_exercise.musicxml"))
        errors = [i for i in parsed["parse_issues"] if i["severity"] == "error"]
        self.assertEqual(errors, [], json.dumps(errors, ensure_ascii=False, indent=2))
        self.assertFalse(parsed["fatal"])
        self.assertEqual(len(parsed["measures"]), 10)
        # 两小节各 4 拍（quarter 长度 4.0）
        self.assertAlmostEqual(parsed["measures"][0]["length"], 4.0)
        # 起始时间还原
        self.assertAlmostEqual(parsed["measures"][1]["start"], 4.0)

    def test_divisions_time_positions(self):
        parsed = musicxml_io.parse_score(read_sample("good_exercise.musicxml"))
        upper_events = [e for e in parsed["events"]
                        if e["role"] == "upper" and e["measure"] == 7]
        starts = [e["start"] for e in upper_events]
        # m7 内两个二分音符：0 与 2（四分音符单位）
        self.assertEqual(starts, [0.0, 2.0])

    def test_ties_detected(self):
        parsed = musicxml_io.parse_score(read_sample("good_exercise.musicxml"))
        ties = [(e["measure"], e["note_index"], e["tie_start"], e["tie_stop"])
                for e in parsed["events"] if e["role"] == "upper"
                and (e["tie_start"] or e["tie_stop"])]
        # m7 两个半音符 start/stop 成对，m8 第一个半音符继续 start
        self.assertTrue(any(t[0] == 7 and t[2] for t in ties))
        self.assertTrue(any(t[0] == 8 and t[2] for t in ties))

    def test_broken_notation_is_fatal_and_located(self):
        parsed = musicxml_io.parse_score(read_sample("broken_notation.musicxml"))
        self.assertTrue(parsed["fatal"])
        codes = {i["code"] for i in parsed["parse_issues"]}
        self.assertIn("multiple_voices_in_part", codes)
        self.assertIn("missing_duration", codes)
        for issue in parsed["parse_issues"]:
            if issue["code"] in ("multiple_voices_in_part",):
                self.assertEqual(issue["part_id"], "P1")
            if issue["code"] == "missing_duration":
                self.assertEqual(issue["part_id"], "P2")
                self.assertEqual(issue["measure"], 2)
                self.assertEqual(issue["note_index"], 1)

    def test_missing_divisions_is_error_not_guessed(self):
        xml = b"""<?xml version="1.0"?>
<score-partwise version="4.0">
  <part-list>
    <score-part id="P1"><part-name>A</part-name></score-part>
    <score-part id="P2"><part-name>B</part-name></score-part>
  </part-list>
  <part id="P1"><measure number="1">
    <attributes><time><beats>4</beats><beat-type>4</beat-type></time></attributes>
    <note><pitch><step>C</step><octave>5</octave></pitch><duration>4</duration><type>half</type></note>
    <note><pitch><step>D</step><octave>5</octave></pitch><duration>4</duration><type>half</type></note>
  </measure></part>
  <part id="P2"><measure number="1">
    <attributes><divisions>2</divisions><time><beats>4</beats><beat-type>4</beat-type></time></attributes>
    <note><pitch><step>C</step><octave>3</octave></pitch><duration>8</duration><type>whole</type></note>
  </measure></part>
</score-partwise>"""
        parsed = musicxml_io.parse_score(xml)
        self.assertTrue(parsed["fatal"])
        self.assertTrue(any(i["code"] == "missing_divisions" for i in parsed["parse_issues"]))

    def test_wrong_part_count_rejected(self):
        xml = b"""<?xml version="1.0"?>
<score-partwise version="4.0">
  <part-list>
    <score-part id="P1"><part-name>A</part-name></score-part>
    <score-part id="P2"><part-name>B</part-name></score-part>
    <score-part id="P3"><part-name>C</part-name></score-part>
  </part-list>
  <part id="P1"/>
  <part id="P2"/>
  <part id="P3"/>
</score-partwise>"""
        with self.assertRaises(musicxml_io.ParseAborted):
            musicxml_io.parse_score(xml)

    def test_dots_and_rests(self):
        xml = b"""<?xml version="1.0"?>
<score-partwise version="4.0">
  <part-list>
    <score-part id="P1"><part-name>A</part-name></score-part>
    <score-part id="P2"><part-name>B</part-name></score-part>
  </part-list>
  <part id="P1"><measure number="1">
    <attributes><divisions>2</divisions>
      <key><fifths>0</fifths></key>
      <time><beats>4</beats><beat-type>4</beat-type></time></attributes>
    <note><pitch><step>C</step><octave>5</octave></pitch><duration>3</duration><type>quarter</type><dot/></note>
    <note><pitch><step>D</step><octave>5</octave></pitch><duration>1</duration><type>eighth</type></note>
    <note><rest/><duration>4</duration><type>half</type></note>
  </measure></part>
  <part id="P2"><measure number="1">
    <attributes><divisions>2</divisions>
      <time><beats>4</beats><beat-type>4</beat-type></time></attributes>
    <note><pitch><step>C</step><octave>3</octave></pitch><duration>8</duration><type>whole</type></note>
  </measure></part>
</score-partwise>"""
        parsed = musicxml_io.parse_score(xml)
        self.assertFalse(parsed["fatal"],
                         [i["message"] for i in parsed["parse_issues"]])
        evs = [e for e in parsed["events"] if e["part_id"] == "P1"]
        self.assertEqual(evs[0]["dots"], 1)
        self.assertAlmostEqual(evs[0]["duration"], 1.5)
        self.assertTrue(evs[2]["rest"])


class TestAnalysis(unittest.TestCase):
    RULES = None

    @classmethod
    def setUpClass(cls):
        cls.RULES = rc.build_rules()

    def analyze_xml(self, xml_bytes):
        parsed = musicxml_io.parse_score(xml_bytes)
        self.assertFalse(parsed["fatal"],
                         [i["message"] for i in parsed["parse_issues"]])
        return counterpoint.analyze(parsed, self.RULES)

    def test_good_sample_no_errors(self):
        result = self.analyze_xml(read_sample("good_exercise.musicxml"))
        errors = [f for f in result["findings"] if f["severity"] == "error"]
        self.assertEqual(errors, [f["message"] for f in errors][:0] or
                         [(f["kind"], f["measure"], f["beat"], f["message"])
                          for f in errors])

    def test_bad_sample_has_expected_kinds(self):
        result = self.analyze_xml(read_sample("bad_exercise.musicxml"))
        kinds = {f["kind"] for f in result["findings"]}
        # 该示例至少触发：平行五度、隐伏八度、连续大跳、超域、重复最高音
        for expected in ("parallel_fifth", "hidden_octave", "consecutive_leaps",
                         "range_violation", "repeated_highest"):
            self.assertIn(expected, kinds,
                          f"应发现 {expected}，实际：{sorted(kinds)}")

    def test_parallel_fifths_detected_with_trace(self):
        # 两声部同向：C/G(P5) -> D/A(P5)
        xml = make_two_bar_whole(("C5", "D5"), ("F3", "G3"))
        result = self.analyze_xml(xml)
        p5 = [f for f in result["findings"] if f["kind"] == "parallel_fifth"]
        self.assertEqual(len(p5), 1)
        f = p5[0]
        self.assertEqual(f["measure"], 2)
        self.assertEqual(len(f["notes"]), 4)  # 前后两点各两个音
        self.assertEqual(f["trace"]["from"]["interval"], "P12")
        self.assertEqual(f["trace"]["to"]["interval"], "P12")

    def test_contrary_motion_to_octave_not_hidden(self):
        # 高 C5->D5 上，低 A3->D4 上... 构造反向到达：C5->B4 下行， F3->B3 上行 -> 八度 B3/B4
        xml = make_two_bar_whole(("C5", "B4"), ("F3", "B3"))
        result = self.analyze_xml(xml)
        self.assertFalse(any(f["kind"] in ("hidden_octave", "parallel_octave")
                             for f in result["findings"]))

    def test_hidden_octave_same_direction_leap(self):
        # 两音同向（上行）、高声部跳进、到达八度：
        # m1: 高 D5 低 A3 (M11? = 12+2... 用 C5/D3 = m13?) 简单点：高 E5 低 G3 = M6
        # m2: 高 A5 低 A3 = P8，高上行纯四（=5 >= hidden leap 4? yes5），低上行大二（2）
        xml = make_two_bar_whole(("E5", "A5"), ("G3", "A3"))
        result = self.analyze_xml(xml)
        self.assertTrue(any(f["kind"] == "hidden_octave"
                            for f in result["findings"]),
                        [f["message"] for f in result["findings"]])

    def test_voice_crossing(self):
        xml = make_two_bar_whole(("C5", "C5"), ("C3", "A4"))
        result = self.analyze_xml(xml)
        self.assertTrue(any(f["kind"] == "voice_crossing" and f["measure"] == 2
                            for f in result["findings"]))

    def test_strong_dissonance_bare(self):
        # m2 两音同时进入强拍且不协和：高 B4 低 F3 = A4 (tritone)
        xml = make_two_bar_whole(("C5", "B4"), ("C3", "F3"))
        result = self.analyze_xml(xml)
        strong = [f for f in result["findings"] if f["kind"] == "strong_dissonance"]
        self.assertEqual(len(strong), 1)
        self.assertIn("both_attack", strong[0]["trace"]["reason"])

    def test_valid_suspension_accepted(self):
        # 手工构造 4-3：低 F2 保持（半小节），高 C5 保持 -> 高 B4 级进下
        xml = make_suspension_case(valid=True)
        result = self.analyze_xml(xml)
        self.assertFalse(any(f["kind"] == "strong_dissonance"
                             for f in result["findings"]),
                         [f["message"] for f in result["findings"]])

    def test_invalid_suspension_leap_resolution(self):
        xml = make_suspension_case(valid=False)
        result = self.analyze_xml(xml)
        strong = [f for f in result["findings"] if f["kind"] == "strong_dissonance"]
        self.assertTrue(strong)
        self.assertIn("not_step_down", strong[0]["trace"]["reasons"])

    def test_weak_dissonance_passing_tone_ok(self):
        # 高：C5 D5 E5 F5 四分；低全音符 C3/F3...
        # beat2: D5-C3 = M9(M2) 不协和，上行级进进入，beat3 E5-C3=M10（M3）同向级进解决
        xml = make_quarter_case(lower_whole=("C3", "C3"),
                                upper_q=["C5", "D5", "E5", "F5"])
        result = self.analyze_xml(xml)
        self.assertFalse(any("weak_dissonance" in f["kind"]
                             for f in result["findings"]),
                         [f["message"] for f in result["findings"]])

    def test_weak_dissonance_leap_entry_flagged(self):
        # 高 C5 A4 ... beat2 下行小三进入不协和（A4 对 B2? ）设计：
        # 低全音符 B2；高 C5(m9 against B2 = m9? C5-B2 = m10? B2->C5 = 13半音=m9? 折算 m3 协和)
        # 直接选：低 A2；beat1 高 C5：C5-A2 = M10(M3) 协和；
        # beat2 高 F4? 太远。用高 B4：B4-A2 = M9(M2) 不协和，进入 C5->B4 大二下行（级进）会合法。
        # 构造跳进进入：beat1 高 E5 对 A2 = P12 协和；beat2 高 B4 对 A2 = M9 不协和，下行纯四跳进
        xml = make_quarter_case(lower_whole=("A2", "A2"),
                                upper_q=["E5", "B4", "C5", "D5"])
        result = self.analyze_xml(xml)
        entries = [f for f in result["findings"]
                   if f["kind"] == "weak_dissonance_entry"]
        self.assertTrue(entries, [f["message"] for f in result["findings"]])
        self.assertEqual(entries[0]["trace"]["reason"], "entered_by_leap")

    def test_range_and_leaps(self):
        result = self.analyze_xml(read_sample("bad_exercise.musicxml"))
        self.assertTrue(any(f["kind"] == "range_violation" for f in result["findings"]))
        self.assertTrue(any(f["kind"] == "leap_too_large"
                            for f in result["findings"]))

    def test_every_finding_has_location_notes_trace(self):
        result = self.analyze_xml(read_sample("bad_exercise.musicxml"))
        self.assertTrue(result["findings"])
        for f in result["findings"]:
            self.assertIn("measure", f)
            self.assertIn("beat", f)
            self.assertTrue(f["notes"])
            self.assertIsInstance(f["trace"], dict)
            for n in f["notes"]:
                self.assertIn("part_id", n)
                self.assertIn("measure", n)
                self.assertIn("note_index", n)


# ---------------------------------------------------------------------------
# 小型 MusicXML 构造助手（测试专用）
# ---------------------------------------------------------------------------

def _pitch_tokens(notes):
    return [(n, "w") for n in notes]


def make_two_bar_whole(upper, lower):
    """两小节 4/4，每声部每小节一个全音符。upper/lower 各给两个音名。"""
    from counterpoint_api.samples_helper import build_doc
    return build_doc([[(upper[0], "w")], [(upper[1], "w")]],
                     [[(lower[0], "w")], [(lower[1], "w")]])


def make_suspension_case(valid=True):
    """m2 强拍延留：高 C5 从 m1 保持到 m2 强拍，对低 F2 构成 P4。"""
    from counterpoint_api.samples_helper import build_doc
    resolve = "B4" if valid else "A4"  # A4 对 G2 是 M7（不协和）且非级进
    upper = [
        [("C5", "w")],
        [("C5", "h", {"tie": "start"}), (resolve, "h")],
    ]
    # m1 低全音符 F2? 准备点需要协和：F2-C5 = P12 协和 ✓
    # m2: F2 半小节（C5 = P4 延留），随后 G2 半小节
    lower = [
        [("F2", "w")],
        [("F2", "h"), ("G2", "h")],
    ]
    # 跨小节延音：m1 的 C5 需要 tie start，m2 第一个音 tie stop
    upper[0] = [("C5", "w", {"tie": "start"})]
    upper[1] = [("C5", "h", {"tie": "stop"}), (resolve, "h")]
    return build_doc(upper, lower)


def make_quarter_case(lower_whole, upper_q):
    from counterpoint_api.samples_helper import build_doc
    return build_doc([upper_q], [[(lower_whole[0], "w")]])


class TestStorageAndService(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")

    def tearDown(self):
        self.db.close()

    def test_full_flow_and_comparison(self):
        # 上传两份谱
        bad = service.upload_score(self.db,
                                   read_sample("bad_exercise.musicxml").decode())
        revised = service.upload_score(self.db,
                                       read_sample("bad_exercise_revised.musicxml").decode())
        a1 = service.create_analysis(self.db, {"score_id": bad["id"]})
        a2 = service.create_analysis(self.db, {"score_id": revised["id"]})
        self.assertEqual(a1["status"], "ok")
        self.assertTrue(a1["summary"]["total"] > 0)

        # 筛选
        only_p5 = service.list_findings(self.db, a1["id"], {"kind": ["parallel_fifth"]})
        self.assertTrue(only_p5)
        self.assertTrue(all(f["kind"] == "parallel_fifth" for f in only_p5))
        upper_only = service.list_findings(self.db, a1["id"], {"role": ["upper"]})
        self.assertTrue(all(any(n["part_id"] == "P1" for n in f["notes"])
                            for f in upper_only))

        # 裁定
        target = a1["findings"][0]
        v = service.record_verdict(self.db, {
            "finding_id": target["id"], "decision": "rejected",
            "comment": "此处为风格性进行", "teacher": "王老师"})
        self.assertEqual(v["decision"], "rejected")
        filtered = service.list_findings(self.db, a1["id"],
                                         {"verdict": ["rejected"]})
        self.assertEqual(len(filtered), 1)
        bad_verdict = {"finding_id": target["id"], "decision": "maybe"}
        with self.assertRaises(service.ServiceError):
            service.record_verdict(self.db, bad_verdict)

        # 比对
        cmp_ = service.compare_analyses(self.db, a1["id"], a2["id"])
        self.assertEqual(cmp_["counts"]["base_total"], a1["summary"]["total"])
        self.assertEqual(cmp_["counts"]["revised_total"], a2["summary"]["total"])
        self.assertGreaterEqual(cmp_["counts"]["removed"], 1)
        self.assertEqual(
            cmp_["counts"]["added"] + cmp_["counts"]["unchanged"],
            cmp_["counts"]["revised_total"])

        # 导出
        export = service.analysis_export(self.db, a1["id"])
        self.assertIn("rules_version", export)
        self.assertEqual(len(export["verdicts"]), 1)

    def test_broken_score_analysis_is_parse_error(self):
        up = service.upload_score(self.db,
                                  read_sample("broken_notation.musicxml").decode())
        analysis = service.create_analysis(self.db, {"score_id": up["id"]})
        self.assertEqual(analysis["status"], "parse_error")
        self.assertTrue(any(i["severity"] == "error"
                            for i in analysis["parse_issues"]))

    def test_rule_copy_creates_version(self):
        rid = service.ensure_default_rule_set(self.db)
        new_id = self.db.copy_rule_set(rid, "宽松规则",
                                       {"melody": {"max_leap_semitones": 24}})
        row = self.db.get_rule_set(new_id)
        rules = json.loads(row["rules_json"])
        self.assertEqual(rules["melody"]["max_leap_semitones"], 24)
        # 其它值沿用默认
        self.assertEqual(rules["beat"]["hidden_leap_min_semitones"], 4)

    def test_invalid_rules_rejected(self):
        with self.assertRaises(service.ServiceError):
            service.create_rule_set(self.db, {
                "name": "坏规则",
                "overrides": {"intervals": {"consonant": ["ZZ9"]}}})


if __name__ == "__main__":
    unittest.main(verbosity=2)
