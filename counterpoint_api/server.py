# -*- coding: utf-8 -*-
"""http.server 路由层。

启动::

    python -m counterpoint_api.server --db counterpoint.db --port 8000

所有接口离线运行，请求/响应均为 JSON（上传原谱接口支持直接发
``application/xml`` 文本或 ``multipart/form-data`` 文件）。
根路径 ``GET /`` 返回接口导航页。
"""

from __future__ import annotations

import argparse
import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from . import service
from .storage import Database

SAMPLE_DIR = os.path.join(os.path.dirname(__file__), "samples")
DOC_PATH = os.path.join(os.path.dirname(__file__), "API.md")


class AppState:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def db(self) -> Database:
        # 每请求一个连接，避免跨线程共享 sqlite 连接
        return Database(self.db_path)


def make_handler(state: AppState) -> type:

    class Handler(BaseHTTPRequestHandler):
        server_version = "CounterpointAPI/1.0"

        # --------------------------------------------------------------
        # 基础工具
        # --------------------------------------------------------------
        def _send_json(self, obj: Any, status: int = 200,
                       filename: Optional[str] = None) -> None:
            body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            ctype = "application/json; charset=utf-8"
            if filename:
                ctype = "application/octet-stream"
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{filename}"')
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, html: str, status: int = 200) -> None:
            body = html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_text(self, text: str, status: int = 200,
                       ctype: str = "text/plain; charset=utf-8") -> None:
            body = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length", 0))
            return self.rfile.read(length) if length else b""

        def _read_json(self) -> Dict[str, Any]:
            raw = self._read_body()
            if not raw:
                return {}
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise service.ServiceError(400, f"请求体不是合法 JSON：{exc}")
            if not isinstance(data, dict):
                raise service.ServiceError(400, "请求 JSON 必须是对象")
            return data

        def _read_score_xml(self) -> Tuple[str, Optional[str]]:
            ctype = self.headers.get("Content-Type", "")
            if ctype.startswith("application/json"):
                data = self._read_json()
                xml = data.get("musicxml") or data.get("xml")
                if not xml:
                    raise service.ServiceError(400, "JSON 中缺少 musicxml 字段")
                return xml, data.get("filename")
            if ctype.startswith("multipart/form-data"):
                return self._parse_multipart(ctype)
            # 其余情况按原始 XML 文本处理
            raw = self._read_body()
            try:
                return raw.decode("utf-8"), None
            except UnicodeDecodeError as exc:
                raise service.ServiceError(400, f"上传内容不是 UTF-8 文本：{exc}")

        def _parse_multipart(self, ctype: str) -> Tuple[str, Optional[str]]:
            # 极小的 multipart 解析（避免引入 cgi 模块依赖）
            boundary = None
            for part in ctype.split(";"):
                part = part.strip()
                if part.startswith("boundary="):
                    boundary = part.split("=", 1)[1].strip('"')
            if not boundary:
                raise service.ServiceError(400, "multipart 请求缺少 boundary")
            raw = self._read_body()
            delim = b"--" + boundary.encode()
            blocks = raw.split(delim)
            for block in blocks:
                if b"Content-Disposition" not in block:
                    continue
                head, _, payload = block.partition(b"\r\n\r\n")
                filename = None
                for token in head.decode("utf-8", "replace").split(";"):
                    token = token.strip()
                    if token.startswith("filename="):
                        filename = token.split("=", 1)[1].strip('"')
                payload = payload.rstrip(b"\r\n")
                if payload:
                    return payload.decode("utf-8"), filename
            raise service.ServiceError(400, "multipart 中未找到文件内容")

        def _error(self, exc: service.ServiceError) -> None:
            self._send_json({"error": exc.message, "details": exc.details},
                            status=exc.status)

        def log_message(self, fmt: str, *args: Any) -> None:
            # 简洁日志
            import sys
            sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

        # --------------------------------------------------------------
        # 路由
        # --------------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def do_PUT(self) -> None:  # noqa: N802
            self._dispatch("PUT")

        def do_DELETE(self) -> None:  # noqa: N802
            self._dispatch("DELETE")

        def _dispatch(self, method: str) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query, keep_blank_values=True)
            try:
                with state.db() as db:
                    handler = ROUTES.get((method, path))
                    # 资源型路径（带 id）
                    if handler is None:
                        handler, kwargs = _match_resource(method, path)
                    else:
                        kwargs = {}
                    if handler is None:
                        if method == "GET" and path.startswith("/samples/"):
                            self._serve_sample(path.split("/", 2)[2])
                            return
                        self._send_json({"error": f"无此接口：{method} {path}"}, 404)
                        return
                    handler(self, db, query, **kwargs)
            except service.ServiceError as exc:
                self._error(exc)
            except Exception as exc:  # 最后防线
                import traceback
                traceback.print_exc()
                self._send_json({"error": f"服务器内部错误：{exc}"}, 500)

        def _serve_sample(self, name: str) -> None:
            # 防目录穿越
            safe = os.path.basename(name)
            full = os.path.join(SAMPLE_DIR, safe)
            if not os.path.isfile(full):
                self._send_json({"error": f"示例文件不存在：{safe}"}, 404)
                return
            with open(full, "rb") as fh:
                body = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.recordare.musicxml+xml; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


# ---------------------------------------------------------------------------
# 资源路径匹配：/api/xxx/<id>
# ---------------------------------------------------------------------------

def _match_resource(method: str, path: str):
    parts = [p for p in path.split("/") if p]
    if len(parts) == 3 and parts[0] == "api":
        try:
            rid = int(parts[2])
        except ValueError:
            return None, {}
        key = (method, f"/api/{parts[1]}/<id>")
        handler = RESOURCE_ROUTES.get(key)
        if handler:
            return handler, {"rid": rid}
    if len(parts) == 4 and parts[0] == "api":
        try:
            rid = int(parts[2])
        except ValueError:
            return None, {}
        key = (method, f"/api/{parts[1]}/<id>/{parts[3]}")
        handler = RESOURCE_ROUTES.get(key)
        if handler:
            return handler, {"rid": rid}
    return None, {}


# ---------------------------------------------------------------------------
# 接口处理函数
# ---------------------------------------------------------------------------

def h_index(h, db, query):
    rules = service.ensure_default_rule_set(db)
    html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>离线对位作业审阅 API</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem;line-height:1.6}}
code{{background:#f4f4f4;padding:.1rem .3rem;border-radius:3px}}
table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ccc;padding:.35rem .6rem;text-align:left}}
</style></head><body>
<h1>离线两声部对位作业审阅 API</h1>
<p>纯 Python 标准库实现（<code>http.server</code> / <code>sqlite3</code> /
<code>xml.etree.ElementTree</code>）。完整文档见
<a href="/docs">/docs</a>，默认规则集 id=<code>{rules}</code>。</p>
<h2>主要接口</h2>
<table>
<tr><th>方法</th><th>路径</th><th>用途</th></tr>
<tr><td>POST</td><td><code>/api/scores</code></td><td>上传 MusicXML 原谱</td></tr>
<tr><td>GET</td><td><code>/api/scores</code></td><td>列出原谱</td></tr>
<tr><td>POST</td><td><code>/api/analyses</code></td><td>创建分析</td></tr>
<tr><td>GET</td><td><code>/api/analyses/&lt;id&gt;</code></td><td>分析详情（含发现）</td></tr>
<tr><td>GET</td><td><code>/api/analyses/&lt;id&gt;/findings</code></td><td>筛选发现</td></tr>
<tr><td>PUT</td><td><code>/api/verdicts</code></td><td>记录教师裁定</td></tr>
<tr><td>POST</td><td><code>/api/comparisons</code></td><td>比较两版问题增减</td></tr>
<tr><td>GET</td><td><code>/api/analyses/&lt;id&gt;/download</code></td><td>下载分析 JSON</td></tr>
<tr><td>GET</td><td><code>/api/rules</code></td><td>规则集列表</td></tr>
</table>
<h2>示例谱</h2>
<ul>
<li><a href="/samples/good_exercise.musicxml">good_exercise.musicxml</a> 干净作业（含跨小节延音与延留音）</li>
<li><a href="/samples/bad_exercise.musicxml">bad_exercise.musicxml</a> 含多类问题的作业</li>
<li><a href="/samples/bad_exercise_revised.musicxml">bad_exercise_revised.musicxml</a> 上一谱的修订版</li>
<li><a href="/samples/broken_notation.musicxml">broken_notation.musicxml</a> 无法分析的记谱（多声部混写/时值缺失）</li>
</ul>
<h2>五类对位示例（定旋律在低声部，2/2 拍）</h2>
<ul>
<li><a href="/samples/species1.musicxml">species1.musicxml</a> 第一类：一音对一音</li>
<li><a href="/samples/species2.musicxml">species2.musicxml</a> 第二类：二音对一音（首小节半休止）</li>
<li><a href="/samples/species3.musicxml">species3.musicxml</a> 第三类：四音对一音</li>
<li><a href="/samples/species4.musicxml">species4.musicxml</a> 第四类：切分延留</li>
<li><a href="/samples/species5.musicxml">species5.musicxml</a> 第五类：混合节奏</li>
</ul>
<p>创建分析时指定 <code>{{"species": 1, "cantus": "lower"}}</code>
即按类别校验；类别非法、定旋律声部不存在会返回 400，谱面节奏无法满足
类别时逐小节/音符报出 <code>species_*</code> 发现，系统不擅自改类。</p>
</body></html>"""
    h._send_html(html)


def h_docs(h, db, query):
    if not os.path.isfile(DOC_PATH):
        h._send_text("文档缺失", status=404)
        return
    with open(DOC_PATH, encoding="utf-8") as fh:
        h._send_text(fh.read(), ctype="text/markdown; charset=utf-8")


def h_health(h, db, query):
    h._send_json({"status": "ok", "offline": True})


# ---------- 规则 ----------

def h_list_rules(h, db, query):
    rows = db.list_rule_sets()
    h._send_json({"rule_sets": [service.rule_set_dict(r) for r in rows]})


def h_create_rule(h, db, query):
    payload = h._read_json()
    h._send_json(service.create_rule_set(db, payload), status=201)


def h_get_rule(h, db, query, rid):
    row = db.get_rule_set(rid)
    if row is None:
        raise service.ServiceError(404, f"规则集 {rid} 不存在")
    h._send_json(service.rule_set_dict(row))


def h_copy_rule(h, db, query, rid):
    payload = h._read_json()
    new_name = payload.get("name") or f"规则集 {rid} 副本"
    new_id = db.copy_rule_set(rid, new_name, payload.get("overrides"))
    h._send_json(service.rule_set_dict(db.get_rule_set(new_id)), status=201)


# ---------- 原谱 ----------

def h_upload_score(h, db, query):
    xml, filename = h._read_score_xml()
    result = service.upload_score(db, xml, filename=filename)
    h._send_json(result, status=201)


def h_list_scores(h, db, query):
    rows = db.list_scores()
    h._send_json({"scores": [service.score_dict(r) for r in rows]})


def h_get_score(h, db, query, rid):
    row = db.get_score(rid)
    if row is None:
        raise service.ServiceError(404, f"原谱 {rid} 不存在")
    include = query.get("xml", ["0"])[0] in ("1", "true", "yes")
    h._send_json(service.score_dict(row, include_xml=include))


def h_download_score(h, db, query, rid):
    row = db.get_score(rid)
    if row is None:
        raise service.ServiceError(404, f"原谱 {rid} 不存在")
    safe_name = (row["title"] or f"score-{rid}").replace(" ", "_")
    h._send_text(row["xml"],
                 ctype="application/vnd.recordare.musicxml+xml; charset=utf-8")


# ---------- 分析 ----------

def h_create_analysis(h, db, query):
    payload = h._read_json()
    result = service.create_analysis(db, payload)
    h._send_json(result, status=201)


def h_list_analyses(h, db, query):
    score_id = query.get("score_id", [None])[0]
    rows = db.list_analyses(int(score_id) if score_id else None)
    out = []
    for r in rows:
        out.append({
            "id": r["id"], "score_id": r["score_id"],
            "rule_set_id": r["rule_set_id"], "status": r["status"],
            "species": r["species"], "cantus_part": r["cantus_part"],
            "summary": json.loads(r["summary_json"]),
            "created_at": r["created_at"],
        })
    h._send_json({"analyses": out})


def h_get_analysis(h, db, query, rid):
    h._send_json(service.analysis_dict(db, rid, include_findings=True))


def h_findings(h, db, query, rid):
    findings = service.list_findings(db, rid, query)
    meta = service.analysis_dict(db, rid)
    h._send_json({"analysis_id": rid,
                  "species": meta["species"],
                  "species_name": meta["species_name"],
                  "cantus_part": meta["cantus_part"],
                  "cantus_role": meta["cantus_role"],
                  "rule_version": meta["rule_version"],
                  "count": len(findings),
                  "findings": findings})


def h_download_analysis(h, db, query, rid):
    data = service.analysis_export(db, rid)
    fname = f"analysis-{rid}.json"
    h._send_json(data, filename=fname)


# ---------- 裁定 ----------

def h_put_verdict(h, db, query):
    payload = h._read_json()
    h._send_json(service.record_verdict(db, payload), status=201)


def h_list_verdicts(h, db, query, rid):
    if db.get_analysis(rid) is None:
        raise service.ServiceError(404, f"分析 {rid} 不存在")
    rows = db.all_verdicts(rid)
    h._send_json({"analysis_id": rid,
                  "verdicts": [dict(r) for r in rows]})


# ---------- 比对 ----------

def h_compare(h, db, query):
    payload = h._read_json()
    base = payload.get("base_analysis_id")
    revised = payload.get("revised_analysis_id")
    if base is None or revised is None:
        raise service.ServiceError(400, "缺少 base_analysis_id / revised_analysis_id")
    persist = payload.get("persist", True)
    h._send_json(service.compare_analyses(db, int(base), int(revised),
                                          persist=persist), status=201)


def h_get_comparison(h, db, query, rid):
    row = db.get_comparison(rid)
    if row is None:
        raise service.ServiceError(404, f"比对 {rid} 不存在")
    h._send_json(service.comparison_dict(row))


ROUTES = {
    ("GET", "/"): h_index,
    ("GET", "/docs"): h_docs,
    ("GET", "/api/health"): h_health,
    ("GET", "/api/rules"): h_list_rules,
    ("POST", "/api/rules"): h_create_rule,
    ("POST", "/api/rules/copy"): None,  # 走资源路由
    ("GET", "/api/scores"): h_list_scores,
    ("POST", "/api/scores"): h_upload_score,
    ("GET", "/api/analyses"): h_list_analyses,
    ("POST", "/api/analyses"): h_create_analysis,
    ("PUT", "/api/verdicts"): h_put_verdict,
    ("POST", "/api/comparisons"): h_compare,
}

RESOURCE_ROUTES = {
    ("GET", "/api/rules/<id>"): h_get_rule,
    ("POST", "/api/rules/<id>/copy"): h_copy_rule,
    ("GET", "/api/scores/<id>"): h_get_score,
    ("GET", "/api/scores/<id>/download"): h_download_score,
    ("GET", "/api/analyses/<id>"): h_get_analysis,
    ("GET", "/api/analyses/<id>/findings"): h_findings,
    ("GET", "/api/analyses/<id>/download"): h_download_analysis,
    ("GET", "/api/analyses/<id>/verdicts"): h_list_verdicts,
    ("GET", "/api/comparisons/<id>"): h_get_comparison,
}
ROUTES.pop(("POST", "/api/rules/copy"), None)


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="离线两声部对位作业审阅 API")
    parser.add_argument("--db", default=os.environ.get("COUNTERPOINT_DB", "counterpoint.db"),
                        help="SQLite 文件路径（默认 counterpoint.db）")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("COUNTERPOINT_PORT", "8000")))
    parser.add_argument("--host", default=os.environ.get("COUNTERPOINT_HOST", "127.0.0.1"))
    args = parser.parse_args(argv)

    state = AppState(args.db)
    # 启动即初始化库与默认规则集
    with state.db() as db:
        rid = service.ensure_default_rule_set(db)
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(state))
    print(f"对位审阅 API 已启动：http://{args.host}:{args.port}/  （数据库 {args.db}，默认规则集 #{rid}）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
