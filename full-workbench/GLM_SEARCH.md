# ICT 智谱搜索路径

按用户要求恢复智谱搜索路径，参考 GitHub main 提交 [a0ba123](https://github.com/Lonid-C/under-box/commit/a0ba123080c980661b49a83684557c4d995dae65) 中的 `ZhipuSearcher` 及 `SEARCH_PROVIDER=zhipu` 分支。适配器的请求、结果解析及域名查询逻辑保持一致，比赛版继续在付费调用前预留费用预算。

独立 ICT 环境启用 `UNDERBOX_ICT_GLM_ONLY=1`，并将比赛数据库的搜索供应商设为 `zhipu`。搜索固定使用智谱官方 Web Search 接口；带域查询沿用 `search_std`，开放查询沿用 `search_pro_sogou`。保留智谱内部的确认/重试策略，关闭 DeepSeek 及 Brave 搜索兜底；未配置智谱Key时不借用其他供应商。账户独立绑定的智谱Key继续独占使用。

这是搜索路由的范围内回退：原站代码和配置不变。简历解析、证据抽取、AgentArts编排及网页读取保持现有配置；取消8项数量上限、每份费用预算和检索规则修复继续生效。运行实例、预算和历史报告不做整库或整仓库回退。

`/api/ict/status` 返回 `search_policy=zhipu_only`、备用搜索次数0以供检查，不返回任何凭据。已有报告仍显示当时实际使用的数据源，新策略在新建核验后使用。
