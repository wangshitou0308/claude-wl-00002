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
            # 上传干净谱（原始 XML 文本）
            status, _, body = srv.request(
                "/api/scores", method="POST",
                raw=read_sample("good_exercise.musicxml"),
                ctype="application/xml")
            self.assertEqual(status, 201, body)
            good_id = json.loads(body)["id"]
            self.assertFalse(json.loads(body)["fatal"])

            # 分析，应为 0 发现
            status, _, body = srv.request("/api/analyses", "POST",
                                          {"score_id": good_id})
            self.assertEqual(status, 201)
            good_analysis = json.loads(body)
            self.assertEqual(good_analysis["status"], "ok")
            self.assertEqual(good_analysis["summary"]["total"], 0)

            # 问题谱与修订版
            bad_id = json.loads(srv.request(
                "/api/scores", "POST", raw=read_sample("bad_exercise.musicxml"),
                ctype="application/xml")[2])["id"]
            rev_id = json.loads(srv.request(
                "/api/scores", "POST",
                raw=read_sample("bad_exercise_revised.musicxml"),
                ctype="application/xml")[2])["id"]
            bad_a = json.loads(srv.request(
                "/api/analyses", "POST", {"score_id": bad_id})[2])
            rev_a = json.loads(srv.request(
                "/api/analyses", "POST", {"score_id": rev_id})[2])
            self.assertGreater(bad_a["summary"]["total"], 0)

            # 筛选 error
            status, _, body = srv.request(
                f"/api/analyses/{bad_a['id']}/findings?severity=error")
            findings = json.loads(body)["findings"]
            self.assertTrue(findings)
            self.assertTrue(all(f["severity"] == "error" for f in findings))

            # 筛选上声部
            status, _, body = srv.request(
                f"/api/analyses/{bad_a['id']}/findings?role=upper")
            for f in json.loads(body)["findings"]:
                self.assertTrue(any(n["part_id"] == "P1" for n in f["notes"]))

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

            # 下载
            status, ctype, body = srv.request(
                f"/api/analyses/{bad_a['id']}/download")
            self.assertEqual(status, 200)
            self.assertEqual(ctype, "application/octet-stream")
            exported = json.loads(body)
            self.assertIn("rules_version", exported)
            self.assertEqual(len(exported["verdicts"]), 1)

    def test_broken_score_returns_parse_error(self):
        with ServerHarness() as srv:
            score_id = json.loads(srv.request(
                "/api/scores", "POST",
                raw=read_sample("broken_notation.musicxml"),
                ctype="application/xml")[2])["id"]
            status, _, body = srv.request("/api/analyses", "POST",
                                          {"score_id": score_id})
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
