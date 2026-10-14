# Optimization v2 Changelog

## 2026-10-15 · 首次本地全量回归 + 修复批次

在临时 venv（Python 3.12 + requirements + pdfminer.six）运行 `scripts/regression.py`：
修复前 49 项新失败（core 34、service 7、legacy 8），经逐项归因（对照未改动的分支副本，
同一环境 0 失败）后修复：

- service：`uid()` 恢复"转发头可信用、网关模式内网 token 强制"；prepared 夹具补 `expires_at`。
- GitHub：`actorConfirmed` 记录恢复一等锚点（B 级已证实 / C 级门槛内 proved / 未签名仅存在性）。
- judge：身份锚点门槛恢复到内容分 ≥2（弱佐证排除、去重、不反推）；结构化记录矛盾直接有效；
  `proved` 恢复 A/B/C 合格支持（D 类自述排除）；`dedupe_by_origin` 支持规则验证的转引簇。
- 出网：`public_request` 经模块级 httpx 发起（保留 mock seam）+ 可注入 `DNS_RESOLVER`；
  新增 `tests/http_kit.py`，受影响用例注入假 DNS，测试不再访问真实网络。
- 契约迁移（均留新旧对照）：legacy 02/06/13/20/21/29、core 协作与 GitHub 夹具；
  `test_02` 真实修复后从 `KNOWN` 移除（REGRESSION.md 留档）。
- 依赖：`pdfminer.six` 补进 `requirements.txt`（缺失时 PDF 隐藏文字检测静默失效，test_14 因此失败）。

最终：core 544/544、service 42/42、legacy 29/33（4 项旧失败签名不变）、两个 `.cjs` 通过；
退出码 2，不等于全通过。见 `validation-report.md`。

## 2026-10-13 · code-first

### 已完成的代码骨架

- 增加 `ElementProof`、逐要素来源等级/身份/理由和规则版本，保留五态及旧字段。
- 增加页面/结构化 provenance 护照、精确引文定位和旧证据兼容标记。
- 协作锚点改为只接受已验证 ElementProof；计划区分 runtime planned/executed。
- 增加检索状态、覆盖度统计、来源调用计数、safe HTTP 预检查及 prepared owner/TTL。
- 增加合成 Bench（10 条样本）、证据护照报告/UI 增量渲染和实习人工授权提示。
- 增加搜索错误细分（401/402/429/5xx/timeout 各自映射到不同日志和状态）。
- 增加上传文件类型检测（PDF/DOCX/XLSX/DOC/XLS 文件签名校验）。
- 增加旧 fixture 迁移策略（`migrate_legacy_evidence`，旧证据标记为 legacy_unrecorded）。
- 增加前端人工复核表单（发起/补证/决定三种操作）。
- 改进 GitHub/Crossref/OpenAlex/institution_record 的错误处理（Timeout/HTTPStatus/通用异常分开捕获）。

### 未完成

- 未运行测试、浏览器、Bench、真实 API 或部署；依赖与线上数据库未触碰。
- 全部外部 API 的统一 allowlist 包装尚未完成（github_hits/crossref_hits/openalex_hits/institution_record 仍是独立调用）。
- 上传 ZIP 膨胀检测、PDF/OCR 资源上限、SSE 完整取消闭环尚未完成。
- 完整 Bench 数据集（120 条）、消融实验、三组对比图尚未完成。
- 复核前端表单已添加但未与历史版本回放 UI 集成。
- 多供应商流式问答的 fake HTTP Server 测试尚未完成。
- 性能优化（P2-2）尚未开始。

## Gate 状态

| Gate | 代码状态 | 验证状态 |
|---|---|---|
| 0 | 当前快照审查及基线文档 | 基线未执行 |
| 1 | 逐要素/provenance/handoff 已落地 | 用例未执行 |
| 2 | 调用统计/token/HTTP/请求/隐私边界已落地 | 集成未执行 |
| 3 | 护照展示/复核 API/10 条 Bench | 前端录入/完整 Bench 未完成，无浏览器结果 |
| 4 | 问答供应商适配/云配置边界文档 | 未执行，性能优化等待 Bench |
