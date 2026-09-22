# Changelog

本项目的所有重要变更都记录在此文件。
格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

产品底线不随版本改变：**只核验可证伪的陈述，不给候选人打分、不做排名、不给录用建议；
判定归规则，理解归模型。**

## [Unreleased]

### 新增 Added
- **智谱 GLM 成为一等供应商**：`PROVIDERS["zhipu"]` 内置端点与 `glm-4.7-flash`
  （官方免费档），切换只要一行 `LLM_PROVIDER=zhipu`，不用再手抄 `LLM_ENDPOINT` /
  `LLM_MODEL`。`thinking` 默认 `disabled`——开着思考会吃光 `max_tokens`、
  让 `content` 返回空串，和 `deepseek-flash` 是同一个坑。
- **论文走免费公开 API**：新增 `openalex_hits()`，与既有 `crossref_hits()` 合并为
  `paper_hits()`。Crossref 只认领了有 DOI 的记录，中文期刊、会议与学位论文常常查不到；
  OpenAlex 补上这一块，还直接给作者机构，正好是判定「是不是本人」要用的信息。
  两个都不消耗搜索额度。
- **专利先查专利域**：`kind="patent"` 的查询限定到 `patents.google.com`（收录 CN
  公开文本，页面可静态读），查空才退回裸搜。中文专利没有可直接调用的免费公开 API——
  CNIPA 公众查询要登录和验证码，EPO OPS / Lens 要申请 key，PatentsView 只覆盖美国，
  所以这是目前最接近「免费且可核」的路。
- **引擎实测脚本** `search-strategy/probe_engines.py`：对同一条查询分别打
  std / pro / sogou，报告返回条数与 `link` 填充率，用来定期复验「裸查询只有搜狗带 URL」
  这条结论还成不成立。

### 改进 Changed
- **搜索成本降到约三分之一**：带域限定的查询（学校官网公示、主办方站，占大头）默认从
  `search_pro`（0.03/次）改为 `search_std`（0.01/次）。官方文档确认 std 支持
  `search_domain_filter`，过滤照常生效，差别在召回深度。
  可选兜底 `SEARCH_ENGINE_FALLBACK=search_pro`：std 空结果时再确认一次，
  **默认关闭**——期望单价 = 0.01 + p(空) × 0.03，窄域查询 p(空) 超过 2/3 反而更贵，
  开不开取决于你的实际空率。
- **裸查询仍走 `search_pro_sogou`**：不是没注意到它最贵（0.05/次），而是实测只有它
  返回 `link`，std/pro 的裸结果连 URL 都没有。想复验就跑上面那个脚本。

### 修复 Fixed
- **429 不再一刀切当限流**：智谱把「余额不足 / 资源包用尽」也放在 429 下，而
  `llm.py` 把整段 429 当成可重试，于是真正的原因被退避三次拖成一句
  「连续 3 次未成功：返回 429」。现在按错误码（1113）或文案识别为硬错误，立即抛
  `LLMUnavailable` 并带上供应商原话；普通限流仍重试，但错误信息里保留 `r.text`——
  没有它，限流和「模型名写错被网关拒了」在日志里长得一模一样。搜索侧早有这个区分
  （`test_28`），LLM 侧此前没有。

### 修复（早前）Fixed
- **模型返回类型不合 schema，导致整次核验失败**：模型把 `identity_conflicts` 写成单个
  字符串（如 `"学院不同：页面为X仪器工程研究所"`）时，`Evidence` 抛 `ValidationError`；
  而 `collect.extract_evidence` 当时**没有兜底**，一个字段类型不对就让整份报告跑不出来。
  现在 `schema` 统一做类型收敛（`str → [str]`、数字 → 字符串、`{"field":..,"diff":..}` →
  `"field：diff"`）。
  **关键：是收敛不是丢弃**——`identity_conflicts` 是「同名一票否决」的唯一依据，
  丢掉会把本该判 `who` 的条目误升为「已证实」，等于把同名他人的记录算到候选人头上。
- **`supports` 为字符串时被逐字符迭代吃掉**：`[s for s in "某要素" if ...]` 会静默得到
  空列表——不报错，但证据全丢。改为先收敛、再按 `elements` 过滤。
- **单条证据构造失败不再拖垮整份报告**：`Evidence` 构造加 `try/except` 兜底，与
  `split.split_claims` 对 `Claim` 的处理保持一致。
- 新增回归用例 `test_30`，锁死「字符串收敛、内容不丢、矛盾仍判 `who`」三条语义。
  规则层验收 **33/33 通过**，检索与学校域两套合计 **31/31 通过**。

## [0.2.0] - 2026-09-21

围绕「查得更准、判得更公道、跑得更快」三条线改进检索与判定。
规则层验收 `open_box/tests/test_rules.py` **28/28 通过**。

### 新增 Added
- **保研/推免检索**：`学历` 默认仍不检索，但命中「保研 / 保送 / 推免 / 推荐免试 / 夏令营」
  等词时恢复检索，专查学校官网的**推免公示 / 拟录取名单 / 优秀营员名单**（`site:` 优先限定学校域）。
- **学历公众号探针**：对「就读于某校」类陈述，增加一条合规的
  `site:mp.weixin.qq.com "学校" "姓名"` 粗检（仅命中搜索引擎已收录文章，不枚举、不绕行）。
- **时间/发布方佐证判定**：`judge` 新增规则派生的弱佐证信号「时间吻合」「发布方为相关机构」，
  把「权威语境 + 发布时间对得上 + 提到本人」的证据从 `身份未确认` 抬到 `部分证实（转人工）`；
  单靠佐证不足以认定本人（需配合内容锚点），身份冲突仍为硬否决。
- **并发读页**：不同域页面并发回读（`COLLECT_WORKERS`，默认 3，范围 1–6；同域仍串行并保持 1 秒间隔）。
- **相关性排序**：读页前按标题/摘要与「候选人 + 陈述要素」的匹配度排序，供应商乱序不再浪费读页预算。

### 改进 Changed
- **读页预算跨查询共享**：不再让第一条查询垄断预算，多角度查询轮转取证。
- **未知学校域动态发现**：`news.x.edu.cn → x.edu.cn`，学校表未收录也能正确加官网域限定。
- **公众号作者判断**：发布方名称含陈述机构名时，允许该公众号文章从 D 升到 B。
- **学校域名表**：补哈尔滨工程大学等 5 所，修正未收录学校导致 `site:` 限定退化为全网搜的问题。

### 修复 Fixed
- **`.docx` 解析兜底**：缺少 `python-docx` 时改用纯标准库（`zipfile` + `xml`）读取
  `word/document.xml`，不再直接崩溃。
- **前端错误显示**：核验失败时展示后端真实错误，不再崩在 `doneEv.report.claims`。

### 升级说明 Upgrade notes
- 完整解析/检索建议安装：
  `pip install pydantic httpx pypdf beautifulsoup4 lxml trafilatura pdfminer.six pymupdf python-docx`
- 真实核验需在 `open_box/.env` 配置 `DEEPSEEK_API_KEY` 与 `SEARCH_API_KEY`（智谱）。
- 并发数可用 `COLLECT_WORKERS` 调整；供应商频繁 429 时可降到 2，设为 1 恢复完全串行。

## [0.1.0] - 初始版本

- 履历核验系统首次提交：解析 + 隐藏文字/注入检测 → 画像 → 拆陈述（≤12 条）→
  策略引擎生成三轮查询 → 检索 + 整页回读 → 逐要素判定 → 澄清问题；
  界面上的流式核验状态窗、流式问答、来源分级面板、逐条检索计划、报告导出；dsh 插件 6 工具。

[Unreleased]: https://github.com/Lonid-C/under-box/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Lonid-C/under-box/releases/tag/v0.2.0
