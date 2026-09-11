# 离线两声部对位作业审阅 API

纯 Python 标准库实现（`http.server` + `sqlite3` + `xml.etree.ElementTree`），
无需联网、无第三方依赖。面向作曲学生与教师审阅两声部 MusicXML 对位作业：
解析拍号、调号、声部、音高、休止、附点与跨小节延音，按 `divisions`
还原时间位置，按可配置规则逐拍检查，并保留教师裁定与两版对比。

## 启动

```bash
python -m counterpoint_api.server --db counterpoint.db --port 8000
# 可选环境变量：COUNTERPOINT_DB / COUNTERPOINT_HOST / COUNTERPOINT_PORT
```

启动后：

| 入口 | 说明 |
| --- | --- |
| `GET /` | 接口导航页 |
| `GET /docs` | 本文档（Markdown） |
| `GET /api/health` | 健康检查 |
| `GET /samples/<文件>` | 示例谱（4 份 `.musicxml`） |

所有数据接口返回 `application/json; charset=utf-8`。错误统一为：

```json
{ "error": "人类可读消息", "details": null }
```

## 设计原则：只还原，不补全

解析器忠实读取谱面。以下情况记为 **error** 级记谱问题并**中止分析**
（返回 `status=parse_error`，不会自行假设默认值）：

- 缺少 `<divisions>`、`<time>`/`beats`/`beat-type`、`<key><fifths>`；
- 音符缺 `<duration>`、时值为负、小节总时值与拍号不符；
- 一个 `<part>` 内混写多个 `<voice>`、出现 `<chord/>`（多声部混写）、
  `<grace>`、`<unpitched>`、音高无法识别；
- 两声部小节数或拍号不一致。

每条问题都定位到 **声部（part_id）+ 小节号 + 声部内音符序号**。
多 `<voice>` 混写还会列出每个 voice id 出现的具体小节与音符位置。

**warning**（不阻塞分析）：延音线未配对/两端音高不一致、未知 `type`
字符串、小节编号非数字等。

延音识别同时接受 `<tie type="start|stop"/>` 与
`<notations><tied type="start|stop"/></notations>`（部分制谱软件只写后者），
跨小节同音高延音合并为**一个发声片段、一个起音**。

时间一律换算为 **四分音符单位**（`duration / divisions`），拍位按拍号
换算（4/4 的第 3 拍为强拍，6/8 的第 4 拍为强拍）。

## 规则配置

默认规则集（Fux 风格严格对位）在首次启动时入库，`built_in=true`。
内容包括：

- `beat`：各拍号强拍表；隐伏五八度跳进阈值（默认 ≥4 半音）；
  反向到达八度豁免；
- `intervals`：协和音程表（默认 P1/m3/M3/P5/m6/M6/P8，复音程折算）；
- `melody`：最大跳进（默认 12 半音）、连续同向大跳阈值（7）、
  大跳后须反向级进、两声部音域、最高音允许出现次数（2）；
- `dissonance`：延留音（准备 + 级进下行解决 + 允许的解决标签 4-3/7-6/9-8）、
  经过音、辅助音、持续音开关；
- `voice`：声部交叉/超越开关；
- `severity`：每类发现的级别（error/warning/info）。

规则按内容算 SHA1 指纹作为 `version`；改名不产生新版本，改任何参数即新版本。

### 规则接口

| 方法/路径 | 说明 |
| --- | --- |
| `GET /api/rules` | 规则集列表 |
| `GET /api/rules/<id>` | 规则集详情 |
| `POST /api/rules` | 以默认规则为底创建规则集 |
| `POST /api/rules/<id>/copy` | 复制（可带 `overrides` 深合并） |

`POST /api/rules` 请求体：

```json
{
  "name": "宽松规则",
  "overrides": {
    "melody": {"max_leap_semitones": 19},
    "dissonance": {"allow_pedal": true},
    "severity": {"parallel_fifth": "warning"}
  }
}
```

非法配置（如协和音程标签无法解析、音域 low 高于 high）返回 400 与错误明细。

## 原谱

| 方法/路径 | 说明 |
| --- | --- |
| `POST /api/scores` | 上传 MusicXML |
| `GET /api/scores` | 原谱列表 |
| `GET /api/scores/<id>` | 元数据（`?xml=1` 含原文） |
| `GET /api/scores/<id>/download` | 下载原始 MusicXML |

上传支持三种请求体：

1. 原始 XML 文本，`Content-Type: application/xml`；
2. JSON：`{"musicxml": "<score-partwise ...>"}`（可带 `filename`）；
3. `multipart/form-data` 文件字段。

同内容（SHA1 相同）重复上传返回同一 score id。谱面声明的声部数不是 2 个时
返回 400。响应示例：

```json
{
  "id": 1, "title": "示例2-含多类问题的对位作业",
  "part_ids": ["P1", "P2"], "digest": "…",
  "parse_issue_count": 0, "fatal": false
}
```

## 分析

| 方法/路径 | 说明 |
| --- | --- |
| `POST /api/analyses` | 创建分析 |
| `GET /api/analyses` | 分析列表（可 `?score_id=`） |
| `GET /api/analyses/<id>` | 分析详情（含全部发现） |
| `GET /api/analyses/<id>/findings` | 筛选发现 |
| `GET /api/analyses/<id>/verdicts` | 该分析的全部裁定 |
| `GET /api/analyses/<id>/download` | 下载完整分析 JSON |

创建：

```bash
curl -X POST localhost:8000/api/analyses \
  -H 'Content-Type: application/json' \
  -d '{"score_id": 1, "rule_set_id": 1}'
```

不传 `rule_set_id` 时使用内置默认规则集。分析时规则内容被**冻结快照**
存入分析记录，之后规则集改动不影响历史分析。谱面有 error 时返回
`status="parse_error"`、`findings` 为空、`parse_issues` 含定位明细。

### 检查项与发现类型

| `kind` | 级别 | 含义 |
| --- | --- | --- |
| `voice_crossing` | error | 声部交叉（高音低于低音） |
| `voice_overlap` | warning | 声部超越（默认关闭） |
| `parallel_fifth` / `parallel_octave` | error | 平行五度/八度（含复音程 P12/P15） |
| `parallel_unison` | warning | 平行一度（配置开关） |
| `hidden_fifth` / `hidden_octave` | error | 隐伏五度/八度（同向新起音、一声部跳进） |
| `strong_dissonance` | error | 强拍不协和（仅允许有准备、级进下行解决的延留音） |
| `weak_dissonance_entry` | error | 弱位不协和进入非法（跳进、两音同击、无准备等） |
| `weak_dissonance_resolution` | error | 弱位不协和解决非法（未级进/解决到不协和/悬空） |
| `consecutive_leaps` | warning | 连续同向大跳 |
| `leap_not_reversed` | warning | 大跳后未反向级进折回 |
| `leap_too_large` | warning | 超过最大跳进 |
| `repeated_highest` | warning | 旋律最高音重复超限 |
| `range_violation` | warning | 超出声部音域 |

每条发现包含：

- `measure` / `beat`：小节号与拍位（拍号意义上的拍，含反拍位置如 `2.5`）；
- `notes`：相关音符数组，每项含 `part_id`、`measure`、`note_index`、
  `pitch`、`event_id`；
- `trace`：**判定轨迹**——实际比较过的前后音程（如 `P12 → P12`）、
  两声部移动半音数、进入/解决音符、延留音准备情况与解决标签、
  触发的规则原文；
- `message`：中文结论；
- `fingerprint`：按类型+相关音符位置计算的稳定指纹（版本比对用）。

同一对发声片段持续的不协和/交叉只在进入点报告一次；合法延留音持续到
解决前的弱拍不重复报告。

### 筛选发现

`GET /api/analyses/<id>/findings` 查询参数均可重复、可组合：

| 参数 | 取值 |
| --- | --- |
| `kind` | 发现类型，如 `parallel_fifth`（可重复） |
| `severity` | `error` / `warning` / `info` |
| `measure` | 小节号整数 |
| `role` | `upper` / `lower`（按谱面声部顺序映射） |
| `part` | 直接按 part_id，如 `P1` |
| `verdict` | 按最新裁定：`confirmed`/`rejected`/`deferred` |

示例：`/api/analyses/3/findings?severity=error&role=upper&measure=4`

## 教师裁定

裁定是**追加式**的：同一发现可多次裁定，筛选与导出始终取最新一条，
历史全部保留。

```
PUT /api/verdicts
{
  "finding_id": 12,
  "decision": "rejected",          // confirmed / rejected / deferred
  "comment": "此处为风格性进行",
  "teacher": "王老师"
}
```

- `confirmed`：确认问题；
- `rejected`：驳回（如判定为风格允许）；
- `deferred`：存疑待定（如课堂讨论）。

## 两版比较

| 方法/路径 | 说明 |
| --- | --- |
| `POST /api/comparisons` | 比较两版分析 |
| `GET /api/comparisons/<id>` | 读取历史比对结果 |

```
POST /api/comparisons
{ "base_analysis_id": 3, "revised_analysis_id": 5, "persist": true }
```

按发现 `fingerprint`（类型 + 相关音符声部/小节/序号）对齐，返回：

```json
{
  "counts": {
    "base_total": 38, "revised_total": 22,
    "added": 10, "removed": 26, "unchanged": 12, "net_change": -16
  },
  "added":   [ { "kind": "...", "kind_zh": "...", "measure": 2, "notes": [...] } ],
  "removed": [ ... ],
  "unchanged": [ ... ],
  "parse_status": {"base": "ok", "revised": "ok"}
}
```

注意：比对的是两份**分析**（可同规则不同谱，亦可不同规则）。典型流程是
上传修订版 MusicXML → 用同一规则集创建分析 → 与旧分析比较。

## 下载 JSON

`GET /api/analyses/<id>/download` 以附件形式返回：

- 原谱元数据、完整规则快照与 `rules_version`；
- 全部发现（含 trace、拍位、相关音符）；
- 全部裁定历史；
- 发现类型中文标签表 `kind_labels_zh`。

## 示例谱（`samples/`）

| 文件 | 用途 |
| --- | --- |
| `good_exercise.musicxml` | 干净作业：圣咏式 1:1，含跨小节延音与合法 4-3 延留音，分析 0 发现 |
| `bad_exercise.musicxml` | 问题作业：平行五、隐伏五/八、强拍与弱位不协和、连续大跳、超域、重复最高音等 |
| `bad_exercise_revised.musicxml` | 上一谱的修订版，供两版比较 |
| `broken_notation.musicxml` | 无法分析：P1 混写两个 `<voice>`、P2 休止符缺 `<duration>` |

示例谱由 `samples/make_samples.py` 生成（`python .../make_samples.py`）。

## 典型使用流程

```bash
# 1. 上传作业
curl -X POST localhost:8000/api/scores -H 'Content-Type: application/xml' \
     --data-binary @counterpoint_api/samples/bad_exercise.musicxml

# 2. 创建分析（默认规则）
curl -X POST localhost:8000/api/analyses \
     -H 'Content-Type: application/json' -d '{"score_id":1}'

# 3. 只看强 error
curl 'localhost:8000/api/analyses/1/findings?severity=error'

# 4. 教师裁定
curl -X PUT localhost:8000/api/verdicts -H 'Content-Type: application/json' \
     -d '{"finding_id":4,"decision":"confirmed","teacher":"李老师"}'

# 5. 上传修订版、分析、比较
curl -X POST localhost:8000/api/comparisons -H 'Content-Type: application/json' \
     -d '{"base_analysis_id":1,"revised_analysis_id":2}'

# 6. 下载归档
curl -OJ localhost:8000/api/analyses/1/download
```

## SQLite 表结构

| 表 | 内容 |
| --- | --- |
| `scores` | 原谱 XML 文本、声部 id、SHA1 摘要（去重） |
| `rule_sets` | 规则 JSON、版本指纹、内置标记（name+version 去重） |
| `analyses` | 规则快照、解析问题、统计摘要、状态 |
| `findings` | 发现（类型/级别/小节/拍位/notes/trace/message/指纹） |
| `verdicts` | 追加式裁定历史 |
| `comparisons` | 两版比对结果快照 |

## 运行测试

```bash
python -m unittest counterpoint_api.tests.test_core -v
```
