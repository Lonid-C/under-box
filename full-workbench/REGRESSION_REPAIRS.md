# ICT 28项回归失败的核对与处理

日期：2026-11-16。范围为独立比赛版`full-workbench/`。这28项是自动检查失败：19项核心检查（含同一测试内的5个子用例）和9项旧验收检查，并非28次线上核验失败。

## 分类

| 分类 | 原失败数 | 处理 |
|---|---:|---|
| 实际运行逻辑缺陷 | 1 | 搜到结果但读页额度为0时，未标记检索受限；现在只在有未读候选页时标记exhausted，无结果不凭空声称有未读页 |
| 缺失演示/测试资源 | 5 | 恢复历史离线演示HTML/CSS及虚构PDF样本、生成器；同一缺失文件导致多个用例失败 |
| 测试与现行契约或材料不一致 | 22 | 按已存在的白名单、实习人工背调、来源授权、原生联网问答与严格身份/年份取证规则修正样例及断言；不放宽生产规则 |

新增12项检查，包括白名单/伪装域/隐私、显式跳过实习、读页0预算有无结果对照、原生联网问答及兜底。保留原有功能覆盖；不使用skip或expectedFailure使失败消失。修复完成后移除比赛版回归脚本的5条旧失败白名单，后续同类错误会按真实失败报告。

## 原28项逐条对应

| # | 套件 | 原失败用例 | 处理依据 |
|---:|---|---|---|
|1|core|`test_award_routes.AwardRouteTests.test_english_plan_uses_its_own_site_and_relaxed_title_words`|查询保留原陈述明示的省赛级别，不能抹成不分赛级|
|2|core|`test_award_routes.SearchConfirmationTests.test_zhipu_confirmation_preserves_shared_engine_and_respects_existing_fallback`|验证官网确认后开放索引补查及域过滤，原引擎不变|
|3|core|`test_enrollment_search.EnrollmentCorroborationTests.test_source_school_roster_found_for_push_claim_proves_undergrad_school`|学历样例明确本科学段；获奖名单不能反向证明就读|
|4|core|`test_enrollment_search.EnrollmentCorroborationTests.test_third_party_list_naming_school_and_person_counts`|学历样例明确本科学段；获奖名单不能反向证明就读|
|5|core|`test_homepage.GateTests.test_non_homepages_and_privacy_pages_stop (answer={'is_personal_homepage': False, 'page_type': '新闻报道', 'reason': '这是一篇采访'})`|非白名单页面用于模型门禁测试；新增教育域白名单、伪装域及私人信息拦截检查|
|6|core|`test_homepage.GateTests.test_non_homepages_and_privacy_pages_stop (answer={'is_personal_homepage': True, 'page_type': '隐私汇编', 'privacy_risk': True})`|非白名单页面用于模型门禁测试；新增教育域白名单、伪装域及私人信息拦截检查|
|7|core|`test_homepage.GateTests.test_non_homepages_and_privacy_pages_stop (answer={'page_type': '其他'})`|非白名单页面用于模型门禁测试；新增教育域白名单、伪装域及私人信息拦截检查|
|8|core|`test_homepage.GateTests.test_non_homepages_and_privacy_pages_stop (answer='不是 JSON 对象')`|非白名单页面用于模型门禁测试；新增教育域白名单、伪装域及私人信息拦截检查|
|9|core|`test_homepage.GateTests.test_non_homepages_and_privacy_pages_stop (answer=RuntimeError('模型挂了'))`|非白名单页面用于模型门禁测试；新增教育域白名单、伪装域及私人信息拦截检查|
|10|core|`test_homepage.GateTests.test_personal_homepage_passes`|非白名单页面用于模型门禁测试；新增教育域白名单、伪装域及私人信息拦截检查|
|11|core|`test_homepage.SourcePageNotEvidenceTests.test_the_submitted_homepage_is_not_evidence_for_itself`|模拟第三方片段补上学校身份锚点，输入主页继续排除|
|12|core|`test_research_split.SkipInternshipTests.test_internships_are_split_but_not_verified_by_default`|按当前实习人工背调契约断言，保留显式跳过及不自动证明本人经历检查|
|13|core|`test_search_recall.RecallTests.test_structured_content_does_not_require_display_page`|奖项中的推断repo不构成GitHub授权，不应启动GitHub调用|
|14|core|`test_document_gate.DocumentGateTests.test_pdf_bytes_override_the_filename`|补齐带隐藏注入文本的虚构PDF样本，继续验证文件嗅探和读页|
|15|core|`test_institution_context.ContextAPITests.test_web_failure_is_visible_and_answer_still_streams`|显式测试旧问答兜底模式，另新增当前原生联网模式、失败兜底和报告不变检查|
|16|core|`test_institution_context.ContextAPITests.test_web_sse_order_credentials_and_report_immutability`|显式测试旧问答兜底模式，另新增当前原生联网模式、失败兜底和报告不变检查|
|17|core|`test_official_search.OfficialSourceTests.test_page_fetcher_extracts_text_from_public_pdf_roster`|补齐带隐藏注入文本的虚构PDF样本，继续验证文件嗅探和读页|
|18|core|`test_push_rosters.PushRosterTests.test_bare_same_name_at_receiver_stays_identity_unconfirmed`|同名或身份冲突材料被剔除；历史快照保持不变，新评估不保留本人证据|
|19|core|`test_search_recall.RecallTests.test_paper_dispatch_uses_free_apis_not_paid_search`|免费论文API测试使用论文类别及真实匹配的合成题名，不借用竞赛类别|
|20|legacy|`test_01_offline_demo_runs_and_is_interactive`|补齐离线演示HTML/CSS，验证启动、交互和免责声明|
|21|legacy|`test_09_no_scoring_or_hiring_advice`|补齐离线演示HTML/CSS，验证启动、交互和免责声明|
|22|legacy|`test_10_footer_disclaimer_present`|补齐离线演示HTML/CSS，验证启动、交互和免责声明|
|23|legacy|`test_18_full_live_pipeline_over_deepseek_wire`|按当前实习人工背调契约断言，保留显式跳过及不自动证明本人经历检查|
|24|legacy|`test_02_fixture_statuses_match_spec`|同名或身份冲突材料被剔除；历史快照保持不变，新评估不保留本人证据|
|25|legacy|`test_03_same_name_different_school_is_who`|同名或身份冲突材料被剔除；历史快照保持不变，新评估不保留本人证据|
|26|legacy|`test_08_search_budget_stops_the_pipeline`|实际代码修复：有未读结果且读页额度为0时正确标记受限；新增有/无结果对照|
|27|legacy|`test_30_model_string_instead_of_list_is_recovered_not_crashed`|补齐模拟原文的赛事与目标年份，继续验证字符串数组/数字字段兼容|
|28|legacy|`test_31_content_filtered_query_is_skipped_not_fatal`|模拟摘录改为原文逐字片段，继续验证审核改述与失败不中断|

## 验证范围

本机比赛版完整回归：核心372/372、工作台58/58、旧验收33/33及三套Node检查通过，共463项Python检查、无失败或跳过。新增用例与修复后的旧用例都实际执行。所有自动检查使用临时DB、合成材料、模拟提供方，封锁公网，不使用真实凭据或简历。

旧验收的浏览器分支由独立真实浏览器操作补验：恢复后的离线演示页展示11条虚构记录，点击第一条和第九条能切换详情，回车选中第五条、单条选中状态和免责声明正常。它是历史演示快照，不是当前线上核验的准确率证明。

本轮只有读页受限标记涉及生产核验逻辑；主页/身份/来源/年份/奖项要求、GLM与DeepSeek路由、AgentArts调用和每份3元目标/4元估算停止线均保留。原报告、回执及问答上下文不改写，没有重新提交真实简历或调用付费提供方。

根目录原项目单独回归保持544/544核心、42/42服务、29/33旧验收（另4项已登记失败）的原基线。本轮不修改根项目及原站；这4项不属于上述独立比赛版28项。

部署验收：`ict-full-20261116.8`已上线。部署前后报告/问答上下文摘要一致，没有中断运行任务、改动凭据或费用配置。对123个已部署源文件逐文件校验后，在隔离副本完整复跑463/463及三套Node检查，0失败/已知失败/跳过；不复制真实DB或运行缓存。实际HTTPS已认证接口、IAM配置状态、材料授权/管理员/worker边界、原历史上下文和前端资源检查通过，原站HTTP200，四项服务active且Nginx检查通过。没有调用付费提供方或重新提交真实简历。
