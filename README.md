# 离线两声部对位作业审阅 API

纯 Python 标准库（`http.server` / `sqlite3` / `xml.etree.ElementTree`）实现的
离线 REST API，帮助作曲学生与教师审阅两声部 MusicXML 对位作业。

## 快速开始

```bash
# 启动（默认 127.0.0.1:8000，数据库 counterpoint.db）
python -m counterpoint_api.server --port 8000

# 运行测试
python -m unittest discover counterpoint_api -v

# 重新生成示例谱
python counterpoint_api/samples/make_samples.py
```

打开 <http://127.0.0.1:8000/> 看接口导航，<http://127.0.0.1:8000/docs>
看完整 API 文档（同目录 [`counterpoint_api/API.md`](counterpoint_api/API.md)）。

## 模块结构

| 模块 | 职责 |
| --- | --- |
| `musicxml_io.py` | MusicXML 解析：拍号/调号/声部/音高/休止/附点/跨小节延音，按 divisions 还原时间；**只还原不补全**，异常定位到声部+小节+音符 |
| `rulesconfig.py` | 规则配置（强弱拍、协和音程、音域、最大跳进、延留音/经过音策略）与音程计算、规则版本指纹 |
| `counterpoint.py` | 分析引擎：交叉、平行/隐伏五八度、强弱拍不协和、连续大跳、重复最高音等，每条发现带相关音符、拍位与判定轨迹 |
| `storage.py` | SQLite 持久层：原谱、规则版本、分析、发现、追加式裁定、比对结果 |
| `service.py` | 编排：上传、建分析、筛选、裁定、两版比对、JSON 导出 |
| `server.py` | `http.server` 路由与请求/响应 |
| `samples/` | 4 份示例谱 + 生成脚本 |
| `tests/` | 核心逻辑测试（28）与 HTTP 端到端测试（6） |

## 示例谱

- `good_exercise.musicxml`：干净作业（圣咏式 1:1，含跨小节延音与合法
  4-3 延留音），分析结果 **0 条发现**；
- `bad_exercise.musicxml`：含平行五、隐伏五/八、强弱拍不协和、连续大跳、
  超域、重复最高音等 38 条发现；
- `bad_exercise_revised.musicxml`：修订版（22 条），用于两版比对；
- `broken_notation.musicxml`：多声部混写 + 时值缺失，解析中止并精确定位。

## 设计要点

- **不补全**：缺 divisions/拍号/时值等一律 error 并中止分析，绝不假设默认值；
- **延音合并**：`<tie>` 与 `<notations><tied>` 两种记法都识别，跨小节同音
  延音合并为一个发声片段与一个起音；
- **判定可追溯**：每条发现附 `trace`（前后音程、声部移动、进入/解决轨迹）；
- **规则可版本化**：规则内容取 SHA1 指纹，分析时冻结快照；
- **裁定追加式**：保留教师全部裁定历史，筛选取最新；
- **离线**：无任何第三方依赖与网络请求。
