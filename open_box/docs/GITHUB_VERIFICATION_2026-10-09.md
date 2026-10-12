# 项目强主张与 GitHub 贡献核验（2026-10-09）

已在现有版本上实现并部署，不覆盖之前的学历学段、奖项独立取证、账户、历史记录与退出查询清理。此次保留线上最新登录动画，同时恢复另一次旧前端上传丢失的历史、导航和清理逻辑。

## 使用

上传并拆分后，在对应项目条目展开“补充候选人主动提供的 GitHub 材料（可选）”。可逐条填写候选人本人 GitHub 账号主页、该项目的仓库/PR/commit 链接；每行一个，最多 2 个账号、6 个关联项目链接。材料只作用于对应条目。

两个触发条件必须同时满足：实际项目/开源贡献陈述，以及与该条明确关联的 GitHub 项目链接。个人主页链接单独存在、熟悉 GitHub、只有模型推断的 repo/账号、一般简历，都不启动 GitHub API、diff 或贡献分析。原始输入中的链接必须在该项目原文或能唯一关联该项目的段落里，不能传播到所有项目，更不能按姓名猜账号或搜索其他仓库。

账号归属基于候选人主动标明的本人账号及 API 账号/记录作者对应，未做自然人身份登录认证。未提供本人账号时停止贡献分析。组织/机器人账号不直接当作个人身份。未验证签名的 commit 作者关联仅作元数据线索，不单独证明本人提交；可补充本人 PR 或署名材料。

## 取证与判定

- 原文保留，分别提取事实锚点、本人参与、个人角色、工作范围、修饰语、量化指标。普通就读、项目数量或存在、Star、PR 数、行数不能证明主导、核心贡献者、独立开发、自研或个人能力。
- GitHub API 只读取输入关联的公开仓库及其 PR/commit。明确 PR/commit 链接优先；仓库链接则在该仓库内分页取有限的账号对应记录，不枚举用户的其他仓库，不读社交、关注、Star 用户或私有活动。
- 记录自己的仓库/外部组织仓库、fork、PR 状态及合并状态、变更路径、类型、原始 diff 摘录、路径领域线索。LLM 内容解读必须带匹配真实 hunk 的逐字 quote；其角色、评分、录用意见、supports 等输出不接入最终判定。
- 仓库元数据仍为 C。作者可与主动提供账号对应、文件列表完整且有原始 diff 的 PR/有效签名 commit，按新增可测试规则判 B；不是把所有 GitHub 内容升级。B 也不代表核心、主导或技术水平。
- “该 PR 已合并”与明确 API merged=false 可以构成具体冲突；单个文档 PR 不能否定整个人的架构经历。只有明确指向该 PR 的架构代码表述、完整文件列表都为文档时才报告限定到该 PR 的差异。
- 仓库存在不证明具体 PR/commit 存在。PR 元数据已确认但 diff 获取失败时保留 C 级线索与失败说明，不丢掉已取得的信息。
- 私有、404、限流、网络失败保持中性。403/429 后不继续网络重试；只说明本次可访问性，不能推断造假。项目角色、范围或指标没有证据时显示“尚无充分证据”，不生成无依据风险标签。

## 数据兼容

新增可选字段：Evidence.github_record、VerifiedClaim.claim_verification、VerifiedClaim.github_audit。旧报告缺失这些字段仍可读取。Web 报告映射为 claimVerification/githubAudit，接入当前 JSON、Markdown 导出和原有按账户最近 10 次历史存储。不增加新数据库表。未触发时不显示 GitHub 审计板块。

补充材料在后台任务中克隆准备好的陈述，不改变缓存原件；拆分 token 绑定当前账户，其他账户不能用该 token 补材料或核验。

## 修改文件

运行文件共 11 个：

1. open_box/app/claim_scope.py（新增）：强主张拆分、本人语句绑定、证明范围过滤。
2. open_box/app/github_audit.py（新增）：URL 校验、输入关联、受限公共 API、分页/限流、作者/diff、分级与证明规则、逐条补充材料。
3. open_box/app/schema.py：兼容旧报告的可选字段。
4. open_box/app/split.py：提取规则与原文链接绑定。
5. open_box/app/strategy.py：确定性双条件门禁、拒绝未授权的 repo/账号查询。
6. open_box/app/collect.py：按需路由，未触发时不调用旧的 GitHub 元数据探针。
7. open_box/app/judge.py：最终可测试规则、强主张证明限制、账号不明与访问失败区分。
8. open_box/app/pipeline.py：姓名绑定、元数据保留与报告集成，不为新审计增加面试出题。
9. open_box/web/report.html：规则层报告展示。
10. underbox/serve.py：补充材料验证、token 账户绑定、报告映射与导出。
11. underbox/index.html：逐项目材料入口、主张核对、贡献路径/类型/diff 与中性限制展示；保留当前已修功能和最新登录动画。

测试文件：新增 open_box/tests/test_github_verification.py、underbox/tests/test_github_contract.py；修改 test_search_recall.py，原来“仅有推断 repo 就访问 GitHub”案例改为必须跳过。保留既有账户、计量、学术等回归。

## 验证结果

- open_box unittest 354 项通过，其中新 GitHub/强主张测试 32 项。
- GitHub 服务契约/跨账户 token/导出/旧报告兼容 2 项通过。
- 现有 SQLite 账户/历史集成 3 项、真实 HTTP/SSE 并发计量 1 项通过。
- 总计 360 项相关 Python 测试，本地与服务器独立验证目录均通过。原仓库的旧 JSON/每账户进程 test_accounts.py 不兼容现行架构，原样保留；使用现行架构集成测试，不宣称所有旧测试均通过。
- 两个报告页面 JS 语法检查、既有前端并发计量/reset/历史/ID 检查通过。
- 合成样例的浏览器流程验证：逐项显示已支持/未证明，真实文档 PR 不证明主导/核心/性能指标，窄屏无横向溢出，材料按条目发送，退出查询后清理且保留登录。

## 边界

最多 20 次 API 读取，沿用该条流水线预算，单次 HTTP 最长 10 秒，分页最多 3 页；仓库最多 3 个、每仓库最多分析 5 条贡献，仓库列表只取有限页面。超过范围明确标记截断，不用采样数量当全部贡献。diff 只保存前 12 文件的有限片段，缺失/截断不会推断技术深度。

不自动核验组织权限、全项目主导/独立原创、完整维护者履历或私有开发，不执行仓库代码/基准测试，不能从代码推断性能百分比。无关联 URL 的主张可走原有公开取证，但不扩展 GitHub 调查。分支/tree、issue、release、短链接等暂不作为审计入口；仓库迁移不自动跨地址调查。未全面验证所有浏览器、所有大型仓库和复杂协作/迁移模式。

本次部署前备份及审核哈希位于 /opt/underbox/backups/github-claims-20261009-0255；before/ 保存旧文件，users-before.db 保存部署前数据库，目录 0700、数据库备份 0600。环境密钥和数据库结构未修改。

## 方法参考

参考 [resume-audit](https://github.com/shaokeyibb/anti-asu-skills/blob/main/skills/resume-audit/SKILL.md)、[github-audit](https://github.com/shaokeyibb/anti-asu-skills/blob/main/skills/github-audit/SKILL.md)、[project-check](https://github.com/shaokeyibb/anti-asu-skills/blob/main/skills/project-check/SKILL.md) 的事实/角色/范围分离和实际 diff 方法，没有安装或照搬其能力评级、怀疑标签或面试流程。API 行为对照 [GitHub PR files](https://docs.github.com/en/rest/pulls/pulls#list-pull-requests-files)、[commits](https://docs.github.com/en/rest/commits/commits#get-a-commit)、[限流说明](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)。
