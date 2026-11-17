# Underbox ICT 完整功能迁移

版本：`ict-full-20261116.11`。对应原仓库 `codex/huawei-ict` 分支，基线提交 `8113e225`；原应用目录 `open_box/`、`underbox/` 不作修改。迁移同时核对原仓库完整源码及服务器 `/opt/underbox` 的 90 份源码快照，补齐此前服务器快照未包含的原仓库能力。

## 功能对齐

| 原项目功能 | ICT 版行为与适配 |
| --- | --- |
| PDF、DOCX、TXT、MD 与扫描件解析 | 保留内容识别、原文预览、隐藏文字/注入检查；接回 DeepSeek 文件解析及 OCR，模型转写不能替换本地可读原文；旧 DOC 仅经支持该格式的解析器处理 |
| 个人主页输入 | 保留主页类型检查、robots、公开 URL 检查、正文拆分；与文件输入共用后续核验 |
| 画像、拆分、选择与并行核验 | 保留原策略档案、章节和类别；不限 8 项，14 项整批提交有离线回归；受 AgentArts 32768 字节摘录传输上限约束 |
| 各类经历专项核验 | 学历、奖学金、荣誉、学生工作、竞赛、论文、专利、会议、项目、开源、任职、教学、艺术、著作、实习；保留 ICT 交流活动分类 |
| 搜索与读页 | 主核验使用原智谱/GLM 搜索凭据与策略，ICT 原生读页；保留已修复的 PDF 表格行匹配、独立奖项取证、失败来源恢复、读页截止时间和筛选日志 |
| 身份、学校、学段和年份核对 | 同名不直接认人；不借邻行学校、其他作者单位、指导教师身份；现任单位排除及明确的本校用途例外继续保留 |
| 逐要素证明及来源溯源 | 补齐 ElementProof、原文引文定位、内容哈希、抓取时间、登记元数据及覆盖统计；模型不能自报通过取代规则；论文标题、年份和刊物独立证明 |
| 企业、院校辅助资料 | 公司介绍、登记信息、规模、公开电话/邮箱、学校专业与办公联系入口；只供人工核实，不证明本人实习、任职或学历 |
| GitHub 按需核验 | 仅对原简历明确关联项目接收本人账号及项目链接；补充材料按报告和勾选范围隔离，避免重复表单或错传 |
| 历史、报告回看与导出 | 每账户最近 10 份、JSON/Markdown；历史激活恢复匹配的原始报告与画像供问答，不重新搜索或重算旧结论 |
| 人工复核 | 管理员/授权角色在自己报告上发起、补充、决定；版本冲突检测、材料索引和理由保留；单独展示记录，不改自动结论、等级或云端回执 |
| Boxy 问答 | 保留流式回答、联网开关、可引用来源、语音识别/朗读、停止与复制；历史报告具备匹配上下文时也可问答；旧版缺上下文报告保留只读导出 |
| 账户、后台及统计 | 保留管理员/成员、账户切换、退出查询保留登录、退出登录、配额和申请、供应商配置、账户专属搜索凭据、用量与日志；ICT 会话和工作进程独立 |

## AgentArts 边界

工作台上传、拆分和选中事项后，AgentArts 调用已有 `RunUnderbox` 工具。服务器执行迁移后的原核验流水线，完整报告保存在服务器；仅在平台返回与该任务报告哈希一致的回执后交付成功报告。

- 工具线程继承账户、搜索凭据、用量及费用作用域；拆分结果保留账户归属、有效期和并发执行检查。
- 断线或平台本轮响应提前结束时只查询同一任务，不重复 submit；私有任务日志支持进程重启后恢复已完成结果。
- 后台结束后的实际用量单独持久化。同一任务恢复时更新同一条用量记录，成功报告和核验配额只提交一次。
- 原项目的模型及搜索适配器继续在服务器执行。AgentArts 负责云端编排和可校验的工具回执；两套调用时延和模型回答不承诺逐字相同。
- 保留已关闭的金额预算限制；搜索仍有防失控上限、来源读取限制和平台输入限制。历史报告不按新策略重跑。
- 比赛实时入口不把私有演示文件回放当作真实新核验。既有报告、QA 上下文和 API 密钥不迁移进 Git。

## 验证

从仓库根目录运行：

```sh
python3 full-workbench/scripts/regression.py
python3 -m unittest discover -s agentarts-adapter -p 'test_*.py'
```

完整回归将 core、原功能服务、ICT 服务、legacy 分别在无真实凭据的独立进程运行，另跑全部五套 Node 前端检查。新旧测试同时执行，不以跳过测试消除差异。

本次本地完整结果为 826/826 Python 用例，五套 Node 检查及 17 项适配器检查通过；另检查桌面、窄屏、手机布局，以及合成材料拆分/选择/回执/历史/复核/问答操作。工具用量显示的最后适配另跑了相关 Node 行为检查，服务器暂存版本再次执行完整回归。

原项目独立基线为 615/619，原有四项 legacy 签名仍存在：test_03、test_08、test_30、test_31。原项目没有为此次迁移修改；ICT 版使用完整原仓库契约并保留已修正的用例，四项在 ICT 版均通过。

全部自动化使用临时数据库、合成材料和模拟模型/云端，禁止付费 API。浏览器语音入口与后端合成音频已检查，未实录麦克风；本次没有重新提交成员真实简历，也没有测量新版本在真实公开资料上的召回率或新一轮云端费用。服务上线检查、数据完整性与截图记录见 `outputs/regression/full-migration-20261116/`。
# 2026-11-17: DeepSeek saved-record diagnosis

`ict-full-20261117.3` adds an explicit per-claim DeepSeek diagnosis action. It
reuses authenticated report QA, consent, SSE streaming and usage accounting. The
diagnostic request excludes raw resumes, other claims and private contacts;
only the selected verification scope, saved query samples, source failures and
screening counts are sent. Names are removed while boolean markers preserve
whether the original query/snippet contained the candidate name. Findings are
advisory and never rewrite evidence, proof states, receipts or reports.

The action forces web search off on both client and server. Ordinary QA retains
its existing optional search. DeepSeek is selected explicitly; a different
provider's endpoint or generic key is not reused for diagnosis. Repeated clicks
while the current answer is pending do not submit extra requests.

ICT query wrappers retain up to 12 query samples and 3 result summaries each,
with explicit truncation metadata. This sampling affects diagnostic context
only, and does not cap search execution. Older reports without saved summaries
can diagnose logs only; the UI and model instructions state that limitation.

Validation: 860 Python cases, 6 frontend suites, 17 adapter cases, synthetic
browser flows and separate user-requested manual DeepSeek diagnostics. Automated
regression remains offline and synthetic. Manual diagnosis reads an existing
report without new search or report mutation. Original-site baseline retains
its 4 existing failures. Artifacts: `outputs/regression/ict-ds-diagnosis-20261117/`.

# 2026-11-17: concrete source failure descriptions

`ict-full-20261117.2` replaces the ambiguous unfinished-search wording with
recorded causes: refused access/HTTP status, connection/read timeout, certificate
failure, attachment parsing failure, robots restrictions or unsafe destinations.
Rows show concrete causes; expanded details show counts, source examples and raw
saved reasons. Existing evidence conclusions remain intact. Historical reports
derive descriptions from saved records; missing reasons stay explicitly unknown.
Markdown exports use the same saved failure records, with no report rerun.

Future tasks retain failure totals independently of the 80-event display cap,
with up to 48 distinct reason groups and three sanitized source examples per
group. Truncation and unknown remainder counts are explicit. This changes source
recording and presentation only; it does not relax proof rules, change search
providers, install document readers or call paid APIs during verification.

Validation: 844 Python cases, 5 frontend suites and 17 adapter cases; synthetic
browser checks cover desktop, tablet, mobile and historical-report rendering.
Artifacts: `outputs/regression/ict-query-reasons-20261117/`.

# 2026-11-17: ICT quota override

`UNDERBOX_ICT_UNLIMITED=1` requires ICT mode. It disables monetary and account
verification quotas, search/read/recovery counts, per-claim duration, report-wide
search limits, and query-plan truncation by budget. GitHub request and pagination
quotas are disabled within the explicitly authorized project scope.

Stored account quotas are retained; the effective quota is unlimited. Usage,
cost estimates, cloud receipts, history and account isolation remain active.
Infinite limits serialize as JSON `null` with an unlimited marker. The UI keeps
current configuration separate from historical execution records. Private task
recovery expires one hour after completion, including for long-running jobs.

Provider balances/rate limits, per-request HTTP timeouts, file/context sizes,
authorized project boundaries, robots and evidence requirements still apply.
Finite query plans stop naturally when relevant work is complete; no repeated
queries are generated just to consume more calls. AgentArts foreground timeouts
do not cancel retained backend tasks; status recovery avoids duplicate billing.

Validation: 838/838 Python cases and 5 frontend suites passed; adapter 17/17.
Original baseline remains 615/619 with its 4 existing failures; original source
is unchanged. Tests use temporary databases and synthetic/mock services only.
No paid calls or real-resume reruns. Deployment/browser audit artifacts:
`outputs/regression/ict-unlimited-20261117/`.
