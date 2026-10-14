# UnderBox 最新仓库专项优化 Prompt（Coding Agent 可执行版 · v2）

> **直接交给 Codex / Claude Code / Cursor Agent 的任务书**。请按本文**修改真实代码、添加测试、运行验证、记录结果**，而不是重新输出一份开发建议。
>
> **目标代码基线**：`under-box-codex-underbox-verification-agents-20261012.zip`，仓库根目录为 `under-box-codex-underbox-verification-agents-20261012/`。这是 2026-10-12 的源码快照；若 Agent 正在一个有 `.git` 的更新工作树中工作，必须先比较差异，以当前工作树为准，并保留未提交修改。压缩包本身没有 Git 历史，不要假称核对过 commit。
>
> **总体目标**：保留 UnderBox 已建成的完整产品和十三类专项核验体系，把它进一步升级成**身份绑定、逐要素证明、来源原文可追溯、检索失败可解释、授权隔离可靠、能离线评测与复现**的 AI 履历公开事实核验系统。
>
> **竞赛场景**：AIC「AI+开源」优先需要**独立技术贡献 + 可复现实验 + 开源材料**；华为 ICT 创新赛继续发展完整的可信 AI 业务流程与可选华为云适配。不要为了参赛而牺牲真实准确性。
>
> **执行优先级**：**P0 判定正确性/安全 > P1 证据护照/评测集/人工复核 > P2 模型与云部署/体验/性能**。先打通最小可用闭环，再增量增强。无真实 API 凭据、未获授权、需付费或需修改线上数据库时不得擅自运行真实调用、采集真实个人资料或发布部署。

---

# 0. 首先明确：这个版本已经有什么，不要重复造轮子

这份源码**已经实现**，后续改动须**继承、复用、验证**而非重写的能力：

| 已有能力 | 最新仓库实际位置/事实 | 本次正确的行动 |
|---|---|---|
| **13 类专项核验 Agent** | `open_box/app/verification_agents.py`：education、employment、paper、book、patent、conference、competition、award、internship、project、teaching、art、student_work | 验证各专员的真实数据源、身份证据规则与边界，**不要再另起一个 Agent 框架** |
| **数据源能力登记与调用日志** | `open_box/app/data_sources.py`：`SOURCES`、`AgentRun`、`invoke_source`、`SourceSearcher`、`SourceFetcher`，使用 `ContextVar` 做任务作用域隔离 | 强化完整拦截、真实调用记录、失败统计、泄露防护；不能把“注册了数据源”写成“已接通数据库” |
| **跨 Agent 导航线索协作** | `VerificationCoordinator.schedule/context_for/record/summary`；学历分学段先行，论文/著作可向任职/教学/艺术交接检索线索 | 强化线索来源有效性、逐要素证明约束和计划回放；**不能让前一条的证据变成后一条的证明** |
| **实习人工背调入口** | `open_box/app/internship.py::build_manual_review()`，使用企查查公司入口和公开电话；`VerifiedClaim.manual_review`；前端已有展示 | 不要重新开发“实习自动核验”。改进公司主体、公共联系方式、告知授权及人工处理闭环 |
| **论文/著作/任职/教学专项核对** | `paper_identity.py`、`book_catalog.py`、`faculty_recall.py`、`verification_agents.py` 等 | 保留姓名/拼音/学段/时间/角色的严格边界，重点做回归和可解释证明 |
| **GitHub 项目按需核验** | `github_audit.py`；仅本条履历明示项目仓库时进入工具，已有前端入口 | 保留按需触发和账号/项目归属约束，不把 stars、PR 数量当能力分数 |
| **真实账户、历史和配额** | `underbox/accounts.py`、`store.py`：SQLite 账户、角色、会话、每账户密钥、最近 10 次报告和用量 | 做更细的权限边界、临时 token 生命周期、复核操作记录；**不要从零做用户系统** |
| **已有前端产品** | `underbox/index.html`：上传 PDF/个人主页、拆分、勾选、SSE 进度、来源详情、人工背调、问答和报告 | 渐进增加“逐要素证据护照”，保留登录/历史/移动端/动画；不要推翻界面 |
| **统一回归门禁** | 根 `AGENTS.md`、`REGRESSION.md`、`scripts/regression.py`、`make test` | 严格遵守；不得删测试、静默跳过测试、擅自把新失败列入已知失败 |
| **现存 5 项旧验收差异** | `REGRESSION.md`：旧验收 02、03、08、30、31；`scripts/regression.py::KNOWN` 精确匹配旧失败签名 | 逐项归因，正确修复或修订不安全的旧测试前提；**不能因为遗留清单存在就宣称全通过** |
| **已知待办** | `README.md` 明确写 `app/coverage.py` 尚未实现 | 增加**检索覆盖度与运行状态**，不是候选人的诚信/可靠性分数 |

特别注意：这次的 `verification_agents.py` 与 `data_sources.py` 属于**已落地的专项分工/路由和真实工具调用包装**，并不意味着每类已获得权威私人数据库访问权限。学信网、雇主内部数据库等非公开信息，不准假设可以自动查询。

## 不可改变的产品底线

1. **只核验可验证的履历陈述，不评价人格。** 不做候选人信用分、诚信风险分、排名、录用/淘汰建议。
2. **判定归确定性规则、理解与候选摘录归模型。** LLM 不能直接设置最终“已证实”“身份可信”或“应拒录”。
3. **查不到公开记录 ≠ 造假；搜索 API 失败 ≠ 查无此人；同名 ≠ 同一人；公开公司电话 ≠ 证明候选人在此实习。**
4. 保留五态 `ok / part / ask / none / who` 的现有内部/前端契约；新增 `retrieval_status`、`element_proofs` 等独立字段实现更清楚的解释，**不要轻易新增第六种破坏兼容的状态**。
5. 只使用允许公开访问/处理的材料；遵守 robots、访问限制、知识产权和数据最小化要求。不抓取非公开人事资料，不绕过验证码/登录，不把个人手机号当成企业背调专线。
6. 保留完整的学历分阶段、奖项独立核验、学校/学段/年份/人员/现任机构排除、输入主页不得自证、官方附件与 OCR 低强度标记等已有严格规则。
7. 离线测试、演示和 Benchmark 使用**合成人物、合成机构及许可数据**。Mock 和真实核验不得混用或误导。
8. **先度量再优化**；所有效能、准确率、提升幅度、通过数量必须来自实际脚本，不能编造。
9. **默认不改线上 `https://underbox.online/`，不登录服务器，不消耗付费 API，不重置线上数据库，不 push 到远端。** 如当前工作区有他人改动，先保留并确认。
10. 所有新安全控制都应有离线测试；不能以“提示词里写了忽略恶意指令”代替真实边界验证。

---

# 1. Gate 0：审查、基线与实施报告（先做）

## 1.1 先读这些真实文件

按顺序读取：

```text
AGENTS.md
REGRESSION.md
README.md
CHANGELOG.md
requirements.txt
scripts/regression.py
open_box/docs/VERIFICATION_AGENTS_2026-10-12.md
open_box/docs/ADVISOR_ARCHITECTURE_2026-10-12.md
open_box/app/schema.py
open_box/app/judge.py
open_box/app/collect.py
open_box/app/search.py
open_box/app/verification_agents.py
open_box/app/data_sources.py
open_box/app/pipeline.py
open_box/app/internship.py
open_box/app/source_guard.py
open_box/app/paper_identity.py
open_box/app/github_audit.py
underbox/serve.py
underbox/accounts.py
underbox/store.py
underbox/index.html
open_box/tests/test_agent_coordination.py
open_box/tests/test_verification_agents.py
underbox/tests/test_workflow_contract.py
underbox/tests/test_manual_review_contract.py
underbox/tests/test_manual_review_frontend.cjs
```

要检验**新分支实际工作树**是否仍然具有这些实现，不以日期或文件名替代检查。

## 1.2 严格执行项目的正式回归流程

仓库根目录：

```bash
python3 -m pip install -r requirements.txt
python3 scripts/regression.py
# 或 make test
```

然后在可用环境中检查前端：

```bash
node underbox/tests/test_manual_review_frontend.cjs
node underbox/tests/test_usage_frontend.cjs
```

`REGRESSION.md` 定义：退出码 0 才是全通过；退出码 2 表示只有经签名确认的旧失败，**仍不等于全通过**；缺环境依赖、未运行真实浏览器或有新失败，必须单列。

**本次上传 zip 在审阅沙盒的有限基线，仅供复现线索，不是你运行后的结果：**

- Python `3.13.5`、Node `22.16.0`；
- `core`：**520 / 529**，9 项未通过（7 项论文作者关联/公开元数据相关、2 项旧 `.xls` 解析相关）；该沙盒缺 `pypinyin`、`xlrd`，这些失败高度疑似缺依赖引起，**需安装完整依赖后才能归因**；
- `service`：**33 / 33**；
- `legacy`：**27 / 33**，其中 5 项属于已记录的旧失败，另外 1 项是 Playwright Chromium 可执行文件未安装引起的浏览器测试失败；
- 两个 `underbox/tests/test*.cjs` 在**分别运行时通过**；
- 审阅沙盒不能联网安装缺失依赖，完整一键回归没有成功跑到最终汇总阶段。绝不能把上述数字当成部署环境或最新版工作树的最终通过率。

行动要求：

- 先补环境，再重跑回归；若还是失败，分析真实功能错误；
- 逐项研究旧失败 02/03/08/30/31，不允许靠删除用例或修改 `KNOWN` 白名单掩盖新回归；
- 对既有旧断言与新的更安全规则冲突的情况，要用**新旧输入、期望与证据**证明为什么应该更改测试契约；
- 保留最少一次完整运行日志（脱敏）及结构化 JSON 报告；
- 创建 `docs/optimization-v2/00-baseline.md`、`docs/optimization-v2/CHANGELOG.md`，记录快照、测试环境、失败归因、变更和每个 Gate 的结果。

## 1.3 第一批必须复现的实际规则边界

以下**已在这个 20261012 压缩包上通过构造输入复现**，不是沿用上一版假设：

**Case J-01，跨等级拼接错误：**

```text
Claim: ["奖项=一等奖", "角色=队长"]
Evidence A: source_tier=A, supports=["奖项=一等奖"], identity_signals=["学校一致","学院一致"]
Evidence D: source_tier=D, supports=["角色=队长"], identity_signals=["学校一致","学院一致"]
两个 URL 不同，均无明确身份冲突，extraction_method=text。
当前 judge.assess(...) → status=ok，proved=两个要素，best_tier=A。
目标：不能得到整条 ok；两个要素分别展示自己的可采信级别。
```

**Case J-02，重复弱身份信号：**

```text
identity_score(["时间吻合"] * 10, []) 当前得 10，
identity_score(["时间吻合"], []) 得 1。
目标：重复同一弱信号不得累计成强身份判断；弱佐证不能独自成为本人确认锚点。
```

此外静态检查发现：

- `collect.py::extract_evidence()` 已对竞赛、学历、荣誉、著作等实行强原文匹配，但尚需覆盖**所有正常网页文本类别**，并为结构化 API 走独立校验路径；
- `judge.py::decide()` 跨证据合并 `supports`，以所有有效来源中的最优 `source_tier` 决定整条状态；
- `VerificationCoordinator.record()` 根据 `vc.proved`、`identity_score >= 2` 和 `extraction_method=text` 推送“已核实线索”，需绑定新的逐要素可采信证明；
- `data_sources.py` 的包装器具有 `__getattr__` 透传，需证明所有实际搜索/读页方法都受授权与记录约束；
- `serve.py` 的 `PREPARED` 只有**最多 6 条**的容量淘汰，token 归属在 SSE `done` 时补写、没有明确 TTL；`/api/verify/stream` 已有 owner 检查，**不要误写成完全没做账户隔离**；
- `serve.py::stream_answer()` 仍硬编码 `DEEPSEEK_API_KEY`、DeepSeek endpoint/model；
- `PageFetcher.get()`、`robots` 及特定公开 POST 方法有自动重定向行为，主页输入校验主要检查 URL 字面值，没有完整连接目标防护；
- `underbox/serve.py::_read_json()` 在工作进程内没有独立的请求体尺寸限制，尽管账户网关有 20 MiB 总限制；
- 报告与界面有 `verificationWorkflow`、`manualReview`，但缺逐要素证据护照和统一检索可用性指标；
- 根目录尚未发现标准 `LICENSE`，不应擅自选定许可证。

下面才是实际编码任务。

---

# P0-1：改造 `judge`，让每一个“已证实”要素都有独立、有效的证明

**优先级最高。先写红测试再改实现。**

主要文件：

```text
open_box/app/judge.py
open_box/app/schema.py
open_box/app/claim_scope.py
open_box/app/academic.py
open_box/app/person_scope.py
open_box/tests/test_rules.py
NEW open_box/tests/test_element_proofs.py
```

## 实现要求

1. **拆分三个概念，不允许再混淆**：
   - `candidate_evidence`：网页/API 返回的候选证据；
   - `accepted_support`：规则认为可以支持**某一个要素**的证据关系；
   - `verified_element`：达到该类别最低证明门槛、可以用于自动“证实”的要素。
2. 重构 `decide`、`valid_evidence`、`split_elements`、`needs_human`、`assess`，使用**同一份标准化判定结果**：
   - 每个 `claim.element` 都单独有 supporting evidence IDs / 证据等级 / 提取方式 / 身份绑定 / 排除原因；
   - A 证据不能替 D 证据提高等级；
   - `status=ok` 时，所有本条**必须证明的要素**都有相应的高质量证明；
   - `status=part` 时，`proved` 必须区分“已经满足正式证明门槛”与“只有线索但未充分证明”；不能把仅见于 D 类个人自述的角色直接标为已证明；
   - `best_tier` 可保留**找到的最高来源级别**作为旧协议兼容字段，但 UI 必须显式说明它**不是每个要素的证明级别**；
   - 类别规则可以为不同字段配置不同的最低来源/独立性要求（如 DOI/题名的结构化登记与奖项/队长的正式公告并不相同），**不要用一套简单的 A/B 白名单误杀已有合法论文元数据**。
3. 修正身份处理：
   - 对重复 signal/conflict 去重；不识别信号只能记 0；
   - `时间吻合`、`发布方为相关机构`、候选人提交 URL、账户由候选人提供等**不能脱离原文身份绑定独立证明同一人**；
   - `_has_identity_anchor()` 不能因为已经被污染的总分高就反向推定强身份锚点；
   - 冲突不得被模型省略、类型变换或多证据拼接冲淡；
   - 相同姓名/相同学校未必同一人；必要时输出 `who` 并 `needs_human=True`。
4. 为所有用于支持 `ok` 的证据建立实体/时序约束：
   - 团队得奖≠本人是成员≠本人担任队长；
   - 本科学历、推免资格、接收方拟录取、就读和获得学位分开；
   - 论文排序第一≠共同一作；预印本≠正式发表；任职活动年份≠入职时间；
   - 书籍馆藏证明题名/责任者不证明个人教学、全部出版总量；
   - GitHub 提交仓库 URL 不证明每次 commit 由本人完成；
   - 已核实作品不能自动证明另一条任职/教学/艺术要素。
5. **矛盾也需校验**：`ask` 只由具有原文依据且确属同一主体、同一时间范围的矛盾触发；不允许模型提供空泛 `contradicts` 即制造负面观感。
6. **转载与原始出处**：规范化明确的追踪参数、真实原始出处与可信转引关系；不要仅因为 LLM 任意填了 `origin_url` 就把两个来源合并或升格。真实不同的独立原始来源可以各自计入。

## 数据契约建议

在 `schema.py` 增加**有默认值、向后兼容**的结构（字段名可根据项目风格调整）：

```python
class ElementProof(BaseModel):
    element: str
    state: Literal["verified", "partial", "unverified", "conflicted"] = "unverified"
    evidence_ids: list[str] = Field(default_factory=list)
    accepted_tier: Tier | None = None
    identity_basis: list[str] = Field(default_factory=list)
    decision_reasons: list[str] = Field(default_factory=list)

# VerifiedClaim 可新增：
# element_proofs: list[ElementProof] = Field(default_factory=list)
# rule_version: str = "proof-v2"
# decision_reasons: list[str] = Field(default_factory=list)
```

用 `schema` 存**报告时的证明结论**，而不是重新打开报告时再让新版本规则对旧证据悄悄改变结论。

## DoD / 自动验收

- [ ] J-01 的 A+D 拼接不再输出 `ok`，并分别展示奖项和队长的证据级别。
- [ ] J-02 的 10 个重复弱信号不会变成身份确认。
- [ ] 两个同名不同人的证据不拼成一个人的完整经历。
- [ ] A/B 合法独立证据分别覆盖所有要素且身份一致，可以得到 `ok`。
- [ ] 证据存在冲突但不属本人/不是同一年，不能自动输出 `ask`。
- [ ] 老 `Report` / `Evidence` / `VerifiedClaim` JSON 能载入，旧前端和 dsh 插件不崩溃。
- [ ] `judge.assess()` 仍是唯一终局判定入口，规则测试无需付费模型。
- [ ] 不因“提高准确率”而恢复旧的错人/错学段/错奖项漏洞。

---

# P0-2：全类别原文硬校验 + Evidence Provenance（证据来源护照基础）

主要文件：

```text
open_box/app/collect.py
open_box/app/search.py
open_box/app/schema.py
open_box/app/paper_identity.py
open_box/app/public_documents.py
NEW open_box/app/provenance.py
NEW open_box/tests/test_provenance.py
```

## 实现

1. 对**所有 LLM 从可回读页面 `text` 提取的文字证据**统一校验：
   - 摘录是否确实出现在**本次实际抓取并解析**的正文中；
   - 允许受控空白、换行、Unicode 归一化；不得允许改变姓名、年份、数字、奖级、角色的模糊近似；
   - `_focus_text()` 拼接了若干正文片段，中间带“［中间原文省略］”，不能将不连续位置拼作一条“连续逐字引用”；
   - 官网列表/表格需保留候选人行与表头、组别、年份、奖级之间的定位关系，避免跨行串证据。
2. **来源类型分层**，不要把所有情况塞进一个 `text`：
   - `page_text_verified`：原始网页正文已定位；
   - `structured_metadata_verified`：Crossref/OpenAlex/GitHub 等 API 的可信结构字段按规则映射；
   - `ocr_unverified`：OCR 仅线索或待人工看原图；
   - `search_snippet_unverified`：搜索引擎摘要，原文未回读；
   - `candidate_submitted`：由候选人提交的线索，仍需验证来源与身份。
   可兼容旧字段 `extraction_method`，加一个明确的 `verification_method` / `provenance_state`；**不能直接把现有 `paper_record` 的合法结构化登记误判为没有原文**。
3. 每条证据新增可选字段：
   - `evidence_id`（稳定/去标识标识）；
   - `original_url`、`resolved_url`（含跳转与原始出处的区别）；
   - `fetched_at`、`source_content_sha256`；
   - `quote_verified`、`quote_start/end` 或精确段落定位；
   - `provenance_state`、`origin_cluster_id`、`validation_reasons`；
   - `source_kind`（页面正文/官方附件/结构化元数据/搜索摘要/OCR/自提交）。
4. `collect.py` 在提取后构造 provenance，`judge.assess()` 在采用之前再次检查有效性；“模型说 supports 了某要素”不是充分证明。
5. 对下载网页、PDF、XLS、图片的内容做好可复验元信息；快照存储默认只保存**短摘录/哈希/定位和元数据**，必要全文应有授权、保存期限和访问控制。
6. 转载关系必须基于可验证的内容/规范链接/原始公告标记；跨站文本相似不能在没有条件时武断归并成同一来源。
7. `Evidence` 老字段缺少 provenance 时须采用**明确的兼容策略**：历史展示保留原结论并标注“旧版未记录本项校验”，但**新核验不可通过缺字段绕过原文门槛**。

## 关键测试

- [ ] 任职、项目、教学、艺术、论文网页、会议、专利页面里，LLM 伪造一句正文没有的支持语句 → 不可作为 `ok` 证明；
- [ ] 空白/换行导致的可复原引文 → 可以定位；
- [ ] 不同姓名、队长/成员、2024/2025、一等奖/二等奖不得模糊通过；
- [ ] OCR 误识别或搜索摘要 → 能展示线索，不能伪装成回读原文；
- [ ] Crossref/OpenAlex 注册记录通过规则与来源 API 校验后仍可保留原有合法 C 级证明；
- [ ] PDF 表格两行人物不交叉证明；
- [ ] 脱敏 provenance 导出不包含个人原始 PDF 内容、Key 或网站大段全文。

---

# P0-3：跨 Agent 协作“可信线索”与实际调用审计

这是本次**最新版本独有的重点任务**，不能从上一版 Prompt 里略过。

主要文件：

```text
open_box/app/verification_agents.py
open_box/app/data_sources.py
open_box/app/pipeline.py
open_box/app/strategy.py
open_box/app/schema.py
underbox/serve.py
open_box/tests/test_agent_coordination.py
open_box/tests/test_verification_agents.py
NEW open_box/tests/test_handoff_trust.py
NEW open_box/tests/test_source_contracts.py
```

## 3.1 可信线索的资格必须与 P0-1 的 `ElementProof` 联动

当前 `VerificationCoordinator.record()` 遍历 `vc.proved` 并从证据中寻找 `identity_score >= 2`、`text` 等条件，足以构造“部分支持被包装成已核实协作锚点”的边界。

改为：

- 只有 `ElementProof.state == "verified"` 且证明链身份有效、原文有效、来源符合该字段的最低门槛时，才允许产生 `confirmedAnchor`；
- 弱支持、身份待人工、冲突项、候选人自述、OCR、搜索摘要、检索目录和实习公司电话均禁止传播为“已核实”；
- `handoff` 必须包含 `fromClaim`、`toClaim`、`element`、`evidenceIds`/可信出处、`purpose=navigation_only`、参与规则版本；
- **下游任务只可用线索产生更好的查询，不复制上游 `supports`、`proved`、`status`，不提升任何来源等级**；
- 如果上游同一证据后来被判无效，下游不得继续把它当“已核实锚点”，应修正输出或标记依赖失效；
- 保持本次报告作用域、`ContextVar` 线程继承和异常复原；不同用户/任务绝不能共享该上下文。

## 3.2 注册的数据源和实际调用必须一致

仔细审查 `SourceSearcher/SourceFetcher.__getattr__` 透传、`paper_hits`、`GitHubClient`、`get_roster`、附件读取、批量查询及额外公开 API，核对是否存在**绕过 `invoke_source()`、类别 allowlist 或调用日志**的路径。

要求：

- 盘点每个专项 agent 的确实可执行 adapter、使用类别、调用预算、落地结果、失败类型；
- 不允许“计划里写某数据源，实际没调用”却标成 `returned`；
- 日志中清楚区分 `planned / attempted / returned / empty / blocked / failed / skipped_by_policy`；
- 记录检索与页面回读的**尝试数**、**成功数**、失败原因；不吞掉错误成为 `empty`；
- 对第三方/API 方法采取 allowlist 或明确代理包装，禁止普通 `__getattr__` 偷偷绕过限制；保留测试替身和已有接口兼容；
- `safe_reference()` 不包含凭据；包含姓名/学校的原始查询是个人信息，报告中应按访问权限和保留期管理，非必要调试日志采用截断/脱敏/散列；
- 日志最多保留有界数量的行为可保留，但需显式标记 `truncated`，避免将截断统计为真实 0 次；
- 不要把 `SOURCES` 中写着的“专利记录/图书馆资料”等描述当成已经具备特殊私有 API 授权。

## 3.3 `/api/plan` 必须区分计划与实际

当前 `underbox/serve.py::_plan()` 在报告生成后**重新调用** `plan_with_notes()`，可能无法还原运行时的协作线索、专项 `refine_plan()`、实际改述和预算消耗。

要求：

- 把**当次运行时**真实的计划（及删减、执行、改述、失败统计）安全地随报告保存；
- `/api/plan` 可以返回 `planned` 与 `executed` 两份信息，但不能把后来重新计算的方案冒充历史实际执行；
- 老报告无执行轨迹时显示“旧版未记录实际执行明细”，不伪造；
- UI 展示“计划查询”“实际调用”“失败与未尝试”分层，评委可看明白 agent 确实运行过哪些工具；
- 计划中的候选姓名、来源 URL 仅对当前授权用户展示。

## 验收

- [ ] 论文只有 `part` 和弱身份信号时，不向任职 Agent 交接“已核实作品”；
- [ ] 论文已经充分证明题名并有可回读出处时，可以交接**查询线索**，下游任职仍须独立取证；
- [ ] 上游失败不阻断下游独立核验，保留原有调度批次顺序；
- [ ] 不同用户、不同并发任务及嵌套任务的 `ContextVar` 互不污染；
- [ ] 对任一 `sourceAccess.returned` 事件可确认确实发起过受允许的 adapter 调用；
- [ ] `/api/plan` 回显运行时真实的已执行计划，不声称事后生成的 plan 是事实；
- [ ] 旧 `verificationWorkflow` 报告仍能加载、导出和历史回看。

---

# P0-4：检索结果不确定性、错误原因、覆盖度

主要文件：

```text
open_box/app/collect.py
open_box/app/search.py
open_box/app/data_sources.py
open_box/app/schema.py
open_box/app/pipeline.py
NEW open_box/app/coverage.py
underbox/serve.py
underbox/index.html
NEW open_box/tests/test_retrieval_summary.py
```

将 `status`（事实状态）与 `retrieval_status`（是否真实完成核验）解耦，不必改掉 `ok/part/ask/none/who` 旧状态。

建议新增：

```text
retrieval_status:
  complete | partial | provider_error | blocked | timeout |
  budget_exhausted | not_attempted | canceled

queries_planned / queries_attempted / query_successes
pages_attempted / pages_read_success / page_failures
verified_evidence_count / unverified_lead_count
covered_elements / selected_elements
limitations[]（结构化错误 code + 温和说明）
```

要求：

1. 检索 API 401/402/429/5xx、超时、内容审核拦截，不能统一成“没有结果”；各供应商状态类型准确映射。
2. 官网 robots/403/验证码/附件解析失败与“真正成功打开名单，名单没有候选人”不同。
3. 已经证明的要素不因部分搜索失败被清空，但未完成范围必须展示。
4. 任务正常完成而确实没找到可关联证据，才显示“暂未找到相关公开记录”；`retrieval_status != complete` 时应醒目标注“本次核验未完成 / 部分来源不可访问”，不能给候选人负面判定。
5. `coverage.py` 输出**本次实际选择核验的要素被合格证据覆盖的比例和分类统计**，不是候选人“诚信 87 分”。跳过项、未勾选项、实习人工背调要单独展示/排除正确。
6. 从 `data_sources.AgentRun` 和 `Budget` 生成统计；对未绕过包装器的所有调用都有记录，不许把 failed 记为 empty。
7. 报告、JSON、Markdown、SSE、历史报告以及 UI 使用同一套统计契约；统计定义写文档并有可重现计算的测试。

测试：全部供应商失败、部分成功、预算 0、只有摘要、官方附件无法回读、明示 `SKIP_CATEGORIES`、实习人工背调、用户取消、正常零结果。

---

# P0-5：公开 URL / SSRF / 文件解析 / 多用户临时态安全加固

主要文件：

```text
open_box/app/homepage.py
open_box/app/search.py
open_box/app/source_guard.py
open_box/app/roster_archive.py
open_box/app/public_documents.py
underbox/serve.py
underbox/accounts.py
underbox/store.py
NEW open_box/app/safe_http.py （或结构相近的统一边界）
NEW open_box/tests/test_safe_http.py
NEW underbox/tests/test_prepared_ownership.py
NEW underbox/tests/test_request_limits.py
```

## 5.1 统一安全出网层

已有 `homepage.normalize_url()` 对本地 IP 等字面形式做了限制，但这**不足以防止外部域名解析到内网、以及公网 URL 跳转到内网**。而 `PageFetcher.get()`、robots 和个别公开 POST 实际用了 `follow_redirects=True`。

要求：

- 只接收合法 `http/https`，拒绝带用户信息、奇怪端口、不可接受的 URL 主机；
- 在连接前检查**实际目标 IP**（IPv4/IPv6、本机、RFC 私网、链路本地、保留地址、云 metadata）；
- DNS 校验与连接目标一致，防重绑定；若当前代理/fake-IP 环境无法在应用层完整保证，必须结合**出网代理/防火墙策略**，不可用假安全检查替代真实约束；
- **关闭无条件自动跳转**，按白名单策略逐跳校验，每一步遵守域名、robots、限制、速率和出网边界；
- 对站点重定向后的最终 URL 做真实目的地和返回内容校验；
- 下载与解压**流式字节上限、压缩膨胀、文件深度、总成员数、PDF/旧 Office/压缩名单大小**受限，保留已有可用附件解析；
- 不会因为新增安全控制破坏虚构 `.example` fixture；采用**注入 Fake Transport / 假 DNS 解析器**测试；
- 对不确定的站点访问限制如实标记 `blocked`，不要绕过登录、付费墙或验证码。

## 5.2 临时 token 和账户身份

`/api/verify/stream` **已有** owner 校验，但 token 在 `_remember_prepared()` 创建时没有同时绑定服务器端用户，`_sse()` 处理拆分 `done` 才写 owner，此外只有最多 6 条的内存淘汰。

改为：

- 在**创建时**绑定可信 `owner_id`、`created_at`、`expires_at`、`job_id`、`consumed_at` 等；
- 服务端执行时再二次校验 owner，不只在请求入口做检查；
- 支持 TTL、显式清除与合理的**每用户**配额，不让一名用户发起大量拆分就迅速挤掉其他用户 token；
- 同用户两个并行任务不能将报告/用量/历史串台；退出查询 generation 变更后旧 SSE 不应覆盖新任务；
- SSE 断开后必须有准确的拆分结果清理/回收策略；已过期 token 返回明确可恢复提示；
- 网关必须覆盖客户端传来的 `X-Underbox-User`；共享 worker 只能通过可信网关访问，必要时加入内部通信认证以防其他本机进程伪造；
- **继续使用现有 SQLite 账户、最近十次历史、用量/配额结构**，不得改为每用户独立 Python 进程。

## 5.3 请求、Cookies 和权限

- `underbox/serve.py::_read_json()` 要有独立长度上限、合法 JSON object 校验，并正确响应 400/413；不能只依赖网关。
- 网关与工作进程对文件上传均有硬限额、实际文件类型检测、安全异常码；防畸形长度、重复传输/慢请求、恶意 ZIP/DOCX/PDF。
- 只有明确资源白名单可以被静态提供；普通模式和网关模式都不准获取源码、SQLite、密钥、备份或用户报告路径。
- 在受信任 HTTPS 部署下 Cookie 带 `Secure`、`HttpOnly`、合适 `SameSite`；不能盲信客户端伪造的 `X-Forwarded-Proto`。
- 复查 CSRF 与授权：导出、历史、账户、API Key、用户数据清除/复核操作均执行服务端归属校验。
- 报告及搜索日志中的个人信息采用最小化记录、短保留期、访问控制和清理；不要无授权长期持久化完整页面快照。
- 用户明确请求“清除本次材料/历史”时必须真实删除服务端相应记录与内存缓存，且不波及其他账户；生产级备份清理/加密若无法完成需诚实写明。

测试：直连 localhost、`169.254.169.254`、IPv6 loopback、域名解析私网、公网跳转私网、DNS 重绑定模拟、超大下载、ZIP 膨胀、上传过大、跨用户 token、token 超时、登出后历史、任务取消后的隔离。对这些测试不得发真实攻击网络请求，只用本地 Fake Transport/隔离环境。

安全参考：<https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html>。

---

# P0-6：实习人工背调和企查查联系方式的隐私边界

**不是新增这个功能——它已经存在。目标是让它真实、审慎、可用。**

主要文件：

```text
open_box/app/internship.py
open_box/app/data_sources.py
open_box/app/schema.py
open_box/tests/test_verification_agents.py
underbox/serve.py
underbox/index.html
underbox/tests/test_manual_review_contract.py
underbox/tests/test_manual_review_frontend.cjs
```

要求：

1. 保留 `internship` 默认**人工背调**、`status=none`、`needs_human=True`、`evidence=[]`、`proved=[]`；支持用户显式 `SKIP_CATEGORIES` 的现有行为。
2. 公司必须核对**完整法定主体**，品牌、子公司、分公司、相关企业不能无条件当成同一主体。现有 `firm_url`、`_page_company` 继续复用但补测试。
3. 公开电话号码仅能作为**公司公开联系电话候选线索**，不能称为“人力资源专线/官方背调电话”。额外检查号码是否位于公司主体的“联系电话”语义位置，不读取客服、关联企业和他人私人号码。
4. 移动电话号码即使出现在页面也可能是个人隐私；默认不做个人号码画像或输出未经确认的个人直联电话。
5. 用户只看到公开联系信息及原文、读取时间、来源状态；不能自动拨号、自动发送信息、批量联系企业或推断招聘背景调查许可。
6. 企查查若需要登录、验证码或遇到机器人限制，不绕过，不把“空电话”写成“公司不存在”，保留人工查询链接与限制原因。
7. 增加明确的**候选人告知与合理授权**及删除/更正流程设计；在未取得相应处理依据时不能向第三方发送候选人姓名及简历。
8. 可选加入人工填写的**复核结果**，但必须区分“联系过公司”和“已取得可靠、经授权核验的本人岗位/时间证明”，不可自动升格为公开来源 A/B 级证据。

测试：同名不同公司、分公司误认、站点客服电话混入、掩码号码、敏感个人手机号、限制访问、公开公司电话可用但本人履历为未知、`SKIP_CATEGORIES` 优先级、JSON/Markdown/历史兼容。

---

# P1-1：Evidence Passport（证据护照）UI 和可审计报告

在已有 `underbox/index.html` 的“分拣台/台账详情/来源卡”**增量改造**。

核心体验：

1. 对每条陈述显示原文、核验目标要素、各要素当前证明状态；
2. 点具体要素，展开该要素**实际支持的证据卡**，带原文高亮/精准定位、发布主体、来源等级、身份锚点、时间、抓取时间、是否成功回读、是否转载；
3. 有候选人自述、弱身份、OCR、搜索摘要的条目，标“仅作线索/待人工核对”，不可用醒目的绿勾暗示自动证实；
4. 对 `A 级获奖 + D 级队长` 演示案，UI 必须明确“奖项可证明、队长未经充分证明”，整体不能是 `ok`；
5. 一眼能看到检索覆盖度、失败原因与未执行部分；
6. 可看到每个专项 Agent 的真实 `sourceAccess` 和当次运行计划，以及 `navigation_only` 协作来源；
7. 对已有 `manualReview` 渲染继续区分“待人工背调”，不能混进普通 `evidence`；
8. 渲染外部 URL 统一白名单/安全协议检查；不把第三方原文或网页 URL 拼进危险 `innerHTML`。检查 `renderGithubAudit`、比赛官网外链等已有路径；
9. 保留 FLIP 动画、PDF/网页阅读视图、登录/历史、窄屏移动端、MOCK 标识、五态颜色与现有产品语言风格。

后端同一份证明结构需进入：

```text
Report JSON
/api/report 和 /api/history
/api/plan 实际轨迹
JSON 导出
Markdown 导出
报告问答的可信材料构造
```

**兼容性**：历史报告没有新字段时正常展示“旧版本未记录完整证据护照”；不能对旧报告伪造逐字定位或改变当时结论。

验收：从合成简历上传/主页输入 → 拆分 → 勾选 → 核验 → 点开要素 → 证据定位 → 导出 → 回看历史，在桌面/窄屏/手机真实浏览器下无布局问题。无浏览器环境时明确未测，不得用 JS 字符串断言冒充完成 UI 验收。

---

# P1-2：人工复核与候选人补证的最小闭环

**注意：当前已有“实习人工背调入口”，不等于已经有可信的复核案件工作流。**

沿用 `underbox/store.py` 的 SQLite 迁移机制，新增足够小的复核工作流：

```text
自动核验发现待澄清/身份未知/部分证实
→ 工作人员发起复核任务
→ 记录候选人补充说明、来源类型、证明材料索引
→ 由有权限的人进行人工审查
→ 记录理由、时间、操作者、证据来源与结果
→ 原始自动结论保留，产生复核后版本
→ 报告、历史和导出可看到版本差异
```

要求：

- 不允许“人工收到材料”直接改写自动 `status=ok`；
- 保留 `automated_verdict` 与 `review_outcome` 两个清晰维度；
- 只有管理员或授权复核角色可做决定，普通候选人只可提交本人的材料和解释；
- 可先实现**工作人员录入候选人补证**，不要在未做好细粒度权限时急于开放公共候选人链接；
- 真要做候选人专用链接，则必须是限时、一次性、作用域限定到某个 case 的 token，并具有撤销能力；
- 每次复核留下 `report_id / claim_id / action / reviewer_id / source / reason / timestamp / version`；
- 不自动做招聘录用决定，不输出候选人“造假”标签；
- 测试跨账户访问、权限提升、重复提交、过期链接、删除和历史版本回放。

---

# P1-3：UnderBox-Bench ——让竞赛评委看到可测量的算法贡献

建立**独立、完全离线可重放**的事实核验测试集与评估脚本，而不是把现有几百条单元测试直接包装为真实核验准确率。

目录建议：

```text
open_box/bench/
  README.md
  schema.py
  fixtures/
  cases/
  baselines/
  run_bench.py
  metrics.py
  results/  # 只提交脱敏的可公开汇总结果
docs/optimization-v2/benchmark.md
```

## 数据集与标注

建议目标规模 **120 条**（是建议，不是仓库已有事实；第一轮可以先做小批高质量样本）：

- 60 条正常样本：学历不同学段、正式获奖名单、论文和作者、专利状态、著作责任者、任职、学生工作、公开项目等；
- 60 条对抗样本：六类各 10 条：同名身份混淆、跨等级支持拼接、年份/赛段错位、同源转载、网页/OCR/搜索不可用、提示词注入与捏造摘录。

每条样本至少包含：

```text
case_id, synthetic=true, claim, elements, identity_context
pages_or_structured_records, retrieval_outcome
gold_element_proofs, gold_allowed_sources
gold_rejected_sources_and_reasons
gold_status, gold_retrieval_status
adversarial_tags, evaluation_notes
```

标注规则应明确：**状态猜对但证据引用错人，算错误；判断为未知且依据充分，不能一律算失败。**

## 指标（都给出公式、分母、置信范围或样本量）

1. Element-level Precision / Recall / F1；
2. **Unsafe-OK Rate**：金标准并非“全项可证”的案例中被错误判 `ok` 的比例；
3. Same-name Misattribution Rate：把同名他人的事实错归候选人的比例；
4. Quote Grounding Rate：需要原文的证据中，成功精确定位的比例；
5. Evidence Sufficiency / Attribution：已证实要素的来源确实充分、确属本人；
6. Selective Risk-Coverage：不同人工复核阈值对应的自动覆盖与危险错误；
7. 延迟、查询次数、成功读取数、预算消耗、调用失败率；可选 token 成本，必须有真实计量。

## 基线与消融

- Baseline A：仅通用 LLM 判定（仅有**合法授权模型凭据**才运行）；
- Baseline B：LLM + 简单网页检索；
- Baseline C：冻结的当前 UnderBox 规则（留版本/标签/可重放输入）；
- Baseline D：本次改良版本；
- 消融：分别关闭**身份绑定、逐要素等级约束、原文校验、同源去重、跨 Agent 线索信任门槛**。

没有模型凭据时必须如实标成“LLM 基线未执行”，先交付离线规则实验，**不能凭 synthetic fixture 推出真实招聘场景 98% 准确率**。

交付：

- 一键命令可跑所有离线样本；
- 可重现的 JSON/CSV 结果和清晰说明；
- 至少三组真实计算的对比图或表（英文图表标签亦可，报告解释用中文）；
- 失败案例解释，包含改进与副作用；
- 明确区分单元/合同测试、合成端到端 Benchmark、真实来源外部验证。

---

# P2-1：修复报告问答多模型不一致，并增强安全的流式输出

主要文件：

```text
open_box/app/llm.py
open_box/app/api_config.py
underbox/serve.py
underbox/accounts.py
underbox/tests/test_usage_streams.py
NEW underbox/tests/test_chat_providers.py
```

现状：前端核验使用可配置模型；但 `underbox/serve.py::stream_answer()` 仍读取 `DEEPSEEK_API_KEY` 并请求 DeepSeek 固定端点/model。管理台切换模型供应商未必作用于报告问答。

要求：

- 统一 `LLM_PROVIDER`、`LLM_ENDPOINT`、`LLM_MODEL`、`LLM_API_KEY` 与现有供应商预设；
- 复用既有 `OpenAICompatLLM` 的配置/错误映射与用量统计，增加小而清晰的 `stream_answer` 适配层；
- 支持 DeepSeek、智谱、通用 OpenAI-compatible 形态；不支持流式的供应商给出明确降级策略；
- `stream_options`、`thinking`、token 计费字段按不同供应商适配，不把某一家参数发给所有供应商；
- 使用受信任的报告结构构造问答上下文，第三方原文必须标记为**引用数据而非系统指令**；不允许问答擅自升格事实或泄漏其他用户报告；
- 保留 SSE `delta / done / error / usage` 及浏览器取消、账户用量统计；
- 服务提供方返回的原始错误正文不应直接泄漏授权头或敏感字段；
- 仅使用本地 fake HTTP Server 进行多供应商、429/401、空 delta、突然断开、计费归属测试。

---

# P2-2：性能、检索策略与结果可解释性（必须先有 Benchmark）

现有实现已有：

```text
CLAIM_WORKERS
COLLECT_WORKERS
LLM_CONCURRENCY
SEARCH_MIN_INTERVAL
按类别检索策略预算
学校官网同域限速
预算提前停止/异常处理
```

**不要又加一套互不兼容的 Agent 并发池。**

在不降低正确性的前提下尝试：

1. 每拿到一份证据就计算当前**未被充分证明**的要素，停止继续搜索只能基于可靠证明；
2. 同源转载反复出现时，降低重复抓取和无效模型调用；
3. 身份歧义时优先搜姓名+学校+届次+具体作品，而非只做姓名泛搜；
4. 实习继续独立人工入口，不因其它 Agent 有线索就自动搜索候选人实习；
5. 异常/可访问性低的站点保留回退，但确保 robots/限流/最小请求边界；
6. 通过 Bench 比较“每条成功且可靠核验的平均搜索次数、P50/P95 延迟、Unsafe-OK、人工复核率”；没有结果则不声称节省费用。

---

# P2-3：华为 ICT 技术结合与部署工程（可选扩展）

**没有授权云账号时，只做可执行的适配器和离线契约测试，不虚构“已接入成功”。**

- 保留 UnderBox Python 核心判定，与可配置的华为云兼容模型推理分层；
- 利用已有 `openai-compatible` 形态提供示例配置与可注入的 fake 服务，**不要写死某个可能变动的华为云 Endpoint / 模型名**；
- 方案文档将 OBS（证据/报告受控存储）、RDS（业务状态）、IAM/KMS/审计（权限与密钥）区分“规划/代码适配/真实验收”，不要将规划冒充现有实现；
- 可用合成资料做整套流程演示；如无实际账号，只展示 mock/本地版本并明确标识；
- 真部署前要求 HTTPS、加密存储、出网限制、个人信息告知、访问控制、保留期限、密钥托管和日志审计；
- 提供 `docs/deployment/huawei-cloud.md`、`docs/optimization-v2/architecture.md`；
- 不将开源演示数据或用户真实简历无授权上传第三方服务。

---

# P2-4：开源成果、文档和 AIC / 华为竞赛交付

主要文件：

```text
README.md
CHANGELOG.md
REGRESSION.md
AGENTS.md
requirements.txt
dsh-resume-verify/README.md
docs/optimization-v2/...
NEW THIRD_PARTY_NOTICES.md
NEW SECURITY.md / PRIVACY.md（可按实际归档）
```

1. 统一文档中的**实际十三类 Agent、企查查实习人工入口、账号历史、用量 SSE、协作边界与“覆盖度”**；避免还写“实习默认不核验”“只能直接用 DeepSeek”等过期描述。
2. 补齐完整可复现安装说明、可选 OCR/Playwright/旧 XLS/系统工具、Node 版本、完全离线运行指令和非收费 demo。
3. `REGRESSION.md` 精确记录旧失败处理；如某个旧失败被真正修好，应修改 `KNOWN` 并保留过程说明，不能一口气把全部失败签名放行。
4. `THIRD_PARTY_NOTICES.md` 列出第三方框架、模型、搜索 API、图标、测试数据、网页材料、许可证/服务条款及自主改动。
5. 当前仓库根目录未发现标准 `LICENSE`：**先核对项目权属/第三方许可证，生成许可证选型说明并请作品权利人确认，不得 Agent 擅自选 MIT/Apache/GPL 并宣称已有授权**。
6. 建立三个完全虚构、可离线重放的演示故事：
   - 官网确实证明一名候选人的某个具体奖项；
   - 同名/跨等级拼接陷阱，UnderBox 不乱认；
   - 官网不可访问或实习待人工背调，系统明确不下负面结论并提供补证流程。
7. 产出 AIC 技术报告可用的“系统架构、原创贡献、算法原理、对照实验、消融、限制、许可清单”材料；与华为 ICT 的“用户闭环、实际使用路径、ICT 技术结合、安全架构”材料分开组织。
8. 对 AIC 报名/材料规则以官方页面为准：<https://www.aicomp.cn/tracks/tracks-5/4924.html>。**基于 2026-10-13 查询的官网版本，报名和初赛作品截止为北京时间 2026-10-30 20:00**；不要为了实现 P2 耽误 P0、Bench 与完整演示的提交。
9. 参考 GitHub 官方许可说明：<https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository>。

---

# 2. 统一验收矩阵（必须写真正可执行的测试）

请新增/加强离线用例，推荐覆盖下列场景；名称可调整但不可只用静态字符串断言代替行为：

| ID | 反例 / 正例 | 关键预期 |
|---|---|---|
| T01 | A 级网页仅证实奖项，D 级网页仅提到队长 | 不能整条 `ok`，要素分级独立 |
| T02 | `时间吻合` 重复 10 次，或重复“发布方相关” | 不增加独立身份锚点 |
| T03 | 多条同名不同学院/单位/届次证据 | 不跨人合并，必要时人工 |
| T04 | 同一官网公告多处转载 + 假 origin_url | 不制造多源证明或错误源级别 |
| T05 | 非竞赛类别里 LLM 胡编原文 | 引文校验失败，不能作为自动证明 |
| T06 | OCR/搜索摘要与 A 级无关页面混合 | 不误给完整证实 |
| T07 | 本人 A/B 独立证据共同覆盖合法必要要素 | 允许 `ok` |
| T08 | 论文 Crossref/OpenAlex 的可信结构字段 | 旧版可用合法元数据仍可用 |
| T09 | 论文共同一作/发表状态/年份被过度推断 | 不能升级 |
| T10 | 奖项错级别/赛段/公示阶段/队长身份 | 不错认 |
| T11 | 前一论文弱支持转给任职 Agent | 不产生“已核实”导航锚点 |
| T12 | 已充分证实作品→下一 Agent 导航 | 可以检索但不借证 |
| T13 | 有权限的 Agent 调工具和无权限的方法直通 | 真实源访问受允许范围约束 |
| T14 | `/api/plan` 的实际执行与协作补查询 | 回显真实已执行轨迹 |
| T15 | 搜索 API 401/402/429、超时、内容审核 | 不伪装正常 0 结果 |
| T16 | 官网 robots/403/PDF 无可读正文 | 显示检索受限，不等于候选人无奖 |
| T17 | 检索成功但无匹配 | 可中性 `none`，覆盖度正确 |
| T18 | 企查查页提供公司公共电话 | 仅人工联系线索，候选人要素均未证明 |
| T19 | 公司客服电话/掩码/私人手机号/分公司 | 不自动作为对应法人电话 |
| T20 | 实习显式 `SKIP_CATEGORIES` | 不进入人工背调预算 |
| T21 | A 用户持有 B 的 prepared token | 不可读取、核验、导出 B 的材料 |
| T22 | token 超时、SSE 中断、并发勾选/核验 | 不串任务，资源及时回收 |
| T23 | 直接访问内网/metadata、重定向内网、DNS 重绑定 | 请求在真实网络连接前被拒 |
| T24 | 超大 PDF/ZIP/JSON、非法路径 | 有界处理，不泄露源码、不 OOM |
| T25 | 账户历史最近 10 次、登录/登出/清空当前查询 | 老契约保持 |
| T26 | 不同账户搜索 API Key / 用量 / SSE | 隔离，不串账单 |
| T27 | 恶意网页内容、外链 `javascript:`、XSS 载荷 | 不执行、不污染事实判定 |
| T28 | 报告 JSON、Markdown、旧 fixture、dsh 插件 | 新旧协议能兼容 |
| T29 | 真实浏览器桌面/900px/390px | 新 UI 能交互且没有遮挡 |
| T30 | Bench 合成样本 + 金标准重放 | 指标可重复，不捏造结果 |

不要仅给用例名单。为每个测试建立合成输入、可调用目标函数、明确断言，并让失败在修复前能真实复现。

---

# 3. 编码节奏：四个 Gate，不要一次性推倒重写

## Gate 0 · 基线

- [ ] 阅读 `AGENTS.md`、`REGRESSION.md` 和本 prompt；核对 git 工作区 / snapshot。
- [ ] 完整执行基础回归，区分缺依赖、旧失败和新失败。
- [ ] 新建 `docs/optimization-v2/00-baseline.md`。
- [ ] 根据当前代码确认所有问题仍有效；若某项已经修好，要引用对应实现与测试，不要重复开发。

## Gate 1 · 核验可信性（**最先交付**）

- [ ] **J-01、J-02 先写红测试，再修 `judge.py`**；
- [ ] `ElementProof`、身份约束、来源独立性、逐要素证明；
- [ ] 全类别原文校验与结构化 API 单独证明路径；
- [ ] 已核实线索只从合格 ElementProof 产生；
- [ ] 所有旧身份/学段/年份/奖项测试不放宽；
- [ ] `make test` + 新增测试记录真实结果。

## Gate 2 · 安全与运行透明度

- [ ] 实际数据源调用、拒绝原因、执行计划与结果统计一致；
- [ ] `retrieval_status` 和覆盖度；
- [ ] token 创建即绑定账户 + TTL + 并行隔离；
- [ ] PageFetcher/robots/重定向 SSRF 防护和下载上限；
- [ ] 实习联系方式与个人隐私边界；
- [ ] API、账户、历史、配额、SSE 集成测试通过。

## Gate 3 · 可展示成果 + Bench

- [ ] UI 证据护照、搜索状态和协作链可点开；
- [ ] JSON/Markdown/历史一致；
- [ ] UnderBox-Bench 真的可以离线跑并输出真实指标；
- [ ] 至少一个人工复核的最小端到端闭环；
- [ ] 三个合成 Demo、桌面/窄屏/手机 UI 检查；
- [ ] README、第三方清单、参赛文档更新。

## Gate 4 · 可选的模型、性能、华为云扩展

- [ ] 多供应商报告问答兼容；
- [ ] 对自适应检索做有指标依据的优化；
- [ ] 华为云接口适配与文档（无授权凭据就不进行实际付费测试）；
- [ ] 安全、隐私和开源许可最终审查；
- [ ] 如 P0/P1 仍有缺口，**不要为了展示云部署忽略核心错误**。

**开发节奏原则**：每个 Gate 都形成一组可单独审查、可回滚的代码变更；如果做不完所有 Gate，完成最高优先级的稳定子集，明确剩余工作，不要把半成品混成“全部完成”。

---

# 4. 交付规范：最终必须给真实修改和清晰报告

请真正提交修改后的**代码 + 测试 + 文档**。最终总结必须包含：

## 4.1 哪些内容改了

- 所有新增/修改的相对路径；
- 每个问题的“旧行为→新行为→为什么修复”的解释；
- 主要类、函数和 API 契约变化；
- 对旧 JSON、五态、dsh 插件、Mock、SQLite、前端的兼容性与迁移方案；
- 是否触及数据库 schema、如何迁移、如何回滚；
- 哪些是已实现、哪些只是建议/设计文档。

## 4.2 真实测试表（不要填估计）

| 项目 | 命令 | Passed | Failed | Skipped/Blocked | 说明 |
|---|---|---:|---:|---:|---|
| 统一回归 | `python3 scripts/regression.py` | 实测 | 实测 | 实测 | 附 summary 路径 |
| J-01/J-02 规则安全测试 | 对应测试命令 | 实测 | 实测 | 实测 | 修复前后对照 |
| 新增 Provenance / Handoff / Retrieval | 对应测试命令 | 实测 | 实测 | 实测 | 逐功能 |
| SSRF/跨用户/API | 对应测试命令 | 实测 | 实测 | 实测 | fake transport |
| 前端脚本 | 两个 `.cjs` | 实测 | 实测 | 实测 | 不替代真实浏览器 |
| 浏览器实操 | 真实 Playwright/浏览器 | 实测 | 实测 | 实测 | 宽屏/窄屏/手机 |
| Bench | `run_bench.py` | 实测 | 实测 | 实测 | 样本数与指标 |
| 真实模型/搜索/华为云 | 用户授权后才运行 | 实测 | 实测 | 未授权则未测 | 不捏造 |
| 线上部署 | 用户授权后才进行 | 实测 | 实测 | 默认未部署 | 不触碰线上 |

## 4.3 需要生成的成果

```text
docs/optimization-v2/00-baseline.md
docs/optimization-v2/CHANGELOG.md
docs/optimization-v2/architecture.md
docs/optimization-v2/benchmark.md
docs/optimization-v2/validation-report.md
docs/optimization-v2/known-limitations.md

# 以及实际可运行的新增代码和测试：
open_box/app/provenance.py         # 名称可以按现有风格调整
open_box/app/coverage.py
open_box/bench/...
open_box/tests/test_element_proofs.py
open_box/tests/test_provenance.py
open_box/tests/test_handoff_trust.py
open_box/tests/test_retrieval_summary.py
open_box/tests/test_safe_http.py
underbox/tests/test_prepared_ownership.py
underbox/tests/test_chat_providers.py
# 等
```

验收报告必须写明无法复现的边界、对合法召回率的潜在影响，以及**哪些指标仅来源于合成数据**；不得使用含真实履历的信息截图、日志或开源测试样本。

## 4.4 最后逐项回答

1. A 级证据证明奖项 + D 级证据证明队长，系统现在怎样判？逐要素依据是什么？
2. 重复的弱身份信号能否累加为 `self`？
3. 每条 `ok` 的每个必要要素有没有同一主体、可回读的合法证明？结构化 API 是否有不同但严格的校验？
4. 跨 Agent 的 confirmed anchor 是否来自真正已证实的要素？后续是否仍独立取证？
5. `sourceAccess` 是否代表真实执行而不是仅计划？`/api/plan` 能否准确回放？
6. 搜索供应商故障、官网被拒访问、预算耗尽时是否会误展示“没有公开记录”？
7. 实习联系人是否只作为人工线索、没有泄露个人号码或自动证实实习？
8. A 用户能否取得 B 的 prepared token、历史/导出、问答上下文或模型密钥？
9. 每一次外部请求和重定向是否受实际网络目标/资源上限约束？
10. 登录、历史、近 10 次淘汰、配额、SSE、PDF、主页、GitHub、MOCK、五态、dsh 插件是否回归通过？
11. Bench 的指标哪些真的运行了？哪些尚未测？样本中是否全部为合成/可授权的素材？
12. 有没有改动/覆盖线上环境、私有数据库或 `.env`？是否得到显式授权？

若有任意一项未通过，必须报告失败用例、原因、后续最小修复操作；不允许只回复“所有优化完成”。

---

# 5. 最终执行命令（Agent 读到此处立即行动）

**现在开始对当前仓库进行真实编码。**

请先读取根目录 `AGENTS.md` 与 `REGRESSION.md`，检查文件和工作区，运行基线，再**先为 J-01 和 J-02 创建会失败的测试**；修复逐要素证据判定，保证老五态、现有十三专项和所有真实业务边界不回退。随后依次完成证据原文溯源、跨 Agent 可信线索、检索状态、token/HTTP 安全，最后实现证据护照、UnderBox-Bench 和最小人工复核闭环。每个 Gate 都执行对应测试并记录真实结果。

如任务较大，**不要只给长计划**：务必先完成能独立落地的最高优先级代码改动。除所有者确认许可证/产权、生产部署、真实付费 API、私人数据处理等需授权行动外，小范围安全的离线开发和测试可自主完成。

成功标准不是“Agent 数量更多”，而是：**每个结论都可解释、每个证据都可追溯、每个身份都经约束、每个未知都如实标明、每个提升都有可复现实验。**
