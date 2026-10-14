# v2 架构增量

`ElementProof` 是报告时冻结的逐要素结论；`Evidence` 是候选来源，`provenance.py`
负责页面正文定位或结构化登记校验。`judge.assess()` 是唯一终局入口。

专项 Agent 仍复用原有十三类路由。`VerificationCoordinator` 只传播已验证要素的
`navigation_only` 线索；下游必须重新取得自己的证据。`AgentRun` 保存 planned/runtime
计划、真实 sourceAccess 和失败原因。覆盖度是合格证据覆盖的履历要素比例，不是候选人评分。

公开 HTTP 通过安全预检查、逐跳校验和流式大小上限；网关工作进程使用内部 token，
prepared token 创建时绑定 owner 和 TTL。真实网络、部署和供应商仍需单独授权验收。
