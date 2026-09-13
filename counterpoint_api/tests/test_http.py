# -*- coding: utf-8 -*-
"""HTTP 端到端测试：在随机端口启动真实 http.server，走完整请求链路。"""

import json
import os
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from counterpoint_api import service
from counterpoint_api.server import AppState, make_handler
from counterpoint_api.storage import Database

SAMPLE_DIR = os.path.join(os.path.dirname(__file__), "..", "samples")


def read_sample(name):
    with open(os.path.join(SAMPLE_DIR, name), "rb") as fh:
        return fh.read()


class ServerHarness:
    def __init__(self):
        Database.reset_shared_memory()
        self.state = AppState(":memory:")
        # 预建默认规则集
        with self.state.db() as db:
            service.ensure_default_rule_set(db)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.state))
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def request(self, path, method="GET", data=None, ctype="application/json",
                raw=None):
        body = None
        headers = {}
        if raw is not None:
            body = raw
            headers["Content-Type"] = ctype
        elif data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = ctype
        req = urllib.request.Request(self.url(path), data=body, method=method,
                                     headers=headers)
        try:
            with urllib.request.urlopen(req) as resp:
                payload = resp.read()
                return resp.status, resp.headers.get("Content-Type"), payload
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers.get("Content-Type"), exc.read()


class TestHTTPApi(unittest.TestCase):
    def test_docs_is_served(self):
        with ServerHarness() as srv:
            status, ctype, body = srv.request("/docs")
            self.assertEqual(status, 200)
            self.assertIn("text/markdown", ctype)
            self.assertIn("对位作业审阅", body.decode())

    def test_index_and_health(self):
        with ServerHarness() as srv:
            status, _, body = srv.request("/api/health")
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)["offline"])
            status, ctype, _ = srv.request("/")
            self.assertEqual(status, 200)
            self.assertIn("text/html", ctype)

    def test_full_workflow_over_http(self):
        with ServerHarness() as srv:
            # 上传干净的第一类作业（原始 XML 文本）
            status, _, body = srv.request(
                "/api/scores", method="POST",
                raw=read_sample("species1.musicxml"),
                ctype="application/xml")
            self.assertEqual(status, 201, body)
            good_id = json.loads(body)["id"]
            self.assertFalse(json.loads(body)["fatal"])

            # 不带 species/cantus 创建分析：400
            status, _, body = srv.request("/api/analyses", "POST",
                                          {"score_id": good_id})
            self.assertEqual(status, 400)
            self.assertIn("species", json.loads(body)["error"])

            # 指定第一类 + 定旋律在低声部，应为 0 发现
            status, _, body = srv.request("/api/analyses", "POST",
                                          {"score_id": good_id, "species": 1,
                                           "cantus": "lower"})
            self.assertEqual(status, 201, body)
            good_analysis = json.loads(body)
            self.assertEqual(good_analysis["status"], "ok")
            self.assertEqual(good_analysis["summary"]["total"], 0)
            self.assertEqual(good_analysis["species"], 1)
            self.assertEqual(good_analysis["cantus_part"], "P2")
            self.assertEqual(good_analysis["cantus_role"], "lower")
            self.assertTrue(good_analysis["rule_version"])

            # 问题谱与修订版（自由对位谱，按第一类校验）
            bad_id = json.loads(srv.request(
                "/api/scores", "POST", raw=read_sample("bad_exercise.musicxml"),
                ctype="application/xml")[2])["id"]
            rev_id = json.loads(srv.request(
                "/api/scores", "POST",
                raw=read_sample("bad_exercise_revised.musicxml"),
                ctype="application/xml")[2])["id"]
            bad_a = json.loads(srv.request(
                "/api/analyses", "POST", {"score_id": bad_id, "species": 1,
                                          "cantus": "lower"})[2])
            rev_a = json.loads(srv.request(
                "/api/analyses", "POST", {"score_id": rev_id, "species": 1,
                                          "cantus": "lower"})[2])
            self.assertGreater(bad_a["summary"]["total"], 0)

            # 筛选 error
            status, _, body = srv.request(
                f"/api/analyses/{bad_a['id']}/findings?severity=error")
            payload = json.loads(body)
            findings = payload["findings"]
            self.assertTrue(findings)
            self.assertTrue(all(f["severity"] == "error" for f in findings))
            # 发现筛选响应保留类别、定旋律与规则版本
            self.assertEqual(payload["species"], 1)
            self.assertEqual(payload["cantus_part"], "P2")
            self.assertTrue(payload["rule_version"])

            # 筛选上声部
            status, _, body = srv.request(
                f"/api/analyses/{bad_a['id']}/findings?role=upper")
            for f in json.loads(body)["findings"]:
                self.assertTrue(any(n["part_id"] == "P1" for n in f["notes"]))

            # 每条发现都带音级、纵向音程、节奏比例
            for f in findings:
                self.assertIn("scale_degrees", f["trace"])
                self.assertIn("vertical_interval", f["trace"])
                self.assertIn("rhythm_ratio", f["trace"])

            # 裁定
            fid = findings[0]["id"]
            status, _, body = srv.request("/api/verdicts", "PUT", {
                "finding_id": fid, "decision": "confirmed", "teacher": "张老师"})
            self.assertEqual(status, 201)
            status, _, body = srv.request(
                f"/api/analyses/{bad_a['id']}/findings?verdict=confirmed")
            self.assertEqual(len(json.loads(body)["findings"]), 1)

            # 非法裁定
            status, _, body = srv.request("/api/verdicts", "PUT", {
                "finding_id": fid, "decision": "nonsense"})
            self.assertEqual(status, 400)

            # 比较
            status, _, body = srv.request("/api/comparisons", "POST", {
                "base_analysis_id": bad_a["id"],
                "revised_analysis_id": rev_a["id"]})
            self.assertEqual(status, 201)
            cmp_ = json.loads(body)
            self.assertIn("added", cmp_)
            self.assertIn("removed", cmp_)
            self.assertEqual(
                cmp_["counts"]["added"] + cmp_["counts"]["unchanged"],
                cmp_["counts"]["revised_total"])
            # 比较结果保留两版的类别、定旋律与规则版本
            self.assertEqual(cmp_["context"]["base"]["species"], 1)
            self.assertEqual(cmp_["context"]["base"]["cantus_part"], "P2")
            self.assertTrue(cmp_["context"]["base"]["rule_version"])
            self.assertEqual(cmp_["context"]["revised"]["species"], 1)

            # 下载
            status, ctype, body = srv.request(
                f"/api/analyses/{bad_a['id']}/download")
            self.assertEqual(status, 200)
            self.assertEqual(ctype, "application/octet-stream")
            exported = json.loads(body)
            self.assertIn("rules_version", exported)
            self.assertEqual(len(exported["verdicts"]), 1)
            self.assertEqual(exported["analysis"]["species"], 1)
            self.assertEqual(exported["analysis"]["cantus_part"], "P2")

    def test_species_params_validated(self):
        with ServerHarness() as srv:
            score_id = json.loads(srv.request(
                "/api/scores", "POST", raw=read_sample("species2.musicxml"),
                ctype="application/xml")[2])["id"]
            # 缺 cantus
            status, _, body = srv.request("/api/analyses", "POST",
                                          {"score_id": score_id, "species": 2})
            self.assertEqual(status, 400)
            # 类别非法
            for bad_species in (0, 6, "二"):
                status, _, body = srv.request(
                    "/api/analyses", "POST",
                    {"score_id": score_id, "species": bad_species,
                     "cantus": "lower"})
                self.assertEqual(status, 400, f"species={bad_species!r} 应 400")
            # 定旋律声部不存在
            status, _, body = srv.request(
                "/api/analyses", "POST",
                {"score_id": score_id, "species": 2, "cantus": "P9"})
            self.assertEqual(status, 400)
            self.assertIn("定旋律声部不存在", json.loads(body)["error"])
            # cantus 也接受 part_id
            status, _, body = srv.request(
                "/api/analyses", "POST",
                {"score_id": score_id, "species": 2, "cantus": "P2"})
            self.assertEqual(status, 201, body)
            analysis = json.loads(body)
            self.assertEqual(analysis["cantus_role"], "lower")
            self.assertEqual(analysis["summary"]["total"], 0)

    def test_broken_score_returns_parse_error(self):
        with ServerHarness() as srv:
            score_id = json.loads(srv.request(
                "/api/scores", "POST",
                raw=read_sample("broken_notation.musicxml"),
                ctype="application/xml")[2])["id"]
            status, _, body = srv.request("/api/analyses", "POST",
                                          {"score_id": score_id, "species": 1,
                                           "cantus": "lower"})
            analysis = json.loads(body)
            self.assertEqual(analysis["status"], "parse_error")
            codes = {i["code"] for i in analysis["parse_issues"]}
            self.assertIn("multiple_voices_in_part", codes)
            self.assertIn("missing_duration", codes)
            # 定位信息：缺 duration 的音符在 P2 第 2 小节第 1 个音符
            md = next(i for i in analysis["parse_issues"]
                      if i["code"] == "missing_duration")
            self.assertEqual(md["part_id"], "P2")
            self.assertEqual(md["measure"], 2)
            self.assertEqual(md["note_index"], 1)
            # 多 voice 问题消息含具体小节与音符
            mv = next(i for i in analysis["parse_issues"]
                      if i["code"] == "multiple_voices_in_part")
            self.assertIn("第1小节第1个音符", mv["message"])

    def test_rule_copy_endpoint(self):
        with ServerHarness() as srv:
            rules = json.loads(srv.request("/api/rules")[2])["rule_sets"]
            rid = rules[0]["id"]
            status, _, body = srv.request(f"/api/rules/{rid}/copy", "POST", {
                "name": "考试规则",
                "overrides": {"intervals": {"consonant": ["P1", "P5", "P8"]}}})
            self.assertEqual(status, 201, body)
            created = json.loads(body)
            self.assertEqual(created["rules"]["intervals"]["consonant"],
                             ["P1", "P5", "P8"])
            self.assertFalse(created["built_in"])

    def test_unknown_route_404(self):
        with ServerHarness() as srv:
            status, _, _ = srv.request("/api/nope")
            self.assertEqual(status, 404)

    def test_revision_chain_over_http(self):
        with ServerHarness() as srv:
            # 上传三轮谱面并分别建分析
            analysis_ids = []
            for name in ("bad_exercise.musicxml", "bad_exercise_revised.musicxml",
                         "bad_exercise_revised2.musicxml"):
                score_id = json.loads(srv.request(
                    "/api/scores", "POST", raw=read_sample(name),
                    ctype="application/xml")[2])["id"]
                status, _, body = srv.request("/api/analyses", "POST", {
                    "score_id": score_id, "species": 1, "cantus": "lower"})
                self.assertEqual(status, 201, body)
                analysis_ids.append(json.loads(body)["id"])

            # 建链
            status, _, body = srv.request("/api/chains", "POST", {
                "analysis_id": analysis_ids[0], "title": "HTTP 链"})
            self.assertEqual(status, 201, body)
            chain = json.loads(body)
            self.assertEqual(chain["revision_count"], 1)
            self.assertEqual(chain["species"], 1)
            self.assertEqual(chain["cantus_part"], "P2")
            self.assertTrue(chain["rule_version"])

            # 追加第二轮、第三轮
            for aid in analysis_ids[1:]:
                status, _, body = srv.request(
                    f"/api/chains/{chain['id']}/revisions", "POST",
                    {"analysis_id": aid})
                self.assertEqual(status, 201, body)
            third = json.loads(body)
            summary = third["tracking_summary"]
            self.assertEqual(summary["carried"], 16)
            self.assertEqual(summary["resolved"], 3)
            self.assertEqual(summary["new"], 1)
            self.assertEqual(summary["ambiguous"], 1)

            # 追加参数不一致的分析：species 不同 → 400
            score_id = json.loads(srv.request(
                "/api/scores", "POST",
                raw=read_sample("bad_exercise_revised.musicxml"),
                ctype="application/xml")[2])["id"]
            bad_a = json.loads(srv.request("/api/analyses", "POST", {
                "score_id": score_id, "species": 3, "cantus": "lower"})[2])
            status, _, body = srv.request(
                f"/api/chains/{chain['id']}/revisions", "POST",
                {"analysis_id": bad_a["id"]})
            self.assertEqual(status, 400)

            # 时间线：三轮，逐轮依据
            status, _, body = srv.request(f"/api/chains/{chain['id']}/timeline")
            self.assertEqual(status, 200)
            timeline = json.loads(body)
            self.assertEqual(len(timeline["rounds"]), 3)
            self.assertEqual(timeline["rounds"][0]["traces"][0]["status"],
                             "initial")
            for rnd in timeline["rounds"][1:]:
                for t in rnd["traces"]:
                    self.assertIn("rule", t["evidence"])

            # 待复核：默认 pending，含 new 与 resolved
            status, _, body = srv.request(f"/api/chains/{chain['id']}/reviews")
            reviews = json.loads(body)["reviews"]
            statuses = {t["status"] for t in reviews}
            self.assertIn("new", statuses)
            self.assertIn("resolved", statuses)
            self.assertTrue(all(t["review_state"] == "pending"
                                for t in reviews))

            # 提交复核：对一条 new 记录裁定
            new_trace = next(t for t in reviews if t["status"] == "new")
            status, _, body = srv.request(
                f"/api/chains/{chain['id']}/reviews", "POST", {
                    "trace_id": new_trace["id"], "decision": "confirmed",
                    "teacher": "李老师"})
            self.assertEqual(status, 201, body)
            reviewed = json.loads(body)
            self.assertEqual(reviewed["review_state"], "reviewed")
            self.assertEqual(reviewed["review_decision"], "confirmed")
            self.assertEqual(reviewed["verdict_recorded"]["decision"],
                             "confirmed")
            # 待复核计数减少
            after = json.loads(srv.request(
                f"/api/chains/{chain['id']}/reviews")[2])
            self.assertEqual(after["count"], len(reviews) - 1)

            # 链间比较
            a1b = json.loads(srv.request("/api/analyses", "POST", {
                "score_id": json.loads(srv.request(
                    f"/api/analyses/{analysis_ids[1]}")[2])["score_id"],
                "species": 1, "cantus": "lower"})[2])
            chain2 = json.loads(srv.request("/api/chains", "POST", {
                "analysis_id": a1b["id"]})[2])
            status, _, body = srv.request("/api/chains/compare", "POST", {
                "chain_id_a": chain["id"], "chain_id_b": chain2["id"]})
            self.assertEqual(status, 200, body)
            cmp_ = json.loads(body)
            self.assertTrue(cmp_["compatible"])
            self.assertIn("latest_comparison", cmp_)

            # 下载链 JSON
            status, ctype, body = srv.request(
                f"/api/chains/{chain['id']}/download")
            self.assertEqual(status, 200)
            self.assertEqual(ctype, "application/octet-stream")
            exported = json.loads(body)
            self.assertEqual(len(exported["rounds"]), 3)
            self.assertIn("note_map", exported["rounds"][1]["alignment"])
            # 导出与时间线的复核状态一致
            exp_states = {t["id"]: t["review_state"]
                          for r in exported["rounds"] for t in r["traces"]}
            tl_states = {t["id"]: t["review_state"]
                         for r in timeline["rounds"] for t in r["traces"]}
            for tid, state in exp_states.items():
                if tid in tl_states and state != tl_states[tid]:
                    # 时间线请求早于复核提交，仅允许 pending→reviewed 的方向
                    self.assertEqual(tl_states[tid], "pending")
                    self.assertEqual(state, "reviewed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
