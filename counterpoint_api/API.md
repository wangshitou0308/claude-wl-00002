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
| `GET /samples/<文件>` | 示例谱（10 份 `.musicxml`） |

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
需要严格的多轮沿革管理时，请用下面的**修订链**。

## 多轮修订链

修订链把一份起点分析与后续各轮修订分析串成有序序列，链内**冻结**
`species`、`cantus_part` 与规则版本；每次追加时自动做谱面对齐与
finding 追踪，并管理教师裁定的沿用与复核。

| 方法/路径 | 说明 |
| --- | --- |
| `POST /api/chains` | 以一份分析为起点建链 |
| `GET /api/chains` | 链列表 |
| `GET /api/chains/<id>` | 链详情（含轮次与待复核计数） |
| `POST /api/chains/<id>/revisions` | 追加一轮分析 |
| `GET /api/chains/<id>/timeline` | 修订时间线（逐轮追踪依据） |
| `GET /api/chains/<id>/reviews` | 待复核筛选（默认 `state=pending`） |
| `POST /api/chains/<id>/reviews` | 提交复核 |
| `POST /api/chains/compare` | 链间比较 |
| `GET /api/chains/<id>/download` | 下载链完整 JSON |

### 建链与追加

```bash
curl -X POST localhost:8000/api/chains -H 'Content-Type: application/json' \
     -d '{"analysis_id": 1, "title": "张三的第一次作业"}'

curl -X POST localhost:8000/api/chains/1/revisions \
     -H 'Content-Type: application/json' -d '{"analysis_id": 2}'
```

* 起点分析必须 `status=ok`（无解析错误）且已带 `species`/`cantus_part`；
  建链时把这三者与规则版本指纹冻结进链。
* 追加的分析必须：与链的 `species`、`cantus_part`、规则版本**完全一致**，
  且未属于任何链（一份分析只能入链一次）。不一致返回 400，`details`
  逐项列出冲突字段。
* 追加时与链内最新一轮的谱面做**声部 → 小节 → 拍位**三级对齐：
  声部 id 及顺序、小节数、逐小节拍号任一不一致即拒绝加入（400，
  `details.checks` 列出每级检查结果）。调号变化不阻断，记入
  `alignment.warnings`。

### finding 追踪与逐轮依据

追加成功后，按相关音符的**声部、时间区间与音高变化**追踪上一轮每条
finding，状态机：

| `status` | 含义 |
| --- | --- |
| `carried` | 原样保留：类型相同，音符声部/时间区间/音高全未变 |
| `moved` | 位置移动：类型相同，音符拍位/时值/序号变化，音高未变（同小节内换位/移动经“失而复得”再配对识别） |
| `pitch_changed` | 音高变化：类型相同、位置对应，但至少一个音符音高变化 |
| `kind_changed` | 类型变化：同一批音符对应到不同类型的 finding |
| `resolved` | 已解决：本轮无对应 finding（含相关音符被删除） |
| `new` | 新出现：本轮没有来源的 finding |
| `ambiguous` | 歧义：一项对应多个候选，**并列保留，不凭相似度自动归属** |

候选判定**精确键优先**：本轮 finding 的音符集合与映射后完全相同者优先；
无精确匹配时退到「有交集」候选，但已被其他 finding 精确匹配的本轮
finding 不再充当交集候选。候选不止一个时一律标 `ambiguous`。

每条追踪记录（trace）的 `evidence` 就是**逐轮依据**：判定规则原文、
相关音符的逐项对齐（`note_alignment`，含前后小节/序号/拍位/音高/时值）、
歧义条目的全部候选（`candidates`）。追加接口的响应与
`GET /api/chains/<id>/timeline` 都包含它们。

### 裁定沿用与待复核

* `carried` 且上一轮有教师裁定：**自动沿用**——裁定复制到本轮 finding
  （备注前缀 `[沿用第N轮裁定]`），trace 记为 `review_state=inherited`，
  `verdict_source` 指向来源裁定；
* 其余一切变化（`moved`/`pitch_changed`/`kind_changed`/`ambiguous`/
  `resolved`/`new`）都进入 `review_state=pending`（待复核），有来源裁定的
  一并保留在 `verdict_source`（`applied=false`）；
* 首轮（`initial`）与无裁定可沿用的 `carried` 为 `review_state=none`。

`GET /api/chains/<id>/reviews` 默认返回 `state=pending` 的追踪记录，
可用 `?state=reviewed|inherited|none|all`（可重复）与 `?seq=` 过滤。

提交复核：

```bash
curl -X POST localhost:8000/api/chains/1/reviews \
     -H 'Content-Type: application/json' \
     -d '{"trace_id": 42, "decision": "confirmed",
          "comment": "确认", "teacher": "李老师"}'
```

* 有本轮 finding 的条目（`new`/`moved`/`pitch_changed`/`kind_changed`）：
  `decision` 作为裁定写入该 finding（同 `PUT /api/verdicts`）；
* `resolved` 条目没有本轮 finding：`decision`/`comment`/`teacher` 记录在
  追踪记录本身（`review_decision`/`review_comment`/`review_teacher`/
  `reviewed_at`）；
* `ambiguous` 条目须先给 `chosen_finding_id`（必须在 `evidence.candidates`
  中）指定归属，裁定落在选定 finding 上；
* 复核后 `review_state=reviewed`，重复复核返回 400。

### 链间比较与下载

```
POST /api/chains/compare
{ "chain_id_a": 1, "chain_id_b": 2 }
```

返回两链的逐轮统计（每轮 finding 数与追踪摘要）、`compatible`（三条冻结
参数是否一致）以及两链**最新一轮**的 finding 增减（同两版比较结构）。

`GET /api/chains/<id>/download` 以附件返回链完整 JSON：链元数据、每轮
分析摘要、**完整对齐结果**（含 `note_map` 音符映射）、逐轮追踪与依据、
每轮裁定历史。筛选、时间线与导出读取同一追踪表，复核状态始终一致。

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
| `bad_exercise_revised.musicxml` | 上一谱的修订版（第 2 轮），供两版比较与修订链 |
| `bad_exercise_revised2.musicxml` | 再修订版（第 3 轮）：演示 resolved/carried/pitch_changed/kind_changed/ambiguous/new 全部追踪状态 |
| `broken_notation.musicxml` | 无法分析：P1 混写两个 `<voice>`、P2 休止符缺 `<duration>` |

示例谱由 `samples/make_samples.py` 生成（`python .../make_samples.py`）。
三轮 `bad_exercise*` 的小节结构一致，可直接建链演示多轮追踪。

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

# 6. 建修订链并逐轮追加
curl -X POST localhost:8000/api/chains -H 'Content-Type: application/json' \
     -d '{"analysis_id":1,"title":"张三的作业"}'
curl -X POST localhost:8000/api/chains/1/revisions \
     -H 'Content-Type: application/json' -d '{"analysis_id":2}'

# 7. 看时间线与待复核，提交复核
curl localhost:8000/api/chains/1/timeline
curl 'localhost:8000/api/chains/1/reviews?state=pending'
curl -X POST localhost:8000/api/chains/1/reviews \
     -H 'Content-Type: application/json' \
     -d '{"trace_id":42,"decision":"confirmed","teacher":"李老师"}'

# 8. 下载归档
curl -OJ localhost:8000/api/analyses/1/download
curl -OJ localhost:8000/api/chains/1/download
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
| `chains` | 修订链：冻结的 species / cantus_part / 规则版本 |
| `chain_revisions` | 链内轮次：分析、谱面、对齐结果（`alignment_json`）、追踪统计 |
| `finding_traces` | finding 追踪：状态、前后 finding、判定依据（`evidence_json`）、复核状态与来源裁定、复核裁定（`review_decision` 等） |

## 运行测试

```bash
python -m unittest counterpoint_api.tests.test_core -v
```
